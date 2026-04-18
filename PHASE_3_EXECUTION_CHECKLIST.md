# Phase 3 Execution Checklist — Week-by-Week Sprint Plan

**Duration**: 6 weeks  
**Starts**: Next sprint  
**Team Capacity**: 1 engineer (you)  
**Rhythm**: 30h/week avg (4 feature days + 1 test/review day)  

---

## Pre-Phase Readiness (Do Before Week 1 Starts)

### [ ] Production Hardening
- [ ] Run mainnet cycle for 72h continuously
  - Log file: `scripts/verify_nxfh01_production.py` output
  - Success: 0 unrecoverable failures
- [ ] Fathom latency check
  - Confirm Ollama median response <2s
  - Max timeout: 15s (already set in config)
- [ ] Database check
  - Verify Supabase connection stable
  - Run migration 005_fee_capture.sql if not done
  - Confirm decision_journal has fee columns

### [ ] Decision Journal Baseline
- [ ] Backfill fee_taker_bps on existing trades (script ready: src/retro/fee_estimation.py)
- [ ] Verify all 449 tests pass
  ```bash
  pytest -xvs 2>&1 | tail -20
  ```
- [ ] Document current decision journal schema
  ```bash
  psql $DATABASE_URL -c "\d strategy_decisions"
  ```

---

## Week 1: Liquidation Cascade Forecaster — Foundation (20h)

### Goals
- Prototype cascade risk detection
- Build decision journal enrichment pattern (reusable for Tier 1B, 1C, 2A)
- NO production integration yet; advisory only

### Breakdown

#### Day 1: Research & Skeleton (6h)
- [ ] Study HL liquidation API docs
  - Save reference: `docs/hl_liquidation_api.md`
  - Key fields: liquidation_levels, total_TVL, concentration_by_level
- [ ] Design `CascadeRisk` data type
  ```python
  # src/market/cascade_forecaster.py (skeleton only)
  @dataclass
  class CascadeRisk:
      risk_score: float  # 0.0 to 1.0
      liquidation_levels_in_range: int
      tvl_at_risk_usd: float
      minutes_to_cascade: float | None
      triggered_at: datetime | None
  ```
- [ ] Create test file stubs
  ```bash
  touch tests/test_cascade_forecaster.py
  ```
- **Check-in**: Skeleton compiles, imports work

#### Day 2: API Integration (6h)
- [ ] Write fetcher for HL liquidation data
  - Follow pattern from `src/market_data/hl_rate_limited_info.py`
  - Rate limit: reuse hyperliquid_api.min_interval_ms
- [ ] Test with real HL API (mock-friendly)
  ```python
  async def fetch_liquidation_cascade_risk(hl_client) -> CascadeRisk:
      """Fetch realtime cascade risk metrics."""
  ```
- [ ] Add to config.yaml (optional: enable/disable)
  ```yaml
  liquidation_forecaster:
    enabled: false  # default to off for Phase 3 week 1
    alert_threshold_score: 0.65
  ```
- **Check-in**: Can fetch data; tests pass

#### Day 3: Backtest Pattern (6h)
- [ ] Backtest on 6 months of HL liquidation data
  - Use historical candles + liquidation snapshots
  - Goal: find cascade signals that preceded HL-wide forced closes
- [ ] Plot false positive rate (should be <10%)
- [ ] Document findings in `docs/cascade_backtest_results.md`
- **Check-in**: Decision ready: proceed to integration or refine?

#### Day 4: Tests + Advisory Logging (2h)
- [ ] Write 25+ tests (happy path, API failures, edge cases)
  - No cascade risk: score = 0
  - High TVL concentration: score > 0.7
  - API failure: fallback score = 0 (safe default)
- [ ] Add MARKET_CASCADE_* log lines
- [ ] Verify 100% test pass
- **Check-in**: All tests green; ready to log

#### Friday: Review & Planning
- [ ] Code review vs NXFH01 rules (no hardcoded thresholds, all in config)
- [ ] Prepare PR description (optional; can batch with 1B + 1C)
- [ ] Update SPRINT_CONTEXT.md with progress
- **Output**: PR-ready branch OR decision to pivot

---

## Week 2: Fee-Optimized Exit Timing — Classifier (18h)

### Goals
- Build historical fee/slippage dataset
- Train decision tree classifier
- Output: advisory exit suggestions (no blocking)

### Breakdown

#### Day 1: Data Backfill (6h)
- [ ] Extract fee + slippage labels from decision_journal
  - Join on trade_id: estimate_fee_bps (from column), actual_slippage_bps (mark vs realized)
  - Filter: closed trades only (has realized_return)
