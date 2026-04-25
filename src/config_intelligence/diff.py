"""Flattened semantic diff between two canonical config dicts."""

from __future__ import annotations

from typing import Any

from src.config_intelligence.bundles import match_semantic_bundles


def _flatten(prefix: str, obj: Any, out: dict[str, Any]) -> None:
    if isinstance(obj, dict):
        for k in sorted(obj.keys(), key=str):
            p = f"{prefix}.{k}" if prefix else str(k)
            _flatten(p, obj[k], out)
        return
    if isinstance(obj, list):
        for i, item in enumerate(obj):
            _flatten(f"{prefix}[{i}]", item, out)
        return
    out[prefix] = obj


def flatten_config(canonical: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    _flatten("", canonical, out)
    return out


def _json_type_name(v: Any) -> str:
    if v is None:
        return "null"
    if isinstance(v, bool):
        return "boolean"
    if isinstance(v, int):
        return "integer"
    if isinstance(v, float):
        return "number"
    if isinstance(v, str):
        return "string"
    return type(v).__name__


def classify_path(path: str, rules: list[dict[str, Any]] | None) -> str:
    """Map dot-path to change_category using optional config rules."""
    if rules:
        for row in rules:
            try:
                pfx = str(row.get("prefix") or "")
                cat = str(row.get("category") or "").strip()
                if pfx and cat and path.startswith(pfx):
                    return cat
            except (TypeError, AttributeError):
                continue
    lp = path.lower()
    if "safety_mode" in lp or lp.startswith("risk.safety"):
        return "safety_mode"
    if "shadow" in lp:
        return "shadow_mode"
    if "stop" in lp and ("loss" in lp or "sl" in lp):
        return "stop_loss"
    if "take_profit" in lp or "tp" in lp:
        return "take_profit"
    if "trail" in lp:
        return "trailing"
    if "time_stop" in lp or "timestop" in lp:
        return "time_stop"
    if lp.startswith("risk."):
        return "risk"
    if "fathom" in lp:
        return "advisor"
    if "kill" in lp:
        return "kill_switch"
    if "regime" in lp:
        return "regime_gate"
    if "score" in lp or "threshold" in lp:
        return "score_threshold"
    if "opportunity" in lp:
        return "entry_filter"
    if "execution" in lp:
        return "execution"
    if "fee" in lp or "slippage" in lp:
        return "slippage"
    return "misc"


def diff_versions(
    old_c: dict[str, Any],
    new_c: dict[str, Any],
    *,
    category_rules: list[dict[str, Any]] | None = None,
    bundle_rules: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Return list of change event row dicts (without ids) suitable for INSERT."""
    fo = flatten_config(old_c)
    fn = flatten_config(new_c)
    keys = sorted(set(fo.keys()) | set(fn.keys()))
    events: list[dict[str, Any]] = []
    for path in keys:
        a, b = fo.get(path), fn.get(path)
        if a == b:
            continue
        cat = classify_path(path, category_rules)
        events.append(
            {
                "path": path or ".",
                "old_value": a,
                "new_value": b,
                "value_type": _json_type_name(b if b is not None else a),
                "change_category": cat,
                "change_tags": [],
                "change_kind": "leaf",
            }
        )
    events.extend(
        match_semantic_bundles(events, bundle_rules or [], old_c, new_c)
    )
    return events
