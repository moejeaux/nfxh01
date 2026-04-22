"""REGIME_* periodic counters for strict ranging vs legacy bucket (grep REGIME_METRICS_SNAPSHOT)."""

from __future__ import annotations

import logging
from threading import Lock
from typing import Any

logger = logging.getLogger(__name__)

_lock = Lock()
_total_cycles = 0
_cycles_legacy_ranging_true = 0
_cycles_strict_ranging_evaluated = 0
_cycles_strict_ranging_passed = 0
_cycles_legacy_true_strict_false = 0
_strict_fail_reason_counts: dict[str, int] = {}
_cycles_final_ranging_and_strict_passed = 0
_summary_every_n = 60

_last_summary_cycle = 0


def configure_summary_interval(n: int) -> None:
    global _summary_every_n
    _summary_every_n = max(1, int(n))


def record_cycle(
    *,
    legacy_ranging: bool,
    final_ranging: bool,
    strict_passed: bool,
    strict_evaluated: bool,
    strict_ranging_pass: bool,
    fail_reasons: list[str],
) -> None:
    global _total_cycles, _cycles_legacy_ranging_true, _cycles_strict_ranging_evaluated
    global _cycles_strict_ranging_passed, _cycles_legacy_true_strict_false
    global _cycles_final_ranging_and_strict_passed, _last_summary_cycle
    global _strict_fail_reason_counts
    with _lock:
        _total_cycles += 1
        if legacy_ranging:
            _cycles_legacy_ranging_true += 1
        if strict_evaluated:
            _cycles_strict_ranging_evaluated += 1
        if strict_evaluated and strict_ranging_pass:
            _cycles_strict_ranging_passed += 1
        if legacy_ranging and strict_evaluated and not strict_ranging_pass:
            _cycles_legacy_true_strict_false += 1
            for r in fail_reasons:
                _strict_fail_reason_counts[r] = _strict_fail_reason_counts.get(r, 0) + 1
        if final_ranging and strict_passed:
            _cycles_final_ranging_and_strict_passed += 1
        if _total_cycles - _last_summary_cycle >= _summary_every_n:
            _last_summary_cycle = _total_cycles
            _emit_summary_locked()


def _emit_summary_locked() -> None:
    reasons_repr = ",".join(
        f"{k}:{_strict_fail_reason_counts[k]}" for k in sorted(_strict_fail_reason_counts)
    )
    logger.info(
        "REGIME_METRICS_SNAPSHOT total_cycles=%d "
        "cycles_legacy_ranging_true=%d cycles_strict_ranging_evaluated=%d "
        "cycles_strict_ranging_passed=%d cycles_legacy_true_strict_false=%d "
        "strict_fail_reason_counts=%s "
        "cycles_final_ranging_and_strict_passed=%d",
        _total_cycles,
        _cycles_legacy_ranging_true,
        _cycles_strict_ranging_evaluated,
        _cycles_strict_ranging_passed,
        _cycles_legacy_true_strict_false,
        reasons_repr or "{}",
        _cycles_final_ranging_and_strict_passed,
    )


def snapshot() -> dict[str, Any]:
    with _lock:
        return {
            "total_cycles": _total_cycles,
            "cycles_legacy_ranging_true": _cycles_legacy_ranging_true,
            "cycles_strict_ranging_evaluated": _cycles_strict_ranging_evaluated,
            "cycles_strict_ranging_passed": _cycles_strict_ranging_passed,
            "cycles_legacy_true_strict_false": _cycles_legacy_true_strict_false,
            "strict_fail_reason_counts": dict(_strict_fail_reason_counts),
            "cycles_final_ranging_and_strict_passed": _cycles_final_ranging_and_strict_passed,
            "cycles_ranging_legacy": _cycles_legacy_ranging_true,
            "cycles_ranging_strict": _cycles_final_ranging_and_strict_passed,
            "cycles_ranging_strict_failed_but_legacy_true": _cycles_legacy_true_strict_false,
        }


def reset() -> None:
    global _total_cycles, _cycles_legacy_ranging_true, _cycles_strict_ranging_evaluated
    global _cycles_strict_ranging_passed, _cycles_legacy_true_strict_false
    global _cycles_final_ranging_and_strict_passed, _last_summary_cycle
    global _strict_fail_reason_counts
    with _lock:
        _total_cycles = 0
        _cycles_legacy_ranging_true = 0
        _cycles_strict_ranging_evaluated = 0
        _cycles_strict_ranging_passed = 0
        _cycles_legacy_true_strict_false = 0
        _cycles_final_ranging_and_strict_passed = 0
        _last_summary_cycle = 0
        _strict_fail_reason_counts.clear()
