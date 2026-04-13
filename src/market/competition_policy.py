"""Competition policy — DegenClaw-aware trade modulator.

Adjusts selectivity and exit urgency based on competition position
without generating signals or overriding hard risk rules.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

from src.config import CompetitionPolicyConfig

logger = logging.getLogger(__name__)


@dataclass
class CompetitionPolicyResult:
    trade_frequency_bias: str = "NORMAL"
    exit_urgency_bias: str = "NORMAL"
    min_confidence_multiplier: float = 1.0
    reason_code: str = "NORMAL_OPERATION"


class CompetitionPolicy:
    """Modulates trading behavior based on competition metrics."""

    def __init__(
        self,
        config: CompetitionPolicyConfig,
        portfolio=None,
        risk_supervisor=None,
    ):
        self._config = config
        self._portfolio = portfolio
        self._risk = risk_supervisor

    def evaluate(
        self,
        base_min_confidence: float,
    ) -> CompetitionPolicyResult:
        """Compute policy adjustments from current competition state."""
        if not self._config.enabled:
            return CompetitionPolicyResult(reason_code="DISABLED")

        if self._portfolio is None:
            return CompetitionPolicyResult(reason_code="NO_PORTFOLIO")

        comp = self._portfolio.competition_score()
        sortino = comp.get("sortino_ratio", 0.0)
        total_trades = comp.get("total_trades", 0)
        win_rate = comp.get("win_rate", 0.0) / 100.0

        if total_trades < self._config.min_trades_for_policy:
            return CompetitionPolicyResult(reason_code="INSUFFICIENT_TRADES")

        freq_bias = "NORMAL"
        exit_bias = "NORMAL"
        conf_mult = 1.0
        reason = "NORMAL_OPERATION"

        # Leading: protect Sortino by being more selective
        if sortino >= self._config.lock_profit_sortino_threshold:
            conf_mult = 1.0 + self._config.leading_confidence_boost
            exit_bias = "LOCK_IN_PROFIT"
            freq_bias = "REDUCE"
            reason = "LEADING_PROTECT_SORTINO"
            logger.info(
                "CompPolicy: LEADING (sortino=%.2f) — raising confidence %.0f%%, "
                "locking profits",
                sortino, self._config.leading_confidence_boost * 100,
            )

        # Chasing: slightly more aggressive but still constrained
        elif sortino < 0.5 and total_trades >= 10 and win_rate < 0.4:
            conf_mult = max(
                0.95,
                1.0 - self._config.chasing_confidence_reduction,
            )
            exit_bias = "LET_RUN"
            freq_bias = "INCREASE"
            reason = "CHASING_NEEDS_EDGE"
            logger.info(
                "CompPolicy: CHASING (sortino=%.2f wr=%.0f%%) — slight confidence reduction",
                sortino, win_rate * 100,
            )

        # Time pressure: lock profits if approaching season end
        if self._config.season_end_utc:
            try:
                end = datetime.fromisoformat(self._config.season_end_utc)
                remaining_hours = (end - datetime.now(timezone.utc)).total_seconds() / 3600
                if 0 < remaining_hours < 24:
                    exit_bias = "LOCK_IN_PROFIT"
                    freq_bias = "REDUCE"
                    conf_mult = max(conf_mult, 1.0 + self._config.leading_confidence_boost)
                    reason = "NEAR_SEASON_END"
                    logger.info(
                        "CompPolicy: %.1f hours to season end — locking profits",
                        remaining_hours,
                    )
            except (ValueError, TypeError):
                pass

        result = CompetitionPolicyResult(
            trade_frequency_bias=freq_bias,
            exit_urgency_bias=exit_bias,
            min_confidence_multiplier=conf_mult,
            reason_code=reason,
        )

        if reason != "NORMAL_OPERATION":
            logger.info(
                "CompPolicy: %s | freq=%s exit=%s conf_mult=%.2f",
                reason, freq_bias, exit_bias, conf_mult,
            )
        else:
            logger.debug("CompPolicy: %s", reason)

        return result

    def apply_to_confidence_threshold(
        self, base_threshold: float,
    ) -> float:
        """Modulate min confidence threshold. Never lowers below base."""
        result = self.evaluate(base_threshold)
        adjusted = base_threshold * result.min_confidence_multiplier
        return max(adjusted, base_threshold)
