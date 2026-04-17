"""Parse JSON objects from model output (shared by retrospective and advisor)."""

from __future__ import annotations

import json
import re
from json import JSONDecoder
from typing import Any


def _strip_markdown_json_fence(text: str) -> str:
    s = text.strip()
    if "```" not in s:
        return s
    m = re.search(
        r"```(?:json)?\s*\r?\n(.*?)\r?\n```",
        s,
        flags=re.DOTALL | re.IGNORECASE,
    )
    if m:
        return m.group(1).strip()
    return s


def try_parse_analysis_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON object from model output (handles fences + first object slice)."""
    s = _strip_markdown_json_fence(raw)
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    i = s.find("{")
    if i < 0:
        return None
    try:
        obj, _ = JSONDecoder().raw_decode(s[i:])
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        return None
