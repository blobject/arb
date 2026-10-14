import asyncio
import contextlib
import logging
from datetime import UTC, datetime

from arb.config import Settings
from arb.detector import Detector, DetectorConfig
from arb.events import HealthEvent, LogSink, Sink
from arb.exchanges.coinbase import CoinbaseFeed
from arb.exchanges.coinbase_rest import ProductError, check_product, fetch_products
from arb.exchanges.okx import OkxFeed
from arb.exchanges.okx_rest import InstrumentError, fetch_instrument
from arb.marketdata.types import Market
from arb.monitor import monitor
from arb.storage.database import Database

log = logging.getLogger("arb")


async def main() -> None:
  settings = Settings()
  logging.basicConfig(
    level=settings.log_level.upper(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
  )
  cb_market = Market("coinbase", settings.base_asset, settings.coinbase_quote)
  okx_market = Market("okx", settings.base_asset, settings.okx_quote)

  try:  # fail fast on a coin either venue doesn't offer, instead of retrying forever
    inst = await fetch_instrument(settings.okx_rest_url, okx_market.symbol)
    check_product(await fetch_products(), cb_market.symbol)
  except (InstrumentError, ProductError) as exc:
    log.error("cannot start: %s", exc)
    raise SystemExit(1) from exc
  log.info(
    "okx instrument %s is live (tick %s, lot %s, min %s)",
    inst.inst_id,
    inst.tick_size,
    inst.lot_size,
    inst.min_size,
  )

  coinbase = CoinbaseFeed(cb_market, url=settings.coinbase_ws_url, read_timeout=settings.ws_read_timeout_s)
  okx = OkxFeed(okx_market, url=settings.okx_ws_url)

  db: Database | None = None
  sink: Sink = LogSink()
  if settings.persist:
    db = Database(
      settings.database_url,
      settings.epoch,
      fees={"coinbase": settings.coinbase_taker_fee, "okx": settings.okx_taker_fee},
      extra_cost=settings.extra_cost,
      probe_notional=settings.probe_notional,
    )
    sink = db
  log.info("epoch=%s persist=%s", settings.epoch, settings.persist)

  detector = Detector(
    DetectorConfig(
      stale_ms={"coinbase": settings.coinbase_stale_ms, "okx": settings.okx_stale_ms},
      extra_cost=settings.extra_cost,
      min_net_edge=settings.min_net_edge,
      probe_notional=settings.probe_notional,
      detect_notional=settings.detect_notional,
      min_interval_s=settings.detect_min_interval_ms / 1000,
    ),
    coinbase,
    okx,
    settings.coinbase_taker_fee,
    settings.okx_taker_fee,
    sink,
  )
  sink.put_health(
    HealthEvent(
      datetime.now(UTC),
      "process",
      "started",
      f"fees coinbase={settings.coinbase_taker_fee} okx={settings.okx_taker_fee} "
      f"extra={settings.extra_cost} min_net_edge={settings.min_net_edge}",
    )
  )
  try:
    async with asyncio.TaskGroup() as tg:
      tg.create_task(coinbase.run(detector.wake))
      tg.create_task(okx.run(detector.wake))
      tg.create_task(detector.run())
      tg.create_task(monitor(coinbase, okx, settings.monitor_interval_s))
      if db is not None:
        tg.create_task(db.run())
  finally:  # Ctrl-C lands here: record open episodes and the stop, then flush once
    detector.close("shutdown")
    sink.put_health(HealthEvent(datetime.now(UTC), "process", "stopped"))
    if db is not None:
      await db.flush()


def run() -> None:
  with contextlib.suppress(KeyboardInterrupt):
    asyncio.run(main())


if __name__ == "__main__":
  run()
