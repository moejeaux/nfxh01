# NXFH01 preflight checklist

Use before enabling each rollout phase (`docs/nxfh01-production-rollout.md`). Check **Applicable?** for your phase; leave N/A rows blank.

## 1. Config sanity

| Check | Applicable? | Pass? |
|-------|---------------|-------|
| Single authoritative `config.yaml` (or explicit override path) loaded by runtime / scripts | All | |
| YAML parses; no duplicate keys at merge sites | All | |
| **`orchestration.track_a_execution_enabled`** — intentional for this environment (live vs dry verification) | Exec phases | |
| **`universe.enabled`**, `top_n`, `block_new_entries_outside_universe` — match rollout intent | Exec phases | |
| **`engines.*.loss_pct` / `cooldown_hours`** — known and acceptable | All | |
| **`intelligence.enabled`**, **`intelligence.weekly_summary.enabled`**, **`intelligence.topk_review.enabled`** — match phase (2)(3) | Phases 2–3 | |
| **`intelligence.model`** / **`intelligence.timeout_seconds`** — match Ollama or remote endpoint | Phases 2–3 | |
| **`opportunity.enabled`** / **`opportunity.shadow_mode`** — match phase (4)(5) | Phases 4–5 | |
| **`fathom.enabled`**, **`fathom.entry_timeout_seconds`** (and related) — understood as **execution-affecting** where used | If Fathom on path | |
| **`acevault.stop_loss_distance_pct`** — within your hard ceiling policy (repo documents immutability after entry) | Exec | |

## 2. Paths and directories

| Check | Pass? |
|-------|-------|
| `research.data_dir` exists and is writable (candidate/outcome logs) | |
| Weekly review archive dir exists and contains at least one archive file (default `data/research/hl_meta_archive`; seed with `python scripts/append_hl_meta_archive.py` or your own exports) | |
| Weekly review output dir writable (default `data/research/weekly_review_out`, created automatically); if using baseline merge, `baseline_metrics.json` dropped **before** run when desired | |
| `calibration` / DB URLs (if used) reachable from this host | |

## 3. Model availability (LLM phases)

| Check | Pass? |
|-------|-------|
| `OLLAMA_BASE_URL` or `FATHOM_API_ENDPOINT` set if not default `127.0.0.1:11434` | |
| `curl`/browser or `httpx` to `<base>/api/tags` succeeds | |
| Model tag in `intelligence.model` (and `fathom` models if used) appears in tags list | |
| Host CPU/RAM headroom for `fathom-r1-14b` / configured model acceptable | |

## 4. Artifact write permissions

| Check | Pass? |
|-------|-------|
| Process user can write under `research.data_dir` | |
| Weekly review output dir writable | |
| Top-K / weekly summary artifact dirs (under engine or review output paths) writable when those features are on | |

## 5. Risk controls enabled

| Check | Pass? |
|-------|-------|
| **`UnifiedRiskLayer`** in use on execution path (no bypass in custom scripts) | |
| Kill switch object wired in runtime (`KillSwitch` from `src/risk/engine_killswitch.py`) | |
| Operators know how loss accumulation trips `engines.<id>.loss_pct` and cooldown | |
| BTC / universe policy files (e.g. `config/btc_context_policy.yaml`) present if your deployment expects them | |

## 6. Kill switch — known procedure

| Check | Pass? |
|-------|-------|
| Documented manual reset procedure (`KillSwitch.reset`) and who may invoke it | |
| Monitoring for `KILLSWITCH_TRIPPED`, `KILLSWITCH_RESET`, `KILLSWITCH_LOSS_RECORDED` | |
| Engine IDs match config (`acevault`, `growi`, `mc`, …) | |

## 7. Test commands (representative)

Run from repository root (adjust Python if needed).

| Command | When |
|---------|------|
| `python -m pytest tests/test_weekly_review_runner.py -q` | After weekly review / baseline changes |
| `python -m pytest tests/test_intelligence_weekly_summary.py tests/test_intelligence_topk_review.py -q` | Phases 2–3 |
| `python -m pytest tests/test_opportunity_ranker.py tests/test_opportunity_ordering.py -q` | Phases 4–5 |
| `python -m pytest src/engines/acevault/tests/test_engine.py -q` | AceVault changes / pre-production |
| `python -m pytest src/risk/tests/test_unified_risk.py -q` | Risk / kill switch |

**Smoke (live HL — use only with explicit ops approval):** `python scripts/verify_nxfh01_production.py` (mainnet verification script in repo).

## 8. Logs to watch

| Prefix / pattern | Meaning |
|------------------|---------|
| `RISK_` / `RISK_REJECTED` / `RISK_APPROVED` | Portfolio gate (`src/risk/unified_risk.py`) |
| `ACEVAULT_` | Engine / opportunity drops / scan |
| `KILLSWITCH_` | Engine kill switch |
| `INTELLIGENCE_WEEKLY_SUMMARY_FAILED` | Weekly summary LLM failure path |
| Top-K / intelligence logger lines | Advisory review lifecycle |

Do **not** treat `src/nxfh01/risk/unified_risk_layer.py` logs as a replacement for `RISK_` portfolio gate logs—they validate **intent shape**, not full portfolio state.

## 9. Sign-off

| Role | Name | Date | Phase ready |
|------|------|------|---------------|
| Operator | | | |
| Technical | | | |
