-- Phase 2a: Fee-drag capture columns — finish the `retro.healthy_gate.max_fee_drag_pct` sensor.
--
-- Today the Hyperliquid executor does not surface realized fills to the close path
-- (`src/execution/executor.py::ExecutionResult` has no fee field and is a sacred module).
-- Until the executor exposes realized fees, `fee_paid_usd` is populated by a
-- config-driven estimate from notional × `retro.fee_estimation.taker_bps_per_side`
-- at journal-write time. Column lives here so swapping the estimator for realized
-- fees later is a one-line change (no further migration required).
--
-- `slippage_bps` is reserved storage for the same eventual surface; left NULL today.

ALTER TABLE acevault_decisions
    ADD COLUMN IF NOT EXISTS fee_paid_usd DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS slippage_bps DOUBLE PRECISION;

ALTER TABLE strategy_decisions
    ADD COLUMN IF NOT EXISTS fee_paid_usd DOUBLE PRECISION,
    ADD COLUMN IF NOT EXISTS slippage_bps DOUBLE PRECISION;
