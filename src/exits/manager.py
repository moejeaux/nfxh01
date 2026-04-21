from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.exits.models import Side, UniversalExit
from src.exits.policy_config import resolve_exit_policy
from src.exits.policies import evaluate_exit, unrealized_r_multiple, update_extremes_and_peak
from src.exits.state import ExitStateStore
from src.regime.models import RegimeType

logger = logging.getLogger(__name__)


@dataclass
class _TrendingUpMassExitTrack:
    consecutive_trending_up: int = 0
    streak_start_at: datetime | None = None
    last_mass_exit_at: datetime | None = None


class TrendingUpMassExitGate:
    """Exit-side hysteresis for AceVault-style ``close_all_on_trending_up`` mass exits."""

    def __init__(self) -> None:
        self._tracks: dict[str, _TrendingUpMassExitTrack] = {}

    def _track(self, strategy_key: str) -> _TrendingUpMassExitTrack:
        if strategy_key not in self._tracks:
            self._tracks[strategy_key] = _TrendingUpMassExitTrack()
        return self._tracks[strategy_key]

    def _reset_track(self, strategy_key: str) -> None:
        self._tracks[strategy_key] = _TrendingUpMassExitTrack()

    def _log_suppressed(
        self,
        *,
        strategy_key: str,
        regime: RegimeType,
        confidence: float | None,
        seconds_in_regime: float,
        consecutive_observations: int,
        cooldown_remaining: float,
        suppression_reason: str,
    ) -> None:
        logger.info(
            "ACEVAULT_MASS_EXIT_SUPPRESSED strategy=%s current_regime=%s confidence=%s "
            "seconds_in_regime=%.3f consecutive_observations=%d cooldown_remaining=%.3f "
            "suppression_reason=%s",
            strategy_key,
            regime.value,
            confidence if confidence is not None else "None",
            seconds_in_regime,
            consecutive_observations,
            cooldown_remaining,
            suppression_reason,
        )

    def regime_exit_all_trending_up(
        self,
        *,
        strategy_key: str,
        now: datetime,
        regime: RegimeType,
        confidence: float | None,
        config: dict[str, Any],
    ) -> bool:
        if now.tzinfo is None:
            now = now.replace(tzinfo=timezone.utc)

        pol = resolve_exit_policy(config, strategy_key)
        r = pol.get("regime") or {}
        close_all = bool(r.get("close_all_on_trending_up", True))

        if not close_all:
            self._reset_track(strategy_key)
            return False

        if regime != RegimeType.TRENDING_UP:
            self._reset_track(strategy_key)
            return False

        tr = self._track(strategy_key)
        if tr.streak_start_at is None:
            tr.streak_start_at = now
        tr.consecutive_trending_up += 1

        seconds_in = (now - tr.streak_start_at).total_seconds()
        consec = tr.consecutive_trending_up

        min_sec = float(r.get("min_seconds_in_trending_up_before_close_all", 0))
        min_conf_raw = r.get("min_confidence_before_close_all", None)
        min_conf_enabled = min_conf_raw is not None
        min_conf = float(min_conf_raw) if min_conf_enabled else 0.0
        min_consec = int(r.get("min_consecutive_trending_up_observations", 0))
        cooldown = float(r.get("mass_exit_cooldown_seconds", 0))

        cooldown_remaining = 0.0
        if tr.last_mass_exit_at is not None and cooldown > 0:
            elapsed = (now - tr.last_mass_exit_at).total_seconds()
            if elapsed < cooldown:
                cooldown_remaining = cooldown - elapsed
                self._log_suppressed(
                    strategy_key=strategy_key,
                    regime=regime,
                    confidence=confidence,
                    seconds_in_regime=seconds_in,
                    consecutive_observations=consec,
                    cooldown_remaining=cooldown_remaining,
                    suppression_reason="cooldown_active",
                )
                return False

        if min_conf_enabled:
            if confidence is None:
                self._log_suppressed(
                    strategy_key=strategy_key,
                    regime=regime,
                    confidence=confidence,
                    seconds_in_regime=seconds_in,
                    consecutive_observations=consec,
                    cooldown_remaining=cooldown_remaining,
                    suppression_reason="confidence_unavailable",
                )
                return False
            if confidence < min_conf:
                self._log_suppressed(
                    strategy_key=strategy_key,
                    regime=regime,
                    confidence=confidence,
                    seconds_in_regime=seconds_in,
                    consecutive_observations=consec,
                    cooldown_remaining=cooldown_remaining,
                    suppression_reason="insufficient_confidence",
                )
                return False

        if min_sec > 0 and seconds_in < min_sec:
            self._log_suppressed(
                strategy_key=strategy_key,
                regime=regime,
                confidence=confidence,
                seconds_in_regime=seconds_in,
                consecutive_observations=consec,
                cooldown_remaining=cooldown_remaining,
                suppression_reason="insufficient_seconds",
            )
            return False

        if min_consec > 0 and consec < min_consec:
            self._log_suppressed(
                strategy_key=strategy_key,
                regime=regime,
                confidence=confidence,
                seconds_in_regime=seconds_in,
                consecutive_observations=consec,
                cooldown_remaining=cooldown_remaining,
                suppression_reason="insufficient_consecutive",
            )
            return False

        tr.last_mass_exit_at = now
        tr.streak_start_at = None
        tr.consecutive_trending_up = 0
        logger.info(
            "ACEVAULT_MASS_EXIT_FIRED strategy=%s current_regime=%s confidence=%s "
            "seconds_in_regime=%.3f consecutive_observations=%d",
            strategy_key,
            regime.value,
            confidence if confidence is not None else "None",
            seconds_in,
            consec,
        )
        return True


