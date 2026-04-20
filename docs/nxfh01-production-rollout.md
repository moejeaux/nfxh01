# NXFH01 production rollout runbook

Staged path from **research-only verification** to **controlled production** use of optional pipelines. All toggles live in **`config.yaml`** unless noted (CLI flags for weekly review only).

## Advisory vs execution-affecting

| Area | Affects live orders / sizing? | Notes |
|------|--------------------------------|--------|
| Deterministic `weekly_review` + optional `baseline_metrics.json` merge | **No** | CLI / artifacts only (`src/research/weekly_review.py`). |
| Weekly LLM summary (`intelligence.weekly_summary`) | **No** | Requires `intelligence.enabled`; writes advisory markdown/JSON; failures fall back (`src/intelligence/weekly_summary.py`). |
| AceVault Top-K review (`intelligence.topk_review`) | **No** | `advisory_only` in config; bounded calls; artifacts (`src/engines/acevault/engine.py`, `src/intelligence/topk_review.py`). |
| Calibration `recompute_mode: recommend_only` | **No** | Recommendations only unless you change mode elsewhere. |
| **`fathom` block** (entry advisor, multipliers, timeouts) | **Yes** | Applied within configured bounds; system must behave if Ollama is down (bounded timeouts). |
| **`opportunity` with `shadow_mode: false`** | **Yes** | Ranker drives sort/drops/leverage proposal for eligible candidates (`opportunity_enforce_ranking` in `src/opportunity/config_helpers.py`). |
| **`orchestration.track_a_execution_enabled`** | **Yes** | Gates whether normalized intents execute. |
| **`universe.*`** | **Yes** | Gates new entries when enabled with risk integration. |

This runbook does **not** cover editing `src/execution/` or `src/signal_ingress/`.

---

## Phase 1 — Deterministic weekly review (+ baseline file only)

**Purpose:** Operator-grade ranker/calibration snapshot without LLM or opportunity changes.

**Flags / inputs**

- No `intelligence` or `opportunity` changes required.
- Optional: place `baseline_metrics.json` (JSON object) in the **same** `--output-dir` **before** running the review so it is merged into `weekly_review.md`.

**Preconditions**

- Archive path and `config.yaml` valid for `run_weekly_review` (see preflight doc).
- `research.data_dir` and outcome logs available if you want meaningful calibration sections.

**How to run**

```bash
python -m src.research.weekly_review --archive-dir <ARCHIVE> --output-dir <OUT> [--candles-dir ...] [--outcomes-dir ...] [--config path/to/config.yaml]
```

**Monitoring**

- Inspect `<OUT>/weekly_review.md`, `historical_summary.json`, `calibration_recommendations.json`.
- If baseline merge failed, `weekly_review.md` completeness / gaps will mention `baseline_metrics_merge:...`.

**Rollback trigger**

- Bad or misleading artifacts (wrong archive, wrong date range).

**Rollback action**

- Re-run with corrected paths/dates; remove or fix `baseline_metrics.json`.

**Promotion criteria**

- Artifacts complete; gaps explained; operator sign-off on data quality for the window.

---

## Phase 2 — Weekly LLM summary enabled

**Purpose:** Same deterministic artifacts, plus advisory narrative for operators (still non-authoritative).

**Flags (`config.yaml`)**

- `intelligence.enabled: true`
- `intelligence.weekly_summary.enabled: true` (must stay true for the runner to call the LLM path)
- Optional tuning: `intelligence.timeout_seconds`, `intelligence.model` (passed to `FathomClient`; default Ollama chat endpoint from `OLLAMA_BASE_URL` / `FATHOM_API_ENDPOINT`).

**CLI**

- After Phase 1 outputs exist in `<OUT>`, either re-run review with:

```bash
python -m src.research.weekly_review ... --output-dir <OUT> --with-llm-summary
```

…or run the weekly review once to regenerate JSON/MD, then invoke with `--with-llm-summary` on the same output directory as required by your ops flow.

**Preconditions**

- Phase 1 stable.
- Ollama (or configured endpoint) serves the model name in `intelligence.model`; endpoint reachable from the host running the job.

**Monitoring**

- `llm_weekly_summary.md` / `llm_weekly_summary.json` (paths printed by runner when enabled).
- Logs: `INTELLIGENCE_WEEKLY_SUMMARY_FAILED` warnings on model errors (fallback summary used when structured output required).

**Rollback trigger**

- Repeated timeouts, garbage summaries, or ops load too high on Ollama host.

**Rollback action**

- Set `intelligence.enabled: false` **or** `intelligence.weekly_summary.enabled: false`.
- Omit `--with-llm-summary` from scheduled jobs.

**Promotion criteria**

- Several consecutive weeks of useful, stable summaries; no operator confusion treating LLM text as auto-apply.

---

## Phase 3 — AceVault Top-K advisory enabled

**Purpose:** Bounded per-cycle commentary on top-ranked candidates; **does not** replace risk or execution.

**Flags (`config.yaml`)**