- [ ] Create feature matrix
  - Features: time_of_day, position_size_usd, vol_regime, btc_price_momentum, time_in_trade_minutes
  - Target: exit_timing_score (0=bad_fee, 1=good_fee)
- [ ] Save dataset: `data/fee_labels_6m.parquet`
- [ ] Exploratory analysis: which features matter most?
- **Check-in**: Dataset ready; 300+ labeled exits

#### Day 2: Classifier Training (6h)
- [ ] Use scikit-learn DecisionTreeClassifier
  - Max depth: 6 (avoid overfitting)
  - Train/test split: 80/20
- [ ] Evaluate: precision, recall, F1
- [ ] Cross-validate: 5-fold
- [ ] Save model: `models/fee_exit_classifier.pkl`
- [ ] Fail criterion: F1 < 0.65 → pivot to hardcoded rules
- **Check-in**: Model saved; metrics recorded

#### Day 3: Integration (4h)
- [ ] Implement `suggest_exit_moment(position, market_context) -> ExitSuggestion`
  - Input: position details (size, entry_price, duration), market (vol, momentum)
  - Output: confidence + timing hint (ASAP | WAIT | HOLD)
- [ ] Integrate with decision_journal logging
- [ ] NO blocking behavior (advisory only)
- **Check-in**: Function works; tested

#### Day 4: Tests + Logging (2h)
- [ ] 20+ unit tests + integration test
- [ ] Log RISK_EXIT_TIMING_ADVISORY_* lines
- [ ] Test model inference speed (<50ms per call)
- **Check-in**: Tests pass; ready to submit

#### Friday: Review & Batch with Week 1
- [ ] Code review
- [ ] Prepare for combined 1A + 1B PR (if time allows) or separate PRs

---

## Week 3: Correlation-Aware Sizing + Execution Metrics (14h)

### 3A: Correlation-Aware Sizing (6h)

#### Task Breakdown
- [ ] Extend `PortfolioState` with `compute_dynamic_beta(coin) -> float`
  - Use 30d rolling correlation with BTC
  - Cached in-memory (update on portfolio state change)
- [ ] Modify `get_available_capital()` to respect dynamic limits
  - `max_usd = base_limit / (1 + beta * correlation_scale_factor)`
  - Config param: `correlation_scale_factor: 0.5` (tunable)
- [ ] Add 15+ tests (beta computation, edge cases, caching)
- **Check-in**: Tests pass; no breaking changes

### 3B: Execution Metrics (8h)

#### Task Breakdown
- [ ] Add decision_journal schema columns
  ```sql
  ALTER TABLE strategy_decisions ADD COLUMN IF NOT EXISTS:
    execution_quality_score FLOAT,
    slippage_bps FLOAT,
    market_impact_bps FLOAT,
    realized_vs_mark_bps FLOAT;
  ```
- [ ] Backfill execution_quality_score on 500+ historical trades
  - Inspect DegenClaw logs: extract actual execution time, order size
  - Compare to mark at entry vs realized at execution
- [ ] Update decision_journal.py to capture on future trades
  - Hook into Track-A executor telemetry
- [ ] Add 10+ tests (schema, backfill, null handling)
- **Check-in**: Schema updated; backfill verified; tests green

---

## Week 4: Volatility Regime Classifier (18h)

### Goals
- Design volatility state machine (SPIKE | COMPRESSION | NORMAL)
- Backtest on 6 months of data
- Extend RegimeDetector with new state

### Breakdown

#### Day 1: Design & Backtest (6h)
- [ ] Define volatility regimes
  ```python
  SPIKE: vol_1h > percentile_90(vol_1h_30d)
  COMPRESSION: vol_1h < percentile_20(vol_1h_30d)
  NORMAL: otherwise
  ```
- [ ] Backtest: which regime has best win-rate for mean-reversion?
  - Goal: 8–12% win-rate improvement for mean-reversion entries
  - If not met: adjust thresholds or add entropy indicator
- [ ] Document findings: `docs/vol_regime_backtest.md`
- **Check-in**: Design locked in; metrics show 8%+ improvement

#### Day 2: Implementation (6h)
- [ ] Extend `RegimeDetector.detect()` to return tuple
  ```python
  (primary_regime, volatility_regime)
  # or extend RegimeState dataclass
  ```
- [ ] Integrate with scanner weakness scoring (vol-aware)
- [ ] No breaking changes to existing logic
- **Check-in**: New regime integrated; all existing tests still pass

#### Day 3: Tests + Config (4h)
- [ ] 25+ tests (state transitions, edge cases, data boundaries)
- [ ] Add to config.yaml
  ```yaml
  regime_detector:
    volatility_regime:
      enabled: true
      spike_percentile: 90
      compression_percentile: 20
  ```
- [ ] Test hot-reload with vol regime changes
- **Check-in**: Tests pass; config verified

