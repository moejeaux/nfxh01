"""Validate env for NXFH02_SIGNAL_SOURCE — independent of execution_mode (order routing)."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger("nxfh02.signal")

SIGNAL_SOURCE_INTERNAL = "internal"
SIGNAL_SOURCE_SENPI = "senpi"


def resolve_signal_source() -> str:
    raw = (os.getenv("NXFH02_SIGNAL_SOURCE") or SIGNAL_SOURCE_INTERNAL).strip().lower()
    if raw in ("senpi", "external", "senpi_http"):
        return SIGNAL_SOURCE_SENPI
    return SIGNAL_SOURCE_INTERNAL


def resolve_signal_ingress_bootstrap() -> tuple[str | None, str | None]:
    """Return (fatal_error, warning). Fatal → process should exit."""
    if resolve_signal_source() != SIGNAL_SOURCE_SENPI:
        return None, None

    auth_disabled = (os.getenv("NXFH02_SIGNAL_AUTH_DISABLED") or "").strip().lower() in (
        "1", "true", "yes",
    )
    secret = (os.getenv("NXFH02_SIGNAL_WEBHOOK_SECRET") or "").strip()
    if not secret and not auth_disabled:
        return (
            "NXFH02_SIGNAL_SOURCE=senpi requires NXFH02_SIGNAL_WEBHOOK_SECRET (non-empty) "
            "or NXFH02_SIGNAL_AUTH_DISABLED=1 for local dev",
            None,
        )

    host = (os.getenv("NXFH02_SIGNAL_HTTP_HOST") or "127.0.0.1").strip()
    port_s = (os.getenv("NXFH02_SIGNAL_HTTP_PORT") or "8788").strip()
    try:
        int(port_s)
    except ValueError:
        return (f"NXFH02_SIGNAL_HTTP_PORT invalid: {port_s!r}", None)

    warn = None
    if not os.getenv("NXFH02_SIGNAL_QUEUE_SIZE"):
        warn = "NXFH02_SIGNAL_QUEUE_SIZE unset — using default 32"

    logger.info(
        "signal_source=%s (independent of execution_mode) http_bind=%s:%s",
        SIGNAL_SOURCE_SENPI,
        host,
        port_s,
    )
    return None, warn


def internal_strategies_enabled_default() -> bool:
    """When signal_source=senpi, internal scan/execute defaults off unless opted in."""
    if resolve_signal_source() != SIGNAL_SOURCE_SENPI:
        return True
    return (os.getenv("NXFH02_INTERNAL_STRATEGIES_ENABLED") or "").lower() in (
        "true",
        "1",
        "yes",
    )
