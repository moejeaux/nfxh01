from __future__ import annotations

from datetime import datetime, timezone

from src.exits.models import PositionExitState, Side


class ExitStateStore:
    """In-memory exit state keyed by position_id."""

    def __init__(self) -> None:
        self._by_id: dict[str, PositionExitState] = {}

    def get(self, position_id: str) -> PositionExitState | None:
        return self._by_id.get(position_id)

    def upsert(self, state: PositionExitState) -> None:
        state.last_updated_at = datetime.now(timezone.utc)
        self._by_id[state.position_id] = state

    def remove(self, position_id: str) -> None:
        self._by_id.pop(position_id, None)

    def ensure_initial(
        self,
        *,
        position_id: str,
        coin: str,
        side: Side,
        strategy_key: str,
        entry_price: float,
        initial_stop_price: float,
        take_profit_price: float,
        position_size_usd: float,
        opened_at: datetime,
    ) -> PositionExitState:
        existing = self.get(position_id)
        if existing is not None:
            return existing
        risk = abs(initial_stop_price - entry_price)
        if risk <= 0:
            risk = max(entry_price * 1e-9, 1e-12)
        px0 = entry_price
        st = PositionExitState(
            position_id=position_id,
            coin=coin,
            side=side,
            strategy_key=strategy_key,
            entry_price=entry_price,
            initial_stop_price=initial_stop_price,
            take_profit_price=take_profit_price,
            position_size_usd=position_size_usd,
            opened_at=opened_at,
            initial_risk_per_unit=risk,
            working_stop_price=initial_stop_price,
            highest_price_seen=px0,
            lowest_price_seen=px0,
            peak_r_multiple=0.0,
        )
        self.upsert(st)
        return st
