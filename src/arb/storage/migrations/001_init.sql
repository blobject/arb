CREATE TABLE IF NOT EXISTS arbitrage_opportunities (
    id              BIGSERIAL PRIMARY KEY,
    started_at      TIMESTAMPTZ NOT NULL,
    ended_at        TIMESTAMPTZ NOT NULL,
    duration_ms     INTEGER NOT NULL,
    buy_exchange    TEXT NOT NULL,
    sell_exchange   TEXT NOT NULL,
    base            TEXT NOT NULL,
    buy_quote       TEXT NOT NULL,
    sell_quote      TEXT NOT NULL,
    peak_qty        NUMERIC NOT NULL,
    peak_net_edge   NUMERIC NOT NULL,
    peak_net_profit NUMERIC NOT NULL,
    buy_vwap        NUMERIC NOT NULL,
    sell_vwap       NUMERIC NOT NULL
);
CREATE INDEX IF NOT EXISTS opp_started_idx ON arbitrage_opportunities (started_at);

CREATE TABLE IF NOT EXISTS health_events (
    id         BIGSERIAL PRIMARY KEY,
    at         TIMESTAMPTZ NOT NULL DEFAULT now(),
    component  TEXT NOT NULL,   -- e.g. 'coinbase_ws'
    event      TEXT NOT NULL,   -- e.g. 'disconnected', 'stale', 'resync'
    detail     TEXT
);
