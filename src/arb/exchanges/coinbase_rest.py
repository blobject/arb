"""Coinbase public REST helpers (no API keys)."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

PRODUCTS_URL = "https://api.coinbase.com/api/v3/brokerage/market/products"
REQUEST_TIMEOUT_S = 15.0


class ProductError(Exception):
  pass


@dataclass(frozen=True, slots=True)
class Product:
  product_id: str
  base: str
  quote: str
  online: bool  # listed, and neither trading- nor fully disabled
  quote_volume_24h: Decimal | None


def _dec(value: Any) -> Decimal | None:
  try:
    return Decimal(str(value)) if value not in (None, "") else None
  except InvalidOperation:
    return None


def parse_products(payload: Any) -> dict[str, Product]:
  """Index a GET /market/products response by product id, skipping malformed rows."""
  rows = payload.get("products") if isinstance(payload, dict) else None
  if not isinstance(rows, list):
    raise ProductError("unexpected response shape from Coinbase products")
  out: dict[str, Product] = {}
  for p in rows:
    if not isinstance(p, dict) or not p.get("product_id"):
      continue
    online = p.get("status") == "online" and not p.get("trading_disabled") and not p.get("is_disabled")
    out[p["product_id"]] = Product(
      product_id=p["product_id"],
      base=str(p.get("base_currency_id", "")),
      quote=str(p.get("quote_currency_id", "")),
      online=online,
      quote_volume_24h=_dec(p.get("approximate_quote_24h_volume")),
    )
  return out


def check_product(products: dict[str, Product], product_id: str) -> Product:
  p = products.get(product_id)
  if p is None:
    raise ProductError(f"{product_id} is not listed on Coinbase")
  if not p.online:
    raise ProductError(f"{product_id} is listed on Coinbase but not currently tradeable")
  return p


async def fetch_products() -> dict[str, Product]:
  import aiohttp  # imported lazily so unit tests don't need it

  timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
  try:
    async with aiohttp.ClientSession(timeout=timeout) as session:
      async with session.get(PRODUCTS_URL, params={"product_type": "SPOT"}) as resp:
        resp.raise_for_status()
        payload = await resp.json()
  except (aiohttp.ClientError, TimeoutError) as exc:  # a timeout is not a ClientError
    raise ProductError(f"request to {PRODUCTS_URL} failed: {exc!r}") from exc
  return parse_products(payload)
