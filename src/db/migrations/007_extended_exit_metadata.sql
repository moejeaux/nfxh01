-- Session 6: Extended exit metadata columns for AceVault decisions.
--
-- Supports granular cost attribution (entry/exit fees, funding, slippage)
-- and gross/net PnL breakdown. Previously applied via runtime DDL in
-- DecisionJournal._ensure_acevault_extended_columns(); now a proper migration.

ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS expected_entry_price DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS realized_entry_price DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS expected_exit_price DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS realized_exit_price DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS entry_fee_usd DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS exit_fee_usd DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS funding_usd DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS slippage_entry_usd DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS slippage_exit_usd DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS gross_pnl_usd DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS net_pnl_usd DOUBLE PRECISION;
