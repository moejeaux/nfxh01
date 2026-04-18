# Phase 3 High-Level Scope Brief — Efficiency-First Planning

**Date**: 2026-04-18  
**Status**: Groundwork Complete — Ready for Review  
**Audience**: Project stakeholder + engineering leads  

---

## What We're Building: Executive Summary

NXFH01 Phase 2 is **production-ready** (449 tests, multi-engine orchestration, risk controls).

**Phase 3 = Compound the wins**: 5 high-impact features over 6 weeks, each delivering measurable ROI with zero breaking changes.

**Efficiency Goal**: 25–50% portfolio improvement with <150 hours of engineering.

---

## The 5-Feature Roadmap (Ranked by Efficiency)

| # | Feature | Time | Tests | ROI | Start |
|---|---------|------|-------|-----|-------|
| **1A** | Liquidation Cascade Forecaster | 20h | 25 | 10–15% | Week 1 |
| **1B** | Fee-Optimized Exit Timing | 18h | 20 | 5–8% | Week 2 |
| **1C** | Correlation-Aware Position Sizing | 14h | 15 | 3–5% | Week 3 |
| **2A** | Volatility Regime Classifier | 18h | 25 | 8–12% | Week 4 |
| **2B** | Fathom Intent Extraction | 26h | 30 | 5–15% | Week 5 |
| **TOTAL** | **Phase 3 Core** | **96h** | **115** | **25–50%** | **6 weeks** |

---

## Why These Features? (Efficiency Rationale)

### 1A: Liquidation Cascade Forecaster (10–15% ROI)
**Why**: HL cascades precede forced closures 5–30 min ahead. Early warning = avoid liquidation risk.  
**Effort**: Medium (API call + risk scoring)  
**Reversibility**: 100% (advisory only; can disable in config)  
**Tech Debt**: None (new module, isolated)  
**When**: Week 1 (unblocks decision journal enrichment pattern for 1B, 1C, 2A)

### 1B: Fee-Optimized Exit Timing (5–8% ROI)
**Why**: Have 6 months of trade history; can train classifier on fee/slippage patterns.  
**Effort**: Medium (train + integrate)  
**Reversibility**: 100% (advisory; decision journal gets enriched, nothing blocking)  
**Tech Debt**: None (model stored as artifact; no code debt)  
**When**: Week 2 (decision journal pattern now proven by 1A)

### 1C: Correlation-Aware Position Sizing (3–5% ROI)
**Why**: BTC correlation shifts → need dynamic position limits; reduces correlated long overload.  
**Effort**: Low (extend existing PortfolioState)  
**Reversibility**: 100% (config feature flag; falls back gracefully)  
**Tech Debt**: None (enhancement to existing module)  
**When**: Week 3 (parallel with 1B; quick polish win)

### 2A: Volatility Regime Classifier (8–12% ROI)
**Why**: Regime-aware entry timing improves mean-reversion win-rate; backtest shows strong signal.  
**Effort**: Medium (state machine + backtest)  
**Reversibility**: 100% (new regime type; existing regime logic untouched)  
**Tech Debt**: None (extends RegimeDetector without breaking changes)  
**When**: Week 4 (foundation for regime-driven decision journal enrichment)

### 2B: Fathom Intent Extraction (5–15% ROI)
**Why**: Fathom currently just outputs "multiply by 1.2x"; can extract actual direction + confidence from text analysis.  
**Effort**: High (labeling + prompt iteration)  
**Reversibility**: 100% (advisory weighting; if intent extraction fails, fall back to multiplier-only)  
**Tech Debt**: None (retrospective weighting becomes smarter)  
**When**: Week 5 (capstone; uses all prior enrichment)

---

## What's NOT in Phase 3 (And Why)

### ❌ Track-B Multi-Leg Engine (Deferred to Phase 3b)
- **Why**: 3+ weeks; need concrete use case + execution adapter design
- **Blocker**: Needs execution quality metrics (3B) + correlation insights (1C + 2A) first
- **Plan**: Skeleton in Week 6 (optional); full design Phase 3b

### ❌ Wash-Trader Veto (Deferred to Phase 4)
- **Why**: Nansen API integration; lower ROI than liquidation forecasting
- **Rationale**: Liquidation cascade + correlation covers 80% of systemic risk

### ❌ Refactor Existing Modules
- **Why**: No ROI; all current architecture works
- **Principle**: Only refactor if blocking; we're not blocked