#### Day 4: Integration (2h)
- [ ] Integrate with decision_journal (new column: vol_regime TEXT)
- [ ] Log REGIME_VOLATILITY_DETECTED_* lines
- [ ] Verify decision journal captures on next trades
- **Check-in**: Logging works; data flows

#### Friday: Review
- [ ] Code review; prepare 2A PR

---

## Week 5: Fathom Intent Extraction (26h)

### Goals
- Label 200+ historical Fathom analyses
- Build intent parser (BULLISH | NEUTRAL | BEARISH | HEDGE)
- Integrate with retrospective weighting

### Breakdown

#### Day 1: Labeling Session (8h)
- [ ] Export Fathom analyses from 6 months of retrospectives
  ```sql
  SELECT id, fathom_analysis, subsequent_10d_pnl, subsequent_win_rate
  FROM retrospectives ORDER BY created_at DESC LIMIT 200;
  ```
- [ ] Label each: BULLISH | NEUTRAL | BEARISH | HEDGE
  - BULLISH: explicit size-up or entry recommendation
  - NEUTRAL: balance or risk management focus
  - BEARISH: size-down or exit recommendation
  - HEDGE: position diversification or correlation focus
- [ ] Cross-check: correlation with actual PnL (min 0.7 correlation)
- [ ] Save labels: `data/fathom_intents_200.csv`
- **Check-in**: Labels consistent; correlation verified

#### Day 2: Prompt Engineering (8h)
- [ ] Iterate prompt to extract intent from fathom_analysis text
  ```python
  def parse_intent(analysis_text: str) -> tuple[Intent, float]:
      """Returns (intent, confidence 0.0–1.0)."""
      # Use Fathom itself or rule-based parser (test both)
  ```
- [ ] Test on labeled dataset: 80%+ accuracy
- [ ] Fail criterion: accuracy < 75% → use rule-based + manual labels
- [ ] Save prompt: `prompts/fathom_intent_v1.txt`
- **Check-in**: Parser works; accuracy >75%

#### Day 3: Integration + Applier (6h)
- [ ] Update `retro/applier.py` to consume intent
  - Weight multiplier recommendations based on intent
  - BULLISH intents → higher multiplier ceiling (within bounds)
  - BEARISH intents → lower multiplier ceiling
- [ ] Integrate into retrospective loop
- [ ] Add decision_journal column: fathom_intent TEXT
- [ ] Log FATHOM_INTENT_PARSED_* lines
- **Check-in**: Intent flows through retrospective

#### Day 4: Tests + Schema (4h)
- [ ] 30+ unit tests (intent parsing, edge cases, weighting)
- [ ] Schema migration for fathom_intent column
- [ ] Test database integration (no failures on missing intent)
- **Check-in**: All tests pass; schema verified

#### Friday: Review & Prep for Batch
- [ ] Code review for 2B
- [ ] Prepare Tier 2A + 2B combined PR (if polish allows)
- [ ] Document prompt version in FATHOM_CHANGELOG.md

---

## Week 6: Prep for Track-B + Documentation (TBD)

### Flexibility Week
Depending on actual progress:

**If Tier 1A–2B Complete & Stable**:
- [ ] Begin Track-B skeleton (multi-leg engine interface contract)
  - Design docs only; no implementation
  - Folder structure: `src/engines/track_b/`
  - Interface: `run_cycle() -> List[TrackBSignal]` (follows Track-A pattern)
- [ ] Backlog: deferred to Phase 3b formal sprint

**If Any Tier 1 Item Overran**:
- [ ] Finish and polish
- [ ] Write comprehensive tests
- [ ] Documentation pass

**If All Ahead of Schedule**:
- [ ] Start 3A: Observable Execution Quality SLA
  - New `execution_metrics` table
  - Dashboard queries for slippage/impact analysis

### Documentation Finalization
- [ ] Update SPRINT_CONTEXT.md
- [ ] Record Phase 3 week 1–6 progress
- [ ] Backlog for Phase 3b

---

## Cross-Week Hygiene Tasks

### Every Friday (4h)
- [ ] Run full test suite
  ```bash
  pytest -x --tb=short 2>&1 | tail -50
  ```
- [ ] Check linter
  ```bash
  flake8 src/ --max-line-length=100
  ```
- [ ] Update SPRINT_CONTEXT.md with week summary
- [ ] Commit + push (batched 2–3 features per PR for clarity)

### End of Each Feature Day
- [ ] New module tests: 100% pass rate required
- [ ] Log lines: all prefixed correctly (ACEVAULT_, REGIME_, RISK_, FATHOM_, MARKET_)
- [ ] Config changes: all in config.yaml, not hardcoded
- [ ] Git status clean or feature branch clean

