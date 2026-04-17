-- Learning registry: structured config changes and evaluation outcomes.

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
CREATE INDEX IF NOT EXISTS idx_learning_change_pending ON learning_change_records (result_status)
    WHERE result_status = 'pending';
