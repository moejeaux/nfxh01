"""Explicit healthy-skip gate for retrospective Fathom calls (RETRO_* logs)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from src.retro.metrics import RetroMetricsSnapshot

logger = logging.getLogger(__name__)


@dataclass
class RetroSkipDecision:
    skip_fathom: bool
    reason: str
    log_fields: dict[str, Any]


def evaluate_retro_skip(
    config: dict[str, Any],
    metrics: RetroMetricsSnapshot,
    *,
    mode: str,
    cooldown_active: bool = False,
) -> RetroSkipDecision:
    """
    When skip_fathom is True, caller logs RETRO_SKIP_FATHOM and skips Ollama.
    Decision doc: skip only when healthy enough AND explicit thresholds met.
    """
    hg = (config.get("retro") or {}).get("healthy_gate") or {}
    min_trades = int(hg.get("min_closing_trades", 30))
    recent_floor = float(hg.get("recent_pf_floor", 1.2))
    global_floor = float(hg.get("global_pf_floor", 1.0))
    max_fee = float(hg.get("max_fee_drag_pct", 0.5))

    gf = metrics.global_profit_factor
    rf = metrics.recent_profit_factor
    n = metrics.closing_trade_count
    fd = metrics.fee_drag_pct

    log_fields: dict[str, Any] = {
        "mode": mode,
        "closing_trade_count": n,
        "global_profit_factor": gf,
        "recent_profit_factor": rf,
        "fee_drag_pct": fd,
        "cooldown_active": cooldown_active,
    }

    if cooldown_active:
        return RetroSkipDecision(
            skip_fathom=True,
            reason="cooldown_after_change",
            log_fields=log_fields,
        )

    if n < min_trades:
        return RetroSkipDecision(
            skip_fathom=False,
            reason="insufficient_sample_for_healthy_skip",
            log_fields=log_fields,
        )

    if gf == float("inf"):
        gf_log = "inf"
    else:
        gf_log = round(gf, 4)

    healthy = (
        rf >= recent_floor
        and (gf >= global_floor or gf == float("inf"))
        and fd <= max_fee
    )
    if healthy:
        log_fields["global_pf_display"] = gf_log
        logger.info(
            "RETRO_SKIP_FATHOM mode=%s reason=healthy_metrics "
            "closing_trades=%d global_pf=%s recent_pf=%.4f fee_drag=%.4f cooldown=%s",
            mode,
            n,
            gf_log,
            rf,
            fd,
            cooldown_active,
        )
        return RetroSkipDecision(skip_fathom=True, reason="healthy_metrics", log_fields=log_fields)

    logger.info(
        "RETRO_GATE_RUN_FATHOM mode=%s reason=needs_analysis "
        "closing_trades=%d global_pf=%s recent_pf=%.4f fee_drag=%.4f",
        mode,
        n,
        gf_log,
        rf,
        fd,
    )
    return RetroSkipDecision(skip_fathom=False, reason="needs_analysis", log_fields=log_fields)
