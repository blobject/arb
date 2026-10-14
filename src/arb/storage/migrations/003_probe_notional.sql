-- Probe size is now configured in quote currency (PROBE_NOTIONAL) so it means the same for
-- every coin. probe_qty stays for rows collected before this change.
ALTER TABLE arbitrage_opportunities
  ADD COLUMN IF NOT EXISTS probe_notional NUMERIC;
