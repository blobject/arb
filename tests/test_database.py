import asyncio
from datetime import UTC, datetime
from decimal import Decimal as D

import pytest

from arb.events import Episode, HealthEvent
from arb.storage.database import Database, load_migrations

NOW = datetime(2026, 9, 29, tzinfo=UTC)


def episode(n=1):
  return Episode(
    "coinbase",
    "okx",
    "BTC",
    "USDC",
    "USDC",
    NOW,
    NOW,
    n,
    "gone",
    1,
    D("0.5"),
    D(10),
    D("0.004"),
    D(100000),
    D(100400),
  )


class FakeDB(Database):
  def __init__(self, fail_first=False, max_backlog=10):
    super().__init__("postgresql://unused", "test", {"coinbase": D("0.006")}, D(0), D(100), max_backlog)
    self.written, self.fail = [], fail_first

  async def _write(self, conn, item):
    if self.fail:
      self.fail = False
      raise OSError("db down")
    self.written.append(item)


def test_migrations_are_found_in_order_and_are_idempotent_sql():
  names = [n for n, _ in load_migrations()]
  assert names == sorted(names) and names[0].startswith("001_")
  for name, sql in load_migrations():
    assert "IF NOT EXISTS" in sql, name  # must be safe on a pre-initialised volume


def test_episode_params_line_up_with_the_insert_statement():
  from arb.storage.database import INSERT_EPISODE

  db = FakeDB()
  params = db._episode_params(episode())
  assert len(params) == INSERT_EPISODE.count("%s") == 20
  assert "probe_notional" in INSERT_EPISODE and params[19] == db.probe_notional
  assert params[13] == "test" and params[16] == D("0.006") and params[17] is None  # okx fee unset


def test_backlog_is_bounded_and_sheds_oldest():
  db = FakeDB(max_backlog=3)
  for i in range(5):
    db.put_episode(episode(i))
  assert db.dropped == 2
  assert [e.duration_ms for e in db._items if isinstance(e, Episode)] == [2, 3, 4]


async def test_failed_write_keeps_item_and_order_after_reconnect():
  db = FakeDB(fail_first=True)
  db.put_episode(episode(1))
  db.put_health(HealthEvent(NOW, "okx", "disconnected"))
  with pytest.raises(OSError):
    await db._drain(None)  # first write fails; nothing may be lost
  assert len(db._items) == 2 and db.written == []
  task = asyncio.create_task(db._drain(None))  # "reconnected"
  await asyncio.sleep(0.05)
  task.cancel()
  assert [type(x).__name__ for x in db.written] == ["Episode", "HealthEvent"]
  assert len(db._items) == 0


class Conn:
  async def __aenter__(self):
    return self

  async def __aexit__(self, *exc):
    return False


async def test_flush_writes_everything_left():
  db = FakeDB()

  async def connect():
    return Conn()

  db._connect = connect
  db.put_episode(episode(1))
  db.put_health(HealthEvent(NOW, "process", "stopped"))
  await db.flush()
  assert len(db.written) == 2 and not db._items


async def test_flush_gives_up_cleanly_when_database_is_down():
  db = FakeDB()  # stub connect raises OSError
  db.put_episode(episode(1))
  await db.flush(limit_s=1)  # must not raise
  assert len(db._items) == 1


async def test_flush_respects_time_limit():
  db = FakeDB()

  async def slow():
    await asyncio.sleep(10)

  db._connect = slow
  db.put_episode(episode(1))
  await asyncio.wait_for(db.flush(limit_s=0.05), 1)  # returns promptly, keeps the item
  assert len(db._items) == 1
