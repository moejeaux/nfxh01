-- Phase 3.1a: Liquidation cascade risk score at decision time.
--
-- Populated by the CascadeForecaster advisory (config: cascade_forecaster.enabled).
-- Column is advisory metadata; no queries gate on it yet. NULL when forecaster is
-- disabled or when the column was added after the trade was recorded.

ALTER TABLE acevault_decisions
    ADD COLUMN IF NOT EXISTS cascade_risk_score DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cascade_risk_level TEXT;

ALTER TABLE strategy_decisions
    ADD COLUMN IF NOT EXISTS cascade_risk_score DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS cascade_risk_level TEXT;
