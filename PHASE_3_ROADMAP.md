# NXFH01 Phase 3+ Strategic Roadmap

**Status**: Groundwork Planning Document  
**Current Sprint**: Phase 2b Complete (Regime-Conditioned Profitability Analysis)  
**Test Baseline**: 449 tests (target 180+ achieved)  
**Created**: 2026-04-18  

---

## Executive Summary

**Phase 2 is production-ready**. The system now has:
- ✅ Multi-engine orchestration (AceVault, Growi/MC, BTC Lanes, Recovered)
- ✅ Production verification script (mainnet-ready)
- ✅ Comprehensive risk controls (UnifiedRiskLayer, KillSwitch, Portfolio State)
- ✅ Retrospective learning loop (Fathom-powered)
- ✅ Peak-R capture + Fee-drag measurement + Regime-conditioned metrics
- ✅ 449 tests covering all major paths

**Phase 3+ must maximize ROI with ruthless efficiency**: High-leverage features only, zero low-impact work.

---

## High-Level Scope: Phase 3 Framework

### Efficiency Principle
Each Phase 3+ initiative must satisfy **both**:
1. **Measurable impact**: >5% expected portfolio improvement OR 50% operational cost reduction
2. **Leverage multiplier**: Compound with existing architecture without refactoring core modules

### The Three-Pillar Architecture (Immutable)
1. **Execution**: DegenClaw (order_executor.py) — sole order path
2. **Signals**: SignalIngress — sole HTTP receiver
3. **Risk**: UnifiedRiskLayer.validate() — all orders must gate through

Future engines plug into these without change.

---

## Discovery: Data Gaps & Opportunities

### What We Can Measure Now
- **Decision Journal**: Every trade (entry/exit, candles, regime, Fathom advice, PnL, peak-R, fees)
- **Retrospective Loop**: 6h cycle analyzing learned patterns, applying size multipliers
- **Market Context**: BTC regime (TRENDING_UP/DOWN, RISK_OFF, RANGING), funding, vol, correlation

### What's Missing (Phase 3+ Candidates)
1. **Correlation Decay Detector**: BTC→ALT correlation shifts → regime fragility signal
2. **Fee Structure Arbitrage**: Taker/maker timing, liquidity rebalancing
3. **Execution Quality Audits**: Slippage vs market impact vs realized vs mark
4. **Engine Cross-Talk**: Shared PnL attribution, order sequencing optimization
5. **Fathom Intent Parsing**: Extract confidence/direction from free-text analysis
6. **Liquidation Cascade Forecast**: HL liquidation levels + TVL concentration
7. **Volatility Term Structure**: IV skew → regime transitions

---

## Phase 3 Candidates: Ranked by Efficiency

### Tier 1 — Quick Wins (1–2 sprints each, 5%+ impact)

#### 1A. **Liquidation Cascade Forecaster**
- **Effort**: Medium (new microservice)
- **Impact**: 10–15% portfolio protection; zero-overhead kill-switch trigger
- **Entry Point**: `src/market/` + Decision Journal enrichment
- **Why Now**: HL liquidation data is real-time API; can forecast cascade risk 5–30min ahead
- **Interface Contract**: `predict_cascade_risk(market_data: dict) -> CascadeRisk` (non-breaking)
- **No Tests Required Yet**: New module can add tests incrementally (Phase 3b)

