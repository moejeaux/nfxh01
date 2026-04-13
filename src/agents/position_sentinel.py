"""PositionSentinel Agent — monitors open DEX positions every 30 seconds.

Tracks thesis health, deployer activity, holder changes, price action.
Publishes thesis_monitor_update and exit_warning events.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from src.adapters.goldrush.client import GoldRushClient
from src.domain.position import SentinelPositionState
from src.events.bus import EventBus
from src.events.schemas import (
    ExitWarningEvent,
    PositionOpenedEvent,
    SellExecutedEvent,
    ThesisMonitorUpdateEvent,
)
from src.persistence.dex_store import DexStore

logger = logging.getLogger(__name__)

MONITORING_INTERVAL_S = 30

# Trailing stop thresholds (starter defaults)
HARD_STOP_PCT = 0.15
BREAKEVEN_TRIGGER_PCT = 0.10
BREAKEVEN_STOP_PCT = 0.0
PROFIT_TRAIL_TRIGGER_PCT = 0.20
PROFIT_TRAIL_STOP_PCT = 0.08
TP1_PCT = 0.25
TP1_SELL_PCT = 50.0
TP1_NEW_STOP_PCT = 0.15
TP2_PCT = 0.50

# Thesis health thresholds
DEPLOYER_SELL_URGENT_PCT = 50.0
DEPLOYER_SELL_ADVISORY_PCT = 30.0
VOLUME_COLLAPSE_RATIO = 0.20
HOLDER_DECLINE_PCT = 15.0


class PositionSentinel:
    """Monitors all open DEX positions and evaluates thesis health.

    Runs a 30s polling loop plus event-driven updates from the bus.
    """

    def __init__(
        self,
        goldrush: GoldRushClient,
        bus: EventBus,
        store: DexStore,
    ):
        self._gr = goldrush
        self._bus = bus
        self._store = store
        self._positions: dict[str, SentinelPositionState] = {}

    async def handle_position_opened(self, event: PositionOpenedEvent) -> None:
        """Track a newly opened position."""
        state = SentinelPositionState(
            position_id=event.position_id,
            pair_id=event.pair_id,
            token_address=event.token_address,
            entry_price=event.entry_price,
            entry_size_usd=event.size_usd,
            entry_size_tokens=event.size_tokens,
            hard_stop_price=event.hard_stop_price,
            tp1_price=event.tp1_price,
            tp2_price=event.tp2_price,
            current_price=event.entry_price,
            peak_price=event.entry_price,
            thesis_snapshot=event.thesis_snapshot,
        )
        self._positions[event.position_id] = state
        logger.info("Sentinel tracking: %s (%s)", event.position_id, event.pair_id[:20])

    async def handle_position_closed(self, event: SellExecutedEvent) -> None:
        """Stop tracking a closed position."""
        if event.size_pct_sold >= 100:
            self._positions.pop(event.position_id, None)
            logger.info("Sentinel untracked: %s", event.position_id)

    async def run_monitoring_loop(self) -> None:
        """Main monitoring loop — runs every 30s."""
        logger.info("PositionSentinel monitoring loop started")
        while True:
            try:
                await self._evaluate_all()
            except Exception as e:
                logger.error("Sentinel loop error: %s", e)
            await asyncio.sleep(MONITORING_INTERVAL_S)

    async def _evaluate_all(self) -> None:
        """Evaluate all open positions."""
        db_positions = self._store.get_open_positions()

        for pos in db_positions:
            pid = pos["position_id"]
            state = self._positions.get(pid)
            if state is None:
                state = SentinelPositionState(
                    position_id=pid,
                    pair_id=pos["pair_id"],
                    token_address=pos.get("token_address", ""),
                    entry_price=pos["entry_price"],
                    entry_size_usd=pos["size_usd"],
                    entry_size_tokens=pos["size_tokens"],
                    hard_stop_price=pos["hard_stop_price"],
                    tp1_price=pos["tp1_price"],
                    tp2_price=pos["tp2_price"],
                )
                self._positions[pid] = state

            await self._evaluate_position(state)

    async def _evaluate_position(self, state: SentinelPositionState) -> None:
        """Evaluate a single position — fetch data, check triggers, publish events."""
        # Fetch current price via GoldRush token balances or OHLCV
        try:
            prices = await self._gr.get_token_prices(state.token_address)
            if prices.items:
                latest = prices.items[-1]
                if latest.price and latest.price > 0:
                    state.update_price(latest.price)
        except Exception as e:
            logger.debug("Price fetch failed for %s: %s", state.position_id, e)

        # Update trailing stop based on current gains
        self._update_trailing_stop(state)

        # Check hard stop
        if state.current_price > 0 and state.current_price <= state.hard_stop_price:
            await self._emit_exit_warning(
                state, "critical", ["hard_stop_price_hit"], "full",
            )
            return

        # Check TP levels
        if not state.tp1_hit and state.current_price >= state.tp1_price > 0:
            state.tp1_hit = True
            await self._emit_exit_warning(
                state, "urgent", ["tp1_reached"], f"partial_{TP1_SELL_PCT:.0f}pct",
            )

        if state.current_price >= state.tp2_price > 0:
            await self._emit_exit_warning(
                state, "critical", ["tp2_reached"], "full",
            )
            return

        # Evaluate thesis health
        flags: list[str] = []
        thesis_health = "intact"

        if state.unrealized_pnl_pct < -10:
            flags.append("significant_loss")
        if state.volume_trend == "collapsed":
            flags.append("volume_collapse")

        if len(flags) >= 2:
            thesis_health = "weakening"
        if "significant_loss" in flags and len(flags) >= 2:
            thesis_health = "invalidated"

        state.thesis_health = thesis_health
        state.flags = flags

        # Publish monitoring update
        update = ThesisMonitorUpdateEvent(
            position_id=state.position_id,
            current_price=state.current_price,
            unrealized_pnl_pct=state.unrealized_pnl_pct,
            peak_price=state.peak_price,
            smart_money_still_holding=state.smart_money_still_holding,
            volume_trending=state.volume_trend,
            thesis_health=thesis_health,
            flags=flags,
        )
        await self._bus.publish("thesis_monitor_update", update)

        # Save state to DB
        self._store.save_position_state(
            state.position_id, state.current_price,
            state.unrealized_pnl_pct, thesis_health, flags,
            {"peak_price": state.peak_price, "tp1_hit": state.tp1_hit},
        )

        # Emit exit warnings for soft triggers
        if flags:
            severity = "advisory"
            if len(flags) >= 2:
                severity = "urgent"
            if thesis_health == "invalidated":
                severity = "critical"

            await self._emit_exit_warning(
                state, severity, flags,
                "full" if severity == "critical" else "partial_50pct",
            )

    def _update_trailing_stop(self, state: SentinelPositionState) -> None:
        """Ratchet the trailing stop upward as price moves favorably."""
        if state.entry_price <= 0:
            return
        gain_pct = (state.current_price - state.entry_price) / state.entry_price

        if state.tp1_hit:
            state.hard_stop_price = max(
                state.hard_stop_price,
                state.entry_price * (1 + TP1_NEW_STOP_PCT),
            )
        elif gain_pct >= PROFIT_TRAIL_TRIGGER_PCT:
            state.hard_stop_price = max(
                state.hard_stop_price,
                state.entry_price * (1 + PROFIT_TRAIL_STOP_PCT),
            )
        elif gain_pct >= BREAKEVEN_TRIGGER_PCT:
            state.hard_stop_price = max(
                state.hard_stop_price,
                state.entry_price * (1 + BREAKEVEN_STOP_PCT),
            )

    async def _emit_exit_warning(
        self, state: SentinelPositionState,
        severity: str, triggers: list[str], recommended_exit: str,
    ) -> None:
        warning = ExitWarningEvent(
            position_id=state.position_id,
            severity=severity,
            triggers=triggers,
            recommended_exit=recommended_exit,
            current_pnl_pct=state.unrealized_pnl_pct,
        )
        await self._bus.publish("exit_warning", warning)
        self._store.save_exit_recommendation(
            state.position_id, "position_sentinel",
            severity, triggers, recommended_exit, state.unrealized_pnl_pct,
        )
        logger.info(
            "EXIT WARNING [%s] %s: %s → %s (pnl=%.1f%%)",
            severity.upper(), state.position_id,
            triggers, recommended_exit, state.unrealized_pnl_pct,
        )

    def status(self) -> dict[str, Any]:
        return {
            "tracked_positions": len(self._positions),
            "positions": {
                pid: {
                    "pair_id": s.pair_id[:20],
                    "pnl_pct": round(s.unrealized_pnl_pct, 2),
                    "thesis": s.thesis_health,
                    "tp1_hit": s.tp1_hit,
                }
                for pid, s in self._positions.items()
            },
        }
