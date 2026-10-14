from decimal import Decimal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
  model_config = SettingsConfigDict(env_file=".env", extra="ignore")

  database_url: str = "postgresql://arb:arb@localhost:5432/arb"
  log_level: str = "INFO"

  base_asset: str = "BTC"
  coinbase_quote: str = "USDC"
  coinbase_ws_url: str = "wss://advanced-trade-ws.coinbase.com"
  ws_read_timeout_s: float = 10.0

  okx_quote: str = "USDC"
  okx_ws_url: str = "wss://ws.okx.com:8443/ws/v5/public"
  okx_rest_url: str = "https://www.okx.com"

  # Defaults are "measurement mode": record every episode with gross edge > 1 bp, and apply
  # real fees afterwards with `arb-report --fee ...`. Stored rows record the values used.
  coinbase_taker_fee: Decimal = Decimal("0")
  okx_taker_fee: Decimal = Decimal("0")
  extra_cost: Decimal = Decimal("0")
  min_net_edge: Decimal = Decimal("0.0001")
  # Sizes in quote currency (USDC), so the same settings work for any coin
  probe_notional: Decimal = Decimal("100")  # the edge a tiny live trade would see
  detect_notional: Decimal = Decimal("100000")  # generous cap so "executable size" is measured, not capped
  detect_min_interval_ms: int = 20
  coinbase_stale_ms: int = 1500  # Coinbase sends heartbeats every second
  okx_stale_ms: int = 3000  # OKX went quiet for up to ~2 s in testing; it has no heartbeat channel

  persist: bool = False
  epoch: str = "dev"  # tag for this collection period; stored on every row
  monitor_interval_s: float = 5.0
