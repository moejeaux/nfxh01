"""Safety mode: scale new entries until recent AceVault metrics clear thresholds."""

from __future__ import annotations

import logging
from typing import Any

from src.db.decision_journal import DecisionJournal

logger = logging.getLogger(__name__)


def _profit_factor(pnls: list[float]) -> float:
    wins = sum(x for x in pnls if x > 0)
    losses = abs(sum(x for x in pnls if x < 0))
    if losses <= 0:
        return float("inf") if wins > 0 else 0.0
    return wins / losses


def _avg_win_loss_ratio(pnls: list[float]) -> float:
    win_vals = [x for x in pnls if x > 0]
    loss_vals = [x for x in pnls if x < 0]
    if not win_vals and not loss_vals:
        return 0.0
    if win_vals and not loss_vals:
        return float("inf")
    if loss_vals and not win_vals:
        return 0.0
    avg_win = sum(win_vals) / len(win_vals)
    avg_loss = abs(sum(loss_vals) / len(loss_vals))
    if avg_loss <= 0:
        return float("inf")
    return avg_win / avg_loss


def compute_safety_multiplier_from_pnls(
    risk_cfg: dict,
    pnls: list[float],
    *,
    learning_cfg: dict | None = None,
) -> tuple[float, dict[str, Any]]:
    """Return (multiplier, meta) using ``risk.safety_mode``; multiplier is 1.0 or ``position_multiplier``."""
    sm = risk_cfg.get("safety_mode")
    if not isinstance(sm, dict) or not sm.get("enabled"):
        return 1.0, {"safety_mode": "disabled"}

    learn = learning_cfg or {}
    reduced = float(sm.get("position_multiplier", 0.1))
    min_n = int(sm.get("min_closed_trades", 30))
    min_pf_raw = sm.get("min_profit_factor_before_leaving_safety_mode")
    if min_pf_raw is None:
        min_pf_raw = learn.get("min_profit_factor_before_leaving_safety_mode")
    if min_pf_raw is None:
        min_pf_raw = sm.get("min_profit_factor", 1.05)
    min_pf = float(min_pf_raw)
    min_rr = float(sm.get("min_avg_win_loss_ratio", 1.0))

    if len(pnls) < min_n:
        return reduced, {
            "safety_mode": "active",
            "reason": "insufficient_sample",
            "n": len(pnls),
            "min_closed_trades": min_n,
        }

    pf = _profit_factor(pnls)
    rr = _avg_win_loss_ratio(pnls)
    pf_ok = pf >= min_pf
    rr_ok = rr >= min_rr

    meta: dict[str, Any] = {
        "safety_mode": "active",
        "n": len(pnls),
        "profit_factor": pf,
        "avg_win_loss_ratio": rr,
        "min_profit_factor_before_leaving_safety_mode": min_pf,
        "min_avg_win_loss_ratio": min_rr,
    }

    if pf_ok and rr_ok:
        meta["safety_mode"] = "graduated"
        return 1.0, meta

    meta["reason"] = "thresholds_not_met"
    meta["pf_ok"] = pf_ok
    meta["rr_ok"] = rr_ok
    return reduced, meta


async def refresh_safety_mode(
    risk_layer: Any,
    config: dict,
    journal: DecisionJournal | None,
) -> None:
    """Set ``UnifiedRiskLayer`` position multiplier from last N closed AceVault trades."""
    risk_cfg = config.get("risk") or {}
    sm = risk_cfg.get("safety_mode")
    if not isinstance(sm, dict) or not sm.get("enabled"):
        risk_layer.set_safety_position_multiplier(1.0)
        logger.info("RISK_SAFETY_MODE_REFRESH multiplier=1.0000 reason=disabled")
        return

    min_n = int(sm.get("min_closed_trades", 30))
    reduced = float(sm.get("position_multiplier", 0.1))

    if journal is None or not journal.is_connected():
        risk_layer.set_safety_position_multiplier(reduced)
        logger.warning(
            "RISK_SAFETY_MODE_REFRESH multiplier=%.4f reason=no_journal",
            reduced,
        )
        return

    rows = await journal.fetch_closed_decisions_for_metrics(min_n)
    pnls = [float(r["pnl_usd"]) for r in rows if r.get("pnl_usd") is not None]
    mult, meta = compute_safety_multiplier_from_pnls(
        risk_cfg,
        pnls,
        learning_cfg=config.get("learning"),
    )
    risk_layer.set_safety_position_multiplier(mult)
    logger.info(
        "RISK_SAFETY_MODE_REFRESH multiplier=%.4f n=%s pf=%s rr=%s meta=%s",
        mult,
        meta.get("n"),
        meta.get("profit_factor"),
        meta.get("avg_win_loss_ratio"),
        {k: meta[k] for k in meta if k not in ("profit_factor", "avg_win_loss_ratio")},
    )
