import asyncio
from decimal import Decimal as D

from arb.detector import Detector, DetectorConfig
from arb.events import Episode, HealthEvent
from arb.marketdata.orderbook import OrderBook
from arb.marketdata.types import Level, Market


class FakeFeed:
  def __init__(self, name: str, connected: bool = True) -> None:
    self.name = name
    self.market = Market(name, "BTC", "USDC")
    self.book = OrderBook(self.market)
    self.connected = connected

  def set(self, bid: int, ask: int, size: int | str = 1) -> None:
    self.book.apply_snapshot([Level(D(bid), D(size))], [Level(D(ask), D(size))])


class ListSink:
  def __init__(self) -> None:
    self.episodes: list[Episode] = []
    self.health: list[HealthEvent] = []

  def put_episode(self, episode: Episode) -> None:
    self.episodes.append(episode)

  def put_health(self, event: HealthEvent) -> None:
    self.health.append(event)


def make(**kw):
  cb, okx, sink = FakeFeed("coinbase"), FakeFeed("okx"), ListSink()
  cfg = DetectorConfig(
    stale_ms={"coinbase": 1500, "okx": 3000},
    extra_cost=D(0),
    min_net_edge=D(0),
    probe_notional=D(100),
    detect_notional=D(100000),
    **kw,
  )
  return cb, okx, sink, Detector(cfg, cb, okx, D(0), D(0), sink)


def test_opportunity_lifecycle_produces_one_episode():
  cb, okx, sink, det = make()
  cb.set(99990, 100000)
  okx.set(100500, 100510, size="0.5")  # okx bid above coinbase ask
  assert det.step(1000.0, 10.0) == []  # opens; not reported until it closes
  okx.set(99000, 99010)  # edge gone
  (ep,) = det.step(1001.0, 11.0)
  assert sink.episodes == [ep]
  assert (ep.buy_exchange, ep.sell_exchange, ep.end_reason) == ("coinbase", "okx", "gone")
  assert ep.duration_ms == 1000
  assert ep.peak_qty == D("0.5")  # limited by okx bid depth
  assert ep.peak_net_edge == D("0.005")  # 100500/100000 - 1, zero fees
  assert ep.buy_vwap == D(100000) and ep.sell_vwap == D(100500)


def test_stale_book_closes_episode_and_blocks_detection():
  cb, okx, sink, det = make()
  cb.set(99990, 100000)
  okx.set(100500, 100510)
  det.step(1000.0, 10.0)
  assert okx.book.local_ts is not None
  okx.book.local_ts -= 10  # okx data goes stale
  (ep,) = det.step(1000.5, 10.5)
  assert ep.end_reason == "unhealthy"
  assert det.step(1001.0, 11.0) == []  # nothing is evaluated against stale data
  assert det.tracker.open_keys == set()


def test_no_episode_when_fees_exceed_spread():
  cb, okx, sink, det = make()
  det.fee_a = D("0.006")  # coinbase taker placeholder
  cb.set(99990, 100000)
  okx.set(100500, 100510)  # 0.5% gross < 0.6% fee
  assert det.step(1.0, 1.0) == []
  assert det.tracker.open_keys == set()


def test_health_events_on_transitions_only():
  cb, okx, sink, det = make()
  cb.set(99990, 100000)
  okx.set(99000, 99010)
  det.step(1.0, 1.0)
  det.step(2.0, 2.0)  # nothing changed: no new events
  kinds = [(h.component, h.event) for h in sink.health if h.event != "alive"]
  assert kinds == [("coinbase", "connected"), ("okx", "connected"), ("detector", "healthy")]
  okx.connected = False
  okx.book.clear()
  det.step(3.0, 3.0)
  kinds = [(h.component, h.event) for h in sink.health if h.event != "alive"][3:]
  assert kinds == [("okx", "disconnected"), ("detector", "unhealthy")]
  assert "okx=" in (sink.health[-1].detail or "")


async def test_run_loop_steps_on_wake_and_on_idle():
  cb, okx, sink, det = make(min_interval_s=0.0, idle_check_s=0.02)
  cb.set(99990, 100000)
  okx.set(99000, 99010)
  task = asyncio.create_task(det.run())
  await asyncio.sleep(0.1)  # idle checks alone must establish health
  assert ("detector", "healthy") in [(h.component, h.event) for h in sink.health]
  task.cancel()


def test_stale_threshold_is_per_venue():
  cb, okx, sink, det = make()
  cb.set(99990, 100000)
  okx.set(100500, 100510)
  assert okx.book.local_ts is not None
  okx.book.local_ts -= 2.0  # 2 s quiet: fine for OKX (3 s), would be stale for Coinbase (1.5 s)
  det.step(1.0, 1.0)
  assert det.tracker.open_keys == {("coinbase", "okx")}
  assert cb.book.local_ts is not None
  cb.book.local_ts -= 2.0
  (ep,) = det.step(2.0, 2.0)
  assert ep.end_reason == "unhealthy"


def test_alive_heartbeat_is_periodic():
  cb, okx, sink, det = make(alive_interval_s=60)
  cb.set(99990, 100000)
  okx.set(99000, 99010)
  for t in (0.0, 30.0, 59.0, 60.0, 90.0, 121.0):
    det.step(1000 + t, t)
  alive = [h for h in sink.health if h.event == "alive"]
  assert len(alive) == 3  # t=0, 60, 121
  assert alive[0].detail == "healthy=True open=0"


def test_close_reports_open_episodes_on_shutdown():
  cb, okx, sink, det = make()
  cb.set(99990, 100000)
  okx.set(100500, 100510)
  det.step(1000.0, 10.0)
  det.step(1000.3, 10.3)
  (ep,) = det.close()
  assert ep.end_reason == "shutdown" and ep.duration_ms == 300
  assert sink.episodes == [ep] and det.tracker.open_keys == set()
