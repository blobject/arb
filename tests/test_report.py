import argparse
from dataclasses import replace
from datetime import UTC, datetime, timedelta
from decimal import Decimal as D

import pytest

from arb.report import (
  EpisodeRow,
  HealthRow,
  drop_dust,
  healthy_seconds,
  longest_runs,
  merge_flicker,
  net_edge,
  parse_fee,
  pct,
  summarize,
)

T0 = datetime(2026, 10, 1, tzinfo=UTC)


def at(s):
  return T0 + timedelta(seconds=s)


def ev(s, component, event):
  return HealthRow(at(s), component, event)


def row(s=0, buy="coinbase", sell="okx", buy_px="100000", sell_px="100030", dur=800, qty="0.5"):
  return EpisodeRow(at(s), dur, buy, sell, "gone", D(qty), D(buy_px), D(sell_px), D(0), D(0), D(0))


def test_net_edge_reprices_with_given_fees():
  r = row(sell_px="100030")  # 3 bps gross
  assert net_edge(r, {"coinbase": D(0), "okx": D(0)}, D(0)) == D("0.0003")
  n = net_edge(r, {"coinbase": D("0.0001"), "okx": D("0.0001")}, D(0))
  assert D("0.00009") < n < D("0.00011")  # ~1 bp left after 2 bps of fees
  assert net_edge(r, {"coinbase": D("0.006"), "okx": D("0.001")}, D(0)) < 0


def test_healthy_seconds_counts_only_healthy_stretches():
  evs = [
    ev(0, "process", "started"),
    ev(1, "detector", "healthy"),
    ev(61, "detector", "alive"),
    ev(100, "detector", "unhealthy"),
    ev(130, "detector", "healthy"),
    ev(160, "process", "stopped"),
  ]
  assert healthy_seconds(evs, at(1000)) == 99 + 30


def test_healthy_seconds_does_not_count_a_crash_as_uptime():
  # healthy, last heartbeat at 61 s, then the process died without a "stopped" row
  evs = [
    ev(0, "process", "started"),
    ev(1, "detector", "healthy"),
    ev(61, "detector", "alive"),
    ev(5000, "process", "started"),
  ]
  assert healthy_seconds(evs, at(6000)) == 60 + 90  # 1->61, then at most 90 s after the last row


def test_helpers():
  assert pct([5, 1, 3, 2, 4], 50) == 3 and pct([1, 2, 3, 4], 100) == 4
  assert longest_runs(["a", "a", "b", "a", "a", "a"]) == {"a": 3, "b": 1}
  assert parse_fee("okx=0.001") == ("okx", D("0.001"))
  for bad in ("okx", "okx=0.6", "=0.1", "okx=abc"):
    with pytest.raises(argparse.ArgumentTypeError):
      parse_fee(bad)


def events_for(hours):
  return [ev(0, "process", "started"), ev(1, "detector", "healthy")] + [
    ev(60 * i, "detector", "alive") for i in range(1, hours * 60 + 1)
  ]


def test_summary_zero_fees_counts_everything():
  rows = [row(10), row(20, dur=100), row(30, buy="okx", sell="coinbase", buy_px="100000", sell_px="100020")]
  text = summarize(rows, events_for(2), {"coinbase": D(0), "okx": D(0)}, D(0), 500, D(100), at(7300))
  assert "buy coinbase / sell okx" in text and "2 episodes (1.0 per healthy hour)" in text
  assert "5. net of fees    2 of 2 still positive" in text
  assert "6. latency proxy  1 of those lasted >= 500 ms" in text  # the 100 ms one is too short
  assert "9. capital" in text and "10. persistence" in text


def test_summary_realistic_fees_kill_everything():
  rows = [row(10), row(20)]
  text = summarize(rows, events_for(1), {"coinbase": D("0.006"), "okx": D("0.001")}, D(0), 500, D(100), at(3700))
  assert "0 of 2 still positive" in text and "nothing tradeable" in text


def test_summary_requires_a_fee_for_every_exchange_seen():
  with pytest.raises(ValueError, match="okx"):
    summarize([row()], [], {"coinbase": D(0)}, D(0), 500, D(100), at(10))


def test_summary_handles_no_data():
  assert "no data" in summarize([], [], {}, D(0), 500, D(100), at(0))


def test_drop_dust_uses_tradeable_value():
  tiny, big = row(qty="0.00001"), row(qty="0.5")  # worth 1 and 50,000
  assert drop_dust([tiny, big], D(10)) == [big]
  assert drop_dust([tiny, big], D(0)) == [tiny, big]


