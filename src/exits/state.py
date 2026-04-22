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
        reference_atr: float = 0.0,
        bar_interval_seconds: float = 300.0,
        range_high: float | None = None,
        range_low: float | None = None,
        range_target_buffer_frac: float = 0.02,
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
            reference_atr=float(reference_atr or 0.0),
            bar_interval_seconds=float(bar_interval_seconds or 300.0),
            range_high=range_high,
            range_low=range_low,
            range_target_buffer_frac=float(range_target_buffer_frac or 0.02),
        )
        self.upsert(st)
        return st
