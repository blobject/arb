import asyncio
import json
from decimal import Decimal as D

import pytest
from websockets.exceptions import ConnectionClosed

from arb.exchanges.base import FeedError, SequenceGap
from arb.exchanges.okx import OkxFeed
from arb.marketdata.types import Market

MARKET = Market("okx", "BTC", "USDC")


def push(action, seq, prev, bids=(), asks=(), inst="BTC-USDC", channel="books"):
  def lv(p, s):
    return [str(p), str(s), "0", "1"]

  data = {
    "asks": [lv(p, s) for p, s in asks],
    "bids": [lv(p, s) for p, s in bids],
    "ts": "1790000000000",
    "checksum": 0,
    "prevSeqId": prev,
    "seqId": seq,
  }
  return {"arg": {"channel": channel, "instId": inst}, "action": action, "data": [data]}


def snap(seq=100, bid=99, ask=101):
  return push("snapshot", seq, -1, bids=[(bid, 1), (bid - 1, 2)], asks=[(ask, 3)])


def feed(**kw):
  return OkxFeed(MARKET, backoff_base=0, **kw)


def feed_process(f, msg):
  return f.process(json.dumps(msg))


def top(f):
  bid, ask = f.book.best_bid(), f.book.best_ask()
  assert bid is not None and ask is not None
  return bid, ask


def recorder(seen):
  def cb(book):
    bid, ask = book.best_bid(), book.best_ask()
    assert bid is not None and ask is not None
    seen.append((bid.price, ask.price))

  return cb


def test_snapshot_then_update_and_delete():
  f = feed()
  assert feed_process(f, snap()) is True
  assert feed_process(f, push("update", 101, 100, bids=[(99, "0.5")], asks=[(101, 0), (102, 4)])) is True
  bid, ask = top(f)
  assert bid.size == D("0.5") and ask.price == D(102)


def test_seq_may_repeat_jump_or_reset_as_long_as_it_links():
  f = feed()
  feed_process(f, snap(seq=100))
  feed_process(f, push("update", 100, 100))  # empty keepalive: seqId repeats
  feed_process(f, push("update", 5000, 100, bids=[(99, 2)]))  # arbitrary forward jump
  feed_process(f, push("update", 7, 5000, bids=[(99, 3)]))  # maintenance reset: seqId goes lower
  assert top(f)[0].size == 3


def test_broken_link_raises_and_is_not_applied():
  f = feed()
  feed_process(f, snap(seq=100))
  with pytest.raises(SequenceGap):
    feed_process(f, push("update", 102, 101, bids=[(99, 9)]))  # prev 101 != last 100
  assert top(f)[0].size == 1  # untouched


def test_update_before_snapshot_and_unknown_action():
  with pytest.raises(FeedError):
    feed_process(feed(), push("update", 1, 0, bids=[(99, 1)]))
  with pytest.raises(FeedError):
    feed_process(feed(), push("bogus", 1, -1))


def test_other_instruments_and_channels_ignored():
  f = feed()
  assert feed_process(f, push("snapshot", 1, -1, bids=[(1, 1)], asks=[(2, 1)], inst="BTC-USD")) is False
  assert feed_process(f, push("snapshot", 1, -1, bids=[(1, 1)], asks=[(2, 1)], channel="books5")) is False
  assert f.book.best_bid() is None


def test_events_pong_and_errors():
  f = feed()
  assert f.process("pong") is False
  assert feed_process(f, {"event": "subscribe", "arg": {"channel": "books", "instId": "BTC-USDC"}}) is False
  assert feed_process(f, {"event": "channel-conn-count", "channel": "books", "connCount": "1"}) is False
  with pytest.raises(FeedError):
    feed_process(f, {"event": "error", "code": "60018", "msg": "Wrong URL or channel"})
  with pytest.raises(FeedError):
    feed_process(f, {"event": "notice", "code": "64008", "msg": "closing for upgrade"})
  assert feed_process(f, {"event": "notice", "code": "99999"}) is False


