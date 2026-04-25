-- Config Attribution & Configuration Intelligence (additive only).

-- ---------------------------------------------------------------------------
-- Registry: effective merged config snapshots
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_versions (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    environment VARCHAR(32) NOT NULL,
    venue VARCHAR(64) NOT NULL,
    strategy_scope VARCHAR(64),
    config_hash CHAR(64) NOT NULL,
    git_commit_sha VARCHAR(64),
    app_version VARCHAR(64),
    source_paths JSONB NOT NULL DEFAULT '{}'::jsonb,
    normalized_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    summary TEXT,
    created_by VARCHAR(128),
    CONSTRAINT uq_config_versions_env_venue_hash UNIQUE (environment, venue, config_hash)
);

CREATE INDEX IF NOT EXISTS idx_config_versions_applied
    ON config_versions (environment, venue, applied_at DESC);

CREATE TABLE IF NOT EXISTS config_version_strategy_fingerprints (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    version_id UUID NOT NULL REFERENCES config_versions(id) ON DELETE CASCADE,
    strategy_key VARCHAR(64) NOT NULL,
    fingerprint_hash CHAR(64) NOT NULL,
    pruned_config JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_version_strategy UNIQUE (version_id, strategy_key)
);

CREATE INDEX IF NOT EXISTS idx_fingerprints_strategy ON config_version_strategy_fingerprints (strategy_key);

