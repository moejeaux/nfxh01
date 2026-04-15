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