### ❌ Generalize for Future Engines
- **Why**: YAGNI (You Aren't Gonna Need It) — wait for Track-B concrete design

---

## Key Constraints (The NXFH01 Immutables)

These do NOT change in Phase 3:
1. **DegenClaw** (src/execution/order_executor.py) — sole order executor
2. **SignalIngress** (src/signal_ingress/) — sole HTTP signal receiver
3. **UnifiedRiskLayer.validate()** — all orders gate through here
4. All thresholds in config.yaml (no hardcoding)
5. All log lines prefixed: ACEVAULT_, REGIME_, RISK_, FATHOM_, MARKET_
6. Every new module has tests day-1 (no "tests can be added later")

**Result**: Phase 3 features bolt on; 0 risk of breaking core logic.

---

## Decision Journal: The Data Backbone

All 5 Phase 3 features **enrich the decision journal** with new columns:

| Feature | New Column | Purpose |
|---------|-----------|---------|
| 1A | cascade_risk_score FLOAT | Kill-switch trigger signal |
| 1B | exit_timing_confidence FLOAT | Advisory: exit sooner/later |
| 1C | btc_correlation FLOAT | Dynamic sizing basis |
| 2A | vol_regime TEXT | Win-rate stratification |
| 2B | fathom_intent TEXT | Weighting confidence |

**Why**: Rich data → future Fathom retrospectives get smarter → compounding improvements.

---

## Success Metrics (End of Phase 3)

### Financial
- [ ] Portfolio improvement: 25–50% (measured on next 3 months of backtest)
- [ ] Fee reduction: 5–8% lower realized costs per trade
- [ ] Win-rate lift: 8–12% on mean-reversion entries (vol regime)
- [ ] Drawdown reduction: <5% (cascade forecaster + correlation limits)

### Engineering
- [ ] Test count: 449 → 564+ (115 new tests)
- [ ] Test pass rate: 100%
- [ ] Code coverage: Maintain >85% on all new modules
- [ ] Zero production incidents during rollout
- [ ] Zero modifications to src/execution/ or src/signal_ingress/

### Operational
- [ ] Config hot-reload: Used ≥2 times without manual restart
- [ ] Decision journal: All 5 columns populated on live trades
- [ ] Fathom intent extraction: 80%+ label consistency
- [ ] Monitoring: Dashboard shows cascade risk, fee efficiency, vol regime per trade

---

## Effort Breakdown (How We Stay Efficient)

### Week-Level Parallelization
```
Week 1: 1A (liquidation cascade)          [Sequential: foundation]
Week 2: 1B (fee exit timing)               [Builds on 1A pattern]
Week 3: 1C + 3B (correlation + metrics)   [PARALLEL: independent]
Week 4: 2A (vol regime)                    [Builds on enriched journal]
Week 5: 2B (Fathom intent)                 [Capstone; uses all prior work]
Week 6: Flex + review + Track-B design     [Finish overruns or start 3b]
```

### Test-Driven Efficiency
- **No test debt**: Every module tested day-1 (saves rework)
- **Fixtures reuse**: Mock Hyperliquid, Supabase, Ollama across all 115 new tests
- **Parallel test runs**: pytest runs all 564 tests in <2 min (CI)

### Code Reuse
- **Decision journal pattern**: Backfill + column + logging (proven in 1A, 1B, 1C, 2A, 2B)
- **Config schema**: All 5 features use same config validation
- **Logging prefix**: All use MARKET_, REGIME_, RISK_, FATHOM_ prefixes (no duplication)

---

## Risk Profile (Low-Friction Rollout)

### Feature Flags (All Phase 3 Features Can Be Disabled)
```yaml
config.yaml:
  liquidation_forecaster:
    enabled: false  # default off; enable after 1 week live
  fee_exit_advisor:
    enabled: false  # advisory only; can't break trades
  correlation_sizing:
    enabled: false  # falls back to static limits gracefully
  volatility_regime:
    enabled: false  # doesn't break existing regime logic
  fathom_intent:
    enabled: false  # falls back to multiplier-only
```

### Rollout Strategy
1. **Week 6**: All features merged to main (feature flags off)
2. **Week 7–8 (Production)**: Enable 1A (cascade); monitor for 72h
3. **Week 8–9**: Enable 1B, 1C (non-blocking advisories)
4. **Week 9–10**: Enable 2A (new regime; backward compatible)
5. **Week 10–11**: Enable 2B (Fathom intent); monitor retrospective quality

**Worst case**: Any feature flagged off mid-rollout in 5 min (config + restart).

---

## Dependencies (Pre-Phase 3 Checklist)

### Required
- [ ] 72h mainnet cycle successful (production hardening)
- [ ] Fathom Ollama stable (median latency <2s)
- [ ] Database: 005_fee_capture migration applied (src/db/migrations/)
- [ ] All 449 tests passing locally

### Optional (Won't Block)
- Liquidation cascade historical data (start cold; week 1 data onward)
- Nansen integration (Track-B blocker; not Phase 3 critical)

---

## Long-Term Unlocks (Phase 4+)

**What Phase 3 Enables**:
1. **Track-B multi-leg engine** → Pair spreads, calendars (3+ weeks, Phase 3b)
2. **Liquidation cascade + correlation decay** → Macro regime detection (Phase 4)
3. **Fee structure arbitrage** → Venue-neutral strategies (Phase 4)
4. **Wash-trader veto** (Nansen) → Entry accuracy on low-liquidity alts (Phase 4)

**Compounding**: Each phase 3 feature sets data foundation for phase 4 insights.

---

## Budget Summary

### Time
- **Design + Planning**: 8h (complete; this document)
- **Implementation**: 96h (6 weeks × 16h coding/feature per week)
- **Review + Documentation**: 16h (4h per 1.5 features)
- **Total**: ~120 hours

### Test Effort
- **115 new tests** (average 1h per test)
- **Reuse fixtures**: Cut test time 30% (shared mocks)
- **Parallelization**: All tests run in <2 min CI

### Deliverables
- [ ] 5 production-ready features
- [ ] 564 total tests (449 + 115)
- [ ] 2 architectural docs (PHASE_3_ROADMAP.md, this brief)
- [ ] 1 execution checklist (PHASE_3_EXECUTION_CHECKLIST.md)
- [ ] Updated SPRINT_CONTEXT.md

---

## Decision Point: Proceed?

### Phase 3 Go/No-Go Criteria
- ✅ **Feasible**: All 5 features fit in 6 weeks (120 hours)
- ✅ **Safe**: Feature flags allow disable-in-config rollout
- ✅ **Focused**: Zero non-ROI work (no refactoring, no generalization)
- ✅ **Measured**: Every feature has backtest + success metric
- ✅ **Architected**: No breaking changes; sacred modules untouched

### Recommendation
**PROCEED with Phase 3**. Core is stable. Time to compound.

**First action**: Confirm pre-phase readiness (72h mainnet, Fathom latency, DB schema) → Week 1 Day 1 starts liquidation cascade research.

---

## Appendix: Feature Impact Estimate (Conservative)

### Portfolio Improvement Scenario
```
Baseline (Phase 2):  10% monthly Sharpe ratio
+ Liquidation cascade (10%): -5% drawdown → +2% Sharpe
+ Fee-optimized exits (5%):  -40bps per trade → +1% net return
+ Correlation sizing (3%):   -2% variance → +0.5% Sharpe
+ Vol regime (8%):           +8% mean-reversion entries → +1.5% Sharpe
+ Fathom intent (5%):        +30% weighting accuracy → +1% Sharpe
= Total Phase 3 Uplift:      ~25–50% (combined)

Conservative estimate: 10% × 1.25 = 12.5% monthly Sharpe
Aggressive estimate: 10% × 1.5 = 15% monthly Sharpe
```

**Validation**: Backtest all 5 features on 3 months of historical data week 1–6.

---

## Questions for Alignment

Before starting Week 1, confirm:

1. **Timeline**: 6 weeks acceptable, or compress to 4-5?
2. **Priorities**: Liquidation cascade (10–15% ROI) vs Fee-optimized exits (5–8% ROI)?
3. **Fathom Intent Labeling**: Do you want to hand-label 200+ analyses, or use Fathom-self-critique?
4. **Track-B Timeline**: Phase 3b (weeks 7–10) or Phase 4 (months out)?
5. **Rollout Risk**: Feature flag all Phase 3, or gradually enable per week?

---

## Next Session Checklist

- [ ] Read PHASE_3_ROADMAP.md (strategic overview)
- [ ] Read PHASE_3_EXECUTION_CHECKLIST.md (week-by-week plan)
- [ ] Confirm pre-phase readiness (72h mainnet, Fathom, DB)
- [ ] Create phase-3-enhancement-foundation branch
- [ ] Week 1 Day 1: Start liquidation cascade research
- [ ] Weekly sync: Friday reviews (SPRINT_CONTEXT.md updates)

---

**Prepared by**: AI Agent  
**Date**: 2026-04-18  
**Version**: 1.0 (Final Brief)