- `intelligence.enabled: true` (Top-K path checks this)
- `intelligence.topk_review.enabled: true`
- Leave `intelligence.topk_review.advisory_only: true` (present in repo; do not use for execution authority).
- Tune `intelligence.topk_review.invoke_when.*` and `top_k` only if you understand call volume (see `AceVaultEngine` rate limits).

**Preconditions**

- Phase 2 proven stable **or** ops accepts LLM load with `weekly_summary` still off (`weekly_summary.enabled: false` is allowed while `intelligence.enabled` is true—verify weekly job expectations).

**Monitoring**

- Top-K artifact paths / logs from AceVault cycle (background task); watch Ollama CPU/latency.
- Confirm no change in risk outcomes: `RISK_REJECTED` / `RISK_APPROVED` patterns unchanged vs pre-phase.

**Rollback trigger**

- Latency spikes in engine cycles, excessive LLM calls, or any operator belief that Top-K “approves” trades.

**Rollback action**

- Set `intelligence.topk_review.enabled: false`.

**Promotion criteria**

- Call volume within budget; artifacts reviewed; no regression in cycle time or error rate.

---

## Phase 4 — Opportunity ranker shadow mode

**Purpose:** Exercise `rank_opportunity` on live candidate flow **without** changing sort order vs legacy weakness ordering (shadow still sorts by weakness in `AceVaultEngine`).

**Flags (`config.yaml`)**

- `opportunity.enabled: true`
- `opportunity.shadow_mode: true`
- Keep `opportunity.require_valid_snapshot` aligned with your HL meta reliability (default `true` in repo).

**Preconditions**

- Meta/snapshot pipeline healthy; `research.save_candidate_logs` / outcomes paths usable if you rely on downstream calibration.

**Monitoring**

- Ranker log lines (`log_rank_line` with `shadow=True`); candidate logs under `research.data_dir`.
- Compare frequency of hard_rejects vs Phase 3; no surprise change in **entry count** yet (ordering unchanged).

**Rollback trigger**

- Log volume or DB/disk pressure; HL meta errors blocking scans if `require_valid_snapshot` is strict.

**Rollback action**

- Set `opportunity.enabled: false` (returns engines to pre-opportunity ranking path).

**Promotion criteria**

- Stable multi-day run; operators understand shadow metrics; disk and HL budgets OK.

---

## Phase 5 — Opportunity ranker conservative enforce mode

**Purpose:** Let shared ranker **affect** which candidates proceed and proposed leverage (enforce path), starting from **current** `opportunity.*` thresholds in `config.yaml` as the conservative baseline—tighten further only via existing keys (no separate “conservative” flag in repo).

**Flags (`config.yaml`)**

- `opportunity.enabled: true`
- `opportunity.shadow_mode: false`
- **Conservative posture (examples of existing knobs—tune only with discipline):**
  - `opportunity.final_score.min_submit_score` — higher = fewer submits.
  - `opportunity.hard_reject.*` — stricter liquidity/spread/funding gates.
  - `opportunity.leverage.*` / `opportunity.leverage.portfolio_caps` — lower caps.
  - `opportunity.emergency_universe.mode` — repo documents `strict_allowlist` when you need allowlist blocking at risk layer (`config.yaml` comment near `opportunity`).

**Preconditions**

- Phase 4 stable.
- `universe`, `risk`, and `orchestration.track_a_execution_enabled` reviewed with enforce on (comment in `config.yaml`: when opportunity is enabled and not shadow, Top-25 is not the sole gate—understand `emergency_universe`).

**Monitoring**

- `ACEVAULT_OPPORTUNITY_DROP` and related info logs; fill quality; drawdown vs prior phase.
- `UnifiedRiskLayer` outcomes unchanged in **authority** (still the final gate); watch for unintended starvation (no signals passing ranker).

**Rollback trigger**

- Elevated reject rate, unexpected leverage proposals, or PnL/regime mismatch vs shadow expectations.

**Rollback action**

- **Fast:** set `opportunity.shadow_mode: true` (restore shadow behavior) **or** `opportunity.enabled: false`.
- **Strong:** activate engine kill switch path (loss-based trip in `engines.<id>` thresholds) or use your operational kill procedure; `KillSwitch` logs `KILLSWITCH_TRIPPED` / `KILLSWITCH_RESET`.

**Promotion criteria**

- Risk metrics acceptable vs baseline; operators agree enforce thresholds are correct; documented config snapshot frozen for production.

---

## Cross-phase reminders

- **Kill switch:** Configured per engine under `engines.<engine_id>` (`loss_pct`, `cooldown_hours`). Trips are logged; `UnifiedRiskLayer.validate` rejects when active.
- **Execution path:** `orchestration.track_a_execution_enabled` remains the coarse switch for posting intents through risk + executor stack—keep explicit in change tickets.
- **Intent vs portfolio risk:** `src/nxfh01/risk/unified_risk_layer.py` (intent invariants) is **not** a substitute for `src/risk/unified_risk.py` (live portfolio gate).
