"""Per-strategy cadence relative to orchestrator ticks."""

from __future__ import annotations

from datetime import datetime, timezone


def seconds_since(last_run_at: datetime | None, now: datetime) -> float | None:
    if last_run_at is None:
        return None
    return (now - last_run_at).total_seconds()


def should_run_strategy(
    *,
    last_run_at: datetime | None,
    now: datetime,
    cycle_interval_seconds: float,
) -> bool:
    """Return True if at least ``cycle_interval_seconds`` since last run (or never run)."""
    if last_run_at is None:
        return True
    elapsed = (now - last_run_at).total_seconds()
    return elapsed >= cycle_interval_seconds
