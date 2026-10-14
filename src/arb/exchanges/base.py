from abc import ABC, abstractmethod
from collections.abc import Callable
from decimal import Decimal
from typing import Protocol

from arb.marketdata.orderbook import OrderBook
from arb.marketdata.types import Level, Market


class FeedError(Exception):
  """Protocol problem on this connection; the feed resyncs from a fresh snapshot."""


class SequenceGap(FeedError):
  def __init__(self, expected: int | None, got: int, channel: str | None) -> None:
    super().__init__(f"sequence gap on {channel}: expected={expected} got={got}")


def make_level(price: str, size: str) -> Level:
  """Parse and sanity-check one price level from a venue message."""
  p, s = Decimal(price), Decimal(size)
  if not (p.is_finite() and p > 0 and s.is_finite() and s >= 0):
    raise FeedError(f"invalid level: price={p} size={s}")
  return Level(p, s)


class FeedView(Protocol):
  """What consumers (detector, monitor) read from a feed. Structural, so tests can use fakes."""

  @property
  def name(self) -> str: ...
  @property
  def market(self) -> Market: ...
  @property
  def book(self) -> OrderBook: ...
  @property
  def connected(self) -> bool: ...


class MarketDataFeed(ABC):
  """Strategy code depends on this, never on venue-specific quirks.

  Implementations own reconnects, sequence-gap handling and resubscription. On any
  gap they must call book.clear() and rebuild from a fresh snapshot.
  """

  name: str
  market: Market
  book: OrderBook

  @abstractmethod
  async def run(self, on_update: Callable[[OrderBook], None]) -> None:
    """Connect and stream forever, invoking on_update after each book change."""

  @property
  @abstractmethod
  def connected(self) -> bool: ...
