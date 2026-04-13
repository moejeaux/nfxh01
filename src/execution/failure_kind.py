"""Execution-layer failure classification (Senpi-first + DegenClaw fallback)."""

from __future__ import annotations

from enum import StrEnum


class ExecutionFailureKind(StrEnum):
    RETRYABLE = "retryable"
    NON_RETRYABLE = "non_retryable"
