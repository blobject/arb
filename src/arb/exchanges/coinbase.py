"""Coinbase Advanced Trade level2 feed (public market data; no API keys needed).

Protocol notes (Coinbase docs, read Sep 2026):
  * Connect to wss://advanced-trade-ws.coinbase.com and send a subscribe message within
    5 seconds, or the server disconnects you.
  * One channel per subscribe message. level2 messages arrive with channel "l2_data".
  * Most channels are closed after ~60-90s idle, so we also subscribe to "heartbeats" (1/s).
  * On the public feed, "-USDC" product ids return the same data as the matching "-USD"
    product (except USDT-USDC and EURC-USDC), so we accept either id in responses.
  * new_quantity is the absolute size at a level, not a delta; "0" removes the level.
  * sequence_num is documented as a per-connection message counter. Any jump means a dropped
    message; we treat any non-consecutive value as a gap and resync from a fresh snapshot.
"""

import asyncio
import json
import logging
from collections.abc import AsyncIterator, Callable
from datetime import datetime
from decimal import InvalidOperation
from typing import Any

from websockets.asyncio.client import connect as ws_connect
from websockets.exceptions import WebSocketException

from arb.exchanges.base import FeedError, MarketDataFeed, SequenceGap, make_level
from arb.marketdata.orderbook import OrderBook, Side
from arb.marketdata.types import Level, Market

log = logging.getLogger(__name__)

WS_URL = "wss://advanced-trade-ws.coinbase.com"
# A full-depth BTC snapshot can exceed websockets' 1 MiB default message limit.
MAX_MESSAGE_BYTES = 32 * 1024 * 1024


def _parse_ts(value: str | None) -> float | None:
  if not value:
    return None
  try:
    return datetime.fromisoformat(value).timestamp()  # copes with 'Z' and ns digits
  except ValueError:
    return None


def _side(value: str) -> Side:
  if value == "bid":
    return Side.BID
  if value == "offer":
    return Side.ASK
  raise FeedError(f"unknown side {value!r}")


class CoinbaseFeed(MarketDataFeed):
  name = "coinbase"

  def __init__(
    self,
    market: Market,
    *,
    url: str = WS_URL,
    read_timeout: float = 10.0,
    check_sequence: bool = True,
    backoff_base: float = 0.5,
    connect: Callable[..., AsyncIterator[Any]] | None = None,
  ) -> None:
    self.market = market
    self.book = OrderBook(market)
    self.url = url
    self.read_timeout = read_timeout
    self.check_sequence = check_sequence
    self.backoff_base = backoff_base
    self._connect = connect or ws_connect
    self.product_id = market.symbol
    self._accepted = {self.product_id}
    if market.quote == "USDC" and market.base not in ("USDT", "EURC"):
      self._accepted.add(f"{market.base}-USD")
    self._connected = False
    self._have_snapshot = False
    self._last_seq: int | None = None
    self._failures = 0
    self._alias_logged = False

  @property
  def connected(self) -> bool:
    return self._connected and self._have_snapshot

  # -- message handling (pure; no I/O) ------------------------------------------------

  def process(self, raw: str | bytes) -> bool:
    """Apply one raw message. Returns True if the book changed. Raises FeedError."""
    try:
      return self._process(json.loads(raw))
    except (AttributeError, KeyError, TypeError, ValueError, InvalidOperation) as exc:
      raise FeedError(f"malformed message: {exc!r}") from exc

  def _process(self, msg: dict[str, Any]) -> bool:
    if msg.get("type") == "error":
      raise FeedError(f"server error: {msg.get('message')}")
    self._check_sequence(msg)
    channel = msg.get("channel")
    ts = _parse_ts(msg.get("timestamp"))

    if channel == "heartbeats":
      if self._have_snapshot:
        self.book.touch(ts)  # a healthy, gap-free connection means the book is current
      return False
    if channel == "subscriptions":
      log.info("subscriptions confirmed: %s", msg.get("events"))
      return False
    if channel != "l2_data":
      return False

    changed = False
    for event in msg.get("events", ()):
      pid = event.get("product_id")
      if pid not in self._accepted:
        continue
      if pid != self.product_id and not self._alias_logged:
        log.info("coinbase serves %s data for subscription %s", pid, self.product_id)
        self._alias_logged = True
      updates = event.get("updates", ())
      kind = event.get("type")
      if kind == "snapshot":
        bids = [_level(u) for u in updates if _side(u["side"]) is Side.BID]
        asks = [_level(u) for u in updates if _side(u["side"]) is Side.ASK]
        self.book.apply_snapshot(bids, asks, ts)
        self._have_snapshot = True
        self._failures = 0
      elif kind == "update":
        if not self._have_snapshot:
          raise FeedError("update received before snapshot")
        for u in updates:
          lv = _level(u)
          self.book.apply_delta(_side(u["side"]), lv.price, lv.size, ts)
      else:
        continue
      changed = True
    return changed

  def _check_sequence(self, msg: dict[str, Any]) -> None:
    seq = msg.get("sequence_num")
    if not self.check_sequence or not isinstance(seq, int):
      return
    last, self._last_seq = self._last_seq, seq
    if last is not None and seq != last + 1:
      raise SequenceGap(last + 1, seq, msg.get("channel"))

  # -- connection management -----------------------------------------------------------

  async def run(self, on_update: Callable[[OrderBook], None]) -> None:
    async for ws in self._connect(self.url, max_size=MAX_MESSAGE_BYTES):
      self._last_seq = None
      self._have_snapshot = False
      try:
        await self._subscribe(ws)
        self._connected = True
        log.info("connected, subscribed to %s", self.product_id)
        while True:
          raw = await asyncio.wait_for(ws.recv(), self.read_timeout)
          if self.process(raw):
            on_update(self.book)
      except FeedError as exc:
        log.warning("resyncing: %s", exc)
      except TimeoutError:
        log.warning("no data for %.1fs, reconnecting", self.read_timeout)
      except (WebSocketException, OSError) as exc:
        log.warning("connection lost: %r", exc)
      finally:
        self._connected = False
        self._have_snapshot = False
        self.book.clear()  # never leave a stale book looking usable
      await self._backoff()

  async def _subscribe(self, ws: Any) -> None:
    level2 = {"type": "subscribe", "channel": "level2", "product_ids": [self.product_id]}
    await ws.send(json.dumps(level2))
    await ws.send(json.dumps({"type": "subscribe", "channel": "heartbeats"}))

  async def _backoff(self) -> None:
    self._failures += 1  # reset to 0 whenever a snapshot is applied
    delay = min(30.0, self.backoff_base * 2 ** (self._failures - 1))
    log.info("reconnecting in %.1fs", delay)
    await asyncio.sleep(delay)


def _level(update: dict[str, Any]) -> Level:
  return make_level(update["price_level"], update["new_quantity"])
