"""Parse JSON objects from model output (shared by retrospective and advisor)."""

from __future__ import annotations

import json
from typing import Any


def try_parse_analysis_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON object from model output."""
    s = raw.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    i = s.find("{")
    j = s.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(s[i : j + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None