def test_merge_joins_a_quick_return_in_the_same_direction():
  a = row(0, dur=400, sell_px="100020")  # 0.0 -> 0.4 s, 2 bps
  b = row(0.6, dur=300, sell_px="100030", qty="0.9")  # back 200 ms later, 3 bps, more size
  (m,) = merge_flicker([b, a], 500)  # order of input doesn't matter
  assert m.started_at == a.started_at and m.duration_ms == 900
  assert m.sell_vwap == D("100030") and m.peak_qty == D("0.9")  # prices of the better moment, max size


def test_merge_leaves_separate_things_separate():
  a = row(0, dur=400)
  assert len(merge_flicker([a, row(1.0)], 500)) == 2  # 600 ms dip: too long
  assert len(merge_flicker([a, row(0.6, buy="okx", sell="coinbase")], 500)) == 2  # other direction
  assert len(merge_flicker([replace(a, end_reason="unhealthy"), row(0.6)], 500)) == 2  # data was lost
  assert len(merge_flicker([a, row(0.6)], 0)) == 2  # disabled


# The 18 episodes from the first 16 minutes of run1 (2026-09-30).
RUN1 = [
  ("10:27:37.078966", 149, "okx", "coinbase", "1.00000000", "83782.4", "83820.21"),
  ("10:28:07.377905", 444, "okx", "coinbase", "0.05464056", "83750.1", "83758.89"),
  ("10:30:45.392443", 168, "okx", "coinbase", "0.00012134", "83770", "83778.49"),
  ("10:30:46.333765", 441, "okx", "coinbase", "0.00001134", "83790", "83799.99"),
  ("10:30:46.961707", 1598, "okx", "coinbase", "0.00001134", "83790", "83799.99"),
  ("10:31:10.959041", 144, "okx", "coinbase", "0.00001134", "83810", "83819.87"),
  ("10:32:09.362394", 40, "okx", "coinbase", "0.13337629", "83812.6", "83827.08"),
  ("10:33:54.763801", 190, "okx", "coinbase", "0.20998768", "83837.4", "83852.98"),
  ("10:34:23.887766", 180, "okx", "coinbase", "0.06245300", "83816.8", "83826.21"),
  ("10:34:43.960361", 911, "okx", "coinbase", "0.00012134", "83830", "83839.66"),
  ("10:37:29.168793", 609, "okx", "coinbase", "0.28510551", "83845.8", "83860.18"),
  ("10:37:29.814652", 864, "okx", "coinbase", "0.12728380", "83844.1", "83860.18"),
  ("10:37:35.230601", 1054, "okx", "coinbase", "0.07236708", "83846.1", "83857.5"),
  ("10:37:46.566115", 103, "coinbase", "okx", "1.00000000", "83858.1", "83872.4"),
  ("10:37:47.54529", 1448, "okx", "coinbase", "0.03541551", "83880", "83890.06"),
  ("10:39:21.296332", 137, "okx", "coinbase", "0.93460649", "83870.1", "83896.27"),
  ("10:41:36.171646", 603, "okx", "coinbase", "0.017032", "83822.2", "83831.72"),
  ("10:41:38.38391", 1386, "okx", "coinbase", "0.017032", "83822.2", "83831.72"),
]


def run1_rows():
  day = "2026-09-30T"
  return [
    EpisodeRow(datetime.fromisoformat(day + t + "+00:00"), d, b, s, "gone", D(q), D(bp), D(sp), D(0), D(0), D(0))
    for t, d, b, s, q, bp, sp in RUN1
  ]


def test_run1_cleanup_matches_what_we_saw_by_eye():
  rows = run1_rows()
  clean = drop_dust(rows, D(10))
  assert len(clean) == 15  # the three ~0.95 USDC episodes go
  merged = merge_flicker(clean, 500)
  assert len(merged) == 14  # 10:37:29 pair (37 ms apart) becomes one
  joined = next(m for m in merged if m.started_at.second == 29)
  assert joined.duration_ms == 1510 and joined.buy_vwap == D("83844.1")  # 1.9 bps beats 1.7 bps
  assert len(merge_flicker(clean, 2000)) == 13  # a 2 s allowance also joins the 10:41 pair (1.6 s apart)
  assert len(merge_flicker(rows, 500)) == 16  # without dust removal, the 10:30:46 pair also joins


def test_summary_reports_the_cleanup():
  text = summarize(
    run1_rows(),
    [],
    {"coinbase": D(0), "okx": D(0)},
    D(0),
    500,
    D(100),
    run1_rows()[-1].started_at,
    min_notional=D(10),
    merge_gap_ms=500,
  )
  expected = "18 recorded, 3 dropped as dust (tradeable value < 10), 1 joined as flicker (dips < 500 ms) -> 14 analysed"
  assert expected in text
