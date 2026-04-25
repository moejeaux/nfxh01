CREATE TABLE IF NOT EXISTS acevault_decisions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    coin VARCHAR(20) NOT NULL,
    decision_type VARCHAR(30) NOT NULL,
    regime VARCHAR(30) NOT NULL,
    weakness_score FLOAT,
    entry_price FLOAT,
    stop_loss_price FLOAT,
    take_profit_price FLOAT,
    position_size_usd FLOAT,
    fathom_override BOOLEAN DEFAULT FALSE,
    fathom_size_mult FLOAT,
    fathom_reasoning TEXT,
    fathom_post_analysis TEXT,
    fathom_post_analysis_at TIMESTAMPTZ,
    exit_price FLOAT,
    exit_reason VARCHAR(30),
    pnl_usd FLOAT,
    pnl_pct FLOAT,
    hold_duration_seconds INTEGER,
    outcome_recorded_at TIMESTAMPTZ,
    signal_source VARCHAR(20) DEFAULT 'acevault',
    regime_at_close VARCHAR(30)
);

CREATE INDEX IF NOT EXISTS idx_acevault_decisions_coin ON acevault_decisions(coin);
CREATE INDEX IF NOT EXISTS idx_acevault_decisions_regime ON acevault_decisions(regime);
CREATE INDEX IF NOT EXISTS idx_acevault_decisions_created ON acevault_decisions(created_at);

CREATE TABLE IF NOT EXISTS fathom_retrospective_runs (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ DEFAULT NOW(),
    window_start TIMESTAMPTZ NOT NULL,
    window_end TIMESTAMPTZ NOT NULL,
    market_snapshot JSONB NOT NULL DEFAULT '{}',
    decisions_digest JSONB,
    analysis_text TEXT,
    analysis_json JSONB,
    previous_run_id UUID REFERENCES fathom_retrospective_runs(id),
    model_used VARCHAR(128)
);

CREATE INDEX IF NOT EXISTS idx_fathom_retrospective_created ON fathom_retrospective_runs(created_at DESC);

CREATE TABLE IF NOT EXISTS strategy_decisions (
    id UUID PRIMARY KEY,
    created_at TIMESTAMPTZ DEFAULT NOW(),
    strategy_key VARCHAR(64) NOT NULL,
    engine_id VARCHAR(64) NOT NULL,
    coin VARCHAR(32) NOT NULL,
    side VARCHAR(8) NOT NULL,
    decision_type VARCHAR(16) NOT NULL DEFAULT 'entry',
    position_size_usd DOUBLE PRECISION NOT NULL,
    entry_price DOUBLE PRECISION,
    stop_loss_price DOUBLE PRECISION,
    take_profit_price DOUBLE PRECISION,
    leverage INTEGER DEFAULT 1,
    job_id VARCHAR(64),
    idempotency_key VARCHAR(128),
    metadata JSONB DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_engine_created ON strategy_decisions (engine_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_coin ON strategy_decisions (coin);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_strategy_key ON strategy_decisions (strategy_key);

CREATE TABLE IF NOT EXISTS learning_change_records (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    retrospective_run_id UUID REFERENCES fathom_retrospective_runs(id) ON DELETE SET NULL,
    change_id UUID NOT NULL UNIQUE,
    schema_version INT NOT NULL,
    config_schema_version INT NOT NULL,
    advisor_schema_version INT NOT NULL,
    retro_mode VARCHAR(16) NOT NULL,
    action_type VARCHAR(64) NOT NULL,
    target_key VARCHAR(256) NOT NULL,
    old_value JSONB,
    new_value JSONB,
    confidence DOUBLE PRECISION,
    auto_applied BOOLEAN NOT NULL DEFAULT FALSE,
    result_status VARCHAR(24) NOT NULL DEFAULT 'pending',
    closing_trade_count_at_apply INT NOT NULL DEFAULT 0,
    baseline_profit_factor DOUBLE PRECISION,
    evaluation_notes TEXT
);

CREATE INDEX IF NOT EXISTS idx_learning_change_created ON learning_change_records (created_at DESC);

-- Full config intelligence DDL: src/db/migrations/008_config_intelligence.sql
