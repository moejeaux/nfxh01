from __future__ import annotations

import json
import logging
from typing import Any

import redis

logger = logging.getLogger(__name__)

_redis = redis.Redis(host="localhost", port=6379, decode_responses=True)


def get_funding_context(coin: str) -> dict[str, Any]:
    try:
        raw = _redis.get("hl:funding:snapshot")
        if raw is None:
            return {}
        if isinstance(raw, str):
            snapshot = json.loads(raw)
        else:
            snapshot = raw
        if not isinstance(snapshot, dict):
            return {}
        inner = snapshot.get(coin, {})
        return dict(inner) if isinstance(inner, dict) else {}
    except Exception as e:
        logger.warning(
            "FUNDING_CONTEXT_GET_FAILED coin=%s error=%s",
            coin,
            e,
        )
        return {}


def enrich_funding_context(coin: str, signal: dict[str, Any]) -> dict[str, Any]:
    try:
        ctx = get_funding_context(coin)
        out = dict(signal)
        cur = ctx.get("current_rate", 0.0)
        pred = ctx.get("predicted_rate", 0.0)
        try:
            cur_f = float(cur)
        except (TypeError, ValueError):
            cur_f = 0.0
        try:
            pred_f = float(pred)
        except (TypeError, ValueError):
            pred_f = 0.0
        out["funding_rate"] = cur_f
        out["predicted_rate"] = pred_f
        out["annualized_carry"] = cur_f * 8760
        if not ctx:
            out["funding_trend"] = "unknown"
        else:
            out["funding_trend"] = "rising" if pred_f > cur_f else "flat"
        return out
    except Exception:
        out = dict(signal)
        out["funding_rate"] = 0.0
        out["predicted_rate"] = 0.0
        out["annualized_carry"] = 0.0
        out["funding_trend"] = "unknown"
        return out
