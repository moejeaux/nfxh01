"""Snapshot helpers for 72h comparisons: grep ``ACEVAULT_METRICS_SNAPSHOT`` / ``REGIME_METRICS_SNAPSHOT`` in logs."""

from __future__ import annotations

from typing import Any

from src.engines.acevault import acevault_metrics
import src.regime.regime_metrics as regime_metrics_mod


def snapshot_all() -> dict[str, Any]:
    """Return current in-process counters (reset on process restart)."""
    return {
        "acevault": acevault_metrics.snapshot(),
        "regime": regime_metrics_mod.snapshot(),
    }


def reset_all() -> None:
    acevault_metrics.reset()
    regime_metrics_mod.reset()
