import asyncio
import contextlib
import time
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from arb.events import Episode, HealthEvent, Sink
from arb.exchanges.base import FeedView
from arb.marketdata.orderbook import OrderBook
from arb.strategy.arbitrage import Opportunity, evaluate
from arb.strategy.tracker import OpportunityTracker, Sample


@dataclass(frozen=True, slots=True)
class DetectorConfig:
  stale_ms: Mapping[str, float]  # per feed name; OKX has no heartbeat channel, so it goes quieter
  extra_cost: Decimal
  min_net_edge: Decimal
  probe_notional: Decimal  # small size, in quote: the edge a tiny live trade would see
  detect_notional: Decimal  # generous cap, in quote: measures how much size is actually executable
  min_interval_s: float = 0.02  # coalesces bursts of book updates; bounds CPU
  idle_check_s: float = 0.25  # re-check health even when no updates arrive
  alive_interval_s: float = 60.0  # periodic "alive" row, so offline analysis can measure uptime


class Detector:
  def __init__(
    self,
    cfg: DetectorConfig,
    a: FeedView,
    b: FeedView,
    fee_a: Decimal,
    fee_b: Decimal,
    sink: Sink,
  ) -> None:
    self.cfg, self.a, self.b, self.fee_a, self.fee_b, self.sink = cfg, a, b, fee_a, fee_b, sink
    self.tracker = OpportunityTracker()
    self._event = asyncio.Event()
    self._connected: dict[str, bool] = {}
    self._healthy = False
    self._last_alive = float("-inf")

  def wake(self, book: OrderBook | None = None) -> None:
    """on_update callback for both feeds."""
    self._event.set()

  async def run(self) -> None:
    while True:
      with contextlib.suppress(TimeoutError):
        await asyncio.wait_for(self._event.wait(), self.cfg.idle_check_s)
      self._event.clear()  # cleared before step(), so updates arriving during it re-trigger
      self.step()
      await asyncio.sleep(self.cfg.min_interval_s)

  def step(self, wall: float | None = None, mono: float | None = None) -> list[Episode]:
    wall = time.time() if wall is None else wall
    mono = time.monotonic() if mono is None else mono
    healthy = all(f.book.is_fresh(self.cfg.stale_ms[f.name]) for f in (self.a, self.b))
    self._record_health(wall, healthy)
    if wall - self._last_alive >= self.cfg.alive_interval_s:
      self._last_alive = wall
      detail = f"healthy={healthy} open={len(self.tracker.open_keys)}"
      self.sink.put_health(HealthEvent(datetime.fromtimestamp(wall, UTC), "detector", "alive", detail))

    samples: dict[tuple[str, str], Sample] = {}
    if healthy:
      for buy, sell, buy_fee, sell_fee in (
        (self.a.book, self.b.book, self.fee_a, self.fee_b),
        (self.b.book, self.a.book, self.fee_b, self.fee_a),
      ):
        full = self._evaluate(buy, sell, buy_fee, sell_fee, self.cfg.detect_notional)
        if full is None:
          continue
        small = self._evaluate(buy, sell, buy_fee, sell_fee, self.cfg.probe_notional)
        if small is not None:
          samples[(buy.market.exchange, sell.market.exchange)] = Sample(full, small)

    closed = self.tracker.update(wall, mono, samples, healthy)
    for episode in closed:
      self.sink.put_episode(episode)
    return closed

  def _evaluate(
    self, buy: OrderBook, sell: OrderBook, buy_fee: Decimal, sell_fee: Decimal, max_notional: Decimal
  ) -> Opportunity | None:
    return evaluate(
      buy,
      sell,
      buy_fee,
      sell_fee,
      extra_cost=self.cfg.extra_cost,
      max_notional=max_notional,
      min_net_edge=self.cfg.min_net_edge,
    )

  def close(self, reason: str = "shutdown") -> list[Episode]:
    """Report still-open episodes (ended at their last confirmed sample)."""
    closed = self.tracker.close_all(reason)
    for episode in closed:
      self.sink.put_episode(episode)
    return closed

  def _record_health(self, wall: float, healthy: bool) -> None:
    now = datetime.fromtimestamp(wall, UTC)
    for feed in (self.a, self.b):
      cur = feed.connected
      if cur != self._connected.get(feed.name, False):
        self.sink.put_health(HealthEvent(now, feed.name, "connected" if cur else "disconnected"))
        self._connected[feed.name] = cur
    if healthy != self._healthy:
      detail = None
      if not healthy:
        ages = [f"{f.name}={f.book.age_ms()}" for f in (self.a, self.b)]
        detail = "book age ms: " + " ".join(ages)
      self.sink.put_health(HealthEvent(now, "detector", "healthy" if healthy else "unhealthy", detail))
      self._healthy = healthy
