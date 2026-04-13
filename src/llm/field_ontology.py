"""Fixed field ontology for Fathom LLM prompts.

Single source of truth for every metric Fathom encounters in signal rationales,
market data, and enrichment outputs. Prepended to all Fathom system prompts so
the model never has to guess at field semantics.
"""

FIELD_ONTOLOGY = """\
=== NXFH02 FIELD ONTOLOGY (use these definitions exactly) ===

MOMENTUM SIGNAL FIELDS:
- spread: (EMA20 - EMA50) / EMA50 on 4H candles. Positive = fast EMA above slow (bullish structure). \
Negative = fast below slow (bearish). NOT bid-ask spread.
- d_spread: Change in spread over the lookback period. Positive = spread widening (trend strengthening). \
Negative = spread narrowing (trend weakening or reversing).
- fast_slope: Percentage change of the 20-period EMA over the slope lookback window. \
Positive = upward momentum. Larger magnitude = faster move. Range typically 0.001 to 0.02.
- volx: Realized volatility ratio = current vol / baseline vol. \
1.0 = normal. >1.0 = elevated volatility (can mean breakout or blow-off). <1.0 = compressed.
- SL%: Stop-loss distance as percentage from entry (based on 2x ATR).
- TP%: Take-profit distance as percentage from entry (SL * reward-risk ratio).

BTC REGIME:
- BTC regime: Coarse 4H trend classification: bullish, bearish, or neutral. \
Based on EMA20 vs EMA50 position and slope.
- stage_v2: Fine-grained 4H regime stage:
  * EARLY_TREND: Spread just turned positive/negative, momentum building. Best risk/reward for entries.
  * MATURE_TREND: Spread wide and sustained. Trend intact but crowding risk increases.
  * ROLLING_OVER: Spread was meaningful but is now shrinking against prior direction. Trend exhaustion.
  * RANGE: Elevated volatility but no directional EMA separation. Chop zone.
  * COMPRESSION: Low ATR + tight EMAs. Potential breakout setup.

MICROSTRUCTURE (MICRO):
- MICRO=SUPPORTS_LONG or SUPPORTS_SHORT: Order book imbalance bias on the current instrument.
- imb: Order book imbalance score, 0 to 1. >0.5 = buy-side dominant. <0.5 = sell-side dominant.
- adj: Confidence adjustment applied. Positive = microstructure confirms signal direction. \
Negative = microstructure contradicts it.

SIGNAL PIPELINE FIELDS:
- div_r: Diversity rank within correlated cluster. 1 = top signal in cluster. \
Higher ranks get confidence haircuts to prevent concentrated bets.
- mult: Diversity multiplier applied. 1.00 = no haircut. <1.00 = reduced due to cluster overlap.
- FINAL cap: Per-strategy confidence ceiling (e.g., momentum max = 0.82).
- global: Global confidence cap applied across all strategies (e.g., 0.92).
- dir_bal: Directional balance score. Positive = net bullish positioning. Negative = net bearish. \
Used for regime-aware directional bias.

NANSEN SMART MONEY:
- Format: "Nansen COIN: XL/YS/ZF" where X=long count, Y=short count, Z=flat count among top traders.
- consensus_strength: 0.0 to 1.0, majority/total ratio. >0.7 = strong consensus.
- Long/short values: USD notional of positions by direction.
- "SM confirms": Smart money agrees with signal direction → confidence boosted.
- "SM diverges": Smart money disagrees → confidence reduced.
- CRITICAL: High consensus ≠ always correct. 75% short consensus during bullish regime = \
potential short squeeze risk, not just bearish confirmation.

ONCHAIN FEATURES:
- accumulation_score: 0.0-1.0. Top wallet balance changes. >0.6 = accumulation (bullish for longs).
- anomaly_score: 0.0-1.0. Unusual transfer/flow patterns. >0.7 = elevated risk.
- spot_perp_basis_pct: (perp_price - spot_price) / spot_price. Positive = perp premium. \
Large positive = speculative excess. Large negative = capitulation or arbitrage.
- smart_money_netflow_usd: Net token flow from tracked wallets. Positive = inflow (accumulation).
- bridge_flow_score: -1 to +1. +1 = net inflow to HyperEVM chain.

FUNDING:
- rate: 8-hour funding rate. Positive = longs pay shorts. Negative = shorts pay longs.
- hourly: rate / 8. The per-hour cost of holding a position.
- High positive funding + long signal = headwind (you pay to hold). \
High negative funding + short signal = headwind.

=== POSITIONING LOGIC RULES ===

You MUST always analyze positioning from BOTH sides:

1. HIGH SHORT CONCENTRATION + NEGATIVE BASIS + NON-EXTREME VOLX:
   Supports: Trend continuation (shorts are being built into the move).
   Risk: Short squeeze if price reverses. Concentrated shorts = liquidation cascade fuel.

2. HIGH LONG CONCENTRATION + EARLY_TREND STAGE:
   Supports: Early but consensus-confirming momentum.
   Risk: Crowding. If the trend stalls, longs become exit liquidity. Watch for funding flip, \
CVD divergence, or whale distribution into strength.

3. MICRO FLIP (SUPPORTS_LONG → SUPPORTS_SHORT or vice versa):
   Treat as a potential regime shift signal. Note it explicitly. If it contradicts the signal \
direction, elevate risk assessment.

4. SMART MONEY DIVERGENCE:
   When Nansen consensus opposes the signal direction, you MUST mention both: \
"Smart money disagrees, which historically reduces win rate" AND \
"However, smart money can be wrong during strong momentum; this adds uncertainty, not certainty."

=== UNCERTAINTY RULES ===

- If you encounter a field you do not recognize, say: "Unknown field [X] — cannot interpret."
- NEVER fabricate definitions for unfamiliar metrics.
- NEVER say "I think this means..." without qualifying "but this interpretation is uncertain."
- When data is missing or stale, note it: "No microstructure data available — cannot assess order flow."
- When metrics conflict, state the conflict explicitly rather than resolving it arbitrarily.
"""

POSITIONING_RULES_SHORT = """\
Always analyze positioning from both sides:
- For: why positioning supports the direction
- Against: how positioning can kill the trade (squeeze/flush/liquidation cascades)
When Nansen diverges from signal, note both the headwind and the possibility smart money is wrong.
"""
