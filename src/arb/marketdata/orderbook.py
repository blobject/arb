import heapq
import time
from collections.abc import Iterable
from decimal import Decimal
from enum import Enum

from .types import Level, Market


class Side(Enum):
  BID = "bid"
  ASK = "ask"


class OrderBook:
  """In-memory L2 book. Price -> size maps; size 0 removes a level.

  Sorting happens on read. That's fine for v0.1 (depth is small); swap in a
  sorted container if profiling says so.
  """

  def __init__(self, market: Market) -> None:
    self.market = market
    self._bids: dict[Decimal, Decimal] = {}
    self._asks: dict[Decimal, Decimal] = {}
    self.exchange_ts: float | None = None  # venue wall clock (seconds)
    self.local_ts: float | None = None  # our monotonic receive time

  def apply_snapshot(
    self,
    bids: Iterable[Level],
    asks: Iterable[Level],
    exchange_ts: float | None = None,
  ) -> None:
    self._bids = {lv.price: lv.size for lv in bids if lv.size > 0}
    self._asks = {lv.price: lv.size for lv in asks if lv.size > 0}
    self._touch(exchange_ts)

  def apply_delta(self, side: Side, price: Decimal, size: Decimal, exchange_ts: float | None = None) -> None:
    book = self._bids if side is Side.BID else self._asks
    if size == 0:
      book.pop(price, None)
    else:
      book[price] = size
    self._touch(exchange_ts)

  def clear(self) -> None:
    """Call on disconnect/sequence gap so a stale book can't produce signals."""
    self._bids.clear()
    self._asks.clear()
    self.local_ts = None

  def touch(self, exchange_ts: float | None = None) -> None:
    """Confirm the book is still current without changing it (e.g. on a heartbeat)."""
    if self._bids or self._asks:
      self._touch(exchange_ts)

  def _touch(self, exchange_ts: float | None) -> None:
    self.exchange_ts = exchange_ts
    self.local_ts = time.monotonic()

  def bids(self, depth: int | None = None) -> list[Level]:
    """Best-first. Pass depth to avoid sorting a full-depth book (~O(n log depth))."""
    prices = heapq.nlargest(depth, self._bids) if depth is not None else sorted(self._bids, reverse=True)
    return [Level(p, self._bids[p]) for p in prices]

  def asks(self, depth: int | None = None) -> list[Level]:
    prices = heapq.nsmallest(depth, self._asks) if depth is not None else sorted(self._asks)
    return [Level(p, self._asks[p]) for p in prices]

  def best_bid(self) -> Level | None:
    if not self._bids:
      return None
    p = max(self._bids)
    return Level(p, self._bids[p])

  def best_ask(self) -> Level | None:
    if not self._asks:
      return None
    p = min(self._asks)
    return Level(p, self._asks[p])

  def age_ms(self, now: float | None = None) -> float | None:
    if self.local_ts is None:
      return None
    return ((now if now is not None else time.monotonic()) - self.local_ts) * 1000

  def is_fresh(self, max_age_ms: float, now: float | None = None) -> bool:
    age = self.age_ms(now)
    return age is not None and age <= max_age_ms and bool(self._bids) and bool(self._asks)
