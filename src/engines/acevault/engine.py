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
        journal: Any = None,
        fathom_advisor: Any = None,
    ) -> None:
        self._config = config
        self._hl_client = hl_client
        self.regime_detector = regime_detector
        self.risk_layer = risk_layer
        self.degen_executor = degen_executor
        self._kill_switch = kill_switch
        self._journal = journal
        self._fathom_advisor = fathom_advisor

        self._open_positions: list[AcePosition] = []
        self._cycle_running: bool = False
        self._scanner = AltScanner(config, hl_client)
        self._entry_manager = EntryManager(config, risk_layer.portfolio_state)
        self._exit_manager = ExitManager(config)

    async def run_cycle(self) -> list[AceExit | AceSignal]:
        if self._cycle_running:
            logger.warning("ACEVAULT_CYCLE_SKIPPED reason=previous_cycle_running")
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

            # Journal exit outcome
            if self._journal is not None:
                try:
                    await self._journal.log_exit(
                        decision_id=exit.position_id,
                        exit=exit,
                        regime_at_close=regime_state.regime.value,
                    )
                    logger.info(
                        "DECISION_JOURNAL_EXIT_LOGGED id=%s coin=%s pnl_pct=%.3f",
                        exit.position_id,
                        exit.coin,
                        exit.pnl_pct,
                    )
                except Exception as e:
                    logger.warning(
                        "DECISION_JOURNAL_EXIT_FAILED coin=%s error=%s",
                        exit.coin,
                        e,
                    )

            self._open_positions = [
                p for p in self._open_positions if p.position_id != exit.position_id
            ]

            if self.risk_layer.portfolio_state is not None:
                self.risk_layer.portfolio_state.close_position(
                    "acevault", exit.position_id, exit
                )

        results.extend(exits)

        # --- kill switch: stop new entries, exits above already ran ---
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

            # --- Fathom advisory ---
            fathom_result = {
                "size_mult": 1.0,
                "reasoning": "fathom_disabled",
                "source": "deterministic",
            }

            if self._fathom_advisor is not None:
                try:
                    prior_context = []
                    if self._journal is not None:
                        prior_context = await self._journal.get_similar_decisions(
                            coin=signal.coin,
                            regime=regime_state.regime.value,
                            limit=5,
                        )
                    prior_str = "\n".join([
                        f"- mult={d.get('fathom_size_mult', 1.0)}, "
                        f"pnl={d.get('pnl_pct', 0):.2%}, "
                        f"regime={d.get('regime')}"
                        for d in prior_context
                        if d.get("pnl_pct") is not None
                    ]) or "No prior decisions in this regime yet."

                    fathom_result = await self._fathom_advisor.advise_acevault(
                        signal=signal,
                        regime_state=regime_state,
                        prior_context=prior_str,
                    )

                    signal.position_size_usd = (
                        signal.position_size_usd * fathom_result["size_mult"]
                    )
                    logger.info(
                        "FATHOM_SIZE_APPLIED coin=%s mult=%.2f source=%s reasoning=%s",
                        signal.coin,
                        fathom_result["size_mult"],
                        fathom_result["source"],
                        fathom_result.get("reasoning", ""),
                    )
                except Exception as e:
                    logger.warning(
                        "FATHOM_ADVISORY_FAILED coin=%s error=%s — using default size",
                        signal.coin,
                        e,
                    )

            # --- submit to DegenClaw ---
            await self.degen_executor.submit(signal)

            # --- journal entry ---
            decision_id = str(uuid.uuid4())
            if self._journal is not None:
                try:
                    decision_id = await self._journal.log_entry(
                        signal=signal,
                        fathom_result=fathom_result,
                    )
                    logger.info(
                        "DECISION_JOURNAL_ENTRY_LOGGED id=%s coin=%s regime=%s",
                        decision_id,
                        signal.coin,
                        regime_state.regime.value,
                    )
                except Exception as e:
                    logger.warning(
                        "DECISION_JOURNAL_ENTRY_FAILED coin=%s error=%s",
                        signal.coin,
                        e,
                    )

            position = AcePosition(
                position_id=decision_id,
                signal=signal,
                opened_at=datetime.now(timezone.utc),
                current_price=signal.entry_price,
                unrealized_pnl_usd=0.0,
                status="open",
            )
            self._open_positions.append(position)

            if self.risk_layer.portfolio_state is not None:
                self.risk_layer.portfolio_state.register_position("acevault", position)

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
        try:
            mids = self._hl_client.info.all_mids()
            btc_price = float(mids.get("BTC", 0))
            _ = btc_price  # reserved for real BTC candle fetch
        except Exception:
            pass
        # Real BTC candle-based returns wired in next iteration
        return {
            "btc_1h_return": 0.0,
            "btc_4h_return": 0.0,
            "btc_vol_1h": 0.004,
        }

    async def _fetch_current_prices(self) -> dict[str, float]:
        try:
            mids = self._hl_client.info.all_mids()
            return {coin: float(price) for coin, price in mids.items()}
        except Exception as e:
            logger.warning("ACEVAULT_PRICE_FETCH_FAILED error=%s", e)
            return {}
