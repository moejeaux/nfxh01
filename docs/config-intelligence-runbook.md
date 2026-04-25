# Config intelligence — operator runbook

## Purpose

Registers merged runtime YAML (`config.yaml` + `config/` fragments) in Postgres, records leaf diffs when the canonical hash changes, and optionally stamps closed-trade rows with `entry_config_version_id` / `exit_config_version_id` (see `config_intelligence.stamp_trades` in `config.yaml`).

## Apply migration

Run after backups:

```bash
psql "$DATABASE_URL" -v ON_ERROR_STOP=1 -f src/db/migrations/008_config_intelligence.sql
```

## Feature flags (`config.yaml`)

| Key | Meaning |
|-----|---------|
| `config_intelligence.enabled` | Master switch for registry + DB writes on bootstrap / hot reload. |
| `config_intelligence.stamp_trades` | When `true`, AceVault and Track A journal writes include attribution columns + `trade_attribution` sidecar. Failures are logged only (`DECISION_JOURNAL_ATTRIBUTION_*`). |

## Environment variables

| Variable | Use |
|----------|-----|
| `NXFH01_ENV` | Overrides `config_intelligence.environment` (default `live`). |
| `NXFH01_EXECUTION_CONTEXT` | Optional explicit `live` / `shadow_pipeline` / `shadow_runner`. |
| `GIT_COMMIT_SHA` / `GITHUB_SHA` | Stored on new `config_versions` rows. |

## Analytics views

- `v_trade_attribution_enriched` — AceVault decisions joined to sidecar + version summary.
- `v_profitability_by_config_version` — Closed-trade aggregates by `entry_config_version_id` (net PnL uses the same `COALESCE(net_pnl_usd, pnl_usd - fee_paid_usd)` pattern as `DecisionJournal.get_regime_stats`).
- `v_profitability_by_config_version_regime` — Adds `regime` × `regime_at_close`.
- `v_profitability_by_change_category` — Net PnL by `change_category` (one row per trade per distinct category on its entry version).
- `v_sample_sufficiency_config_version` — Bands `insufficient` / `caution` / `strong` using fixed cutoffs 20 / 50 (align Python reads with `config_intelligence.sample_thresholds` for reporting).

## Python helpers

`src/config_intelligence/analytics.py` exposes `diff_config_versions`, `list_change_events`, `get_trade_attribution`, `get_pre_post_analysis` (asyncpg pool required).

## Backfill

`scripts/attribution_backfill.py` marks legacy closed AceVault rows with `trade_attribution.attribution_tier=unknown` (or `inferred`) without inventing config UUIDs. Run with `--limit` and review `attribution_backfill_audit`.

## Rollback

1. Set `config_intelligence.stamp_trades: false`.
2. Optionally `config_intelligence.enabled: false` to stop new `config_versions` rows.
3. Nullable columns — no destructive rollback required.

## Hot reload

After Fathom learning auto-apply merges YAML into the live dict, `register_after_hot_reload` runs on the shared journal pool so the in-process `ActiveConfigVersionHolder` matches Postgres.
