from __future__ import annotations

import logging
from typing import Any

from src.exits.models import Side, UniversalExit
from src.exits.policy_config import resolve_exit_policy
from src.exits.policies import evaluate_exit, update_extremes_and_peak
from src.exits.state import ExitStateStore

logger = logging.getLogger(__name__)


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
        policy = resolve_exit_policy(self._config, sk)
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
