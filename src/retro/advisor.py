"""Fathom 14B retrospective advisor — JSON-only structured output."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from src.retro.analysis_parse import try_parse_analysis_json

logger = logging.getLogger(__name__)

STRICT_SYSTEM_PROMPT = """You are the retrospective intelligence layer for an automated Hyperliquid perps system.
You do NOT execute trades or modify live order flow. Output JSON ONLY — no markdown fences, no prose before or after.
The JSON must match this shape exactly (keys required):
{
  "schema_version": <int>,
  "diagnosis": "<string: main bottleneck or strength>",
  "low_risk_actions": [ {"action": "<disable_coin|reduce_position_size|raise_min_signal_score|lower_max_concurrent_positions|reduce_fathom_acevault_max_mult|no_action>", "target": "<string>", "value": <number|string|null> } ],
  "high_risk_suggestions": [ {"action": "<string>", "detail": "<string>" } ],
  "confidence": <float 0..1>,
  "evaluation_horizon": "<string, e.g. 25 trades>",
  "rollback_criteria": "<string: when to revert>"
}
Use only actions from the low_risk_actions set for automated application; put exploratory ideas in high_risk_suggestions.
"""


def build_strict_retro_user_prompt(
    *,
    mode: str,
    window_start: datetime,
    window_end: datetime,
    performance_snapshot: dict[str, Any],
    decisions_block: str,
    previous_review: str | None,
) -> str:
    prev = previous_review or "(no prior retrospective in database)"
    snap = json.dumps(performance_snapshot, indent=2, ensure_ascii=False, default=str)
    return f"""Mode: {mode}
=== TIME WINDOW (UTC) ===
start: {window_start.isoformat()}
end: {window_end.isoformat()}

=== PERFORMANCE SNAPSHOT (structured) ===
{snap}

=== PRIOR REVIEW (continuity) ===
{prev}

=== DECISIONS IN WINDOW (truncated) ===
{decisions_block}

=== TASK ===
Diagnose performance using the snapshot. Propose at most one low-risk auto-apply action unless no_action.
Respond with the JSON object only.
"""


async def call_retro_ollama(
    config: dict[str, Any],
    ollama_base_url: str,
    user_prompt: str,
    *,
    model: str,
    num_predict: int,
    temperature: float,
    timeout_s: float,
) -> str:
    fathom_cfg = config.get("fathom") or {}
    timeout_s = float(fathom_cfg.get("retro_timeout_seconds", timeout_s))
    url = ollama_base_url.rstrip("/") + "/api/chat"
    payload = {
        "model": model,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": temperature},
        "messages": [
            {"role": "system", "content": STRICT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    raw_text = ""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, json=payload)
            resp.raise_for_status()
            raw_text = (resp.json().get("message") or {}).get("content") or ""
            raw_text = str(raw_text).strip()
    except Exception as e:
        logger.error("RETRO_OLLAMA_FAILED error=%s", e)
        raise
    return raw_text


def parse_advisor_json(raw: str) -> dict[str, Any] | None:
    obj = try_parse_analysis_json(raw)
    return obj if isinstance(obj, dict) else None


async def call_with_retry(
    config: dict[str, Any],
    ollama_base_url: str,
    user_prompt: str,
    *,
    model: str,
    num_predict: int,
    temperature: float,
    timeout_s: float,
) -> tuple[str, dict[str, Any] | None]:
    """Return (raw_text, parsed_dict or None). Retries once if parse fails."""
    raw = await call_retro_ollama(
        config,
        ollama_base_url,
        user_prompt,
        model=model,
        num_predict=num_predict,
        temperature=temperature,
        timeout_s=timeout_s,
    )
    parsed = parse_advisor_json(raw)
    if parsed is not None:
        return raw, parsed
    logger.warning("RETRO_ADVISOR_PARSE_RETRY raw_len=%d", len(raw))
    raw2 = await call_retro_ollama(
        config,
        ollama_base_url,
        user_prompt + "\n\nOutput valid JSON only.",
        model=model,
        num_predict=min(num_predict, 4096),
        temperature=0.0,
        timeout_s=timeout_s,
    )
    return raw2, parse_advisor_json(raw2)
