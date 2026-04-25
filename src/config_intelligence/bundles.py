"""Higher-level semantic bundle rows derived from raw diffs."""

from __future__ import annotations

from typing import Any


def match_semantic_bundles(
    leaf_events: list[dict[str, Any]],
    bundle_rules: list[dict[str, Any]],
    _old_c: dict[str, Any],
    _new_c: dict[str, Any],
) -> list[dict[str, Any]]:
    """
    If bundle_rules define ``bundle_id`` + ``path_prefixes`` (all must match
    at least one changed path), emit one synthetic ``semantic_bundle`` row.
    """
    if not leaf_events or not bundle_rules:
        return []
    changed_paths = {e["path"] for e in leaf_events if e.get("change_kind") == "leaf"}
    out: list[dict[str, Any]] = []
    for rule in bundle_rules:
        if not isinstance(rule, dict):
            continue
        bid = str(rule.get("bundle_id") or "").strip()
        prefs = rule.get("path_prefixes")
        if not bid or not isinstance(prefs, list):
            continue
        ok = True
        for pfx in prefs:
            ps = str(pfx)
            if not any(cp.startswith(ps) for cp in changed_paths):
                ok = False
                break
        if ok:
            out.append(
                {
                    "path": f"bundle:{bid}",
                    "old_value": None,
                    "new_value": {"bundle_id": bid, "matched_prefixes": list(prefs)},
                    "value_type": "object",
                    "change_category": str(rule.get("category") or "policy"),
                    "change_tags": list(rule.get("tags") or []) if isinstance(rule.get("tags"), list) else [],
                    "change_kind": "semantic_bundle",
                }
            )
    return out
