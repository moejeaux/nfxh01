"""AceVault ranging counters and exit aggregates (grep ACEVAULT_METRICS_SNAPSHOT)."""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

_lock = Lock()
_total_ranging_entry_candidates = 0
_rejected_ranging_structure = 0
_rejected_midpoint = 0
_rejected_edge_distance = 0
_rejected_loss_cooldown = 0
_rejected_reset = 0
_rejected_cost_ratio = 0
_cycles_with_ranging_structure_block = 0
_total_exits = 0
_exits_by_reason: dict[str, int] = {}
_sum_realized_r_by_reason: dict[str, float] = {}
_sum_capture_by_reason: dict[str, float] = {}
_count_by_reason: dict[str, int] = {}
_summary_every_n_exits = 25
_last_exit_summary_at = 0


def configure_exit_summary_every(n: int) -> None:
    global _summary_every_n_exits
    _summary_every_n_exits = max(1, int(n))


def incr_ranging_candidate() -> None:
    global _total_ranging_entry_candidates
    with _lock:
        _total_ranging_entry_candidates += 1


def incr_cycle_with_ranging_structure_block() -> None:
    global _cycles_with_ranging_structure_block
    with _lock:
        _cycles_with_ranging_structure_block += 1


def incr_reject(gate: str) -> None:
    global _rejected_ranging_structure, _rejected_midpoint, _rejected_edge_distance
    global _rejected_loss_cooldown, _rejected_reset, _rejected_cost_ratio
    with _lock:
        if gate == "ranging_structure_gate":
            _rejected_ranging_structure += 1
        elif gate == "midpoint_gate":
            _rejected_midpoint += 1
        elif gate == "edge_distance_gate":
            _rejected_edge_distance += 1
        elif gate == "loss_cooldown_gate":
            _rejected_loss_cooldown += 1
        elif gate == "reset_gate":
            _rejected_reset += 1
        elif gate == "cost_ratio_gate":
            _rejected_cost_ratio += 1


def record_exit_metrics(
    *,
    exit_reason: str,
    realized_r: float | None,
    peak_r: float | None,
) -> None:
    global _total_exits, _last_exit_summary_at
    tiny = 1e-9
    cap = None
    if peak_r is not None and peak_r > tiny and realized_r is not None:
        cap = realized_r / peak_r
    with _lock:
        _total_exits += 1
        _exits_by_reason[exit_reason] = _exits_by_reason.get(exit_reason, 0) + 1
        _count_by_reason[exit_reason] = _count_by_reason.get(exit_reason, 0) + 1
        if realized_r is not None:
            _sum_realized_r_by_reason[exit_reason] = (
                _sum_realized_r_by_reason.get(exit_reason, 0.0) + realized_r
            )
        if cap is not None:
            _sum_capture_by_reason[exit_reason] = (
                _sum_capture_by_reason.get(exit_reason, 0.0) + cap
            )
        if _total_exits - _last_exit_summary_at >= _summary_every_n_exits:
            _last_exit_summary_at = _total_exits
            _emit_exit_summary_locked()


def _emit_exit_summary_locked() -> None:
    parts: list[str] = [f"total_exits={_total_exits}", f"by_reason={dict(_exits_by_reason)}"]
    for reason, c in sorted(_count_by_reason.items()):
        sr = _sum_realized_r_by_reason.get(reason, 0.0) / c if c else 0.0
        sc = _sum_capture_by_reason.get(reason, 0.0) / c if c else 0.0
        parts.append(f"reason_{reason}_avg_r={sr:.4f}_avg_capture={sc:.4f}")
    logger.info("ACEVAULT_METRICS_SNAPSHOT exits %s", " ".join(parts))


def log_entry_gate_snapshot() -> None:
    with _lock:
        logger.info(
            "ACEVAULT_METRICS_SNAPSHOT entry_gates total_ranging_entry_candidates=%d "
            "rejected_by_ranging_structure_gate=%d ranging_candidates_blocked_by_structure=%d "
            "cycles_with_ranging_structure_block=%d rejected_by_midpoint_gate=%d "
            "rejected_by_edge_distance_gate=%d rejected_by_loss_cooldown_gate=%d "
            "rejected_by_reset_gate=%d rejected_by_cost_ratio_gate=%d",
            _total_ranging_entry_candidates,
            _rejected_ranging_structure,
            _rejected_ranging_structure,
            _cycles_with_ranging_structure_block,
            _rejected_midpoint,
            _rejected_edge_distance,
            _rejected_loss_cooldown,
            _rejected_reset,
            _rejected_cost_ratio,
        )


def snapshot() -> dict[str, Any]:
    with _lock:
        avg_r: dict[str, float] = {}
        avg_cap: dict[str, float] = {}
        for reason, c in _count_by_reason.items():
            if c:
                avg_r[reason] = _sum_realized_r_by_reason.get(reason, 0.0) / c
                avg_cap[reason] = _sum_capture_by_reason.get(reason, 0.0) / c
        return {
            "total_ranging_entry_candidates": _total_ranging_entry_candidates,
            "rejected_by_ranging_structure_gate": _rejected_ranging_structure,
            "ranging_candidates_blocked_by_structure": _rejected_ranging_structure,
            "cycles_with_ranging_structure_block": _cycles_with_ranging_structure_block,
            "rejected_by_midpoint_gate": _rejected_midpoint,
            "rejected_by_edge_distance_gate": _rejected_edge_distance,
            "rejected_by_loss_cooldown_gate": _rejected_loss_cooldown,
            "rejected_by_reset_gate": _rejected_reset,
            "rejected_by_cost_ratio_gate": _rejected_cost_ratio,
            "total_exits": _total_exits,
            "exits_by_reason": dict(_exits_by_reason),
            "avg_realized_r_by_reason": avg_r,
            "avg_capture_ratio_by_reason": avg_cap,
        }


def reset() -> None:
    global _total_ranging_entry_candidates, _rejected_ranging_structure, _rejected_midpoint
    global _rejected_edge_distance, _rejected_loss_cooldown, _rejected_reset, _rejected_cost_ratio
    global _cycles_with_ranging_structure_block
    global _total_exits, _last_exit_summary_at
    with _lock:
        _total_ranging_entry_candidates = 0
        _rejected_ranging_structure = 0
        _cycles_with_ranging_structure_block = 0
        _rejected_midpoint = 0
        _rejected_edge_distance = 0
        _rejected_loss_cooldown = 0
        _rejected_reset = 0
        _rejected_cost_ratio = 0
        _total_exits = 0
        _last_exit_summary_at = 0
        _exits_by_reason.clear()
        _sum_realized_r_by_reason.clear()
        _sum_capture_by_reason.clear()
        _count_by_reason.clear()
