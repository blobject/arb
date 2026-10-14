import asyncio
import logging
from decimal import Decimal

from arb.exchanges.base import FeedView
from arb.marketdata.orderbook import OrderBook

log = logging.getLogger("arb.monitor")
BPS = Decimal(10_000)


def book_line(feed: FeedView) -> str:
  book = feed.book
  bid, ask, age = book.best_bid(), book.best_ask(), book.age_ms()
  head = f"{feed.name:9} {feed.market.symbol}"
  if bid is None or ask is None or age is None:
    return f"{head} waiting for book (connected={feed.connected})"
  return (
    f"{head} bid {bid.price} x {bid.size} | ask {ask.price} x {ask.size} "
    f"| spread {ask.price - bid.price} | age {age:.0f}ms"
  )


def cross_line(a: OrderBook, b: OrderBook) -> str | None:
  """Gross top-of-book edge in each direction, in bps. None if either book is empty."""
  a_bid, a_ask, b_bid, b_ask = a.best_bid(), a.best_ask(), b.best_bid(), b.best_ask()
  if a_bid is None or a_ask is None or b_bid is None or b_ask is None:
    return None
  a_to_b = (b_bid.price - a_ask.price) / a_ask.price * BPS  # buy on a, sell on b
  b_to_a = (a_bid.price - b_ask.price) / b_ask.price * BPS  # buy on b, sell on a
  return (
    f"cross     buy {a.market.exchange}/sell {b.market.exchange} {a_to_b:+.2f} bps | "
    f"buy {b.market.exchange}/sell {a.market.exchange} {b_to_a:+.2f} bps (gross, top of book)"
  )


async def monitor(a: FeedView, b: FeedView, interval: float) -> None:
  """Periodic human-readable status. Detection lives in arb.detector."""
  while True:
    await asyncio.sleep(interval)
    log.info(book_line(a))
    log.info(book_line(b))
    if (line := cross_line(a.book, b.book)) is not None:
      log.info(line)
