from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)


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
            max_correlated = config.get("risk", {}).get("max_correlated_longs", 3)
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
            state = hl_client.info.user_state(address)
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
