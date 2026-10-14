"""Non-blocking Postgres writer. The detector only appends to an in-memory backlog; a single
background task drains it, reconnecting (and re-running migrations) after any failure."""

import asyncio
import logging
from collections import deque
from decimal import Decimal
from importlib import resources
from typing import Any

from arb.events import Episode, HealthEvent, describe, log_health

log = logging.getLogger("arb.db")

INSERT_EPISODE = """
INSERT INTO arbitrage_opportunities
    (started_at, ended_at, duration_ms, buy_exchange, sell_exchange, base, buy_quote, sell_quote,
     peak_qty, peak_net_edge, peak_net_profit, buy_vwap, sell_vwap,
     epoch, end_reason, samples, buy_fee, sell_fee, extra_cost, probe_notional)
VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
"""
INSERT_HEALTH = "INSERT INTO health_events (at, component, event, detail, epoch) VALUES (%s, %s, %s, %s, %s)"


def load_migrations() -> list[tuple[str, str]]:
  folder = resources.files("arb.storage") / "migrations"
  files = sorted((f for f in folder.iterdir() if f.name.endswith(".sql")), key=lambda f: f.name)
  return [(f.name, f.read_text()) for f in files]


async def migrate(conn: Any) -> None:
  await conn.execute(
    "CREATE TABLE IF NOT EXISTS schema_migrations "
    "(name TEXT PRIMARY KEY, applied_at TIMESTAMPTZ NOT NULL DEFAULT now())"
  )
  cur = await conn.execute("SELECT name FROM schema_migrations")
  done = {row[0] for row in await cur.fetchall()}
  for name, sql in load_migrations():
    if name in done:
      continue
    async with conn.transaction():
      await conn.execute(sql)
      await conn.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (name,))
    log.info("applied migration %s", name)


class Database:
  def __init__(
    self,
    url: str,
    epoch: str,
    fees: dict[str, Decimal],
    extra_cost: Decimal,
    probe_notional: Decimal,
    max_backlog: int = 10_000,
  ) -> None:
    self.url, self.epoch, self.fees = url, epoch, fees
    self.extra_cost, self.probe_notional, self.max_backlog = extra_cost, probe_notional, max_backlog
    self.dropped = 0
    self._items: deque[Episode | HealthEvent] = deque()
    self._ready = asyncio.Event()

  # -- Sink interface (never blocks) --------------------------------------------------

  def put_episode(self, episode: Episode) -> None:
    log.info(describe(episode))
    self._enqueue(episode)

  def put_health(self, event: HealthEvent) -> None:
    log_health(event)
    self._enqueue(event)

  def _enqueue(self, item: Episode | HealthEvent) -> None:
    if len(self._items) >= self.max_backlog:
      self._items.popleft()  # database has been down a long time: shed the oldest
      self.dropped += 1
      log.error("backlog full, dropped oldest record (total dropped: %d)", self.dropped)
    self._items.append(item)
    self._ready.set()

  # -- writer task ---------------------------------------------------------------------

  async def run(self) -> None:
    import psycopg  # imported lazily so unit tests don't need it

    delay = 1.0
    while True:
      try:
        conn = await self._connect()
        async with conn:
          await migrate(conn)
          log.info("database ready (%d queued)", len(self._items))
          delay = 1.0
          await self._drain(conn)
      except (psycopg.Error, OSError) as exc:
        log.warning("database error: %r; retrying in %.0fs", exc, delay)
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30.0)

  async def _connect(self) -> Any:
    import psycopg

    return await psycopg.AsyncConnection.connect(self.url, autocommit=True, connect_timeout=10)

  async def flush(self, limit_s: float = 5.0) -> None:
    """Best-effort write of whatever is still queued; called once on shutdown."""
    if not self._items:
      return
    import psycopg

    pending = len(self._items)
    try:
      async with asyncio.timeout(limit_s):
        conn = await self._connect()
        async with conn:
          while self._items:
            await self._write(conn, self._items[0])
            self._items.popleft()
    except (TimeoutError, psycopg.Error, OSError) as exc:
      log.error("shutdown flush incomplete: %d of %d records lost (%r)", len(self._items), pending, exc)
    else:
      log.info("flushed %d records on shutdown", pending)

  async def _drain(self, conn: Any) -> None:
    while True:
      if not self._items:
        self._ready.clear()
        await self._ready.wait()
        continue
      item = self._items[0]
      await self._write(conn, item)  # on failure the item stays at the head
      if self._items and self._items[0] is item:
        self._items.popleft()

  async def _write(self, conn: Any, item: Episode | HealthEvent) -> None:
    if isinstance(item, Episode):
      await conn.execute(INSERT_EPISODE, self._episode_params(item))
    else:
      await conn.execute(INSERT_HEALTH, (item.at, item.component, item.event, item.detail, self.epoch))

  def _episode_params(self, e: Episode) -> tuple[Any, ...]:
    return (
      e.started_at,
      e.ended_at,
      e.duration_ms,
      e.buy_exchange,
      e.sell_exchange,
      e.base,
      e.buy_quote,
      e.sell_quote,
      e.peak_qty,
      e.peak_net_edge,
      e.peak_net_profit,
      e.buy_vwap,
      e.sell_vwap,
      self.epoch,
      e.end_reason,
      e.samples,
      self.fees.get(e.buy_exchange),
      self.fees.get(e.sell_exchange),
      self.extra_cost,
      self.probe_notional,
    )