-- ---------------------------------------------------------------------------
-- Semantic / leaf diffs between consecutive versions
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_change_events (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    config_version_id UUID NOT NULL REFERENCES config_versions(id) ON DELETE CASCADE,
    previous_config_version_id UUID REFERENCES config_versions(id) ON DELETE SET NULL,
    detected_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    path TEXT NOT NULL,
    old_value JSONB,
    new_value JSONB,
    value_type VARCHAR(32),
    change_category VARCHAR(48) NOT NULL,
    change_tags TEXT[] NOT NULL DEFAULT '{}',
    strategy_scope VARCHAR(64),
    regime_scope VARCHAR(64),
    symbol_scope VARCHAR(32),
    experiment_tags TEXT[] NOT NULL DEFAULT '{}',
    release_tag VARCHAR(128),
    rationale TEXT,
    hypothesis TEXT,
    risk_level VARCHAR(24),
    rollback_condition TEXT,
    actor VARCHAR(128),
    git_commit_sha VARCHAR(64),
    change_kind VARCHAR(24) NOT NULL DEFAULT 'leaf',
    learning_change_id UUID REFERENCES learning_change_records(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_change_events_version ON config_change_events (config_version_id, detected_at DESC);
CREATE INDEX IF NOT EXISTS idx_change_events_category ON config_change_events (change_category);

-- ---------------------------------------------------------------------------
-- Releases & experiments (human layer)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS config_releases (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    slug VARCHAR(128) NOT NULL,
    title TEXT NOT NULL,
    notes TEXT,
    hypothesis TEXT,
    success_criteria TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'draft',
    canary_metadata JSONB NOT NULL DEFAULT '{}'::jsonb,
    CONSTRAINT uq_config_releases_slug UNIQUE (slug)
);

CREATE TABLE IF NOT EXISTS config_release_versions (
    release_id UUID NOT NULL REFERENCES config_releases(id) ON DELETE CASCADE,
    config_version_id UUID NOT NULL REFERENCES config_versions(id) ON DELETE CASCADE,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (release_id, config_version_id)
);

CREATE TABLE IF NOT EXISTS config_experiments (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    slug VARCHAR(128) NOT NULL,
    title TEXT NOT NULL,
    tags TEXT[] NOT NULL DEFAULT '{}',
    notes TEXT,
    hypothesis TEXT,
    success_criteria TEXT,
    status VARCHAR(32) NOT NULL DEFAULT 'inactive',
    activation_window TSTZRANGE,
    CONSTRAINT uq_config_experiments_slug UNIQUE (slug)
);

CREATE TABLE IF NOT EXISTS config_experiment_versions (
    experiment_id UUID NOT NULL REFERENCES config_experiments(id) ON DELETE CASCADE,
    config_version_id UUID NOT NULL REFERENCES config_versions(id) ON DELETE CASCADE,
    linked_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (experiment_id, config_version_id)
);

-- ---------------------------------------------------------------------------
-- Trade attribution sidecar (1:1 with decision row)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS trade_attribution (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    trade_table VARCHAR(16) NOT NULL CHECK (trade_table IN ('acevault', 'track_a')),
    trade_id UUID NOT NULL,
    entry_config_version_id UUID REFERENCES config_versions(id) ON DELETE SET NULL,
    exit_config_version_id UUID REFERENCES config_versions(id) ON DELETE SET NULL,
    entry_experiment_tags TEXT[] NOT NULL DEFAULT '{}',
    exit_experiment_tags TEXT[] NOT NULL DEFAULT '{}',
    entry_release_tag VARCHAR(128),
    exit_release_tag VARCHAR(128),
    attribution_tier VARCHAR(16) NOT NULL DEFAULT 'exact'
        CHECK (attribution_tier IN ('exact', 'inferred', 'unknown')),
    cohorts JSONB NOT NULL DEFAULT '{}'::jsonb,
    inference_notes TEXT,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    CONSTRAINT uq_trade_attribution_row UNIQUE (trade_table, trade_id)
);

CREATE INDEX IF NOT EXISTS idx_trade_attribution_entry_version
    ON trade_attribution (entry_config_version_id);
CREATE INDEX IF NOT EXISTS idx_trade_attribution_exit_version
    ON trade_attribution (exit_config_version_id);

CREATE TABLE IF NOT EXISTS attribution_backfill_audit (
    id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    trade_table VARCHAR(16) NOT NULL,
    trade_id UUID NOT NULL,
    tier VARCHAR(16) NOT NULL,
    actor VARCHAR(128),
    details JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_attr_backfill_trade ON attribution_backfill_audit (trade_table, trade_id);

-- ---------------------------------------------------------------------------
-- Additive columns on existing trade tables
-- ---------------------------------------------------------------------------
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS entry_config_version_id UUID REFERENCES config_versions(id) ON DELETE SET NULL;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS exit_config_version_id UUID REFERENCES config_versions(id) ON DELETE SET NULL;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS entry_config_hash CHAR(64);
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS exit_config_hash CHAR(64);
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS execution_context_entry VARCHAR(32);
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS execution_context_exit VARCHAR(32);
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS safety_position_multiplier_entry DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS safety_position_multiplier_exit DOUBLE PRECISION;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS venue VARCHAR(64);

ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS entry_config_version_id UUID REFERENCES config_versions(id) ON DELETE SET NULL;
ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS exit_config_version_id UUID REFERENCES config_versions(id) ON DELETE SET NULL;
ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS entry_config_hash CHAR(64);
ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS exit_config_hash CHAR(64);
ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS execution_context_entry VARCHAR(32);
ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS execution_context_exit VARCHAR(32);
ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS safety_position_multiplier_entry DOUBLE PRECISION;
ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS safety_position_multiplier_exit DOUBLE PRECISION;
ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS venue VARCHAR(64);

CREATE INDEX IF NOT EXISTS idx_acevault_entry_cfg_version ON acevault_decisions (entry_config_version_id);
CREATE INDEX IF NOT EXISTS idx_acevault_exit_cfg_version ON acevault_decisions (exit_config_version_id);
CREATE INDEX IF NOT EXISTS idx_strategy_entry_cfg_version ON strategy_decisions (entry_config_version_id);
CREATE INDEX IF NOT EXISTS idx_strategy_exit_cfg_version ON strategy_decisions (exit_config_version_id);

-- ---------------------------------------------------------------------------
-- Analytics views (net PnL aligned with DecisionJournal.get_regime_stats)
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW v_trade_attribution_enriched AS
SELECT
    d.id AS trade_id,
    'acevault'::text AS trade_table,
    d.coin,
    d.regime,
    d.regime_at_close,
    d.exit_reason,
    d.signal_source,
    d.created_at AS entry_at,
    d.outcome_recorded_at,
    d.pnl_usd,
    d.net_pnl_usd,
    d.gross_pnl_usd,
    d.fee_paid_usd,
    d.realized_r_multiple,
    d.peak_r_multiple,
    d.peak_r_capture_ratio,
    d.hold_duration_seconds,
    d.entry_config_version_id,
    d.exit_config_version_id,
    d.entry_config_hash,
    d.exit_config_hash,
    d.execution_context_entry,
    d.execution_context_exit,
    d.safety_position_multiplier_entry,
    d.safety_position_multiplier_exit,
    d.venue,
    ta.entry_experiment_tags,
    ta.exit_experiment_tags,
    ta.entry_release_tag,
    ta.exit_release_tag,
    ta.attribution_tier,
    ta.cohorts,
    ev.config_hash AS entry_version_hash,
    ev.summary AS entry_version_summary
FROM acevault_decisions d
LEFT JOIN trade_attribution ta
    ON ta.trade_table = 'acevault' AND ta.trade_id = d.id
LEFT JOIN config_versions ev ON ev.id = d.entry_config_version_id
WHERE d.decision_type = 'entry';

CREATE OR REPLACE VIEW v_profitability_by_config_version AS
SELECT
    d.entry_config_version_id AS config_version_id,
    COUNT(*) FILTER (WHERE d.outcome_recorded_at IS NOT NULL)::bigint AS closed_trades,
    COUNT(*) FILTER (
        WHERE d.outcome_recorded_at IS NOT NULL
          AND COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0)) > 0
    )::bigint AS winning_trades,
    SUM(COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0))) AS sum_net_pnl_usd,
    AVG(COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0))) AS avg_net_pnl_usd,
    SUM(CASE WHEN COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0)) > 0
        THEN COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0)) ELSE 0 END) AS sum_wins_net,
    SUM(CASE WHEN COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0)) < 0
        THEN COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0)) ELSE 0 END) AS sum_losses_net,
    SUM(COALESCE(d.gross_pnl_usd, d.pnl_usd)) AS sum_gross_pnl_usd,
    AVG(d.realized_r_multiple) AS avg_realized_r,
    AVG(d.peak_r_capture_ratio) AS avg_peak_capture_ratio,
    SUM(COALESCE(d.fee_paid_usd, 0)) AS sum_fee_paid_usd,
    AVG(d.hold_duration_seconds::double precision) AS avg_hold_seconds
