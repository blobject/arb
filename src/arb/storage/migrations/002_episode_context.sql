-- Extra context so stored episodes can be re-interpreted later.
--   peak_qty         largest executable size seen (all profitable depth)
--   peak_net_profit  largest total net profit at that depth
--   peak_net_edge    best net edge at probe_qty (what a tiny trade would have earned)
--   buy_vwap/sell_vwap  taken from the sample with peak_net_edge (probe size)
--   end_reason       'gone' = edge disappeared; 'unhealthy' = data lost, true end unknown
--   epoch            free-form tag for the collection period (e.g. around the OKX USDC cutover)
ALTER TABLE arbitrage_opportunities
    ADD COLUMN IF NOT EXISTS epoch TEXT NOT NULL DEFAULT '',
    ADD COLUMN IF NOT EXISTS end_reason TEXT,
    ADD COLUMN IF NOT EXISTS samples INTEGER,
    ADD COLUMN IF NOT EXISTS buy_fee NUMERIC,
    ADD COLUMN IF NOT EXISTS sell_fee NUMERIC,
    ADD COLUMN IF NOT EXISTS extra_cost NUMERIC,
    ADD COLUMN IF NOT EXISTS probe_qty NUMERIC;

ALTER TABLE health_events
    ADD COLUMN IF NOT EXISTS epoch TEXT NOT NULL DEFAULT '';
