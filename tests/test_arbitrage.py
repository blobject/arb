from decimal import Decimal as D

from arb.marketdata.orderbook import OrderBook, Side
from arb.marketdata.types import Level, Market
from arb.strategy.arbitrage import detect, evaluate


def book(ex: str, bids=(), asks=()) -> OrderBook:
  b = OrderBook(Market(ex, "BTC", "USDC"))
  b.apply_snapshot(
    [Level(D(str(p)), D(str(s))) for p, s in bids],
    [Level(D(str(p)), D(str(s))) for p, s in asks],
  )
  return b


Z = D(0)


def test_no_cross_returns_none():
  a = book("cb", asks=[(101, 1)])
  b = book("okx", bids=[(100, 1)])
  assert evaluate(a, b, Z, Z) is None


def test_simple_cross_no_fees():
  a = book("cb", asks=[(100, 1)])
  b = book("okx", bids=[(101, D("0.5"))])
  o = evaluate(a, b, Z, Z)
  assert o is not None
  assert o.qty == D("0.5")
  assert o.buy_vwap == 100 and o.sell_vwap == 101
  assert o.net_profit == D("0.5")


def test_stops_when_marginal_slice_unprofitable():
  a = book("cb", asks=[(100, 1), (102, 1)])
  b = book("okx", bids=[(101, 2)])
  o = evaluate(a, b, Z, Z)
  assert o is not None and o.qty == 1


def test_vwap_across_levels():
  a = book("cb", asks=[(100, 1), (101, 1)])
  b = book("okx", bids=[(105, 2)])
  o = evaluate(a, b, Z, Z)
  assert o is not None
  assert o.qty == 2
  assert o.buy_vwap == D("100.5") and o.sell_vwap == 105


def test_fees_can_erase_spread():
  # 0.2% spread, 10 bps each side -> exactly breakeven, not profitable
  a = book("cb", asks=[(100000, 1)])
  b = book("okx", bids=[(100200, 1)])
  assert evaluate(a, b, D("0.001"), D("0.001")) is None
  assert evaluate(a, b, D("0.0005"), D("0.0005")) is not None


def test_max_qty_cap():
  a = book("cb", asks=[(100, 5)])
  b = book("okx", bids=[(101, 5)])
  o = evaluate(a, b, Z, Z, max_qty=D("0.001"))
  assert o is not None and o.qty == D("0.001")


def test_extra_cost_buffer_and_min_edge():
  a = book("cb", asks=[(100, 1)])
  b = book("okx", bids=[(100.2, 1)])
  assert evaluate(a, b, Z, Z, extra_cost=D("0.003")) is None
  assert evaluate(a, b, Z, Z, min_net_edge=D("0.01")) is None
  assert evaluate(a, b, Z, Z, extra_cost=D("0.001")) is not None


def test_summary_example_full_depth():
  cb = book("cb", asks=[(100000, "0.002"), (100010, "0.005"), (100030, "0.020")])
  okx = book("okx", bids=[(100200, "0.001"), (100190, "0.004"), (100150, "0.020")])
  o = evaluate(cb, okx, D("0.0005"), D("0.0005"))
  assert o is not None
  assert o.qty == D("0.025")  # all bid depth is consumed
  assert o.sell_vwap < D("100200") and o.buy_vwap > D("100000")


def test_detect_checks_both_directions():
  a = book("cb", bids=[(99, 1)], asks=[(100, 1)])
  b = book("okx", bids=[(101, 1)], asks=[(102, 1)])
  opps = detect(a, b, Z, Z)
  assert len(opps) == 1 and opps[0].buy_market.exchange == "cb"


def test_orderbook_delta_and_freshness():
  ob = book("cb", bids=[(99, 1)], asks=[(100, 1)])
  ob.apply_delta(Side.ASK, D(100), D(0))
  assert ob.best_ask() is None
  ob.apply_delta(Side.ASK, D(101), D(2))
  assert ob.best_ask() == Level(D(101), D(2))
  assert ob.is_fresh(1000)
  ob.clear()
  assert not ob.is_fresh(1000)


def test_depth_limits_levels_considered():
  a = book("cb", asks=[(100, 1), (100.1, 1), (100.2, 1)])
  b = book("okx", bids=[(101, 5)])
  full, capped = evaluate(a, b, Z, Z), evaluate(a, b, Z, Z, depth=2)
  assert full is not None and full.qty == 3
  assert capped is not None and capped.qty == 2


def test_orderbook_depth_reads_are_best_first():
  ob = book("cb", bids=[(97, 1), (99, 1), (98, 1)], asks=[(103, 1), (101, 1), (102, 1)])
  assert [lv.price for lv in ob.bids(2)] == [D(99), D(98)]
  assert [lv.price for lv in ob.asks(2)] == [D(101), D(102)]
  assert len(ob.asks()) == 3


def test_max_notional_caps_quote_spent():
  a = book("cb", asks=[(100, 5)])
  b = book("okx", bids=[(101, 5)])
  o = evaluate(a, b, Z, Z, max_notional=D(250))
  assert o is not None and o.qty == D("2.5")  # 250 quote at 100
  deep = book("cb", asks=[(100, 1), (200, 5)])
  o = evaluate(deep, book("okx", bids=[(300, 9)]), Z, Z, max_notional=D(300))
  assert o is not None and o.qty == D("2")  # 1 at 100, then 200 of quote buys 1 more at 200
