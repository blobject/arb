from decimal import Decimal as D

import pytest

from arb.exchanges.okx_rest import InstrumentError, parse_instrument


def payload(state="live", inst="BTC-USDC", code="0"):
  row = {
    "instId": inst,
    "baseCcy": "BTC",
    "quoteCcy": "USDC",
    "state": state,
    "tickSz": "0.1",
    "lotSz": "0.00000001",
    "minSz": "0.00001",
  }
  return {"code": code, "msg": "", "data": [row]}


def test_parses_live_instrument():
  inst = parse_instrument(payload(), "BTC-USDC")
  assert inst.quote == "USDC" and inst.tick_size == D("0.1") and inst.min_size == D("0.00001")


def test_rejects_bad_responses():
  for bad in [
    payload(state="suspend"),
    payload(code="51001"),
    {"code": "0", "msg": "", "data": []},
    payload(inst="BTC-USD"),  # wrong instrument returned
    {"code": "0", "data": [{"instId": "BTC-USDC"}]},  # missing fields
    [],
  ]:
    with pytest.raises(InstrumentError):
      parse_instrument(bad, "BTC-USDC")
