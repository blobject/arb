"""OKX public REST helpers (no API keys)."""

from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

REQUEST_TIMEOUT_S = 10.0


class InstrumentError(Exception):
  pass


@dataclass(frozen=True, slots=True)
class Instrument:
  inst_id: str
  base: str
  quote: str
  state: str
  tick_size: Decimal
  lot_size: Decimal
  min_size: Decimal


def _row(d: dict[str, Any]) -> Instrument:
  try:
    return Instrument(
      inst_id=d["instId"],
      base=d["baseCcy"],
      quote=d["quoteCcy"],
      state=d["state"],
      tick_size=Decimal(d["tickSz"]),
      lot_size=Decimal(d["lotSz"]),
      min_size=Decimal(d["minSz"]),
    )
  except (KeyError, InvalidOperation) as exc:
    raise InstrumentError(f"unexpected instrument fields: {exc!r}") from exc


def _data(payload: Any) -> list[Any]:
  if not isinstance(payload, dict):
    raise InstrumentError("unexpected response shape")
  if payload.get("code") != "0":
    raise InstrumentError(f"OKX error {payload.get('code')}: {payload.get('msg')}")
  return payload.get("data") or []


def parse_live_instruments(payload: Any) -> dict[str, Instrument]:
  """All live SPOT instruments from a GET /api/v5/public/instruments response, by id."""
  out: dict[str, Instrument] = {}
  for d in _data(payload):
    try:
      inst = _row(d)
    except InstrumentError:
      continue
    if inst.state == "live":
      out[inst.inst_id] = inst
  return out


def parse_quote_volumes(payload: Any) -> dict[str, Decimal]:
  """24 h volume in quote currency per instrument, from GET /api/v5/market/tickers."""
  out: dict[str, Decimal] = {}
  for t in _data(payload):
    try:
      out[t["instId"]] = Decimal(t["volCcy24h"])
    except (KeyError, TypeError, InvalidOperation):
      continue
  return out


def parse_instrument(payload: Any, inst_id: str) -> Instrument:
  """Validate a GET /api/v5/public/instruments response for one SPOT instrument."""
  matches = [d for d in _data(payload) if d.get("instId") == inst_id]
  if not matches:
    raise InstrumentError(f"{inst_id} not found (has the USDC pair been enabled for this entity?)")
  inst = _row(matches[0])
  if inst.state != "live":
    raise InstrumentError(f"{inst_id} state is {inst.state!r}, expected 'live'")
  return inst


async def fetch_instrument(base_url: str, inst_id: str) -> Instrument:
  import aiohttp  # imported lazily so unit tests don't need it

  url = f"{base_url.rstrip('/')}/api/v5/public/instruments"
  timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
  try:
    async with aiohttp.ClientSession(timeout=timeout) as session:
      async with session.get(url, params={"instType": "SPOT", "instId": inst_id}) as resp:
        resp.raise_for_status()
        payload = await resp.json()
  except (aiohttp.ClientError, TimeoutError) as exc:  # a timeout is not a ClientError
    raise InstrumentError(f"request to {url} failed: {exc!r}") from exc
  return parse_instrument(payload, inst_id)


async def fetch_listing(base_url: str) -> tuple[dict[str, Instrument], dict[str, Decimal]]:
  """All live SPOT instruments, plus their 24 h quote volumes."""
  import aiohttp

  base = base_url.rstrip("/")
  timeout = aiohttp.ClientTimeout(total=REQUEST_TIMEOUT_S)
  try:
    async with aiohttp.ClientSession(timeout=timeout) as session:
      async with session.get(f"{base}/api/v5/public/instruments", params={"instType": "SPOT"}) as resp:
        resp.raise_for_status()
        instruments = parse_live_instruments(await resp.json())
      async with session.get(f"{base}/api/v5/market/tickers", params={"instType": "SPOT"}) as resp:
        resp.raise_for_status()
        volumes = parse_quote_volumes(await resp.json())
  except (aiohttp.ClientError, TimeoutError) as exc:
    raise InstrumentError(f"request to {base} failed: {exc!r}") from exc
  return instruments, volumes
