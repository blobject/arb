"""Offline analysis of collected episodes.

Answers the project's ten questions as far as observation-only data can, and re-prices every
episode with the fees you pass in, so you can collect in measurement mode (zero fees, 1 bp
threshold) and evaluate any fee schedule afterwards:

    uv run arb-report --epoch okx-usdc-post-cutover --fee coinbase=0.006 --fee okx=0.001

Fees are fractions: 0.001 = 0.1% = 10 bps per side.

Before analysing, two clean-ups (both adjustable, both reported in the output):
  * dust: episodes where only a trivial amount could have been traded (e.g. one tiny order)
    are dropped, see --min-notional.
  * flicker: when a gap vanishes for a split second and comes back in the same direction,
    the two pieces are joined into one episode, see --merge-gap-ms.
"""

import argparse
import math
from collections import Counter, defaultdict
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal, InvalidOperation

BPS = 10_000
MAX_SEGMENT_S = 90.0  # detector writes an "alive" row every 60 s; longer silence = process was down


@dataclass(frozen=True, slots=True)
class EpisodeRow:
  started_at: datetime
  duration_ms: int
  buy_exchange: str
  sell_exchange: str
  end_reason: str | None
  peak_qty: Decimal
  buy_vwap: Decimal  # probe-size VWAPs from the best sample of the episode
  sell_vwap: Decimal
  buy_fee: Decimal | None  # values in force when the row was collected
  sell_fee: Decimal | None
  extra_cost: Decimal | None

  @property
  def direction(self) -> str:
    return f"buy {self.buy_exchange} / sell {self.sell_exchange}"

  @property
  def gross_edge(self) -> Decimal:
    return self.sell_vwap / self.buy_vwap - 1


@dataclass(frozen=True, slots=True)
class HealthRow:
  at: datetime
  component: str
  event: str


def net_edge(row: EpisodeRow, fees: dict[str, Decimal], extra_cost: Decimal) -> Decimal:
  """Re-price the episode's best probe-size sample with the given taker fees."""
  cost = row.buy_vwap * (1 + fees[row.buy_exchange] + extra_cost)
  proceeds = row.sell_vwap * (1 - fees[row.sell_exchange])
  return proceeds / cost - 1


def healthy_seconds(events: Iterable[HealthRow], end: datetime, max_segment_s: float = MAX_SEGMENT_S) -> float:
  """Seconds during which the detector reported healthy data.

  A process start or stop resets the state, and no single stretch between two rows counts
  for more than max_segment_s, so a crash (no "stopped" row) can't be mistaken for uptime.
  """
  total, healthy, prev = 0.0, False, None
  for ev in sorted(events, key=lambda e: e.at):
    if healthy and prev is not None:
      total += min((ev.at - prev).total_seconds(), max_segment_s)
    if ev.component == "process":
      healthy = False
    elif ev.component == "detector" and ev.event in ("healthy", "unhealthy"):
      healthy = ev.event == "healthy"
    prev = ev.at
  if healthy and prev is not None:
    total += min(max((end - prev).total_seconds(), 0.0), max_segment_s)
  return total


def drop_dust(rows: Iterable[EpisodeRow], min_notional: Decimal) -> list[EpisodeRow]:
  """Drop episodes where the tradeable amount was worth less than min_notional (quote currency)."""
  return [r for r in rows if r.peak_qty * r.buy_vwap >= min_notional]


def merge_flicker(rows: Iterable[EpisodeRow], gap_ms: int) -> list[EpisodeRow]:
  """Join same-direction episodes separated by less than gap_ms into one.

  Only an episode that ended because the gap vanished ("gone") is joined to the next one. One
  that ended because data was lost ("unhealthy", "shutdown") is never bridged, since what
  happened during the missing stretch is unknown.
  """
  if gap_ms <= 0:
    return list(rows)
  gap = timedelta(milliseconds=gap_ms)
  out: list[EpisodeRow] = []
  latest: dict[str, int] = {}  # direction -> index in out of its most recent episode
  for r in sorted(rows, key=lambda r: r.started_at):
    i = latest.get(r.direction)
    if i is not None and out[i].end_reason == "gone" and r.started_at - _end(out[i]) <= gap:
      out[i] = _join(out[i], r)
      continue
    latest[r.direction] = len(out)
    out.append(r)
  return out


def _end(r: EpisodeRow) -> datetime:
  return r.started_at + timedelta(milliseconds=r.duration_ms)


def _join(a: EpisodeRow, b: EpisodeRow) -> EpisodeRow:
  best = a if a.gross_edge >= b.gross_edge else b  # keep the prices of the better moment
  end = max(_end(a), _end(b))
  return replace(
    best,
    started_at=a.started_at,
    duration_ms=round((end - a.started_at) / timedelta(milliseconds=1)),
    end_reason=b.end_reason,
    peak_qty=max(a.peak_qty, b.peak_qty),
  )


def pct(values: Sequence[float], p: float) -> float:
  """Nearest-rank percentile; values need not be sorted."""
  s = sorted(values)
  return s[min(len(s) - 1, max(0, math.ceil(p / 100 * len(s)) - 1))]


