import json
import tempfile
from decimal import Decimal as D

import jwt
import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec, ed25519

from arb.tools.fees_coinbase import KeyProblem, build_jwt, describe, load_key, parse_summary

NAME = "organizations/org-1/apiKeys/key-1"


def pem_of(key) -> str:
  return key.private_bytes(
    serialization.Encoding.PEM, serialization.PrivateFormat.TraditionalOpenSSL, serialization.NoEncryption()
  ).decode()


def test_jwt_matches_coinbase_spec_and_verifies():
  key = ec.generate_private_key(ec.SECP256R1())
  token = build_jwt(NAME, pem_of(key), "GET", "api.coinbase.com", "/api/v3/brokerage/transaction_summary", now=1000)
  header = jwt.get_unverified_header(token)
  assert header["alg"] == "ES256" and header["kid"] == NAME and len(header["nonce"]) >= 32
  claims = jwt.decode(token, key.public_key(), algorithms=["ES256"], options={"verify_exp": False, "verify_nbf": False})
  assert claims == {
    "sub": NAME,
    "iss": "cdp",
    "nbf": 1000,
    "exp": 1120,
    "uri": "GET api.coinbase.com/api/v3/brokerage/transaction_summary",
  }


def test_every_token_gets_a_fresh_nonce():
  pem = pem_of(ec.generate_private_key(ec.SECP256R1()))
  a, b = (build_jwt(NAME, pem, "GET", "h", "/p", now=1) for _ in range(2))
  assert jwt.get_unverified_header(a)["nonce"] != jwt.get_unverified_header(b)["nonce"]


def test_wrong_key_types_are_explained():
  ed = ed25519.Ed25519PrivateKey.generate()
  ed_pem = ed.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8, serialization.NoEncryption())
  with pytest.raises(KeyProblem, match="ECDSA"):
    build_jwt(NAME, ed_pem.decode(), "GET", "h", "/p")
  with pytest.raises(KeyProblem, match="ECDSA"):
    build_jwt(NAME, pem_of(ec.generate_private_key(ec.SECP384R1())), "GET", "h", "/p")
  with pytest.raises(KeyProblem, match="PEM"):
    build_jwt(NAME, "not a key", "GET", "h", "/p")


def test_load_key_from_file_and_environment():
  pem = pem_of(ec.generate_private_key(ec.SECP256R1()))
  with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as f:
    json.dump({"name": NAME, "privateKey": pem}, f)
  assert load_key(f.name, {}) == (NAME, pem)
  one_line = pem.replace("\n", "\\n")  # as pasted into an env var
  env = {"COINBASE_API_KEY_NAME": NAME, "COINBASE_API_PRIVATE_KEY": one_line}
  assert load_key(None, env) == (NAME, pem)
  with pytest.raises(KeyProblem):
    load_key(None, {})
  with pytest.raises(KeyProblem):
    load_key("/nonexistent/key.json", {})
  with tempfile.NamedTemporaryFile("w", suffix=".json", delete=False) as g:
    json.dump([1, 2], g)
  with pytest.raises(KeyProblem, match="JSON object"):
    load_key(g.name, {})


def test_parse_summary_singapore_style_gst_is_already_included():
  payload = {
    "total_fees": 0,
    "fee_tier": {"pricing_tier": "Advanced 1", "taker_fee_rate": "0.012", "maker_fee_rate": "0.006"},
    "goods_and_services_tax": {"rate": "0.09", "type": "INCLUSIVE"},
    "advanced_trade_only_volume": 0,
  }
  s = parse_summary(payload)
  assert (s.taker, s.maker, s.effective_taker) == (D("0.012"), D("0.006"), D("0.012"))
  text = describe(s)
  assert "1.20% (120 bps)" in text and "already included" in text and "--fee coinbase=0.012" in text


def test_exclusive_gst_is_added_to_the_taker_rate():
  payload = {
    "fee_tier": {"pricing_tier": "x", "taker_fee_rate": "0.004", "maker_fee_rate": "0.0025"},
    "goods_and_services_tax": {"rate": "0.1", "type": "EXCLUSIVE"},
  }
  assert parse_summary(payload).effective_taker == D("0.0044")


def test_unexpected_responses_are_rejected():
  for bad in ({}, {"fee_tier": {}}, {"fee_tier": {"taker_fee_rate": "abc", "maker_fee_rate": "0"}}, []):
    with pytest.raises(ValueError):
      parse_summary(bad)
