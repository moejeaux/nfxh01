from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


def engine_id_to_strategy_key(config: dict | None, engine_id: str) -> str | None:
    if not config:
        return None
    for sk, row in (config.get("strategies") or {}).items():
        if isinstance(row, dict) and str(row.get("engine_id", "")) == str(engine_id):
            return str(sk)
    return None


def _user_state_compat(hl_client: Any, address: str) -> Any:
    """Prefer nested ``client.info.user_state`` (tests / wrappers), else ``client.user_state`` (HL SDK)."""
    inner = getattr(hl_client, "info", None)
    if inner is not None and callable(getattr(inner, "user_state", None)):
        return inner.user_state(address)
    direct = getattr(hl_client, "user_state", None)
    if callable(direct):
        return direct(address)
    raise AttributeError("hl_client has no user_state")


@dataclass
class RiskDecision:
    approved: bool
    reason: str


class PortfolioState:
    def __init__(self) -> None:
        self._positions: dict[str, dict[str, Any]] = {}
        self._closed_positions: list = []
        self._equity_history: list[tuple[datetime, float]] = []

    def register_position(self, engine_id: str, position: Any) -> None:
        self._positions.setdefault(engine_id, {})[position.position_id] = position
        logger.info(
            "RISK_POSITION_OPEN engine=%s pos=%s coin=%s size=%.2f",
            engine_id, position.position_id,
            position.signal.coin, position.signal.position_size_usd,
        )

    def close_position(self, engine_id: str, position_id: str, exit: Any) -> None:
        engine_positions = self._positions.get(engine_id, {})
        position = engine_positions.pop(position_id, None)
        if position is None:
            logger.warning(
                "RISK_POSITION_CLOSE_MISS engine=%s pos=%s not found",
                engine_id, position_id,
            )
            return
        self._closed_positions.append({
            "engine_id": engine_id,
            "position": position,
            "exit": exit,
            "closed_at": datetime.now(timezone.utc),
        })
        logger.info(
            "RISK_POSITION_CLOSED engine=%s pos=%s pnl=%.2f",
            engine_id, position_id, exit.pnl_usd,
        )

    def get_open_positions(self, engine_id: str | None = None) -> list:
        if engine_id is not None:
            return list(self._positions.get(engine_id, {}).values())
        all_positions = []
        for positions in self._positions.values():
            all_positions.extend(positions.values())
        return all_positions

    def get_last_closed_exit_for_engine_coin(self, engine_id: str, coin: str) -> dict | None:
        """Most recent closed record for ``engine_id`` and perp ``coin`` (by ``closed_at``)."""
        want = (coin or "").strip().upper()
        best: dict | None = None
        best_ts: datetime | None = None
        for rec in self._closed_positions:
            if rec.get("engine_id") != engine_id:
                continue
            pos = rec.get("position")
            c = getattr(getattr(pos, "signal", None), "coin", None)
            if c is None:
                c = getattr(pos, "coin", None)
            if str(c).strip().upper() != want:
                continue
            ts = rec.get("closed_at")
            if not isinstance(ts, datetime):
                continue
            if best_ts is None or ts > best_ts:
                best_ts = ts
                best = rec
        return best

    def get_gross_exposure(self) -> float:
        total = 0.0
        for positions in self._positions.values():
            for pos in positions.values():
                total += abs(pos.signal.position_size_usd)
        return total

    def get_net_exposure(self) -> float:
        net = 0.0
        for positions in self._positions.values():
            for pos in positions.values():
                size = pos.signal.position_size_usd
                if pos.signal.side == "long":
                    net += size
                else:
                    net -= size
        return net

    def get_engine_pnl(self, engine_id: str, window_hours: int) -> float:
        cutoff = datetime.now(timezone.utc).timestamp() - (window_hours * 3600)
        total = 0.0
        for record in self._closed_positions:
            if record["engine_id"] != engine_id:
                continue
            if record["closed_at"].timestamp() >= cutoff:
                total += record["exit"].pnl_usd
        return total

    def get_portfolio_drawdown_24h(self) -> float:
        if not self._equity_history:
            return 0.0
        cutoff = datetime.now(timezone.utc).timestamp() - 86400
        recent = [eq for ts, eq in self._equity_history if ts.timestamp() >= cutoff]
        if not recent:
            return 0.0
        peak = max(recent)
        current = recent[-1]
        if peak <= 0:
            return 0.0
        return (peak - current) / peak

    def record_equity_snapshot(self, equity: float) -> None:
        self._equity_history.append((datetime.now(timezone.utc), equity))

    def is_correlated_overloaded(self, new_signal: Any, config: dict | None = None) -> bool:
        max_correlated = 3
        if config is not None:
            risk = config.get("risk") or {}
            if "max_correlated_longs" in risk and risk["max_correlated_longs"] is None:
                return False
            max_correlated = risk.get("max_correlated_longs", 3)
        if new_signal.side != "long":
            return False
        open_long_count = 0
        for positions in self._positions.values():
            for pos in positions.values():
                if pos.signal.side == "long":
                    open_long_count += 1
        overloaded = open_long_count >= max_correlated
        if overloaded:
            logger.warning(
                "RISK_CORRELATED_OVERLOAD open_longs=%d max=%d",
                open_long_count, max_correlated,
            )
        return overloaded

    def sync_from_hl(self, hl_client: Any, address: str) -> None:
        try:
            state = _user_state_compat(hl_client, address)
        except Exception as e:
            logger.error("PORTFOLIO_SYNC_FAILED error=%s", str(e))
            return

        known_coins: set[str] = set()
        for engine_positions in self._positions.values():
            for pos in engine_positions.values():
                known_coins.add(pos.signal.coin)

        recovered = 0
        for ap in state.get("assetPositions", []):
            pos_data = ap.get("position", {})
            szi = float(pos_data.get("szi", 0))
            if szi == 0:
                continue
            coin = pos_data.get("coin", "")
            if coin in known_coins:
                continue

            side = "long" if szi > 0 else "short"
            size_usd = abs(szi) * float(pos_data.get("entryPx", 0))

            recovered_pos = _RecoveredPosition(
                position_id=str(uuid.uuid4()),
                coin=coin,
                side=side,
                position_size_usd=size_usd,
            )
            self.register_position("recovered", recovered_pos)
            recovered += 1
            logger.info(
                "PORTFOLIO_RECOVERED_POSITION coin=%s size=%.2f side=%s",
                coin, size_usd, side,
            )

        existing = sum(len(ep) for ep in self._positions.values()) - recovered
        logger.info(
            "PORTFOLIO_SYNC_COMPLETE recovered=%d existing=%d",
            recovered, existing,
        )

    def reconcile_open_positions_vs_hl(self, hl_client: Any, address: str) -> None:
        """Compare in-memory open positions to HL ``user_state``; log ``RISK_RECONCILE_*`` on gaps (no auto-delete)."""
        try:
            state = _user_state_compat(hl_client, address)
        except Exception as e:
            logger.error("RISK_RECONCILE_FAILED error=%s", str(e))
            return

        hl_coins: set[str] = set()
        for ap in state.get("assetPositions", []):
            pos_data = ap.get("position", {})
            szi = float(pos_data.get("szi", 0))
            if szi == 0:
                continue
            coin = pos_data.get("coin", "")
            if coin:
                hl_coins.add(coin)

        for engine_id, pmap in self._positions.items():
            for position_id, pos in pmap.items():
                coin = pos.signal.coin
                if coin in hl_coins:
                    continue
                logger.warning(
                    "RISK_RECONCILE_MEMORY_NOT_ON_VENUE engine_id=%s position_id=%s coin=%s "
                    "(tracked in memory; no open HL perp — may have been closed externally)",
                    engine_id,
                    position_id,
                    coin,
                )

        tracked = {p.signal.coin for _e, pmap in self._positions.items() for p in pmap.values()}
        for c in hl_coins:
            if c not in tracked:
                logger.info(
                    "RISK_RECONCILE_VENUE_NOT_IN_MEMORY coin=%s "
                    "(HL shows position; run sync_from_hl to import or verify attribution)",
                    c,
                )

    def resolve_btc_sensitivity_tier(self, coin: str, engine_id: str, config: dict) -> str:
        pol = config.get("btc_context_policy") or {}
        cj = (pol.get("coin_sensitivity") or {}).get((coin or "").strip().upper())
        if cj in ("low", "medium", "high"):
            return str(cj)
        sk = engine_id_to_strategy_key(config, engine_id)
        row = (config.get("strategies") or {}).get(sk) if sk else None
        tier = (row or {}).get("btc_sensitivity") if isinstance(row, dict) else None
        if tier in ("low", "medium", "high"):
            return str(tier)
        return "medium"

    def btc_sensitivity_weight(self, tier: str, config: dict) -> float:
        pol = (config.get("btc_context_policy") or {}).get("portfolio_beta") or {}
        wmap = pol.get("sensitivity_weight") or {}
        return float(wmap.get(tier, wmap.get("medium", 1.0)))

    def portfolio_btc_weighted_exposure(self, config: dict) -> tuple[float, float, int]:
        long_b = 0.0
        short_b = 0.0
        high_n = 0
        for eid, pmap in self._positions.items():
            for pos in pmap.values():
                coin = pos.signal.coin
                tier = self.resolve_btc_sensitivity_tier(coin, eid, config)
                w = self.btc_sensitivity_weight(tier, config)
                usd = abs(float(pos.signal.position_size_usd))
                contrib = usd * w
                if pos.signal.side == "long":
                    long_b += contrib
                else:
                    short_b += contrib
                if tier == "high":
                    high_n += 1
        return long_b, short_b, high_n

    def get_estimated_btc_beta_long(self, config: dict) -> float:
        long_b, _, _ = self.portfolio_btc_weighted_exposure(config)
        return long_b

    def get_estimated_btc_beta_short(self, config: dict) -> float:
        _, short_b, _ = self.portfolio_btc_weighted_exposure(config)
        return short_b

    def get_num_high_beta_positions(self, config: dict) -> int:
        _, _, n = self.portfolio_btc_weighted_exposure(config)
        return n

    def would_exceed_btc_beta_cap(
        self,
        signal: Any,
        proposed_size_usd: float,
        engine_id: str,
        config: dict,
    ) -> bool:
        pol = (config.get("btc_context_policy") or {}).get("portfolio_beta") or {}
        if not pol.get("enabled", True):
            return False
        max_l = float(pol.get("max_long", 1e18))
        max_s = float(pol.get("max_short", 1e18))
        tier = self.resolve_btc_sensitivity_tier(signal.coin, engine_id, config)
        w = self.btc_sensitivity_weight(tier, config)
        long_b, short_b, _ = self.portfolio_btc_weighted_exposure(config)
        add = float(proposed_size_usd) * w
        if signal.side == "long":
            return long_b + add > max_l
        return short_b + add > max_s


@dataclass
class _RecoveredPosition:
    """Lightweight stand-in for positions recovered from HL on startup."""
    position_id: str
    coin: str
    side: str
    position_size_usd: float

    @property
    def signal(self) -> _RecoveredPosition:
        return self