def test_garbage_and_invalid_levels_raise_feed_error():
  for raw in ["[]", '"hi"', "null", "not json", json.dumps({"arg": {"channel": "books", "instId": "BTC-USDC"}})]:
    with pytest.raises(FeedError):
      feed().process(raw)
  for price, qty in [("NaN", "1"), ("0", "1"), ("-5", "1"), ("5", "-1"), ("Infinity", "1")]:
    msg = push("snapshot", 1, -1, bids=[(price, qty)])
    with pytest.raises(FeedError):
      feed_process(feed(), msg)


def test_crossed_book_is_rejected():
  f = feed()
  feed_process(f, snap())
  with pytest.raises(FeedError):
    feed_process(f, push("update", 101, 100, bids=[(105, 1)]))  # bid above best ask
  with pytest.raises(FeedError):
    feed_process(feed(), push("snapshot", 1, -1, bids=[(102, 1)], asks=[(101, 1)]))


def test_empty_keepalive_keeps_quiet_book_fresh():
  f = feed()
  feed_process(f, snap())
  assert f.book.local_ts is not None
  f.book.local_ts -= 10
  assert not f.book.is_fresh(1000)
  assert feed_process(f, push("update", 100, 100)) is False  # nothing changed...
  assert f.book.is_fresh(1000)  # ...but it is confirmed current


# ---- run loop, driven by fake connections (no network) ----------------------------


class FakeWS:
  def __init__(self, *items):
    self.items = list(items)
    self.sent = []

  async def send(self, data):
    self.sent.append(data)  # raw strings: OKX's "ping" is not JSON

  async def recv(self):
    if self.items:
      item = self.items.pop(0)
      if isinstance(item, Exception):
        raise item
      return item if isinstance(item, str) else json.dumps(item)
    await asyncio.sleep(3600)


def fake_connect(*sessions, kwargs_log=None):
  async def gen():
    for s in sessions:
      yield s

  def connect(url, **kw):
    if kwargs_log is not None:
      kwargs_log.append(kw)
    return gen()

  return connect


def closed():
  return ConnectionClosed(None, None)


async def test_run_subscribes_and_resyncs_after_broken_link():
  s1 = FakeWS(snap(100), push("update", 101, 100, bids=[(100, 1)]), push("update", 105, 103))
  s2 = FakeWS(snap(1, bid=200, ask=201), closed())
  opts = []
  f = feed(connect=fake_connect(s1, s2, kwargs_log=opts))
  seen = []
  await f.run(recorder(seen))
  assert seen == [(D(99), D(101)), (D(100), D(101)), (D(200), D(201))]
  assert json.loads(s1.sent[0]) == {"op": "subscribe", "args": [{"channel": "books", "instId": "BTC-USDC"}]}
  assert opts[0]["ping_interval"] is None  # OKX uses text ping/pong, not protocol pings
  assert f.book.best_bid() is None and not f.connected


async def test_run_sends_ping_then_reconnects_if_no_pong():
  s1 = FakeWS(snap())  # then silence
  s2 = FakeWS(snap(1, bid=10, ask=11), closed())
  f = feed(idle_timeout=0.05, connect=fake_connect(s1, s2))
  seen = []
  await f.run(recorder(seen))
  assert "ping" in s1.sent
  assert seen == [(D(99), D(101)), (D(10), D(11))]


async def test_run_pong_keeps_connection_alive():
  class PongWS(FakeWS):
    async def send(self, data):
      self.sent.append(data)
      if data == "ping":
        self.items.append("pong")  # server answers

  s1 = PongWS(snap())
  f = feed(idle_timeout=0.05, connect=fake_connect(s1))
  task = asyncio.create_task(f.run(lambda b: None))
  await asyncio.sleep(0.35)  # several idle windows
  assert not task.done()  # never gave up on the connection
  assert s1.sent.count("ping") >= 2
  task.cancel()


async def test_run_reconnects_on_upgrade_notice_and_drops():
  s1 = FakeWS(snap(), {"event": "notice", "code": "64008", "msg": "upgrade"})
  s2 = FakeWS(snap(1, bid=10, ask=11), closed())
  s3 = FakeWS(snap(1, bid=20, ask=21), closed())
  f = feed(connect=fake_connect(s1, s2, s3))
  seen = []
  await f.run(recorder(seen))
  assert seen == [(D(99), D(101)), (D(10), D(11)), (D(20), D(21))]