#### 1B. **Fee-Optimized Exit Timing**
- **Effort**: Medium (replay + decision tree)
- **Impact**: 5–8% cost reduction per trade
- **Entry Point**: `src/retro/metrics.py` + `src/execution/trade_port.py`
- **Why Now**: Have historical fee/slippage data; decision journal has all exits; can train classifier
- **Interface Contract**: `suggest_exit_moment(position, market_context) -> ExitTiming`
- **Non-Invasive**: Logged as advisory (doesn't block existing exits)

#### 1C. **Correlation-Aware Position Sizing**
- **Effort**: Low (configuration + portfolio_state enhancement)
- **Impact**: 3–5% variance reduction on correlated long longs
- **Entry Point**: Extend `PortfolioState.correlated_overloaded()` with dynamic beta
- **Why Now**: Have BTC context + correlation tracking; existing BTC lanes config ready
- **Interface Contract**: `compute_dynamic_position_limit(coin, btc_beta) -> usd_max`
- **No Breaking Changes**: Falls back to static limits if correlation data missing

---

### Tier 2 — Medium Leverage (2–3 sprints each, 3–8% impact)

#### 2A. **Volatility Regime Classifier**
- **Effort**: Medium (indicator + decision journal backfill)
- **Impact**: 8–12% win-rate improvement in mean-reversion entries
- **Entry Point**: Extend `RegimeDetector` with volatility state machine
- **Why Now**: Have 4h/1h vol data in decision journal; can backtest classifier
- **Interface Contract**: `get_volatility_regime() -> VolatilityRegime (SPIKE | COMPRESSION | NORMAL)`
- **Backward Compat**: New enum, existing regime logic unchanged

#### 2B. **Fathom Intent Extraction**
- **Effort**: High (prompt engineering + schema versioning)
- **Impact**: 5–15% size recommendation accuracy (Fathom currently just multiplier)
- **Entry Point**: `src/fathom/` + `src/retro/applier.py`
- **Why Now**: Have 6-month Fathom advisory history; can label outcomes
- **Interface Contract**: `parse_analysis_to_intent(text: str) -> Intent (BULLISH | NEUTRAL | BEARISH | HEDGE)`
- **Advisory Only**: No blocking; feeds into retrospective weighting

#### 2C. **Track-B Multi-Leg Engine** (Futures spreads, calendar spreads)
- **Effort**: High (new engine + execution adapter)
- **Impact**: 10–20% capital efficiency on correlated pairs
- **Entry Point**: New `src/engines/track_b/` following Track-A pattern
- **Why Now**: Hyperliquid supports spreads natively; decision journal ready for multi-leg PnL
- **Interface Contract**: New engine follows orchestrator interface (no changes to existing)
- **Production Ready**: Full test coverage required (new module = new test suite)

---

### Tier 3 — Foundational (3–4 sprints, 1–5% impact, unlocks Tier 4+)

#### 3A. **Observable Execution Quality SLA**
- **Effort**: Medium (ETL + dashboard)
- **Impact**: Confidence signal; identifies venue issues early
- **Entry Point**: `src/db/` new `execution_metrics` table
- **Why Now**: DegenClaw logs execution; can backfill 6 months
- **No Breaking Changes**: Purely observational; no logic changes

#### 3B. **Fathom Latency SLO Enforcement**
- **Effort**: Low (timeout + circuit breaker)
- **Impact**: Predictable fallback behavior; unblocks Tier 2B
- **Entry Point**: `src/nxfh01/advisory/fathom.py` (already has 15s timeout; add metrics)
- **Why Now**: Have telemetry; production can enforce strict SLA
- **No Refactoring**: Purely observational metrics

#### 3C. **Config Hot-Reload v2** (Canary → Progressive Rollout)
- **Effort**: Low (config versioning + deployment orchestration)
- **Impact**: Risk reduction on parameter changes; enables real-time tuning
- **Entry Point**: Extend `src/nxfh01/config_reload.py`
- **Why Now**: Already have hot-reload; just need safety gates

---

## What NOT to Do (Efficiency Guardrails)

### Anti-Patterns
- **❌ Generalize Early**: Don't add Track-C/D "just in case"
- **❌ Perfect Before Production**: Tier 3 items are observational; ship with gaps
- **❌ Refactor for Beauty**: Existing architecture works; only refactor if blocking
- **❌ Add Tests Retroactively**: All new modules must include tests day-1 (see NXFH01 rules)
- **❌ Modify src/execution/ or src/signal_ingress/**: These are sacred (see rules)

### Forbidden Work
- Do NOT rewrite AceVault scanner (works; only enhance)
- Do NOT touch DegenClaw order logic (production-critical)
- Do NOT generalize Fathom advisors (ALT/BTC-specific; new coins = new advisors)
- Do NOT add "future engines" skeleton (wait for concrete use case)

---

## Phase 3 Execution Plan (Weeks 1–6)

### Week 1–2: Liquidation Cascade Forecaster (1A)
1. Research HL liquidation API + cascade mechanics (2h)
2. Skeleton + tests for `CascadeRisk` type (4h)
3. Market feed integration (4h)
4. Backtest on historical data (6h)
5. Advisory integration + logging (4h)
6. **Total**: ~20h; **Outcome**: Non-blocking kill-switch trigger ready

### Week 2–3: Fee-Optimized Exit Timing (1B)
1. Backfill fee/slippage labels from decision journal (4h)
2. Train decision tree classifier (6h)
3. Implement `suggest_exit_moment()` (4h)
4. Integration test + logging (4h)
5. **Total**: ~18h; **Outcome**: Advisory module, 5–8% fee savings measured

### Week 3–4: Correlation-Aware Sizing (1C)
1. Extend `PortfolioState` with beta computation (6h)
2. Integrate into risk validation (4h)
3. Config schema update + tests (4h)
4. **Total**: ~14h; **Outcome**: Config-driven; falls back gracefully

### Week 4–5: Volatility Regime Classifier (2A)
1. Design volatility state machine (4h)
2. Backtest on 6 months of data (6h)
3. Extend `RegimeDetector` (4h)
4. Tests + config integration (4h)
5. **Total**: ~18h; **Outcome**: New regime type; 8–12% win-rate uplift

### Week 5–6: Fathom Intent Extraction (2B)
1. Label 200+ historical Fathom analyses (8h)
2. Prompt engineering + schema iteration (8h)
3. Parser + applier integration (6h)
4. Retrospective weighting v2 (4h)
5. **Total**: ~26h; **Outcome**: Fathom confidence becomes measurable

### Week 6+: Prep for Tier 2B (Track-B Engine)
- Skeleton only (multi-leg engine interface contract)
- Deferred to Phase 3b (requires deeper design)

---

## Success Metrics

### By End of Phase 3
- [ ] Portfolio improvement: >8% (liquidation + fee + sizing + vol + Fathom)
- [ ] Test count: 500+ (449 + new modules)
- [ ] Fathom intent extraction: 3 new intent types, 80%+ label consistency
- [ ] Kill-switch triggers: ≥1 cascade forecast per month (production alert)
- [ ] Config changes zero-touch deployable
- [ ] Decision journal enriched with: execution quality, fee estimates, correlation data

### Operational Efficiency
- [ ] No production outages during feature rollout
- [ ] Config hot-reload used ≥5 times without manual restarts
- [ ] Fathom intent used to 50%+ of retrospective decisions
- [ ] Zero modifications to DegenClaw or SignalIngress

---

## Decision Journal: Data Enrichment Roadmap

### Already Captured
- Regime, scanner weakness, risk decision, Fathom multiplier
- Peak-R (max favorable excursion), realized return
- Fee estimates (taker bps)
- Entry/exit candle prices

### To Add (Priority)
1. **Liquidation Cascade Risk** (Week 1–2) → `cascade_risk_score FLOAT`
2. **Execution Metrics** (Week 3) → `slippage_bps`, `market_impact_bps`, `realized_vs_mark_bps`
3. **Correlation Snapshot** (Week 3–4) → `btc_alt_correlation FLOAT`
4. **Volatility Regime** (Week 4–5) → `vol_regime TEXT` (SPIKE/COMPRESSION/NORMAL)
5. **Fathom Intent** (Week 5–6) → `fathom_intent TEXT` (BULLISH/NEUTRAL/BEARISH/HEDGE)

### Not Needed
- Granular Ollama latency (just 15s timeout + fallback)
- Per-candle market impact (retroactively unmeasurable)

---

## Risk Mitigation

| Risk | Mitigation |
|------|-----------|
| Fathom latency blocks trades | 15s timeout + fallback (already implemented) |
| Liquidation forecaster false positives | Advisory only; logged separately; no kill-switch until proven |
| Config hot-reload breaks live | Canary on test mode first; validate schema strictly |
| Fee optimizer suggests bad exits | Logged as advisory; human review before enabling |
| New tests slow CI | Run in parallel; exclude slow integration tests from dev loop |

---

## Dependencies & Blockers

### Required Before Phase 3 Start
- [ ] Verify mainnet cycle runs clean for 72h (production hardening)
- [ ] Confirm Fathom Ollama stable on Mac Mini (latency <2s median)
- [ ] Database schema migrations tested on Supabase (005_fee_capture already in)

### Optional (Can Proceed Without)
- Nansen client integration (only for wash-trader filtering; Phase 4)
- Liquidation cascade historical backfill (can start cold on Phase 3 week 1)

---

## Long-Term Outlook (Phase 4+)

### What Unblocks Next
1. **Track-B multi-leg engine** → enables ratio spreads, calendar spreads
2. **Liquidation cascade + correlation decay** → enables macro regime shifts
3. **Fee structure arbitrage** → enables venue-neutral strategies
4. **Wash-trader veto** (Nansen) → improves entry accuracy on low-liquidity alts

### What Becomes Obsolete
- Manual risk gate adjustments (config hot-reload owns this)
- Hardcoded thresholds in code (all → config.yaml)
- Mocks in production tests (real Hyperliquid API calls with fixtures)

---

## Efficiency Scorecard

| Initiative | LOC Est. | Tests Est. | Weeks | ROI | Priority |
|-----------|----------|-----------|-------|-----|----------|
| Liquidation Cascade (1A) | 400 | 25 | 2 | 10–15% | HIGH |
| Fee-Optimized Exits (1B) | 350 | 20 | 2 | 5–8% | HIGH |
| Correlation Sizing (1C) | 200 | 15 | 1 | 3–5% | MEDIUM |
| Volatility Regime (2A) | 300 | 25 | 2 | 8–12% | MEDIUM |
| Fathom Intent (2B) | 400 | 30 | 2 | 5–15% | MEDIUM |
| Track-B Engine (2C) | 1000+ | 60+ | 3+ | 10–20% | LOW (Phase 3b) |

**Total Phase 3 Effort**: ~1500 LOC, ~125 tests, ~6 weeks, 25–50% portfolio improvement expected.

---

## Next Actions

### Before Next Session
1. Review this roadmap; confirm priorities align with business goals
2. If liquidation cascade data unavailable, pivot to Fee-Optimized Exits (1B)
3. Prepare backfill script for decision journal enrichment (cascades + execution metrics)

### First Task (Week 1)
```bash
# Create Tier 1A skeleton + tests
python -c "from src.market.cascade_forecaster import CascadeRisk; print('Skeleton ready')"
pytest tests/test_cascade_forecaster.py -v
```

---

## Document Control

- **Author**: AI Agent
- **Date**: 2026-04-18
- **Version**: 1.0 (Initial Phase 3 Framework)
- **Status**: Ready for Review
- **Next Update**: After Phase 3 Week 1 checkpoint

---

## Appendix: Module Inventory

### Sacred Modules (Do Not Modify Without Explicit Instruction)
- `src/execution/order_executor.py` — DegenClaw sole executor
- `src/signal_ingress/` — HTTP signal receiver
- Core risk gate in `UnifiedRiskLayer.validate()`

### Actively Maintained (OK to Enhance)
- `src/engines/acevault/` (entry, exit, scanner, models)
- `src/regime/detector.py` (add new regimes here)
- `src/risk/portfolio_state.py` (add metrics here)
- `src/retro/metrics.py` (add measurements here)
- `src/db/decision_journal.py` (add columns here)

### Ready for New Work
- `src/market/` (add cascade_forecaster.py here)
- `src/fathom/` (enhance advisor.py intent parsing)
- `src/engines/` (add Track-B here in Phase 3b)

---
