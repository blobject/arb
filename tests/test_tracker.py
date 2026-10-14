from datetime import UTC, datetime
from decimal import Decimal as D

from arb.marketdata.types import Market
from arb.strategy.arbitrage import Opportunity
from arb.strategy.tracker import OpportunityTracker, Sample


def opp(buy="coinbase", sell="okx", qty="0.5", edge="0.004", profit="10"):
  return Opportunity(
    buy_market=Market(buy, "BTC", "USDC"),
    sell_market=Market(sell, "BTC", "USDC"),
    qty=D(qty),
    buy_vwap=D(100000),
    sell_vwap=D(100500),
    gross_edge=D("0.005"),
    net_edge=D(edge),
    net_profit=D(profit),
  )


def sample(qty="0.5", edge="0.004", profit="10", small_edge="0.004"):
  return Sample(full=opp(qty=qty, edge=edge, profit=profit), small=opp(qty="0.001", edge=small_edge))


KEY = ("coinbase", "okx")


def test_no_samples_no_episodes():
  t = OpportunityTracker()
  assert t.update(1.0, 1.0, {}, True) == []


def test_episode_opens_tracks_peaks_and_closes_when_gone():
  t = OpportunityTracker()
  assert t.update(1000.0, 10.0, {KEY: sample("0.5", profit="10", small_edge="0.004")}, True) == []
  assert t.update(1000.5, 10.5, {KEY: sample("0.9", profit="25", small_edge="0.006")}, True) == []
  assert t.update(1000.8, 10.8, {KEY: sample("0.2", profit="4", small_edge="0.002")}, True) == []
  (ep,) = t.update(1001.0, 11.0, {}, True)
  assert (ep.buy_exchange, ep.sell_exchange, ep.end_reason) == ("coinbase", "okx", "gone")
  assert ep.duration_ms == 1000 and ep.samples == 3
  assert (ep.peak_qty, ep.peak_net_profit, ep.peak_net_edge) == (D("0.9"), D(25), D("0.006"))
  assert ep.started_at == datetime.fromtimestamp(1000.0, UTC)
  assert ep.ended_at == datetime.fromtimestamp(1001.0, UTC)
  assert t.open_keys == set()


def test_unhealthy_closes_at_last_confirmed_sample():
  t = OpportunityTracker()
  t.update(1000.0, 10.0, {KEY: sample()}, True)
  t.update(1000.4, 10.4, {KEY: sample()}, True)
  (ep,) = t.update(1005.0, 15.0, {}, False)  # data lost 4.6 s later
  assert ep.end_reason == "unhealthy"
  assert ep.duration_ms == 400  # not 5000: we only know it existed until the last sample
  assert ep.ended_at == datetime.fromtimestamp(1000.4, UTC)


def test_directions_are_independent_and_reopen_as_new_episode():
  t = OpportunityTracker()
  other = ("okx", "coinbase")
  t.update(1.0, 1.0, {KEY: sample(), other: Sample(opp("okx", "coinbase"), opp("okx", "coinbase"))}, True)
  (ep,) = t.update(2.0, 2.0, {KEY: sample()}, True)
  assert (ep.buy_exchange, ep.sell_exchange) == ("okx", "coinbase")
  t.update(3.0, 3.0, {}, True)
  t.update(4.0, 4.0, {KEY: sample()}, True)
  assert t.open_keys == {KEY}  # the reopened one is a fresh episode


def test_close_all_uses_last_confirmed_sample():
  t = OpportunityTracker()
  t.update(1000.0, 10.0, {KEY: sample()}, True)
  t.update(1000.2, 10.2, {KEY: sample()}, True)
  (ep,) = t.close_all("shutdown")
  assert (ep.end_reason, ep.duration_ms, ep.samples) == ("shutdown", 200, 2)
  assert t.close_all("shutdown") == []
