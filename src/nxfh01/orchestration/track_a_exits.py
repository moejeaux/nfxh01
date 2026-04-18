"""Track A (Growi / MC) software exits via ``LiveExitEngine`` — runs once per orchestrator tick."""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from src.acp.degen_claw import AcpCloseRequest
from src.exits.manager import LiveExitEngine
from src.exits.models import UniversalExit
from src.risk.portfolio_state import PortfolioState

if TYPE_CHECKING:
    from src.db.decision_journal import DecisionJournal

logger = logging.getLogger(__name__)


async def run_track_a_exits(
    config: dict[str, Any],
    *,
    portfolio_state: PortfolioState,
    degen_executor: Any,
    hl_client: Any,
    exit_engine: LiveExitEngine,
    decision_journal: DecisionJournal | None = None,
) -> None:
    root = config.get("exits") or {}
    if not root.get("enabled", True):
        return
    ta = root.get("track_a") or {}
    if not ta.get("enabled", False):
        return
    try:
        mids_raw = hl_client.all_mids()
        current_prices = {k: float(v) for k, v in mids_raw.items()}
    except Exception as e:
        logger.warning("EXIT_TRACK_A_MIDS_FAILED error=%s", e)
        return

    for engine_id, strategy_key in (
        ("growi", "growi_hf"),
        ("mc", "mc_recovery"),
        ("btc_lanes", "btc_lanes"),
    ):
        positions = portfolio_state.get_open_positions(engine_id)
        if not positions:
            continue
        universal = exit_engine.evaluate_portfolio_positions(
            engine_id=engine_id,
            positions=positions,
            current_prices=current_prices,
            regime_exit_all=False,
            strategy_key=strategy_key,
        )
        for u in universal:
            await _apply_close(
                portfolio_state,
                degen_executor,
                engine_id,
                u,
                decision_journal=decision_journal,
            )


async def _apply_close(
    portfolio_state: PortfolioState,
    degen_executor: Any,
    engine_id: str,
    u: UniversalExit,
    *,
    decision_journal: DecisionJournal | None = None,
) -> None:
    try:
        degen_executor.submit_close(
            AcpCloseRequest(
                coin=u.coin,
                rationale=(
                    f"TrackA exit engine_id={engine_id} reason={u.exit_reason} "
                    f"pnl_pct={u.pnl_pct:.5f}"
                ),
                idempotency_key=str(uuid.uuid4()),
            )
        )
    except Exception as e:
        logger.error(
            "EXIT_TRACK_A_CLOSE_FAILED coin=%s engine_id=%s error=%s", u.coin, engine_id, e
        )
        return

    class _ExitShim:
        pnl_usd = u.pnl_usd

    portfolio_state.close_position(engine_id, u.position_id, _ExitShim())
    logger.info(
        "EXIT_TRACK_A_CLOSED coin=%s engine_id=%s reason=%s pnl_usd=%.4f",
        u.coin,
        engine_id,
        u.exit_reason,
        u.pnl_usd,
    )

    # DB persistence is best-effort: if the journal is absent or the row was never
    # inserted (e.g. HL-sync imported position), skip without failing the close.
    if decision_journal is None or not decision_journal.is_connected():
        return
    try:
        await decision_journal.log_track_a_exit(position_id=u.position_id, exit=u)
    except Exception as e:
        logger.warning(
            "EXIT_TRACK_A_JOURNAL_FAILED position_id=%s coin=%s engine_id=%s error=%s",
            u.position_id,
            u.coin,
            engine_id,
            e,
        )
