"""OKX spot order book feed (public `books` channel; no API keys needed).

Protocol notes (OKX API changelog + docs, read Sep 2026):
  * Public endpoint: wss://ws.okx.com:8443/ws/v5/public
  * Subscribe: {"op": "subscribe", "args": [{"channel": "books", "instId": "BTC-USDC"}]}
  * `books` = 400 levels per side, pushed every 100 ms. The first push is a full snapshot
    (action "snapshot", prevSeqId -1); later pushes are deltas (action "update").
    Levels are [price, size, "0" (deprecated), orderCount]; a size of "0" deletes the level.
  * Integrity: the checksum was deprecated on 2026-06-23 (the field is still sent, fixed at 0).
    OKX says to verify continuity with seqId/prevSeqId instead: an update is valid only if its
    prevSeqId equals the previously accepted seqId. seqId does NOT have to increase: empty
    keepalive updates repeat it, and maintenance can reset it to a lower value.
  * Keepalive: OKX drops the connection after 30 s without data. After N < 30 s of silence send
    the text "ping" and expect "pong"; if nothing arrives within another N s, reconnect.
    We therefore disable websockets' protocol-level pings and rely on this.
  * event "notice" with code 64008 announces a service upgrade; we reconnect proactively.
  * We use `books`, not `books-rpi`: the latter also contains Retail Price Improvement
    liquidity, which a taker only reaches with rpiTakerAccess=true (default false).
  * BTC-USDC is the instrument id before and after the 2026-09-30 USD->USDC migration.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from decimal import InvalidOperation
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from arb.exchanges.base import FeedError, MarketDataFeed, SequenceGap, make_level
from arb.marketdata.orderbook import OrderBook, Side
from arb.marketdata.types import Level, Market

log = logging.getLogger(__name__)

WS_URL = "wss://ws.okx.com:8443/ws/v5/public"
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
SERVICE_UPGRADE_CODE = "64008"


class OkxFeed(MarketDataFeed):
  name = "okx"

  def __init__(
    self,
    market: Market,
    *,
    url: str = WS_URL,
    channel: str = "books",
    idle_timeout: float = 10.0,
    backoff_base: float = 0.5,
    connect: Callable[..., AsyncIterator[Any]] | None = None,
  ) -> None:
    self.market = market
    self.book = OrderBook(market)
    self.url = url
    self.channel = channel
    self.idle_timeout = idle_timeout  # must stay well under OKX's 30 s limit
    self.backoff_base = backoff_base
    self._connect = connect or ws_connect
    self.inst_id = market.symbol
    self._connected = False
    self._have_snapshot = False
    self._last_seq: int | None = None
    self._failures = 0

  @property
  def connected(self) -> bool:
    return self._connected and self._have_snapshot

  # -- message handling (pure; no I/O) ------------------------------------------------

  def process(self, raw: str | bytes) -> bool:
    """Apply one raw message. Returns True if the book changed. Raises FeedError."""
    if raw in ("pong", b"pong"):
      return False
    try:
      return self._process(json.loads(raw))
    except (AttributeError, IndexError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
      raise FeedError(f"malformed message: {exc!r}") from exc

  def _process(self, msg: Any) -> bool:
    if not isinstance(msg, dict):
      raise FeedError("message is not a JSON object")
    event = msg.get("event")
    if event == "error":
      raise FeedError(f"server error {msg.get('code')}: {msg.get('msg')}")
    if event == "notice":
      if str(msg.get("code")) == SERVICE_UPGRADE_CODE:
        raise FeedError("service upgrade notice, reconnecting")
      log.info("notice: %s", msg)
      return False
    if event in ("subscribe", "unsubscribe"):
      log.info("%s confirmed: %s", event, msg.get("arg"))
      return False
    if event is not None:
      return False

    arg = msg.get("arg") or {}
    if arg.get("channel") != self.channel or arg.get("instId") != self.inst_id:
      return False
    action = msg.get("action")
    changed = False
    for data in msg["data"]:
      changed = self._apply(action, data) or changed
    return changed

  def _apply(self, action: str | None, data: dict[str, Any]) -> bool:
    seq, prev = int(data["seqId"]), int(data["prevSeqId"])
    bids = [_level(x) for x in data["bids"]]
    asks = [_level(x) for x in data["asks"]]
    ts = int(data["ts"]) / 1000

    if action == "snapshot":
      self.book.apply_snapshot(bids, asks, ts)
      self._check_not_crossed()
      self._have_snapshot = True
      self._last_seq = seq
      self._failures = 0
      return True

    if action != "update":
      raise FeedError(f"unknown action {action!r}")
    if not self._have_snapshot:
      raise FeedError("update received before snapshot")
    last = self._last_seq
    if last is None or prev != last:
      raise SequenceGap(last, prev, self.channel)  # nothing has been applied yet
    for lv in bids:
      self.book.apply_delta(Side.BID, lv.price, lv.size, ts)
    for lv in asks:
      self.book.apply_delta(Side.ASK, lv.price, lv.size, ts)
    self._last_seq = seq
    self._check_not_crossed()
    self.book.touch(ts)  # even an empty keepalive update proves the book is current
    return bool(bids or asks)

  def _check_not_crossed(self) -> None:
    # With checksums gone, a crossed book is the cheapest corruption signal we have.
    bid, ask = self.book.best_bid(), self.book.best_ask()
    if bid is not None and ask is not None and bid.price >= ask.price:
      raise FeedError(f"crossed book: bid={bid.price} ask={ask.price}")

  # -- connection management -----------------------------------------------------------

  async def run(self, on_update: Callable[[OrderBook], None]) -> None:
    async for ws in self._connect(self.url, max_size=MAX_MESSAGE_BYTES, ping_interval=None):
      self._last_seq = None
      self._have_snapshot = False
      try:
        await self._subscribe(ws)
        self._connected = True
        log.info("connected, subscribed to %s/%s", self.channel, self.inst_id)
        awaiting_pong = False
        while True:
          try:
            raw = await asyncio.wait_for(ws.recv(), self.idle_timeout)
          except TimeoutError:
            if awaiting_pong:
              raise
            await ws.send("ping")
            awaiting_pong = True
            continue
          awaiting_pong = False
          if self.process(raw):
            on_update(self.book)
      except FeedError as exc:
        log.warning("resyncing: %s", exc)
      except TimeoutError:
        log.warning("no data or pong for %.0fs, reconnecting", 2 * self.idle_timeout)
      except (WebSocketException, OSError) as exc:
        log.warning("connection lost: %r", exc)
      finally:
        self._connected = False
        self._have_snapshot = False
        self.book.clear()  # never leave a stale book looking usable
      await self._backoff()

  async def _subscribe(self, ws: Any) -> None:
    args = [{"channel": self.channel, "instId": self.inst_id}]
    await ws.send(json.dumps({"op": "subscribe", "args": args}))

  async def _backoff(self) -> None:
    self._failures += 1  # reset to 0 whenever a snapshot is applied
    delay = min(30.0, self.backoff_base * 2 ** (self._failures - 1))
    log.info("reconnecting in %.1fs", delay)
    await asyncio.sleep(delay)


def _level(entry: list[str]) -> Level:
  return make_level(entry[0], entry[1])  # [price, size, deprecated, orderCount]
