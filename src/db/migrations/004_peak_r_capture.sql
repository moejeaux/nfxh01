-- Phase 1: Peak-R capture & Track A exit persistence.
--
-- Adds per-trade Peak-R / realized-R columns so retrospectives can measure how
-- much favorable excursion each trade captured (trailing / time-stop tuning signal).
-- Also back-fills ``strategy_decisions`` with exit columns so Track A closes land
-- in the journal instead of only stdout (prior data-leak noted in Phase 1 review).

ALTER TABLE acevault_decisions
    ADD COLUMN IF NOT EXISTS peak_r_multiple DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS realized_r_multiple DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS peak_r_capture_ratio DOUBLE PRECISION;

ALTER TABLE strategy_decisions
    ADD COLUMN IF NOT EXISTS exit_price DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS exit_reason VARCHAR(30),
    ADD COLUMN IF NOT EXISTS pnl_usd DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS pnl_pct DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS hold_duration_seconds INTEGER,
    ADD COLUMN IF NOT EXISTS outcome_recorded_at TIMESTAMPTZ,
    ADD COLUMN IF NOT EXISTS peak_r_multiple DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS realized_r_multiple DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS peak_r_capture_ratio DOUBLE PRECISION;

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_outcome
    ON strategy_decisions (outcome_recorded_at)
    WHERE outcome_recorded_at IS NOT NULL;
