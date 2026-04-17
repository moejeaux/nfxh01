"""Post-change evaluation — trade-count checkpoints vs baseline profit factor (registry DB)."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from src.db.decision_journal import DecisionJournal
from src.retro.metrics import build_metrics_from_decision_rows

logger = logging.getLogger(__name__)


def _norm_pf(pf: float) -> float:
    if pf == float("inf"):
        return 1e9
    if pf == float("-inf"):
        return -1e9
    return float(pf)


def _parse_checkpoints(raw: Any) -> list[int]:
    if not isinstance(raw, list) or not raw:
        return [25, 50, 100]
    out: list[int] = []
    for x in raw:
        try:
            out.append(int(x))
        except (TypeError, ValueError):
            continue
    return sorted(set(out)) if out else [25, 50, 100]


async def evaluate_pending_learning_changes(
    config: dict[str, Any],
    journal: DecisionJournal,
) -> None:
    """Classify pending learning rows at checkpoints: improved | worsened | inconclusive."""
    learn = config.get("learning") or {}

    if not journal.is_connected():
        logger.warning("LEARN_EVAL_SKIP reason=journal_disconnected")
        return

    max_rows = int(learn.get("evaluation_max_closed_rows", 50000))
    checkpoints = _parse_checkpoints(learn.get("evaluation_trade_checkpoints"))
    min_cp = min(checkpoints)
    max_cp = max(checkpoints)
    worsen_ratio = float(learn.get("revert_pf_worsen_ratio", 0.85))
    improve_ratio = float(learn.get("improve_pf_ratio", 1.05))
    timeout_days = float(learn.get("inconclusive_timeout_days", 7))

    rows = await journal.fetch_closed_decisions_for_metrics(max_rows)
    snap = build_metrics_from_decision_rows(rows, recent_hours=24.0)
    current_pf = _norm_pf(snap.global_profit_factor)
    current_closed = int(snap.closing_trade_count)

    pending = await journal.fetch_pending_learning_evaluations(limit=100)
    if not pending:
        return

    now = datetime.now(timezone.utc)
    for row in pending:
        change_id = str(row.get("change_id", ""))
        if not change_id:
            continue

        try:
            at_apply = int(row.get("closing_trade_count_at_apply") or 0)
        except (TypeError, ValueError):
            at_apply = 0

        delta = current_closed - at_apply
        if delta < min_cp:
            continue

        created_at = row.get("created_at")
        if isinstance(created_at, datetime):
            ca = created_at if created_at.tzinfo else created_at.replace(tzinfo=timezone.utc)
            age_days = (now - ca).total_seconds() / 86400.0
        else:
            age_days = 0.0

        baseline = row.get("baseline_profit_factor")
        try:
            bval = float(baseline) if baseline is not None else None
        except (TypeError, ValueError):
            bval = None

        notes: list[str] = []
        status: str | None = None

        if bval is None or bval <= 0:
            notes.append("baseline_pf_missing_or_invalid")
            if delta >= max_cp or age_days >= timeout_days:
                status = "inconclusive"
            else:
                continue
        else:
            worsen_thr = bval * worsen_ratio
            improve_thr = bval * improve_ratio
            if current_pf <= worsen_thr:
                status = "worsened"
                notes.append(f"current_pf={current_pf:.4f}<=worsen_thr={worsen_thr:.4f}")
            elif current_pf >= improve_thr:
                status = "improved"
                notes.append(f"current_pf={current_pf:.4f}>=improve_thr={improve_thr:.4f}")
            else:
                if delta >= max_cp or age_days >= timeout_days:
                    status = "inconclusive"
                    notes.append(
                        f"ambiguous_pf current={current_pf:.4f} band=[{worsen_thr:.4f},{improve_thr:.4f}]"
                    )
                else:
                    continue

        if status is None:
            continue

        note_txt = ";".join(notes) if notes else None
        await journal.update_learning_result(
            change_id=change_id,
            result_status=status,
            evaluation_notes=note_txt,
        )
        logger.info(
            "LEARN_EVAL_RESULT change_id=%s status=%s delta_trades=%d baseline=%s",
            change_id,
            status,
            delta,
            baseline,
        )


def evaluate_pending_changes_placeholder(config: dict[str, Any]) -> None:
    """Sync no-op; use ``evaluate_pending_learning_changes`` from async contexts."""
    logger.debug(
        "LEARN_EVAL_SYNC_PLACEHOLDER keys=%s — use evaluate_pending_learning_changes",
        list(config.keys())[:5],
    )
