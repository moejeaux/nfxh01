"""Senpi primary execution — real MCP Streamable HTTP (mcp.prod.senpi.ai/mcp).

Venue TP/SL after a successful open is handled in ``hl_protective.maybe_place_venue_tpsl_after_open``
(OrderExecutor), not here: it signs Hyperliquid ``bulk_orders`` trigger exits when
``SENPI_STRATEGY_WALLET_PRIVATE_KEY`` is set (same address as ``SENPI_STRATEGY_WALLET_ADDRESS``).
Senpi MCP ``edit_position`` is not called from this repo (no stable public tool schema).

References: https://senpi.ai/quickstart , Senpi-ai/senpi-skills (create_position / close_position).
"""

from __future__ import annotations

import logging
import os
from importlib.metadata import PackageNotFoundError, version

from src.acp.degen_claw import AcpCloseRequest, AcpTradeRequest, AcpTradeResponse
from src.execution.failure_kind import ExecutionFailureKind
from src.execution.senpi_mcp import (
    classify_senpi_failure,
    extract_job_id_from_data,
    normalize_mcp_url,
    run_senpi_mcp_tool_sync,
)

logger = logging.getLogger(__name__)

TOOL_CREATE_POSITION = "create_position"
TOOL_CLOSE_POSITION = "close_position"


def is_senpi_execution_enabled() -> bool:
    return os.getenv("SENPI_EXECUTION_ENABLED", "").lower() in ("true", "1", "yes")


def senpi_mcp_credentials_complete() -> bool:
    """Token + strategy wallet required for real Senpi MCP execution."""
    return bool(
        os.getenv("SENPI_AUTH_TOKEN", "").strip()
        and os.getenv("SENPI_STRATEGY_WALLET_ADDRESS", "").strip(),
    )


def resolve_senpi_mcp_url() -> str:
    raw = os.getenv("SENPI_MCP_URL") or os.getenv("SENPI_BASE_URL") or ""
    return normalize_mcp_url(raw)


def _skill_attribution() -> tuple[str, str]:
    name = os.getenv("SENPI_SKILL_NAME", "nxfh02-agent")
    try:
        ver = os.getenv("SENPI_SKILL_VERSION") or version("nxfh02-agent")
    except PackageNotFoundError:
        ver = os.getenv("SENPI_SKILL_VERSION") or "0.1.0"
    return name, ver


class SenpiTradeExecution:
    """Primary path: real MCP tools create_position / close_position when config is complete."""

    def submit_trade(self, request: AcpTradeRequest) -> AcpTradeResponse:
        intent = (request.idempotency_key or "")[:16]
        if not senpi_mcp_credentials_complete():
            logger.warning(
                "Senpi submit_trade blocked (incomplete MCP config) intent_id=%s",
                intent or "none",
            )
            return AcpTradeResponse(
                success=False,
                error="senpi_mcp_config_incomplete: set SENPI_AUTH_TOKEN and SENPI_STRATEGY_WALLET_ADDRESS",
                failure_kind=ExecutionFailureKind.NON_RETRYABLE.value,
            )

        url = resolve_senpi_mcp_url()
        token = os.getenv("SENPI_AUTH_TOKEN", "").strip()
        wallet = os.getenv("SENPI_STRATEGY_WALLET_ADDRESS", "").strip()
        lev = max(int(request.leverage), 1)
        margin_amount = request.size_usd / float(lev)

        order: dict = {
            "coin": request.coin,
            "direction": "LONG" if request.side == "long" else "SHORT",
            "leverage": lev,
            "marginAmount": round(margin_amount, 6),
            "orderType": "MARKET",
        }

        skill_name, skill_ver = _skill_attribution()
        arguments: dict = {
            "strategyWalletAddress": wallet,
            "orders": [order],
            "skill_name": skill_name,
            "skill_version": skill_ver,
        }

        logger.info(
            "Senpi MCP create_position intent_id=%s coin=%s side=%s",
            intent or "none",
            request.coin,
            request.side,
        )

        try:
            env = run_senpi_mcp_tool_sync(
                url, token, TOOL_CREATE_POSITION, arguments,
                timeout_s=float(os.getenv("SENPI_MCP_TIMEOUT_S", "90")),
            )
        except Exception as e:
            fk, msg = classify_senpi_failure(None, e)
            logger.exception("Senpi MCP transport error: %s", e)
            return AcpTradeResponse(
                success=False,
                error=f"senpi_mcp_transport: {msg}",
                failure_kind=fk,
            )

        if env.get("success"):
            jid = extract_job_id_from_data(env.get("data"))
            return AcpTradeResponse(
                success=True, job_id=jid, failure_kind=None, filled_via="senpi",
            )

        fk, msg = classify_senpi_failure(env, None)
        return AcpTradeResponse(
            success=False,
            error=msg or str(env.get("error")),
            failure_kind=fk,
        )

    def submit_close(self, request: AcpCloseRequest) -> AcpTradeResponse:
        intent = (request.idempotency_key or "")[:16]
        if not senpi_mcp_credentials_complete():
            return AcpTradeResponse(
                success=False,
                error="senpi_mcp_config_incomplete: set SENPI_AUTH_TOKEN and SENPI_STRATEGY_WALLET_ADDRESS",
                failure_kind=ExecutionFailureKind.NON_RETRYABLE.value,
            )

        url = resolve_senpi_mcp_url()
        token = os.getenv("SENPI_AUTH_TOKEN", "").strip()
        wallet = os.getenv("SENPI_STRATEGY_WALLET_ADDRESS", "").strip()
        skill_name, skill_ver = _skill_attribution()
        arguments = {
            "strategyWalletAddress": wallet,
            "coin": request.coin,
            "reason": (request.rationale or "close")[:500],
            "skill_name": skill_name,
            "skill_version": skill_ver,
        }

        logger.info(
            "Senpi MCP close_position intent_id=%s coin=%s",
            intent or "none",
            request.coin,
        )

        try:
            env = run_senpi_mcp_tool_sync(
                url, token, TOOL_CLOSE_POSITION, arguments,
                timeout_s=float(os.getenv("SENPI_MCP_TIMEOUT_S", "90")),
            )
        except Exception as e:
            fk, msg = classify_senpi_failure(None, e)
            logger.exception("Senpi MCP close transport error: %s", e)
            return AcpTradeResponse(
                success=False,
                error=f"senpi_mcp_transport: {msg}",
                failure_kind=fk,
            )

        if env.get("success"):
            jid = extract_job_id_from_data(env.get("data"))
            return AcpTradeResponse(
                success=True, job_id=jid, failure_kind=None, filled_via="senpi",
            )

        fk, msg = classify_senpi_failure(env, None)
        return AcpTradeResponse(
            success=False,
            error=msg or str(env.get("error")),
            failure_kind=fk,
        )


# Backwards compatibility for execution_mode imports
def senpi_transport_configured() -> bool:
    """True when MCP URL can be resolved (always true with default) and creds present."""
    return senpi_mcp_credentials_complete()


def senpi_http_transport_ready() -> bool:
    """Deprecated name: use senpi_mcp_credentials_complete(). Kept for any stale imports."""
    return senpi_mcp_credentials_complete()
