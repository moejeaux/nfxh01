"""Fathom 14B retrospective advisor — JSON-only structured output."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

import httpx

from src.intelligence.recommendations import REQUIRED_TOP_LEVEL, validate_advisor_response
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
    advisor_schema_version: int = 1,
) -> str:
    prev = previous_review or "(no prior retrospective in database)"
    snap = json.dumps(performance_snapshot, indent=2, ensure_ascii=False, default=str)
    return f"""Mode: {mode}
=== TIME WINDOW (UTC) ===
start: {window_start.isoformat()}
end: {window_end.isoformat()}

=== JSON SCHEMA VERSION (mandatory) ===
Set "schema_version" to exactly {int(advisor_schema_version)} (integer). The server rejects any other value for automated config application.

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
    use_json_response_format: bool = True,
) -> str:
    fathom_cfg = config.get("fathom") or {}
    timeout_s = float(fathom_cfg.get("retro_timeout_seconds", timeout_s))
    url = ollama_base_url.rstrip("/") + "/api/chat"
    retro_cfg = config.get("retro") or {}
    want_json = bool(retro_cfg.get("ollama_json_response_format", True)) and use_json_response_format
    payload: dict[str, Any] = {
        "model": model,
        "stream": False,
        "options": {"num_predict": num_predict, "temperature": temperature},
        "messages": [
            {"role": "system", "content": STRICT_SYSTEM_PROMPT},
            {"role": "user", "content": user_prompt},
        ],
    }
    if want_json:
        payload["format"] = "json"
    raw_text = ""
    try:
        async with httpx.AsyncClient(timeout=timeout_s) as client:
            resp = await client.post(url, json=payload)
            if resp.status_code >= 400 and want_json and "format" in payload:
                logger.warning(
                    "RETRO_OLLAMA_FORMAT_JSON_REJECTED status=%s retrying_without_format",
                    resp.status_code,
                )
                del payload["format"]
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


def _minimal_advisor_example_json(schema_version: int) -> str:
    ex = {
        "schema_version": int(schema_version),
        "diagnosis": "example_only_delete_and_replace",
        "low_risk_actions": [
            {"action": "no_action", "target": "", "value": None},
        ],
        "high_risk_suggestions": [],
        "confidence": 0.5,
        "evaluation_horizon": "25 trades",
        "rollback_criteria": "none",
    }
    return json.dumps(ex, ensure_ascii=False)


def _schema_repair_suffix(errs: list[str], schema_version: int) -> str:
    keys = ", ".join(REQUIRED_TOP_LEVEL)
    err_s = "; ".join(errs[:20])
    ex = _minimal_advisor_example_json(schema_version)
    return (
        f"\n\nPrevious output failed validation: {err_s}. "
        f"Reply with ONE JSON object only (no markdown). "
        f"Required top-level keys: {keys}. "
        f"Minimal valid shape example (use schema_version={int(schema_version)} exactly): {ex}"
    )


def validated_advisor_fallback_dict(schema_version: int) -> dict[str, Any]:
    """Deterministic JSON when the model will not emit a valid advisor object."""
    obj: dict[str, Any] = {
        "schema_version": int(schema_version),
        "diagnosis": "advisor_model_output_invalid_fallback",
        "low_risk_actions": [
            {"action": "no_action", "target": "", "value": None},
        ],
        "high_risk_suggestions": [],
        "confidence": 0.0,
        "evaluation_horizon": "n/a",
        "rollback_criteria": "n/a",
    }
    ok, errs = validate_advisor_response(obj)
    if not ok:
        logger.error("RETRO_ADVISOR_FALLBACK_BROKEN errs=%s", errs)
    return obj


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
    """Return (raw_text, parsed_dict or None). Ollama JSON format + parse + schema repair."""
    np_r = min(int(num_predict), 8192)
    retro_cfg = config.get("retro") or {}
    adv_sv = int(retro_cfg.get("advisor_schema_version", 1))
    repair_temp = float(retro_cfg.get("advisor_retry_temperature", 0.0))

    async def o(msg: str, temp: float, npp: int) -> str:
        return await call_retro_ollama(
            config,
            ollama_base_url,
            msg,
            model=model,
            num_predict=npp,
            temperature=temp,
            timeout_s=timeout_s,
        )

    r1 = await o(user_prompt, temperature, num_predict)
    p1 = parse_advisor_json(r1)
    if p1:
        ok, errs = validate_advisor_response(p1)
        if ok:
            return r1, p1
        logger.warning("RETRO_ADVISOR_SCHEMA_RETRY errs=%s", errs[:10])
        r_a = await o(
            user_prompt + _schema_repair_suffix(errs, adv_sv),
            repair_temp,
            np_r,
        )
        p_a = parse_advisor_json(r_a)
        if p_a and validate_advisor_response(p_a)[0]:
            return r_a, p_a

    logger.warning("RETRO_ADVISOR_PARSE_RETRY raw_len=%d", len(r1))
    r2 = await o(
        user_prompt
        + "\n\nOutput a single JSON object only. No markdown fences or commentary.",
        repair_temp,
        np_r,
    )
    p2 = parse_advisor_json(r2)
    if p2:
        ok2, errs2 = validate_advisor_response(p2)
        if ok2:
            return r2, p2
        logger.warning("RETRO_ADVISOR_SECOND_SCHEMA_RETRY errs=%s", errs2[:10])
        r_b = await o(
            user_prompt + _schema_repair_suffix(errs2, adv_sv),
            repair_temp,
            np_r,
        )
        p_b = parse_advisor_json(r_b)
        if p_b and validate_advisor_response(p_b)[0]:
            return r_b, p_b

    fb = validated_advisor_fallback_dict(adv_sv)
    raw_fb = json.dumps(fb, ensure_ascii=False)
    logger.warning(
        "RETRO_ADVISOR_FALLBACK reason=all_retries_invalid schema_version=%s",
        adv_sv,
    )
    return raw_fb, fb
