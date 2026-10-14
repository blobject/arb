"""List coins tradeable against USDC on both Coinbase and OKX, busiest first.

    uv run arb-pairs [--top 25]

Volumes are the last 24 h in quote currency (~USD), as each venue reports it; the smaller of the
two is what limits arbitrage. This uses OKX's public listing, which may include coins your
Singapore account can't trade: check each candidate in the OKX SG app, including that deposits
and withdrawals are supported. OKX Singapore lists some tokens as trade-only, and inventory in
those can't be moved in or out, so they can't be rebalanced.
"""

import argparse
import asyncio
from dataclasses import dataclass
from decimal import Decimal

from arb.exchanges.coinbase_rest import Product, ProductError, fetch_products
from arb.exchanges.okx_rest import Instrument, InstrumentError, fetch_listing

# stablecoins against USDC are a different game (fixed ~1.0 prices); leave them out
STABLES = {"USDT", "USDC", "EURC", "DAI", "PYUSD", "USDG", "RLUSD", "FDUSD", "USDE", "TUSD"}


@dataclass(frozen=True, slots=True)
class PairRow:
  base: str
  coinbase_product: str
  okx_inst: str
  coinbase_volume: Decimal | None
  okx_volume: Decimal | None

  @property
  def limiting_volume(self) -> Decimal:
    return min(self.coinbase_volume or Decimal(0), self.okx_volume or Decimal(0))


def common_pairs(
  products: dict[str, Product],
  instruments: dict[str, Instrument],
  okx_volumes: dict[str, Decimal],
  quote: str = "USDC",
) -> list[PairRow]:
  rows = []
  for inst_id, inst in instruments.items():
    if inst.quote != quote or inst.base in STABLES:
      continue
    # Coinbase serves the same book for -USDC and -USD ids; prefer the id matching our quote
    product = products.get(f"{inst.base}-{quote}") or products.get(f"{inst.base}-USD")
    if product is None or not product.online:
      continue
    rows.append(PairRow(inst.base, product.product_id, inst_id, product.quote_volume_24h, okx_volumes.get(inst_id)))
  return sorted(rows, key=lambda r: r.limiting_volume, reverse=True)


def render(rows: list[PairRow], top: int) -> str:
  def money(v: Decimal | None) -> str:
    return "?" if v is None else f"{v:,.0f}"

  lines = [f"{len(rows)} coins on both venues against USDC; top {min(top, len(rows))} by the smaller 24 h volume:", ""]
  lines.append(f"{'coin':<8}{'coinbase 24h':>16}{'okx 24h':>16}   run it with")
  for r in rows[:top]:
    lines.append(f"{r.base:<8}{money(r.coinbase_volume):>16}{money(r.okx_volume):>16}   BASE_ASSET={r.base}")
  return "\n".join(lines)


async def _load(okx_rest_url: str) -> list[PairRow]:
  products, (instruments, volumes) = await asyncio.gather(fetch_products(), fetch_listing(okx_rest_url))
  return common_pairs(products, instruments, volumes)


def cli(argv: list[str] | None = None) -> None:
  from arb.config import Settings

  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--top", type=int, default=25)
  a = ap.parse_args(argv)
  try:
    rows = asyncio.run(_load(Settings().okx_rest_url))
  except (ProductError, InstrumentError) as exc:
    raise SystemExit(f"could not load listings: {exc}") from exc
  print(render(rows, a.top))
