"""Turns a stream of per-evaluation opportunities into discrete episodes (pure; no I/O)."""

from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal

from arb.events import Episode
from arb.strategy.arbitrage import Opportunity

Key = tuple[str, str]  # (buy_exchange, sell_exchange)


@dataclass(frozen=True, slots=True)
class Sample:
  full: Opportunity  # max profitable size across the considered depth
  small: Opportunity  # capped at the probe trade size


@dataclass(slots=True)
class _Open:
  started_wall: float
  started_mono: float
  last_wall: float
  last_mono: float
  samples: int
  peak_qty: Decimal
  peak_net_profit: Decimal
  best_small: Opportunity


def _utc(ts: float) -> datetime:
  return datetime.fromtimestamp(ts, UTC)


class OpportunityTracker:
  """Call update() after every evaluation.

  An episode opens on the first evaluation showing a positive net edge in a direction and
  closes on the first evaluation that doesn't ("gone"; duration is therefore an upper bound,
  within one evaluation interval). If the data itself becomes unhealthy the episode closes
  at its last confirmed sample ("unhealthy"), because whether the edge persisted is unknown.
  """

  def __init__(self) -> None:
    self._open: dict[Key, _Open] = {}

  @property
  def open_keys(self) -> set[Key]:
    return set(self._open)

  def update(self, wall: float, mono: float, samples: dict[Key, Sample], healthy: bool) -> list[Episode]:
    if not healthy:
      return self.close_all("unhealthy")
    for key, s in samples.items():
      o = self._open.get(key)
      if o is None:
        self._open[key] = _Open(wall, mono, wall, mono, 1, s.full.qty, s.full.net_profit, s.small)
        continue
      o.last_wall, o.last_mono = wall, mono
      o.samples += 1
      o.peak_qty = max(o.peak_qty, s.full.qty)
      o.peak_net_profit = max(o.peak_net_profit, s.full.net_profit)
      if s.small.net_edge > o.best_small.net_edge:
        o.best_small = s.small
    return [self._close(k, "gone", wall, mono) for k in list(self._open) if k not in samples]

  def close_all(self, reason: str) -> list[Episode]:
    """Close every open episode at its last confirmed sample (true end unknown)."""
    return [self._close(k, reason, o.last_wall, o.last_mono) for k, o in list(self._open.items())]

  def _close(self, key: Key, reason: str, end_wall: float, end_mono: float) -> Episode:
    o = self._open.pop(key)
    b = o.best_small
    return Episode(
      buy_exchange=b.buy_market.exchange,
      sell_exchange=b.sell_market.exchange,
      base=b.buy_market.base,
      buy_quote=b.buy_market.quote,
      sell_quote=b.sell_market.quote,
      started_at=_utc(o.started_wall),
      ended_at=_utc(end_wall),
      duration_ms=round((end_mono - o.started_mono) * 1000),
      end_reason=reason,
      samples=o.samples,
      peak_qty=o.peak_qty,
      peak_net_profit=o.peak_net_profit,
      peak_net_edge=b.net_edge,
      buy_vwap=b.buy_vwap,
      sell_vwap=b.sell_vwap,
    )
