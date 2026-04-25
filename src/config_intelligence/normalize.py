"""Canonical config representation for stable hashing."""

from __future__ import annotations

import copy
from typing import Any

_FLOAT_DECIMALS = 8

_SENSITIVE_KEY_SUBSTRINGS = (
    "secret",
    "password",
    "private_key",
    "api_key",
    "api_secret",
    "webhook_secret",
    "token",
)


def _is_sensitive_key(key: str) -> bool:
    lk = key.lower()
    return any(s in lk for s in _SENSITIVE_KEY_SUBSTRINGS)


def strip_sensitive(obj: Any) -> Any:
    """Remove or redact credentials before hashing or persisting snapshots."""
    if isinstance(obj, dict):
        out: dict[str, Any] = {}
        for k, v in obj.items():
            ks = str(k)
            if _is_sensitive_key(ks):
                out[ks] = "<redacted>"
            elif ks.lower() == "database" and isinstance(v, dict):
                safe = {}
                for dk, dv in v.items():
                    dks = str(dk).lower()
                    if dks in ("pool_min_size", "pool_max_size"):
                        safe[dk] = dv
                out[ks] = safe if safe else {"_redacted": True}
            else:
                out[ks] = strip_sensitive(v)
        return out
    if isinstance(obj, list):
        return [strip_sensitive(x) for x in obj]
    return copy.deepcopy(obj)


def canonicalize_for_hash(obj: Any) -> Any:
    """Deterministic structure: sorted dict keys, rounded floats, deep copy."""
    if obj is None:
        return None
    if isinstance(obj, float):
        return round(obj, _FLOAT_DECIMALS)
    if isinstance(obj, bool):
        return obj
    if isinstance(obj, int):
        return obj
    if isinstance(obj, str):
        return obj
    if isinstance(obj, dict):
        return {
            str(k): canonicalize_for_hash(obj[k])
            for k in sorted(obj.keys(), key=lambda x: str(x))
        }
    if isinstance(obj, list):
        return [canonicalize_for_hash(x) for x in obj]
    return str(obj)