def spread(values: Sequence[float], fmt: str = "{:.2f}") -> str:
  if not values:
    return "n/a"
  f = fmt.format
  return f"p50 {f(pct(values, 50))}  p90 {f(pct(values, 90))}  p99 {f(pct(values, 99))}  max {f(max(values))}"


def longest_runs(directions: Sequence[str]) -> dict[str, int]:
  """Longest streak of consecutive same-direction trades, per direction."""
  best: dict[str, int] = defaultdict(int)
  run, last = 0, None
  for d in directions:
    run = run + 1 if d == last else 1
    last = d
    best[d] = max(best[d], run)
  return dict(best)


def summarize(
  rows: Sequence[EpisodeRow],
  events: Sequence[HealthRow],
  fees: dict[str, Decimal],
  extra_cost: Decimal,
  latency_ms: int,
  trade_notional: Decimal,
  now: datetime,
  *,
  min_notional: Decimal = Decimal(0),
  merge_gap_ms: int = 0,
) -> str:
  out: list[str] = []
  w = out.append
  missing = {r.buy_exchange for r in rows} | {r.sell_exchange for r in rows}
  missing -= fees.keys()
  if missing:
    raise ValueError(f"no --fee given for: {', '.join(sorted(missing))}")

  stamps = [e.at for e in events] + [r.started_at for r in rows]
  w("== Coverage ==")
  if not stamps:
    w("no data for this selection")
    return "\n".join(out)
  first = min(stamps)
  # a finished collection shouldn't keep accruing span just because the report runs later
  end = min(now, max(stamps) + timedelta(seconds=MAX_SEGMENT_S))
  up_s = healthy_seconds(events, end)
  up_h = up_s / 3600
  span_h = (end - first).total_seconds() / 3600
  w(
    f"span {first:%Y-%m-%d %H:%M} -> {end:%Y-%m-%d %H:%M} UTC ({span_h:.1f} h), "
    f"healthy {up_h:.1f} h ({100 * up_h / span_h if span_h else 0:.0f}%)"
  )
  kinds = Counter((e.component, e.event) for e in events if e.event != "alive")
  w("events: " + ", ".join(f"{c} {e} x{n}" for (c, e), n in sorted(kinds.items())))
  stored = Counter((r.buy_fee, r.sell_fee, r.extra_cost) for r in rows)
  w(
    "collected with (buy fee, sell fee, extra): "
    + ", ".join(f"({'/'.join(str(v) for v in k)}) x{n}" for k, n in stored.most_common())
  )
  if any(any(v for v in k if v) for k in stored):
    w(
      "  note: rows collected with non-zero costs only include episodes that cleared those "
      "costs; re-pricing with LOWER costs cannot recover the ones never recorded."
    )
  w(
    f"re-priced with fees {', '.join(f'{k}={v}' for k, v in sorted(fees.items()))}, "
    f"extra {extra_cost}, latency {latency_ms} ms, trade size {trade_notional} quote"
  )
  recorded = len(rows)
  rows = drop_dust(rows, min_notional)
  kept = len(rows)
  rows = merge_flicker(rows, merge_gap_ms)
  w(
    f"episodes: {recorded} recorded, {recorded - kept} dropped as dust (tradeable value < {min_notional}), "
    f"{kept - len(rows)} joined as flicker (dips < {merge_gap_ms} ms) -> {len(rows)} analysed"
  )
  if not rows:
    w("\nno episodes to analyse")
    return "\n".join(out)

  per_dir: dict[str, list[EpisodeRow]] = defaultdict(list)
  for r in rows:
    per_dir[r.direction].append(r)

  for direction, rs in sorted(per_dir.items()):
    gross = [float(r.gross_edge) * BPS for r in rs]
    dur = [float(r.duration_ms) for r in rs]
    notional = [float(r.peak_qty * r.buy_vwap) for r in rs]
    nets = [net_edge(r, fees, extra_cost) for r in rs]
    profitable = [(r, n) for r, n in zip(rs, nets, strict=True) if n > 0]
    survivors = [(r, n) for r, n in profitable if r.duration_ms >= latency_ms]
    w(f"\n== {direction} ==")
    rate = f"{len(rs) / up_h:.1f} per healthy hour" if up_h else "rate n/a"
    w(
      f"1. frequency      {len(rs)} episodes ({rate}); end reasons "
      + ", ".join(f"{k} {v}" for k, v in Counter(r.end_reason for r in rs).most_common())
    )
    w(f"2. gross edge bps {spread(gross)}")
    w(f"3. duration ms    {spread(dur, '{:.0f}')}")
    w(f"4. executable $   {spread(notional, '{:,.0f}')}  (size at collection costs; an upper bound)")
    w(
      f"5. net of fees    {len(profitable)} of {len(rs)} still positive"
      + (f"; net bps {spread([float(n) * BPS for _, n in profitable])}" if profitable else "")
    )
    w(f"6. latency proxy  {len(survivors)} of those lasted >= {latency_ms} ms")

  # Cross-direction questions use only episodes that would plausibly have been traded.
  tradeable = sorted(
    ((r, n) for r in rows if (n := net_edge(r, fees, extra_cost)) > 0 and r.duration_ms >= latency_ms),
    key=lambda x: x[0].started_at,
  )
  w("\n== Across directions (net-positive episodes that outlasted the latency) ==")
  w("7. one leg fills, other doesn't: not measurable from observation; needs paper/live trading")
  if not tradeable:
    w("8-10. nothing tradeable at these fees, so inventory, capital and persistence questions are moot")
    return "\n".join(out)
  def size(r: EpisodeRow) -> Decimal:  # base units actually traded: the trade size, or less if that's all there was
    return min(trade_notional / r.buy_vwap, r.peak_qty)

  qty = {r.direction: Decimal(0) for r, _ in tradeable}
  for r, _ in tradeable:
    qty[r.direction] += size(r)
  w(
    "8. inventory flow "
    + ", ".join(f"{d}: {q} base" for d, q in sorted(qty.items()))
    + "  (the difference is what rebalancing has to move)"
  )
  runs = longest_runs([r.direction for r, _ in tradeable])
  worst = max(runs.values())
  w(
    f"9. capital        longest same-direction streak {worst} trades -> each venue needs about "
    f"{worst * trade_notional} quote worth of the coin on the selling side, and the same in quote on the "
    f"buying side (at {trade_notional} per trade, before rebalancing)"
  )
  profit = sum(n * size(r) * r.buy_vwap for r, n in tradeable)
  days = up_s / 86400
  per_day = f"{profit / Decimal(str(days)):.2f}" if days else "n/a"
  w(
    f"   upper-bound profit {profit:.2f} quote total, {per_day} per healthy day "
    "(assumes every capture at the best observed price, no competition, no failed legs)"
  )
  by_day: dict[str, int] = Counter(r.started_at.strftime("%Y-%m-%d") for r, _ in tradeable)
  w("10. persistence   tradeable episodes per UTC day: " + ", ".join(f"{d} {n}" for d, n in sorted(by_day.items())))
  return "\n".join(out)


