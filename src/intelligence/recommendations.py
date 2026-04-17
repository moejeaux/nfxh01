"""Validate Fathom retrospective JSON against allowed action taxonomy."""

from __future__ import annotations

from typing import Any

LOW_RISK_ACTIONS = frozenset(
    {
        "disable_coin",
        "reduce_position_size",
        "raise_min_signal_score",
        "lower_max_concurrent_positions",
        "no_action",
    }
)

QUEUE_ONLY_ACTIONS = frozenset(
    {
        "enable_coin",
        "tighten_trail",
        "widen_trail",
        "enable_partial_tp",
        "disable_strategy",
    }
)

ALL_ACTIONS = LOW_RISK_ACTIONS | QUEUE_ONLY_ACTIONS

REQUIRED_TOP_LEVEL = (
    "schema_version",
    "diagnosis",
    "low_risk_actions",
    "high_risk_suggestions",
    "confidence",
    "evaluation_horizon",
    "rollback_criteria",
)


def validate_advisor_response(obj: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return (ok, error_messages)."""
    errs: list[str] = []
    for k in REQUIRED_TOP_LEVEL:
        if k not in obj:
            errs.append(f"missing_key:{k}")
    if errs:
        return False, errs
    try:
        sv = int(obj["schema_version"])
        if sv < 1:
            errs.append("schema_version_invalid")
    except (TypeError, ValueError):
        errs.append("schema_version_not_int")
    conf = obj.get("confidence")
    try:
        cf = float(conf)
        if not 0.0 <= cf <= 1.0:
            errs.append("confidence_out_of_range")
    except (TypeError, ValueError):
        errs.append("confidence_invalid")

    for bucket in ("low_risk_actions", "high_risk_suggestions"):
        val = obj.get(bucket)
        if val is None:
            errs.append(f"{bucket}_null")
            continue
        if not isinstance(val, list):
            errs.append(f"{bucket}_not_list")
            continue
        for i, item in enumerate(val):
            if isinstance(item, dict):
                a = item.get("action") or item.get("type")
                if a and str(a) not in ALL_ACTIONS and a != "no_action":
                    errs.append(f"{bucket}[{i}]_unknown_action:{a}")
            elif isinstance(item, str) and item.strip():
                pass

    return len(errs) == 0, errs


def first_low_risk_action(obj: dict[str, Any]) -> dict[str, Any] | None:
    """First actionable low-risk item, or None."""
    raw = obj.get("low_risk_actions") or []
    if not isinstance(raw, list):
        return None
    for item in raw:
        if not isinstance(item, dict):
            continue
        action = str(item.get("action") or item.get("type") or "").strip()
        if action in LOW_RISK_ACTIONS and action != "no_action":
            return item
    return None
