# NXFH01 System Context

## Project Overview
**nxfh01** is a production trading agent for Hyperliquid perpetuals DEX, built on Python 3.11+ with async execution, deterministic risk gating, and advisory-only LLM integration.

## Core Architecture

### Multi-Engine Design
- **Engine #1 (AceVault):** Immutable 0.3% stop-loss strategy.
- **Engines #2–5:** Reserved for future strategies (RegimeDetector, etc.).
- All engines route through `UnifiedRiskLayer.validate()` before `DegenClaw` order execution.

### Multi-strategy orchestration (`src/nxfh01/orchestration/`)
- **`StrategyOrchestrator`:** Global tick; per-strategy cadence from `config.yaml` (`strategies.*`, `orchestration.*`); failures isolated per strategy (`ORCH_STRATEGY_FAILED`).
- **Track B (AceVault):** `AceVaultEngine.run_cycle()` — full internal loop (scan → risk → Fathom → DegenClaw). Same-tick arbitration with other strategies is not applied inside this path unless AceVault is later split into propose vs execute.
- **Track A (Growi HF, MC Recovery, future):** Strategies return `NormalizedEntryIntent` items; orchestrator runs `ConflictPolicy` → `TrackAExecutor` → `UnifiedRiskLayer.validate` → DegenClaw → `PortfolioState.register_position`. Toggle with `orchestration.track_a_execution_enabled`.
- **Config validation:** `validate_multi_strategy_config()` at startup (`engine_id` must match `engines:` keys, no duplicate `execution_order`, etc.).
- **Track A DB:** `strategy_decisions` table + `DecisionJournal.log_track_a_entry` (migration `002_strategy_decisions.sql`).  
- **HL reconciliation:** `PortfolioState.reconcile_open_positions_vs_hl` logs `RISK_RECONCILE_*`; optional startup hooks `orchestration.hl_sync_on_startup` / `hl_reconcile_on_startup` (requires `HL_WALLET_ADDRESS`). `sync_from_hl` uses `_user_state_compat` for SDK vs mock clients.  
- **Backlog:** Optional AceVault propose/execute split for unified same-tick conflict with Track A.

### Key Components

#### 1. **Execution Layer** (`src/execution/`)
- `DegenClaw` (order executor): sole interface to Hyperliquid mainnet.
- **Sacred:** Never modify without explicit approval.

#### 2. **Signal Ingress** (`src/signals/`; legacy rule may say `src/signal_ingress/`)
- HTTP / mapping pipeline for external trade signals (separate from engine loops).
- **Sacred:** Never modify without explicit approval.

#### 3. **Risk Layer** (`src/risk/` — canonical `UnifiedRiskLayer`)
- `UnifiedRiskLayer.validate()`: mandatory gate for all orders.
- Enforces: no bypass flags, AceVault stop immutability (0.3% canonical), config-driven thresholds.

#### 4. **Positions** (`src/nxfh01/positions/`)
- `AceVaultStop`: frozen dataclass; immutable after entry.
- Canonical stop calculated from `entry_px`, `is_long`, and `config.yaml`.

#### 5. **Advisory** (`src/nxfh01/advisory/`)
- `FathomAdvisory`: wraps Ollama LLM (Fathom-r1-14b) for sizing recommendations.
- Non-blocking: times out in 15s, falls back to deterministic defaults.
- Cannot block trades, widen stops, or change direction.

#### 6. **Logging** (`src/nxfh01/logging/`)
- Structured event prefixes: `RISK_`, `ACEVAULT_`, `REGIME_`, `ORCH_`, `ORCH_TRACK_A_`, strategy-specific (e.g. `GROWI_HF_`, `MC_RECOVERY_`), `KILLSWITCH_*`.
- No silent failures; every state change emitted.

### Interface Contracts (Never Change)
```python
RegimeDetector.detect(market_data: dict) -> RegimeState
UnifiedRiskLayer.validate(signal, engine_id) -> RiskDecision
AceVaultEngine.run_cycle() -> List[AceExit | AceSignal]
DecisionJournal.log_entry(signal) -> UUID
KillSwitch.is_active(engine_id) -> bool
```

### Config (`config.yaml`)
All numeric thresholds live here; never hardcode:
- `acevault.stop_loss_distance_pct = 0.3`
- `fathom.timeout_seconds = 15`
- `fathom.acevault_max_mult = 1.5`
- `fathom.majors_max_mult = 2.0`

### Runtime config hot-reload (learning applier)
Hot-reload is **intentionally limited** to **patch-style, in-place updates** of the **shared live config root** (`merge_into_live_config` in `src/nxfh01/config_reload.py`). It is **safe** for config domains whose consumers read from that shared structure on each use or hold **aliased nested dicts** (engines, `UnifiedRiskLayer`, `KillSwitch`, shallow-copied executor roots, etc.).

It is **not** full runtime reconfiguration for components that **cache scalar values at initialization** (e.g. some fields on `FathomAdvisor`). Those remain **restart-bound** unless given an **explicit refresh hook** later.

In practice, retrospective-approved changes to **reload-safe** domains (`learning.*`, `acevault` / Growi / MC fields touched by the applier) can take effect **without a restart** when `learning.reload_runtime_config_after_apply` is true. **Execution-critical** or other components that cache at startup still rely on **restart** or a **future explicit refresh hook**.

## Non-Negotiable Rules

