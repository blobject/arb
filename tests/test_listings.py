from decimal import Decimal as D

import pytest

from arb.exchanges.coinbase_rest import ProductError, check_product, parse_products
from arb.exchanges.okx_rest import InstrumentError, parse_live_instruments, parse_quote_volumes
from arb.tools.pairs import common_pairs, render


def cb(pid, status="online", disabled=False, vol="1000000"):
  base, quote = pid.split("-")
  return {
    "product_id": pid,
    "base_currency_id": base,
    "quote_currency_id": quote,
    "status": status,
    "trading_disabled": disabled,
    "approximate_quote_24h_volume": vol,
  }


def okx(inst, state="live"):
  base, quote = inst.split("-")
  return {
    "instId": inst,
    "baseCcy": base,
    "quoteCcy": quote,
    "state": state,
    "tickSz": "0.01",
    "lotSz": "0.0001",
    "minSz": "0.001",
  }


def test_parse_products_and_check():
  products = parse_products({"products": [cb("ETH-USDC"), cb("XYZ-USDC", disabled=True), {"junk": 1}]})
  assert set(products) == {"ETH-USDC", "XYZ-USDC"}
  assert check_product(products, "ETH-USDC").quote_volume_24h == D(1000000)
  with pytest.raises(ProductError, match="not currently tradeable"):
    check_product(products, "XYZ-USDC")
  with pytest.raises(ProductError, match="not listed"):
    check_product(products, "NOPE-USDC")
  with pytest.raises(ProductError):
    parse_products([])


def test_okx_listing_parsers():
  payload = {"code": "0", "data": [okx("ETH-USDC"), okx("OLD-USDC", state="suspend"), {"instId": "BAD"}]}
  assert set(parse_live_instruments(payload)) == {"ETH-USDC"}
  tickers = {"code": "0", "data": [{"instId": "ETH-USDC", "volCcy24h": "2500000"}, {"instId": "X-USDC"}]}
  assert parse_quote_volumes(tickers) == {"ETH-USDC": D(2500000)}
  with pytest.raises(InstrumentError):
    parse_live_instruments({"code": "50011", "msg": "rate limited"})


def test_common_pairs_ranks_by_the_smaller_volume():
  products = parse_products(
    {
      "products": [
        cb("ETH-USDC", vol="900000000"),
        cb("SOL-USD", vol="400000000"),  # only a -USD id on Coinbase: same book, still counts
        cb("DOGE-USDC", vol="50000000"),
        cb("OFF-USDC", status="offline"),
        cb("USDT-USDC"),
      ]
    }
  )
  instruments = parse_live_instruments(
    {"code": "0", "data": [okx(i) for i in ("ETH-USDC", "SOL-USDC", "DOGE-USDC", "OFF-USDC", "USDT-USDC", "ETH-USDT")]}
  )
  volumes = {"ETH-USDC": D(3_000_000), "SOL-USDC": D(8_000_000), "DOGE-USDC": D(500_000)}
  rows = common_pairs(products, instruments, volumes)
  assert [r.base for r in rows] == ["SOL", "ETH", "DOGE"]  # limited by OKX's smaller USDC books
  assert rows[0].coinbase_product == "SOL-USD" and rows[0].limiting_volume == D(8_000_000)
  text = render(rows, top=2)
  assert "3 coins on both venues" in text and "BASE_ASSET=SOL" in text and "DOGE" not in text
