# Enable Liquidation Cascade Forecaster (Phase 3.1a)

**Current Status**: Disabled (safe default) ✅

## Quick Start

### 1. Enable the Feature

**File**: `config.yaml`

```yaml
cascade_forecaster:
  enabled: true  # ← Change this from false to true
```

**That's it.** Next orchestrator tick will start assessment.

### 2. Verify It's Running

**Watch logs for**:
```
MARKET_CASCADE_FORECASTER_INITIALIZED
MARKET_CASCADE_ASSESSED score=0.234 level=low oi_delta_pct=-0.0001 ...
```

**Expected**: One `MARKET_CASCADE_ASSESSED` log per tick (every 15s by default).

### 3. No Action Needed

⚠️ **Currently advisory only** — forecaster does NOT:
- ❌ Block trades
- ❌ Trigger kill-switch
- ❌ Change position sizing
- ❌ Force exits

It just measures cascade risk and logs it.

## Configuration Tuning

### Risk Level Thresholds
**File**: `config.yaml` → `cascade_forecaster.thresholds`

```yaml
cascade_forecaster:
  thresholds:
    low: 0.15           # Score ≥ 15% → LOW
    elevated: 0.40      # Score ≥ 40% → ELEVATED
    high: 0.65          # Score ≥ 65% → HIGH
    critical: 0.85      # Score ≥ 85% → CRITICAL
```

### Signal Weights
**File**: `config.yaml` → `cascade_forecaster.weights`

Control what signals matter most:
```yaml
weights:
  oi_delta: 0.30        # Open Interest % change (most important)
  funding: 0.20         # Absolute funding rate
  premium: 0.15         # Mark-oracle divergence
  oi_cap: 0.10          # Assets at OI ceiling
  book_thin: 0.25       # Order book depth collapse
```

Reduce a weight to 0.0 to disable that signal entirely.

### Normalization (Signal Scaling)
**File**: `config.yaml` → `cascade_forecaster.normalization`

Adjust what counts as "extreme" for each signal:
```yaml
normalization:
  oi_delta_extreme_pct: 0.05      # 5% OI drop = max score for that signal
  funding_extreme: 0.001           # 0.1% funding = max
  premium_extreme: 0.005           # 0.5% premium = max
  oi_cap_extreme_count: 10         # 10 assets at cap = max
```

Lower = more sensitive. Raise = less sensitive.

## Monitoring

### Decision Journal Integration
The `cascade_risk_score` and `cascade_risk_level` columns will be populated every trade:

```sql
SELECT 
  coin, 
  entry_price, 
  cascade_risk_score,
  cascade_risk_level,
  pnl_usd
FROM strategy_decisions
WHERE cascade_risk_score > 0
ORDER BY created_at DESC
LIMIT 10;
```

### Expected Behavior

**Calm market**:
- Score: 0.0–0.15 (NONE/LOW)
- Funding: ~0.0001
- OI delta: ±0.1–0.5%
- Book: full depth

**Stress test** (coordinated liquidations):
- Score: 0.65+ (HIGH/CRITICAL)
- Funding: 0.001+ (10bps)
- OI delta: >5% drop
- Book: thinning 50%+

## Disable (Rollback)

If issues arise:

```yaml
cascade_forecaster:
  enabled: false  # ← Set to false
```

Restart. System behaves as if forecaster never ran (zero performance impact).

## Production Readiness Checklist

Before going live:
- [ ] Enable in config.yaml
- [ ] Run for 24h in calm market
- [ ] Verify logs appear every tick
- [ ] Check decision_journal has cascade_risk_score populated
- [ ] Monitor for unexpected high scores on calm days
- [ ] If tuning needed, adjust thresholds or weights
- [ ] Run full test suite: `pytest --tb=short -q`
- [ ] Commit config changes: `git add config.yaml; git commit -m "..."`

## Questions?

See:
- `PHASE_3_ROADMAP.md` — Strategic overview
- `src/market/cascade_forecaster.py` — Implementation details
- `tests/test_cascade_forecaster.py` — Test cases (show expected behavior)

---