def _side_from_signal(signal: Any) -> Side:
    s = getattr(signal, "side", "short")
    if str(s).lower() == "long":
        return "long"
    return "short"


def _strategy_key_from_engine(engine_id: str, config: dict[str, Any]) -> str:
    strategies = config.get("strategies") or {}
    for sk, row in strategies.items():
        if (row or {}).get("engine_id") == engine_id:
            return sk
    if engine_id == "acevault":
        return "acevault"
    if engine_id == "growi":
        return "growi_hf"
    if engine_id == "mc":
        return "mc_recovery"
    if engine_id == "btc_lanes":
        return "btc_lanes"
    return "acevault"


class LiveExitEngine:
    """
    Deterministic per-cycle exit evaluation with persistent state.
    Logs major transitions with EXIT_* prefixes.
    """

    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._store = ExitStateStore()

    @property
    def store(self) -> ExitStateStore:
        return self._store

    def evaluate_portfolio_positions(
        self,
        *,
        engine_id: str,
        positions: list[Any],
        current_prices: dict[str, float],
        regime_exit_all: bool,
        strategy_key: str | None = None,
    ) -> list[UniversalExit]:
        root = self._config.get("exits") or {}
        if not root.get("enabled", True):
            return []
        sk = strategy_key or _strategy_key_from_engine(engine_id, self._config)
        policy_cache: dict[str | None, dict[str, Any]] = {}
        out: list[UniversalExit] = []
        for pos in positions:
            coin = getattr(pos.signal, "coin", "").strip()
            price = current_prices.get(coin)
            if price is None or price <= 0:
                price = float(getattr(pos, "current_price", 0) or 0)
            if price <= 0:
                logger.warning(
                    "EXIT_SKIP_BAD_PRICE position_id=%s coin=%s", pos.position_id, coin
                )
                continue
            sig = pos.signal
            entry_regime = getattr(sig, "regime_at_entry", None)
            if isinstance(getattr(sig, "metadata", None), dict):
                entry_regime = sig.metadata.get("regime_at_entry", entry_regime)
            if entry_regime not in policy_cache:
                policy_cache[entry_regime] = resolve_exit_policy(
                    self._config, sk, regime=entry_regime
                )
            policy = policy_cache[entry_regime]
            side = _side_from_signal(sig)
            entry = float(getattr(sig, "entry_price", 0) or 0)
            sl = float(getattr(sig, "stop_loss_price", 0) or 0)
            tp = float(getattr(sig, "take_profit_price", 0) or 0)
            size_usd = float(getattr(sig, "position_size_usd", 0) or 0)
            if entry <= 0 or sl <= 0:
                logger.warning(
                    "EXIT_SKIP_INCOMPLETE_STATE position_id=%s coin=%s", pos.position_id, coin
                )
                continue
            st = self._store.ensure_initial(
                position_id=pos.position_id,
                coin=coin,
                side=side,
                strategy_key=sk,
                entry_price=entry,
                initial_stop_price=sl,
                take_profit_price=tp,
                position_size_usd=size_usd,
                opened_at=pos.opened_at,
            )
            update_extremes_and_peak(st, price)
            ev = evaluate_exit(
                st,
                price,
                policy,
                regime_exit_all=regime_exit_all,
            )
            if ev.should_exit:
                # Capture R-multiple diagnostics BEFORE store.remove(); the state object
                # holds peak favorable excursion across every prior evaluation tick and is
                # the only source of truth for peak_r_capture_ratio downstream.
                peak_r = float(st.peak_r_multiple)
                realized_r = float(unrealized_r_multiple(st, price))
                capture = (realized_r / peak_r) if peak_r > 0 else None
                logger.info(
                    "%s position_id=%s coin=%s side=%s reason=%s pnl_usd=%.4f pnl_pct=%.5f",
                    ev.log_tag,
                    pos.position_id,
                    coin,
                    side,
                    ev.exit_reason,
                    ev.pnl_usd,
                    ev.pnl_pct,
                )
                logger.info(
                    "EXIT_R_METRICS position_id=%s coin=%s side=%s reason=%s "
                    "peak_r=%.4f realized_r=%.4f capture=%s",
                    pos.position_id,
                    coin,
                    side,
                    ev.exit_reason,
                    peak_r,
                    realized_r,
                    f"{capture:.4f}" if capture is not None else "None",
                )
                out.append(
                    UniversalExit(
                        position_id=pos.position_id,
                        coin=coin,
                        exit_price=price,
                        exit_reason=ev.exit_reason,
                        pnl_usd=ev.pnl_usd,
                        pnl_pct=ev.pnl_pct,
                        hold_duration_seconds=ev.hold_duration_seconds,
                        entry_price=entry,
                        stop_loss_price=sl,
                        take_profit_price=tp,
                        engine_id=engine_id,
                        peak_r_multiple=peak_r,
                        realized_r_multiple=realized_r,
                        position_size_usd=size_usd if size_usd > 0 else None,
                    )
                )
                self._store.remove(pos.position_id)
        return out


def regime_exit_trending_up_acevault(
    config: dict[str, Any], strategy_key: str = "acevault"
) -> bool:
    pol = resolve_exit_policy(config, strategy_key)
    r = pol.get("regime") or {}
    return bool(r.get("close_all_on_trending_up", True))
