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
- **asyncpg** for async queries; schema TBD in sprint.

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