1. **Never modify `src/execution/`** unless explicitly instructed.
2. **Never modify `src/signal_ingress/`** unless explicitly instructed.
3. **All thresholds in `config.yaml`** — no hardcoded numbers in source.
4. **All log lines must have prefixes:** `ACEVAULT_`, `REGIME_`, `RISK_`.
5. **Every new module must have pytest tests** — "tests can be added later" is not acceptable.
6. **`UnifiedRiskLayer.validate()` gates every order** — no bypass flags, no fast paths.
7. **Fathom calls always have 15s timeout** with deterministic fallback.
8. **No mocks, stubs, or paper trading.** This is a production system.

## Database
- **Postgres via Supabase:** connection string in `DATABASE_URL` env var.
- **asyncpg** for async queries; schema in [`src/db/schema.sql`](src/db/schema.sql), migrations in [`src/db/migrations/`](src/db/migrations/).

## Six-hour Fathom retrospective (not the main agent)

The 14b window review runs as a **separate** process. Restarting the trading agent does **not** start this; schedule it or run it manually.

**Config:** [`config.yaml`](config.yaml) → `fathom_retrospective` (`deep_model`, `lookback_hours`, `timeout_seconds`, etc.).

**Optional Telegram synopsis:** set `fathom_retrospective.telegram_notify: true` and provide `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID` in the environment (same as any other bot usage). After each successful run, a short message (summary + bullets + `run_id`) is sent; failures to send do not fail the job.

**Manual run (from repo root, venv active):**
```bash
cd ~/projects/nfxh01   # adjust to your clone path
source .venv/bin/activate
python -m src.fathom.retrospective
```
Requires `DATABASE_URL` (and optionally `OLLAMA_BASE_URL`) in the environment—e.g. `export` after `source .env` if you use a dotenv file.

**macOS cron (every six hours, example):** `crontab -e` and add one line. Replace `YOUR_USER`, repo path, venv path, and paste your real `DATABASE_URL` (cron does not load `.env` automatically).

```cron
0 */6 * * * cd /Users/YOUR_USER/projects/nfxh01 && /Users/YOUR_USER/projects/nfxh01/.venv/bin/python -m src.fathom.retrospective >> /Users/YOUR_USER/logs/fathom_retro.log 2>&1
```

If you prefer not to embed secrets in crontab, use a two-line wrapper script that `source`s `.env` then runs the same `python -m` command, and point cron at that script.

## Testing
- **pytest** with `@pytest.mark.live` for real Hyperliquid mainnet tests.
- Default: run without `live` marker (`pytest -m "not live"`).
- Run live deliberately: `pytest -m live`.

### Production validation (staged — do not skip gates)
Treat **mainnet** as irreversible money at risk. Use **small size**, **explicit env** (see [`.env.example`](.env.example)), and **`HL_TESTNET=false`** only when you intend mainnet reads.

| Stage | Goal | Actions |
|--------|------|---------|
| **1. CI / local** | Regressions caught before deploy | `pytest -m "not live"` (full suite green). Install deps from `pyproject.toml` (e.g. `ruamel.yaml`). |
| **2. Connectivity** | HL + DB + Ollama reachable from the **same host** that will run the agent | Set `DATABASE_URL`, `HL_WALLET_ADDRESS`, `OLLAMA_BASE_URL`. Confirm Postgres migration applied (`learning_change_records`, etc.). |
| **3. Read-only / no orders** | Market data, regime, scanner, risk globals without execution | Run [`scripts/verify_nxfh01_production.py`](scripts/verify_nxfh01_production.py) on the target machine. It asserts `HL_TESTNET` is not true and exercises HL **Info** + one AceVault-style cycle without DegenClaw. Fix any import/signature drift vs current `AceVaultEngine` before relying on it. |
| **4. Intelligence loop (optional)** | Retrospective + registry + hot-reload path | With `DATABASE_URL`: `python scripts/retro_now.py` (one retro run); `python scripts/evaluate_change.py` (pending learning evaluations). Watch logs for `RETRO_*`, `LEARN_*`, `RISK_CONFIG_HOT_RELOAD`. |
| **5. Supervised live** | Full orchestrator with **human watching** first session | Run the real entry point (`nxfh01` / `src/nxfh01/main.py`). Confirm `RISK_*`, `ACEVAULT_*`, `REGIME_*`, `ORCH_*` lines; verify kill switch and risk gates behave. Start with **minimal** strategy exposure in `config.yaml` if possible. |
| **6. Soak** | Stability over hours | Log rotation, memory, DB connections, no silent task death. Re-run stage 3 or health checks after deploy. |

**Push for quality:** block releases on **stage 1** in CI; require **stage 3** on the **production host** before enabling unattended trading; keep **stage 5** short and staffed until metrics look normal.

## Hardware & LLM
- **Hardware:** Mac Mini M4, 16GB RAM.
- **LLM:** Fathom-r1-14b via Ollama (local, advisory only, never blocks execution).

## Entry Point
```bash
nxfh01
```
Runs `src/nxfh01/main.py::main()` async entry point.

- For additive upgrade sessions, modify only the files explicitly listed in the prompt
- Never rename existing classes, methods, fields, config keys, or log lines unless the prompt explicitly says to
- If the prompt says additive changes only, preserve existing behavior and append logic rather than rewriting working code
- Do not refactor unrelated code
- Do not create new dependencies unless explicitly requested
- When API response structure is uncertain, use deterministic fallback behavior from config
- Return only code changes for the requested files
- If tests fail, fix the minimum code required to satisfy the existing prompt and tests