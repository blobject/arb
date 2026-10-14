import logging
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Protocol

log = logging.getLogger("arb.events")


@dataclass(frozen=True, slots=True)
class Episode:
  """One contiguous period during which a direction had a positive net edge."""

  buy_exchange: str
  sell_exchange: str
  base: str
  buy_quote: str
  sell_quote: str
  started_at: datetime
  ended_at: datetime
  duration_ms: int
  end_reason: str  # "gone": edge disappeared. "unhealthy": data lost, true end unknown
  samples: int
  peak_qty: Decimal  # largest executable size seen (all profitable depth)
  peak_net_profit: Decimal  # largest total net profit at that depth
  peak_net_edge: Decimal  # best net edge at probe size (what a tiny trade would earn)
  buy_vwap: Decimal  # from the sample with peak_net_edge
  sell_vwap: Decimal


@dataclass(frozen=True, slots=True)
class HealthEvent:
  at: datetime
  component: str
  event: str
  detail: str | None = None


class Sink(Protocol):
  def put_episode(self, episode: Episode) -> None: ...
  def put_health(self, event: HealthEvent) -> None: ...


def describe(ep: Episode) -> str:
  return (
    f"EPISODE buy {ep.buy_exchange} / sell {ep.sell_exchange} {ep.duration_ms}ms "
    f"({ep.end_reason}, {ep.samples} samples) peak qty {ep.peak_qty} "
    f"net edge {ep.peak_net_edge * 100:.4f}% profit {ep.peak_net_profit:.4f}"
  )


def log_health(event: HealthEvent) -> None:
  level = logging.DEBUG if event.event == "alive" else logging.INFO
  log.log(level, "HEALTH %s %s %s", event.component, event.event, event.detail or "")


class LogSink:
  """Used when persistence is off: episodes and health events go to the log only."""

  def put_episode(self, episode: Episode) -> None:
    log.info(describe(episode))

  def put_health(self, event: HealthEvent) -> None:
    log_health(event)
