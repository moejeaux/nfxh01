"""
Track A: normalized intents → UnifiedRiskLayer.validate → DegenClaw submit → PortfolioState.register.

Uses the same risk gate as AceVault; does not bypass ``UnifiedRiskLayer``.
Optional ``DecisionJournal.log_track_a_entry`` for audit parity with AceVault rows.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from src.acp.degen_claw import AcpTradeRequest
from src.nxfh01.orchestration.track_a_models import TrackAOpenPosition, TrackARiskSignal
from src.nxfh01.orchestration.types import NormalizedEntryIntent
from src.risk.portfolio_state import PortfolioState
from src.risk.unified_risk import UnifiedRiskLayer

logger = logging.getLogger(__name__)


@dataclass
class TrackAExecutionSummary:
    risk_rejected: int
    submit_failed: int
    submitted: int
    registered: int
    journal_logged: int
    journal_failed: int


class TrackAExecutor:
    def __init__(
        self,
        config: dict,
        risk_layer: UnifiedRiskLayer,
        portfolio_state: PortfolioState,
        degen_executor: Any,
        hl_client: Any,
        journal: Any | None = None,
    ) -> None:
        self._config = config
        self._risk_layer = risk_layer
        self._portfolio_state = portfolio_state
        self._degen = degen_executor
        self._hl = hl_client
        self._journal = journal

    def _resolve_reference_price(self, intent: NormalizedEntryIntent) -> float | None:
        if intent.entry_reference_price is not None and intent.entry_reference_price > 0:
            return float(intent.entry_reference_price)
        try:
            mids = self._hl.all_mids()
            px = mids.get(intent.coin.strip())
            if px is None:
                return None
            return float(px)
        except Exception as e:
            logger.warning(
                "ORCH_TRACK_A_PRICE_FAIL coin=%s error=%s",
                intent.coin,
                e,
            )
            return None

    def _leverage_for(self, intent: NormalizedEntryIntent) -> int:
        sk = intent.strategy_key
        strategies = self._config.get("strategies") or {}
        row = strategies.get(sk) or {}
        lev = intent.leverage
        if lev >= 1:
            return int(lev)
        cfg_lev = row.get("default_leverage")
        if cfg_lev is not None:
            return max(1, int(cfg_lev))
        return 1

    async def execute(self, intents: list[NormalizedEntryIntent]) -> TrackAExecutionSummary:
        risk_rejected = 0
        submit_failed = 0
        submitted = 0
        registered = 0
        journal_logged = 0
        journal_failed = 0

        for intent in intents:
            risk_signal = TrackARiskSignal(
                coin=intent.coin.strip(),
                side=intent.side,
                position_size_usd=float(intent.position_size_usd),
                strategy_key=str(intent.strategy_key),
                leverage=self._leverage_for(intent),
                metadata=dict(intent.metadata or {}),
            )
            decision = self._risk_layer.validate(risk_signal, intent.engine_id)
            if not decision.approved:
                risk_rejected += 1
                logger.info(
                    "ORCH_TRACK_A_RISK_REJECT coin=%s engine_id=%s reason=%s",
                    intent.coin,
                    intent.engine_id,
                    decision.reason,
                )
                continue

            ref_px = self._resolve_reference_price(intent)
            if ref_px is None or ref_px <= 0:
                submit_failed += 1
                logger.warning(
                    "ORCH_TRACK_A_SKIP coin=%s reason=no_reference_price",
                    intent.coin,
                )
                continue

            sl = intent.stop_loss_price
            tp = intent.take_profit_price
            scaled_usd = float(risk_signal.position_size_usd)
            lev = self._leverage_for(intent)
            proxy = TrackARiskSignal(
                coin=intent.coin.strip(),
                side=intent.side,
                position_size_usd=scaled_usd,
                entry_price=float(ref_px),
                stop_loss_price=float(sl) if sl is not None else 0.0,
                take_profit_price=float(tp) if tp is not None else 0.0,
                strategy_key=str(intent.strategy_key),
                leverage=lev,
                metadata=dict(intent.metadata or {}),
            )
            pos_id = str(uuid.uuid4())
            try:
                req = AcpTradeRequest(
                    coin=intent.coin.strip(),
                    side=intent.side,
                    size_usd=scaled_usd,
                    leverage=lev,
                    order_type="market",
                    stop_loss=intent.stop_loss_price,
                    take_profit=intent.take_profit_price,
                    rationale=(
                        "TrackA strategy=%s engine_id=%s ref_px=%.6f"
                        % (intent.strategy_key, intent.engine_id, ref_px)
                    ),
                    idempotency_key=str(uuid.uuid4()),
                )
                resp = self._degen.submit_trade(req)
                if not resp.success:
                    submit_failed += 1
                    logger.error(
                        "ORCH_TRACK_A_SUBMIT_FAIL coin=%s engine_id=%s error=%s",
                        intent.coin,
                        intent.engine_id,
                        resp.error,
                    )
                    continue
                submitted += 1
                logger.info(
                    "ORCH_TRACK_A_SUBMITTED coin=%s side=%s engine_id=%s size_usd=%.2f job_id=%s",
                    intent.coin,
                    intent.side,
                    intent.engine_id,
                    scaled_usd,
                    resp.job_id,
                )
            except Exception as e:
                submit_failed += 1
                logger.error(
                    "ORCH_TRACK_A_SUBMIT_EXCEPTION coin=%s engine_id=%s error=%s",
                    intent.coin,
                    intent.engine_id,
                    e,
                    exc_info=True,
                )
                continue

            pos = TrackAOpenPosition(
                position_id=pos_id,
                signal=proxy,
                opened_at=datetime.now(timezone.utc),
            )
            self._portfolio_state.register_position(intent.engine_id, pos)
            registered += 1

            if self._journal is not None:
                try:
                    await self._journal.log_track_a_entry(
                        position_id=pos_id,
                        intent=intent,
                        entry_price=ref_px,
                        job_id=resp.job_id,
                        idempotency_key=req.idempotency_key,
                        leverage_used=lev,
                        submitted_position_size_usd=scaled_usd,
                    )
                    journal_logged += 1
                except Exception as e:
                    journal_failed += 1
                    logger.warning(
                        "ORCH_TRACK_A_JOURNAL_FAILED coin=%s position_id=%s error=%s",
                        intent.coin,
                        pos_id,
                        e,
                        exc_info=True,
                    )

        logger.info(
            "ORCH_TRACK_A_BATCH risk_rejected=%d submit_failed=%d submitted=%d registered=%d "
            "journal_logged=%d journal_failed=%d",
            risk_rejected,
            submit_failed,
            submitted,
            registered,
            journal_logged,
            journal_failed,
        )
        return TrackAExecutionSummary(
            risk_rejected=risk_rejected,
            submit_failed=submit_failed,
            submitted=submitted,
            registered=registered,
            journal_logged=journal_logged,
            journal_failed=journal_failed,
        )