---

## Success Criteria by Week

### Week 1: Cascade Forecaster
- [ ] 25+ tests, 100% pass
- [ ] Can fetch HL liquidation data reliably
- [ ] Advisory logging works
- [ ] No production integration (feature flag off)
- **Expected Outcome**: PR-ready, deferred integration to week 2

### Week 2: Fee-Optimized Exits
- [ ] 20+ tests, 100% pass
- [ ] Model F1 >0.65
- [ ] Advisory logged; no blocking
- [ ] Integration with decision_journal confirmed
- **Expected Outcome**: Feature-ready, deferred to production after week 3 review

### Week 3: Correlation + Execution Metrics
- [ ] 25+ new tests, 100% pass
- [ ] Schema migration tested on Supabase
- [ ] Backfill 500+ trades confirmed
- [ ] No breaking changes
- **Expected Outcome**: New decision_journal columns + portfolio_state ready

### Week 4: Volatility Regime
- [ ] 25+ tests, 100% pass
- [ ] Backtest shows 8%+ win-rate gain
- [ ] Config hot-reload tested
- [ ] Logging integrated
- **Expected Outcome**: New regime state operational

### Week 5: Fathom Intent
- [ ] 30+ tests, 100% pass
- [ ] Intent parser >75% accuracy
- [ ] 200+ labels verified
- [ ] Retrospective weighting updated
- [ ] New decision_journal column populated
- **Expected Outcome**: Intent-aware Fathom retrospectives live

### Week 6: Documentation + Prep
- [ ] All Phase 3 features integrated
- [ ] 500+ total tests (449 + ~125 new)
- [ ] SPRINT_CONTEXT.md finalized
- [ ] Track-B skeleton (optional)
- **Expected Outcome**: Phase 3 complete; ready for Phase 3b planning

---

## Efficiency Guardrails

### Do NOT
- ❌ Add "future engine" skeleton without concrete feature
- ❌ Refactor existing modules for beauty (only if blocking)
- ❌ Write tests "later" (tests day-1 of every module)
- ❌ Hardcode thresholds (all in config.yaml)
- ❌ Modify src/execution/ or src/signal_ingress/ (sacred)

### Do
- ✅ Test every module day-1 (at least skeleton tests)
- ✅ Use decision_journal for rich data collection
- ✅ Design for future engines (Track-B, Track-C) but don't build them yet
- ✅ Log every state change (REGIME_, RISK_, FATHOM_, MARKET_ prefixes)
- ✅ Batch-review features 2–3 per PR (clarity + efficiency)

---

## Risk Mitigation

| Week | Risk | Mitigation |
|------|------|-----------|
| 1 | HL liquidation API unreliable | Fallback to safe default (score=0); offline until stable |
| 2 | Fee classifier overfits | Use 5-fold CV; max depth 6; test on holdout 2 weeks after deploy |
| 3 | Correlation data sparse | Fall back to static limits if correlation unavailable |
| 4 | Vol regime spams false alerts | Conservative thresholds (90th percentile); test on 3 months before enabling |
| 5 | Fathom intent labels inconsistent | Re-label with second opinion (you); min agreement 80% |
| All | Production breaks mid-week | Feature flag all new features; disable in config if issues arise |

---

## Commit Strategy

- **Week 1**: `feat(market): liquidation cascade forecaster` (Phase 3.1a)
- **Week 2**: `feat(market): fee-optimized exit timing advisor` (Phase 3.1b)
- **Week 3**: `feat(risk): correlation-aware position sizing + execution metrics` (Phase 3.1c)
- **Week 4**: `feat(regime): volatility regime classifier` (Phase 3.2a)
- **Week 5**: `feat(fathom): intent extraction + retrospective weighting v2` (Phase 3.2b)
- **Week 6**: `docs: Phase 3 completion + Track-B design` (Phase 3 close-out)

**PR Style**: 1 feature per PR; link to PHASE_3_ROADMAP.md; document expected impact

---

## Next Steps (Right Now)

1. **Review This Checklist**: Confirm all tasks make sense
2. **Run Readiness Checks**: 72h mainnet + database verify
3. **Create Phase 3 Branch**: `git checkout -b phase-3-enhancement-foundation`
4. **Start Week 1, Day 1**: Research liquidation API + skeleton

**Estimated First Commit**: 2–3 days into Week 1

---

## Questions to Answer Before Week 1 Starts

- [ ] Is Fathom Ollama stable on Mac Mini? (Required for baseline)
- [ ] Can we access HL liquidation API without special permissions? (Required for 1A)
- [ ] Do we have 6 months of decision_journal history for 1B backtest? (Required for 1B)
- [ ] Is Supabase connection stable for schema changes? (Required for weeks 3–5)

---