FROM acevault_decisions d
WHERE d.decision_type = 'entry'
GROUP BY d.entry_config_version_id;

CREATE OR REPLACE VIEW v_profitability_by_config_version_regime AS
SELECT
    d.entry_config_version_id AS config_version_id,
    d.regime,
    d.regime_at_close,
    COUNT(*) FILTER (WHERE d.outcome_recorded_at IS NOT NULL)::bigint AS closed_trades,
    SUM(COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0))) AS sum_net_pnl_usd,
    AVG(COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0))) AS avg_net_pnl_usd
FROM acevault_decisions d
WHERE d.decision_type = 'entry'
GROUP BY d.entry_config_version_id, d.regime, d.regime_at_close;

CREATE OR REPLACE VIEW v_profitability_by_change_category AS
SELECT
    d.entry_config_version_id AS config_version_id,
    cats.change_category,
    COUNT(*)::bigint AS closed_trades,
    SUM(COALESCE(d.net_pnl_usd, d.pnl_usd - COALESCE(d.fee_paid_usd, 0))) AS sum_net_pnl_usd
FROM acevault_decisions d
CROSS JOIN LATERAL (
    SELECT DISTINCT cce.change_category
    FROM config_change_events cce
    WHERE cce.config_version_id = d.entry_config_version_id
      AND cce.change_kind = 'leaf'
) cats
WHERE d.decision_type = 'entry'
  AND d.outcome_recorded_at IS NOT NULL
GROUP BY d.entry_config_version_id, cats.change_category;

CREATE OR REPLACE VIEW v_sample_sufficiency_config_version AS
SELECT
    v.config_version_id,
    v.closed_trades,
    CASE
        WHEN v.closed_trades IS NULL OR v.closed_trades < 20 THEN 'insufficient'
        WHEN v.closed_trades < 50 THEN 'caution'
        ELSE 'strong'
    END AS sample_band
FROM v_profitability_by_config_version v;
