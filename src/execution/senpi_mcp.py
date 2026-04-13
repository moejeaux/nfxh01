"""Senpi MCP client — Streamable HTTP to https://mcp.prod.senpi.ai/mcp per senpi.ai/quickstart.

Tool names and argument shapes follow Senpi-ai/senpi-skills (wolf-strategy
open-position.py): create_position, close_position.

All request/response mapping for Senpi lives in this module.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.types import CallToolResult, TextContent

logger = logging.getLogger(__name__)

DEFAULT_SENPI_MCP_URL = "https://mcp.prod.senpi.ai/mcp"


def normalize_mcp_url(raw: str | None) -> str:
    """Return MCP Streamable HTTP endpoint (Senpi prod ends with /mcp)."""
    base = (raw or "").strip()
    if not base:
        return DEFAULT_SENPI_MCP_URL
    base = base.rstrip("/")
    if base.endswith("/mcp"):
        return base
    if "mcp.prod.senpi.ai" in base and not base.endswith("mcp"):
        return f"{base}/mcp"
    return base


def _tool_result_to_envelope(result: CallToolResult) -> dict[str, Any]:
    """Normalize MCP CallToolResult to mcporter-style {success, data, error}."""
    if result.isError:
        err_text = ""
        for block in result.content or []:
            if isinstance(block, TextContent):
                err_text += block.text
        return {"success": False, "error": err_text or "mcp_tool_error", "data": None}

    if result.structuredContent is not None:
        sc = result.structuredContent
        if isinstance(sc, dict):
            if "success" in sc:
                return sc
            return {"success": True, "data": sc, "error": None}

    text = ""
    for block in result.content or []:
        if isinstance(block, TextContent):
            text += block.text
    text = text.strip()
    if text:
        try:
            parsed = json.loads(text)
            if isinstance(parsed, dict) and "success" in parsed:
                return parsed
            return {"success": True, "data": parsed, "error": None}
        except json.JSONDecodeError:
            return {"success": True, "data": {"raw": text}, "error": None}

    return {"success": True, "data": {}, "error": None}


async def call_senpi_mcp_tool(
    url: str,
    auth_token: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_s: float = 90.0,
) -> dict[str, Any]:
    """Invoke one MCP tool via Streamable HTTP."""
    headers = {"Authorization": auth_token}
    t = httpx.Timeout(timeout_s, read=timeout_s * 2)
    async with httpx.AsyncClient(headers=headers, timeout=t) as http_client:
        async with streamable_http_client(url, http_client=http_client) as (read, write, _get_sid):
            async with ClientSession(read, write) as session:
                await session.initialize()
                logger.debug("Senpi MCP call_tool %s", tool_name)
                result = await session.call_tool(tool_name, arguments)
                return _tool_result_to_envelope(result)


def run_senpi_mcp_tool_sync(
    url: str,
    auth_token: str,
    tool_name: str,
    arguments: dict[str, Any],
    timeout_s: float = 90.0,
) -> dict[str, Any]:
    """Sync entrypoint for OrderExecutor (runs asyncio under the hood)."""
    return asyncio.run(
        call_senpi_mcp_tool(url, auth_token, tool_name, arguments, timeout_s=timeout_s),
    )


def classify_senpi_failure(
    envelope: dict[str, Any] | None,
    exc: BaseException | None = None,
) -> tuple[str, str]:
    """Return (failure_kind, message). failure_kind is retryable or non_retryable."""
    if exc is not None:
        es = str(exc).lower()
        if any(x in es for x in ("timeout", "timed out", "connection", "connect", "reset", "refused")):
            return "retryable", str(exc)
        if "401" in es or "403" in es:
            return "non_retryable", str(exc)
        return "retryable", str(exc)

    if not envelope:
        return "retryable", "empty_envelope"

    if envelope.get("success"):
        return "non_retryable", "unexpected_success_envelope_in_failure_classifier"

    err = str(envelope.get("error") or envelope.get("message") or "senpi_error")
    el = err.lower()
    if any(
        x in el
        for x in (
            "validation",
            "invalid",
            "rejected",
            "insufficient",
            "not found",
            "unknown asset",
            "forbidden",
            "unauthorized",
            "margin",
            "below minimum",
            "nonce",
        )
    ):
        return "non_retryable", err
    return "retryable", err


def extract_job_id_from_data(data: Any) -> str | None:
    if data is None:
        return None
    if isinstance(data, dict):
        for key in ("orderId", "jobId", "job_id", "id", "operationId"):
            v = data.get(key)
            if v is not None:
                return str(v)
    return None
