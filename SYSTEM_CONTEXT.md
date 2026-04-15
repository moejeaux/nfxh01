# NXFH01 System Context

## Project Overview
**nxfh01** is a production trading agent for Hyperliquid perpetuals DEX, built on Python 3.11+ with async execution, deterministic risk gating, and advisory-only LLM integration.

## Core Architecture

### Multi-Engine Design
- **Engine #1 (AceVault):** Immutable 0.3% stop-loss strategy.
- **Engines #2–5:** Reserved for future strategies (RegimeDetector, etc.).
- All engines route through `UnifiedRiskLayer.validate()` before `DegenClaw` order execution.

### Key Components

#### 1. **Execution Layer** (`src/execution/`)
- `DegenClaw` (order executor): sole interface to Hyperliquid mainnet.
- **Sacred:** Never modify without explicit approval.

#### 2. **Signal Ingress** (`src/signal_ingress/`)
- HTTP webhook receiver for external trade signals.
- **Sacred:** Never modify without explicit approval.

#### 3. **Risk Layer** (`src/nxfh01/risk/`)
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
- Structured event prefixes: `RISK_`, `ACEVAULT_`, `REGIME_`, `KILLSWITCH_ACTIVE`.
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

## Hardware & LLM
- **Hardware:** Mac Mini M4, 16GB RAM.
- **LLM:** Fathom-r1-14b via Ollama (local, advisory only, never blocks execution).

## Entry Point
```bash
nxfh01
```
Runs `src/nxfh01/main.py::main()` async entry point.