# -- database access -------------------------------------------------------------------------

EPISODE_SQL = """
SELECT started_at, duration_ms, buy_exchange, sell_exchange, end_reason, peak_qty,
       buy_vwap, sell_vwap, buy_fee, sell_fee, extra_cost
FROM arbitrage_opportunities {where} ORDER BY started_at
"""
HEALTH_SQL = "SELECT at, component, event FROM health_events {where} ORDER BY at, id"


def load(url: str, epoch: str | None) -> tuple[list[EpisodeRow], list[HealthRow]]:
  import psycopg

  where, params = ("WHERE epoch = %s", (epoch,)) if epoch is not None else ("", ())
  with psycopg.connect(url) as conn:
    rows = [EpisodeRow(*r) for r in conn.execute(EPISODE_SQL.format(where=where), params)]
    events = [HealthRow(*r) for r in conn.execute(HEALTH_SQL.format(where=where), params)]
  return rows, events


def parse_fee(text: str) -> tuple[str, Decimal]:
  name, sep, value = text.partition("=")
  if not sep or not name:
    raise argparse.ArgumentTypeError(f"expected exchange=rate, got {text!r}")
  try:
    rate = Decimal(value)
  except InvalidOperation:
    raise argparse.ArgumentTypeError(f"{text!r}: {value!r} is not a number") from None
  if not Decimal(0) <= rate < Decimal("0.1"):
    raise argparse.ArgumentTypeError(f"{text!r}: rate is a fraction, e.g. 0.001 for 0.1%")
  return name, rate


def cli(argv: Sequence[str] | None = None) -> None:
  from arb.config import Settings

  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--epoch", help="only rows with this EPOCH tag (default: all)")
  ap.add_argument("--fee", type=parse_fee, action="append", required=True, metavar="EXCHANGE=RATE")
  ap.add_argument("--extra-cost", type=Decimal, default=Decimal(0), help="slippage/rebalance buffer")
  ap.add_argument("--latency-ms", type=int, default=500, help="episodes shorter than this count as uncatchable")
  ap.add_argument("--trade-notional", type=Decimal, default=Decimal(100), help="quote spent per trade")
  ap.add_argument(
    "--min-notional",
    type=Decimal,
    default=Decimal(10),
    help="drop episodes tradeable for less than this, in quote currency (0 keeps all)",
  )
  ap.add_argument(
    "--merge-gap-ms",
    type=int,
    default=500,
    help="join same-direction episodes split by a dip shorter than this (0 disables)",
  )
  ap.add_argument("--database-url", default=None)
  a = ap.parse_args(argv)
  rows, events = load(a.database_url or Settings().database_url, a.epoch)
  text = summarize(
    rows,
    events,
    dict(a.fee),
    a.extra_cost,
    a.latency_ms,
    a.trade_notional,
    datetime.now(UTC),
    min_notional=a.min_notional,
    merge_gap_ms=a.merge_gap_ms,
  )
  print(text)
