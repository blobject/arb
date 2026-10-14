import asyncio
import json
from decimal import Decimal as D

import pytest
from websockets.exceptions import ConnectionClosed

from arb.exchanges.base import FeedError, SequenceGap
from arb.exchanges.coinbase import CoinbaseFeed
from arb.marketdata.types import Market

MARKET = Market("coinbase", "BTC", "USDC")
TS = "2026-09-29T10:00:00.123456789Z"


def upd(side, price, qty):
  return {"side": side, "event_time": TS, "price_level": str(price), "new_quantity": str(qty)}


def l2(seq, kind, updates, product="BTC-USDC"):
  ev = [{"type": kind, "product_id": product, "updates": updates}]
  return {"channel": "l2_data", "timestamp": TS, "sequence_num": seq, "events": ev}


def hb(seq):
  return {"channel": "heartbeats", "timestamp": TS, "sequence_num": seq, "events": [{}]}


def snap(seq, bid=99, ask=101, product="BTC-USDC"):
  return l2(seq, "snapshot", [upd("bid", bid, 1), upd("bid", bid - 1, 2), upd("offer", ask, 3)], product)


def feed(**kw):
  return CoinbaseFeed(MARKET, backoff_base=0, **kw)


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


def test_snapshot_builds_book():
  f = feed()
  assert feed_process(f, snap(1)) is True
  bid, ask = top(f)
  assert (bid.price, ask.price) == (D(99), D(101))
  assert f.connected is False  # not connected until run() has subscribed
  assert f.book.is_fresh(1000)


def test_update_replaces_and_removes_levels():
  f = feed()
  feed_process(f, snap(1))
  feed_process(f, l2(2, "update", [upd("bid", 99, "0.5"), upd("offer", 101, "0"), upd("offer", 102, 1)]))
  bid, ask = top(f)
  assert bid.size == D("0.5")
  assert ask.price == D(102)


def test_update_before_snapshot_is_an_error():
  with pytest.raises(FeedError):
    feed_process(feed(), l2(1, "update", [upd("bid", 99, 1)]))


def test_other_products_ignored_and_usd_twin_accepted():
  f = feed()
  assert feed_process(f, snap(1, product="ETH-USDC")) is False
  assert f.book.best_bid() is None
  assert feed_process(f, snap(2, product="BTC-USD")) is True
  assert top(f)[0].price == D(99)


def test_sequence_is_per_connection_across_channels():
  f = feed()
  feed_process(f, snap(5))
  feed_process(f, hb(6))
  feed_process(f, l2(7, "update", [upd("bid", 99, 1)]))  # consecutive despite heartbeat between


def test_sequence_gap_and_out_of_order_raise():
  f = feed()
  feed_process(f, snap(1))
  with pytest.raises(SequenceGap):
    feed_process(f, l2(3, "update", [upd("bid", 99, 1)]))
  g = feed()
  feed_process(g, snap(4))
  with pytest.raises(SequenceGap):
    feed_process(g, hb(4))


def test_sequence_check_can_be_disabled():
  f = feed(check_sequence=False)
  feed_process(f, snap(1))
  feed_process(f, l2(50, "update", [upd("bid", 99, 1)]))


def test_server_error_and_malformed_messages():
  with pytest.raises(FeedError):
    feed_process(feed(), {"type": "error", "message": "nope"})
  with pytest.raises(FeedError):
    feed_process(feed(), l2(1, "snapshot", [{"side": "bid"}]))
  with pytest.raises(FeedError):
    feed().process("not json")
  with pytest.raises(FeedError):
    feed_process(feed(), l2(1, "snapshot", [upd("sideways", 1, 1)]))


def test_garbage_never_escapes_as_a_non_feed_error():
  bad = [
    "[]",
    '"hi"',
    "123",
    "null",
    json.dumps({"channel": "l2_data", "events": "abc"}),
    json.dumps({"channel": "l2_data", "events": [1]}),
  ]
  for raw in bad:
    with pytest.raises(FeedError):
      feed().process(raw)


def test_invalid_levels_rejected():
  for price, qty in [("NaN", "1"), ("Infinity", "1"), ("0", "1"), ("-5", "1"), ("5", "-1")]:
    with pytest.raises(FeedError):
      feed_process(feed(), l2(1, "snapshot", [upd("bid", price, qty)]))


def test_heartbeat_keeps_quiet_book_fresh():
  f = feed()
  feed_process(f, snap(1))
  assert f.book.local_ts is not None
  f.book.local_ts -= 10  # pretend nothing has changed for 10 s
  assert not f.book.is_fresh(1000)
  feed_process(f, hb(2))
  assert f.book.is_fresh(1000)


# ---- run loop, driven by fake connections (no network) ----------------------------


class FakeWS:
  def __init__(self, *items):
    self.items = list(items)
    self.sent = []

  async def send(self, data):
    self.sent.append(json.loads(data))

  async def recv(self):
    if self.items:
      item = self.items.pop(0)
      if isinstance(item, Exception):
        raise item
      return json.dumps(item)
    await asyncio.sleep(3600)  # idle connection


def fake_connect(*sessions):
  async def gen():
    for s in sessions:
      yield s

  return lambda url, **kw: gen()


def closed():
  return ConnectionClosed(None, None)


async def test_run_subscribes_and_resyncs_after_gap():
  s1 = FakeWS(snap(1), l2(2, "update", [upd("bid", 100, 1)]), l2(5, "update", [upd("bid", 50, 1)]))
  s2 = FakeWS(snap(1, bid=200, ask=201), closed())
  f = feed(connect=fake_connect(s1, s2))
  seen = []
  await f.run(recorder(seen))
  assert seen == [(D(99), D(101)), (D(100), D(101)), (D(200), D(201))]
  assert {m["channel"] for m in s1.sent} == {"level2", "heartbeats"}
  assert s1.sent[0]["product_ids"] == ["BTC-USDC"]
  # session ended: book must not be left looking usable
  assert f.book.best_bid() is None and not f.connected


async def test_run_reconnects_after_silence():
  s1 = FakeWS()  # never sends anything
  s2 = FakeWS(snap(1), closed())
  f = feed(read_timeout=0.05, connect=fake_connect(s1, s2))
  seen = []
  await f.run(recorder(seen))
  assert seen == [(D(99), D(101))]


async def test_run_recovers_from_connection_drop_and_server_error():
  s1 = FakeWS(snap(1), closed())
  s2 = FakeWS({"type": "error", "message": "boom"})
  s3 = FakeWS(snap(1, bid=10, ask=11), closed())
  f = feed(connect=fake_connect(s1, s2, s3))
  seen = []
  await f.run(recorder(seen))
  assert seen == [(D(99), D(101)), (D(10), D(11))]
