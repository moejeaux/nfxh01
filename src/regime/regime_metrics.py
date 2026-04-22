"""REGIME_* periodic counters for strict ranging vs legacy bucket (grep REGIME_METRICS_SNAPSHOT)."""

from __future__ import annotations

import logging
from threading import Lock

logger = logging.getLogger(__name__)

_lock = Lock()
_total_cycles = 0
_cycles_ranging_legacy = 0
_cycles_ranging_strict = 0
_cycles_strict_failed_legacy_ranging = 0
_summary_every_n = 60

_last_summary_cycle = 0


def configure_summary_interval(n: int) -> None:
    global _summary_every_n
    _summary_every_n = max(1, int(n))


def record_cycle(
    *,
    legacy_ranging: bool,
    final_ranging: bool,
    strict_applied: bool,
    strict_passed: bool,
) -> None:
    global _total_cycles, _cycles_ranging_legacy, _cycles_ranging_strict
    global _cycles_strict_failed_legacy_ranging, _last_summary_cycle
    with _lock:
        _total_cycles += 1
        if legacy_ranging:
            _cycles_ranging_legacy += 1
        if final_ranging and strict_passed:
            _cycles_ranging_strict += 1
        if legacy_ranging and strict_applied and not strict_passed:
            _cycles_strict_failed_legacy_ranging += 1
        if _total_cycles - _last_summary_cycle >= _summary_every_n:
            _last_summary_cycle = _total_cycles
            _emit_summary_locked()


def _emit_summary_locked() -> None:
    logger.info(
        "REGIME_METRICS_SNAPSHOT total_cycles=%d cycles_ranging_legacy=%d "
        "cycles_ranging_strict=%d cycles_ranging_strict_failed_but_legacy_true=%d",
        _total_cycles,
        _cycles_ranging_legacy,
        _cycles_ranging_strict,
        _cycles_strict_failed_legacy_ranging,
    )


def snapshot() -> dict[str, int]:
    with _lock:
        return {
            "total_cycles": _total_cycles,
            "cycles_ranging_legacy": _cycles_ranging_legacy,
            "cycles_ranging_strict": _cycles_ranging_strict,
            "cycles_ranging_strict_failed_but_legacy_true": _cycles_strict_failed_legacy_ranging,
        }


def reset() -> None:
    global _total_cycles, _cycles_ranging_legacy, _cycles_ranging_strict
    global _cycles_strict_failed_legacy_ranging, _last_summary_cycle
    with _lock:
        _total_cycles = 0
        _cycles_ranging_legacy = 0
        _cycles_ranging_strict = 0
        _cycles_strict_failed_legacy_ranging = 0
        _last_summary_cycle = 0
