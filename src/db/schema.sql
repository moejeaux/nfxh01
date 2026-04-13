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
    exit_price FLOAT,
    exit_reason VARCHAR(30),
    pnl_usd FLOAT,
    pnl_pct FLOAT,
    hold_duration_seconds INTEGER,
    outcome_recorded_at TIMESTAMPTZ,
    signal_source VARCHAR(20) DEFAULT 'acevault',
    regime_at_close VARCHAR(30)
);

CREATE INDEX idx_acevault_decisions_coin ON acevault_decisions(coin);
CREATE INDEX idx_acevault_decisions_regime ON acevault_decisions(regime);
CREATE INDEX idx_acevault_decisions_created ON acevault_decisions(created_at);
