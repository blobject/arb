"""Pure, I/O-free opportunity calculation. Everything here is unit-testable."""

from dataclasses import dataclass
from decimal import Decimal

from arb.marketdata.orderbook import OrderBook
from arb.marketdata.types import Market

ZERO = Decimal(0)
ONE = Decimal(1)


@dataclass(frozen=True, slots=True)
class Opportunity:
  buy_market: Market
  sell_market: Market
  qty: Decimal  # base units
  buy_vwap: Decimal
  sell_vwap: Decimal
  gross_edge: Decimal  # sell_vwap / buy_vwap - 1
  net_edge: Decimal  # after fees + extra_cost buffer
  net_profit: Decimal  # in quote units (assumes buy/sell quotes are 1:1)


def evaluate(
  buy_book: OrderBook,
  sell_book: OrderBook,
  buy_fee: Decimal,
  sell_fee: Decimal,
  extra_cost: Decimal = ZERO,
  max_qty: Decimal | None = None,
  min_net_edge: Decimal = ZERO,
  depth: int = 100,
  max_notional: Decimal | None = None,
) -> Opportunity | None:
  """Walk asks on the buy venue and bids on the sell venue together.

  A slice is taken only while its *marginal* net edge is positive. Asks rise and
  bids fall as we walk, so marginal edge is monotonically decreasing and the
  greedy stop is optimal for total profit.

  extra_cost is a haircut (fraction of notional) for slippage/rebalance costs
  not modeled elsewhere. Fees are fractions (0.001 = 10 bps).

  Size can be capped in base units (max_qty) and/or in quote spent on the buy side
  (max_notional); the latter works the same for any coin.

  Only the top `depth` levels of each book are considered (real books can hold tens
  of thousands of levels); size beyond that is ignored, which is conservative.

  NOTE: if buy and sell quotes differ (USD vs USDC) this treats them as 1:1.
  Model the basis explicitly before trusting results.
  """
  buy_rate = ONE + buy_fee + extra_cost
  sell_rate = ONE - sell_fee
  best_ask, best_bid = buy_book.best_ask(), sell_book.best_bid()
  if best_ask is None or best_bid is None:
    return None
  if best_bid.price * sell_rate <= best_ask.price * buy_rate:
    return None  # cheap rejection before sorting/walking depth; by far the common case

  asks = iter(buy_book.asks(depth))
  bids = iter(sell_book.bids(depth))
  ask = next(asks, None)
  bid = next(bids, None)
  if ask is None or bid is None:
    return None

  ask_rem, bid_rem = ask.size, bid.size
  qty = buy_notional = sell_notional = ZERO

  while ask is not None and bid is not None:
    if bid.price * sell_rate <= ask.price * buy_rate:
      break
    take = min(ask_rem, bid_rem)
    if max_qty is not None:
      take = min(take, max_qty - qty)
    if max_notional is not None:
      take = min(take, (max_notional - buy_notional) / ask.price)
    if take <= 0:
      break
    qty += take
    buy_notional += take * ask.price
    sell_notional += take * bid.price
    ask_rem -= take
    bid_rem -= take
    if ask_rem == 0:
      ask = next(asks, None)
      ask_rem = ask.size if ask else ZERO
    if bid_rem == 0:
      bid = next(bids, None)
      bid_rem = bid.size if bid else ZERO

  if qty == 0:
    return None

  cost = buy_notional * buy_rate
  proceeds = sell_notional * sell_rate
  net_edge = proceeds / cost - ONE
  if net_edge < min_net_edge:
    return None
  return Opportunity(
    buy_market=buy_book.market,
    sell_market=sell_book.market,
    qty=qty,
    buy_vwap=buy_notional / qty,
    sell_vwap=sell_notional / qty,
    gross_edge=sell_notional / buy_notional - ONE,
    net_edge=net_edge,
    net_profit=proceeds - cost,
  )


def detect(
  a: OrderBook,
  b: OrderBook,
  fee_a: Decimal,
  fee_b: Decimal,
  **kwargs,
) -> list[Opportunity]:
  """Check both directions between two venues."""
  out = []
  if (o := evaluate(a, b, fee_a, fee_b, **kwargs)) is not None:
    out.append(o)
  if (o := evaluate(b, a, fee_b, fee_a, **kwargs)) is not None:
    out.append(o)
  return out
