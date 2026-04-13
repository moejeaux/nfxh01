"""Resolve execution_mode for startup logs and agent status."""

from __future__ import annotations

import logging
import os

logger = logging.getLogger(__name__)

MODE_DEGEN_ONLY = "degen_only"
MODE_SENPI_STUB_FALLBACK = "senpi_stub_fallback"
MODE_SENPI_PRIMARY_ACTIVE = "senpi_primary_active"


def resolve_execution_bootstrap() -> tuple[str | None, str | None]:
    """Return (execution_mode, fatal_error). If fatal_error is set, main must sys.exit(1)."""
    from src.execution.senpi_trade import (
        is_senpi_execution_enabled,
        senpi_mcp_credentials_complete,
    )

    if not is_senpi_execution_enabled():
        return MODE_DEGEN_ONLY, None

    require = os.getenv("SENPI_REQUIRE_TRANSPORT", "").lower() in ("true", "1", "yes")
    if require and not senpi_mcp_credentials_complete():
        return (
            None,
            "SENPI_EXECUTION_ENABLED=true and SENPI_REQUIRE_TRANSPORT=true but "
            "SENPI_AUTH_TOKEN or SENPI_STRATEGY_WALLET_ADDRESS is empty",
        )

    if senpi_mcp_credentials_complete():
        return MODE_SENPI_PRIMARY_ACTIVE, None

    # Senpi enabled but cannot run MCP (missing token/wallet) — not "primary", not silent.
    return MODE_SENPI_STUB_FALLBACK, None


def log_execution_mode_banner(mode: str) -> None:
    if mode == MODE_DEGEN_ONLY:
        logger.info("execution_mode=%s (DegenClaw only)", mode)
        return
    if mode == MODE_SENPI_STUB_FALLBACK:
        logger.warning(
            "execution_mode=%s — SENPI_EXECUTION_ENABLED but SENPI_AUTH_TOKEN or "
            "SENPI_STRATEGY_WALLET_ADDRESS missing; Senpi MCP will not execute until set.",
            mode,
        )
        return
    if mode == MODE_SENPI_PRIMARY_ACTIVE:
        logger.info(
            "execution_mode=%s — Senpi MCP credentials present; primary execution uses "
            "create_position/close_position at mcp.prod.senpi.ai",
            mode,
        )
