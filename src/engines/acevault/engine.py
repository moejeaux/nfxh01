from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

from src.engines.acevault.entry import EntryManager
from src.engines.acevault.exit import AceExit, ExitManager
from src.engines.acevault.models import AcePosition, AceSignal
from src.engines.acevault.scanner import AltScanner
from src.regime.detector import RegimeDetector
from src.regime.models import RegimeType

logger = logging.getLogger(__name__)


class AceVaultEngine:
    def __init__(
        self,
        config: dict,
        hl_client: Any,
        regime_detector: RegimeDetector,
        risk_layer: Any,
        degen_executor: Any,
        kill_switch: Any = None,
    ) -> None:
        self._config = config
        self._hl_client = hl_client
        self.regime_detector = regime_detector
        self.risk_layer = risk_layer
        self.degen_executor = degen_executor
        self._kill_switch = kill_switch

        self._open_positions: list[AcePosition] = []
        self._cycle_running: bool = False
        self._scanner = AltScanner(config, hl_client)
        self._entry_manager = EntryManager(config, risk_layer.portfolio_state)
        self._exit_manager = ExitManager(config)

    async def run_cycle(self) -> list[AceExit | AceSignal]:
        if self._cycle_running:
            logger.warning(
                "ACEVAULT_CYCLE_SKIPPED reason=previous_cycle_running"
            )
            return []

        self._cycle_running = True
        try:
            return await self._run_cycle_inner()
        finally:
            self._cycle_running = False

    async def _run_cycle_inner(self) -> list[AceExit | AceSignal]:
        results: list[AceExit | AceSignal] = []

        market_data = await self._fetch_market_data()
        regime_state = self.regime_detector.detect(market_data=market_data)
        weight = self._get_regime_weight(regime_state.regime)

        logger.info(
            "ACEVAULT_CYCLE_START regime=%s weight=%.2f open_positions=%d",
            regime_state.regime.value,
            weight,
            len(self._open_positions),
        )

        if weight == 0.0:
            logger.info("ACEVAULT_ENGINE_OFF regime=%s", regime_state.regime.value)
            return []

        # --- exits first (always run, even if kill switch is active) ---
        current_prices = await self._fetch_current_prices()
        self._update_position_prices(current_prices)

        exits = self._exit_manager.check_exits(
            self._open_positions, current_prices, regime_state.regime
        )
        for exit in exits:
            await self.degen_executor.close(exit)
            self._open_positions = [
                p for p in self._open_positions if p.position_id != exit.position_id
            ]
        results.extend(exits)

        # --- kill switch: stop new entries, but exits above already ran ---
        if self._kill_switch is not None and self._kill_switch.is_active("acevault"):
            logger.warning(
                "ACEVAULT_KILL_SWITCH_ACTIVE entries_blocked=True exits_processed=%d",
                len(exits),
            )
            return results

        # --- new entries ---
        candidates = self._scanner.scan()
        if not candidates:
            logger.info("ACEVAULT_NO_CANDIDATES_THIS_CYCLE")
            return results

        for candidate in candidates:
            signal = self._entry_manager.should_enter(candidate, regime_state, weight)
            if signal is None:
                continue

            risk_decision = self.risk_layer.validate(signal, "acevault")
            if not risk_decision.approved:
                logger.info(
                    "ACEVAULT_RISK_REJECTED coin=%s reason=%s",
                    signal.coin,
                    risk_decision.reason,
                )
                continue

            await self.degen_executor.submit(signal)

            position = AcePosition(
                position_id=str(uuid.uuid4()),
                signal=signal,
                opened_at=datetime.now(timezone.utc),
                current_price=signal.entry_price,
                unrealized_pnl_usd=0.0,
                status="open",
            )
            self._open_positions.append(position)
            results.append(signal)

        signal_count = sum(1 for r in results if isinstance(r, AceSignal))
        exit_count = sum(1 for r in results if isinstance(r, AceExit))
        logger.info(
            "ACEVAULT_CYCLE_END signals=%d exits=%d open_positions=%d",
            signal_count,
            exit_count,
            len(self._open_positions),
        )

        return results

    def _get_regime_weight(self, regime: RegimeType) -> float:
        return self._config["acevault"]["regime_weights"][regime.value.lower()]

    def _update_position_prices(self, current_prices: dict[str, float]) -> None:
        for pos in self._open_positions:
            price = current_prices.get(pos.signal.coin)
            if price is not None:
                pos.current_price = price
                entry = pos.signal.entry_price
                pos.unrealized_pnl_usd = (
                    (entry - price) / entry
                ) * pos.signal.position_size_usd

    async def _fetch_market_data(self) -> dict:
        # TODO: Implement real market data fetching via hl_client
        return {
            "btc_1h_return": 0.0,
            "btc_4h_return": 0.0,
            "btc_vol_1h": 0.004,
        }

    async def _fetch_current_prices(self) -> dict[str, float]:
        # TODO: Implement real price fetching via hl_client
        return {}
