"""Funding Rate Carry strategy — confirmed entries only."""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from src.config import StrategyConfig, get_asset_risk_params
from src.market.types import FundingRate
from src.strategy.base import MarketSnapshot, Strategy, StrategySignal
from src.strategy.regime import BtcRegime, BtcRegimeStage, detect_regime, detect_regime_stage

logger = logging.getLogger(__name__)

_SETTLEMENT_MINUTES = [0, 480, 960, 1440]

# Only signal when rate is this many times above threshold
# Filters out weak carry opportunities
MIN_RATE_MULTIPLE = 1.5


def _minutes_to_next_funding() -> float:
    now = datetime.now(timezone.utc)
    current_min = now.hour * 60 + now.minute
    for s in _SETTLEMENT_MINUTES:
        if s > current_min:
            return s - current_min
    return _SETTLEMENT_MINUTES[-1] - current_min


def _in_funding_window(pre_settlement_minutes: int) -> bool:
    return _minutes_to_next_funding() <= pre_settlement_minutes


class FundingCarryStrategy(Strategy):

    @property
    def name(self) -> str:
        return "funding_carry"

    def is_enabled(self, config: StrategyConfig) -> bool:
        return config.funding_carry.enabled

    def evaluate(
        self,
        snapshot: MarketSnapshot,
        config: StrategyConfig,
    ) -> list[StrategySignal]:
        signals: list[StrategySignal] = []
        fc = config.funding_carry
        allowed = config.allowed_markets.perps

        mins_to_settlement = _minutes_to_next_funding()
        in_window = mins_to_settlement <= fc.pre_settlement_minutes

        if not in_window:
            logger.debug(
                "Funding carry: %d min to settlement (window=%d) — waiting",
                int(mins_to_settlement), fc.pre_settlement_minutes,
            )
            return signals

        btc_regime = BtcRegime.NEUTRAL
        btc_candles = snapshot.candles.get("BTC_4h", [])
        if btc_candles:
            btc_regime = detect_regime(btc_candles, config.btc_regime)

        btc_stage = (
            detect_regime_stage(btc_candles, config.btc_regime)
            if btc_candles else None
        )

        def _short_carry_mult() -> float:
            if btc_regime == BtcRegime.BEARISH:
                return 1.0
            if btc_regime == BtcRegime.NEUTRAL:
                return fc.short_carry_threshold_mult_neutral
            return fc.short_carry_threshold_mult_bullish

        for rate in snapshot.funding_rates:
            if not isinstance(rate, FundingRate):
                continue
            if rate.coin not in allowed:
                continue

            hourly = rate.hourly
            threshold = fc.min_funding_rate_hourly

            if hourly > 0:
                eff_mult = MIN_RATE_MULTIPLE * _short_carry_mult()
                if btc_stage == BtcRegimeStage.ROLLING_OVER:
                    eff_mult *= fc.rolling_over_short_mult
            else:
                eff_mult = MIN_RATE_MULTIPLE

            if abs(hourly) < threshold * eff_mult:
                continue

            if hourly > 0:
                side = "short"
                st = btc_stage.value if btc_stage else "n/a"
                rationale = (
                    f"Funding carry SHORT {rate.coin}: "
                    f"rate={hourly:.6f}/hr ({abs(hourly)/threshold:.1f}x threshold, "
                    f"eff_mult={eff_mult:.2f} BTC={btc_regime.value} stage={st}). "
                    f"Settlement in {int(mins_to_settlement)} min."
                )
            else:
                side = "long"
                rationale = (
                    f"Funding carry LONG {rate.coin}: "
                    f"rate={abs(hourly):.6f}/hr ({abs(hourly)/threshold:.1f}x threshold). "
                    f"Settlement in {int(mins_to_settlement)} min."
                )

            rate_multiple = abs(hourly) / threshold
            base_confidence = min(
                fc.confidence_ceiling,
                0.55 + (rate_multiple - eff_mult) * 0.12,
            )

            if mins_to_settlement <= 15:
                confidence = min(fc.confidence_ceiling, base_confidence + 0.10)
            elif mins_to_settlement <= 30:
                confidence = min(fc.confidence_ceiling, base_confidence + 0.05)
            else:
                confidence = base_confidence

            max_lev, risk_pct = get_asset_risk_params(config, rate.coin)
            signals.append(StrategySignal(
                strategy_name=self.name,
                coin=rate.coin,
                side=side,
                confidence=confidence,
                recommended_size_pct=risk_pct,
                leverage=max_lev,
                stop_loss_pct=0.015,
                take_profit_pct=0.015 * config.momentum.min_rr_ratio,
                rationale=rationale,
                constraints_checked=["funding_rate_minimum", "allowed_markets_check"],
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)

        if signals:
            logger.info(
                "Funding carry: %d confirmed opportunities (%d min to settlement)",
                len(signals), int(mins_to_settlement),
            )

        return signals