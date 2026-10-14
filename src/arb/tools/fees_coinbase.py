"""Print your Coinbase Advanced fee tier, using a read-only CDP API key.

The key must be a Secret API key with the ECDSA signature algorithm (Coinbase rejects
Ed25519 keys for these APIs) and only the View permission. Give it either as the JSON file
downloaded from the CDP portal:

    uv run arb-fees-cb --key-file ~/cdp_api_key.json

or through COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY in the environment.
"""

import argparse
import json
import os
import secrets
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

HOST = "api.coinbase.com"
PATH = "/api/v3/brokerage/transaction_summary"
TOKEN_LIFETIME_S = 120  # Coinbase rejects tokens older than two minutes


class KeyProblem(Exception):
  pass


@dataclass(frozen=True, slots=True)
class FeeSummary:
  pricing_tier: str
  taker: Decimal
  maker: Decimal
  gst_rate: Decimal | None
  gst_type: str | None
  volume_30d: Decimal | None

  @property
  def effective_taker(self) -> Decimal:
    """Taker rate you actually pay: adds GST only when Coinbase says it isn't included."""
    if self.gst_type == "EXCLUSIVE" and self.gst_rate is not None and 0 <= self.gst_rate < 1:
      return self.taker * (1 + self.gst_rate)
    return self.taker


def load_key(key_file: str | None, env: Mapping[str, str]) -> tuple[str, str]:
  """Return (key name, PEM private key) from a CDP key file or the environment."""
  if key_file:
    try:
      data = json.loads(Path(key_file).read_text())
    except (OSError, ValueError) as exc:
      raise KeyProblem(f"cannot read {key_file} as a CDP key file: {exc}") from exc
    if not isinstance(data, dict):
      raise KeyProblem(f"{key_file} is not a CDP key file (expected a JSON object)")
    name, pem = data.get("name") or data.get("id"), data.get("privateKey")
  else:
    name, pem = env.get("COINBASE_API_KEY_NAME"), env.get("COINBASE_API_PRIVATE_KEY")
  if not name or not pem:
    raise KeyProblem("no key: pass --key-file, or set COINBASE_API_KEY_NAME and COINBASE_API_PRIVATE_KEY")
  return name, pem.replace("\\n", "\n")  # tolerate a PEM pasted on one line with escaped newlines


def build_jwt(name: str, pem: str, method: str, host: str, path: str, now: float | None = None) -> str:
  """Short-lived ES256 token for one request, as described in Coinbase's API key docs."""
  try:
    key = serialization.load_pem_private_key(pem.encode(), password=None)
  except ValueError as exc:
    raise KeyProblem(f"private key is not a valid PEM key: {exc}") from exc
  if not isinstance(key, ec.EllipticCurvePrivateKey) or not isinstance(key.curve, ec.SECP256R1):
    raise KeyProblem(
      f"this is a {type(key).__name__.replace('PrivateKey', '')} key; Coinbase needs an ECDSA (P-256) key. "
      "Create a new key and choose ECDSA under Advanced settings."
    )
  t = int(time.time() if now is None else now)
  claims = {"sub": name, "iss": "cdp", "nbf": t, "exp": t + TOKEN_LIFETIME_S, "uri": f"{method} {host}{path}"}
  return jwt.encode(claims, key, algorithm="ES256", headers={"kid": name, "nonce": secrets.token_hex()})


def _dec(value: Any) -> Decimal | None:
  try:
    return Decimal(str(value)) if value not in (None, "") else None
  except InvalidOperation:
    return None


def parse_summary(payload: Any) -> FeeSummary:
  tier = payload.get("fee_tier") if isinstance(payload, dict) else None
  if not isinstance(tier, dict):
    raise ValueError(f"unexpected response, no fee_tier: {payload!r}")
  taker, maker = _dec(tier.get("taker_fee_rate")), _dec(tier.get("maker_fee_rate"))
  if taker is None or maker is None:
    raise ValueError(f"unexpected fee_tier: {tier!r}")
  gst = payload.get("goods_and_services_tax") or {}
  return FeeSummary(
    pricing_tier=str(tier.get("pricing_tier", "?")),
    taker=taker,
    maker=maker,
    gst_rate=_dec(gst.get("rate")),
    gst_type=gst.get("type"),
    volume_30d=_dec(payload.get("advanced_trade_only_volume")),
  )


def fetch(name: str, pem: str, timeout_s: float = 10.0) -> Any:
  token = build_jwt(name, pem, "GET", HOST, PATH)
  req = urllib.request.Request(
    f"https://{HOST}{PATH}?product_type=SPOT", headers={"Authorization": f"Bearer {token}"}
  )
  with urllib.request.urlopen(req, timeout=timeout_s) as resp:
    return json.load(resp)


def pct(rate: Decimal) -> str:
  return f"{rate * 100:.2f}% ({rate * 10_000:.0f} bps)"


def describe(s: FeeSummary) -> str:
  lines = [
    f"Coinbase fee tier: {s.pricing_tier}",
    f"  taker {s.taker}  = {pct(s.taker)}",
    f"  maker {s.maker}  = {pct(s.maker)}",
  ]
  if s.gst_type:
    included = s.gst_type == "INCLUSIVE"
    note = "already included in the rates above" if included else "added on top of the rates above"
    lines.append(f"  GST {s.gst_rate} {s.gst_type}: {note}")
  if s.volume_30d is not None:
    lines.append(f"  30-day Advanced volume: {s.volume_30d} USD")
  lines.append(f"\nfor arb-report, the bot pays taker on both legs:  --fee coinbase={s.effective_taker.normalize()}")
  return "\n".join(lines)


HINTS = {
  401: "unauthorised: check the key is ECDSA, not revoked, that your clock is right, and any IP allowlist",
  403: "forbidden: the key needs the View permission",
}


def cli(argv: list[str] | None = None) -> None:
  ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
  ap.add_argument("--key-file", help="JSON key file downloaded from the CDP portal")
  ap.add_argument("--raw", action="store_true", help="print Coinbase's full response as well")
  a = ap.parse_args(argv)
  try:
    name, pem = load_key(a.key_file, os.environ)
    payload = fetch(name, pem)
  except KeyProblem as exc:
    sys.exit(f"key problem: {exc}")
  except urllib.error.HTTPError as exc:
    body = exc.read().decode(errors="replace")[:300]
    sys.exit(f"HTTP {exc.code} from Coinbase: {HINTS.get(exc.code, body)}\n{body}")
  except (urllib.error.URLError, TimeoutError) as exc:
    sys.exit(f"could not reach Coinbase: {exc}")
  if a.raw:
    print(json.dumps(payload, indent=2))
  print(describe(parse_summary(payload)))
