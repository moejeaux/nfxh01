from __future__ import annotations

from typing import Any


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def resolve_exit_policy(config: dict[str, Any], strategy_key: str) -> dict[str, Any]:
    """Merge global ``exits`` with ``strategies.<key>.exits`` overrides."""
    root = config.get("exits") or {}
    strategies = config.get("strategies") or {}
    sk_row = strategies.get(strategy_key) or {}
    ov = sk_row.get("exits") or {}
    if not ov:
        return dict(root)
    return _deep_merge(dict(root), dict(ov))
