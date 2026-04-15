-- Apply to Supabase / Postgres before relying on post-exit analysis or six-hour retrospective.
-- Idempotent where supported.

ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS fathom_post_analysis TEXT;
ALTER TABLE acevault_decisions ADD COLUMN IF NOT EXISTS fathom_post_analysis_at TIMESTAMPTZ;

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
