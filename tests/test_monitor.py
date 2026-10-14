from decimal import Decimal as D

from arb.exchanges.okx import OkxFeed
from arb.marketdata.orderbook import OrderBook
from arb.marketdata.types import Level, Market
from arb.monitor import book_line, cross_line


def book(ex, bid, ask):
  b = OrderBook(Market(ex, "BTC", "USDC"))
  b.apply_snapshot([Level(D(str(bid)), D(1))], [Level(D(str(ask)), D(1))])
  return b


def test_cross_line_reports_both_directions_in_bps():
  line = cross_line(book("coinbase", 99, 100), book("okx", 101, 102))
  assert line is not None
  assert "buy coinbase/sell okx +100.00 bps" in line  # (101-100)/100
  assert "buy okx/sell coinbase -294.12 bps" in line  # (99-102)/102


def test_cross_line_none_when_a_book_is_empty():
  assert cross_line(book("coinbase", 99, 100), OrderBook(Market("okx", "BTC", "USDC"))) is None


def test_book_line_waiting_then_populated():
  feed = OkxFeed(Market("okx", "BTC", "USDC"))
  assert "waiting for book" in book_line(feed)
  feed.book.apply_snapshot([Level(D(99), D(1))], [Level(D(101), D(2))])
  line = book_line(feed)
  assert "bid 99 x 1" in line and "ask 101 x 2" in line and "spread 2" in line
