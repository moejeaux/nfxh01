-- Track A / multi-strategy audit trail (separate from acevault_decisions shape).
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

CREATE INDEX IF NOT EXISTS idx_strategy_decisions_engine_created
    ON strategy_decisions (engine_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_coin
    ON strategy_decisions (coin);
CREATE INDEX IF NOT EXISTS idx_strategy_decisions_strategy_key
    ON strategy_decisions (strategy_key);
