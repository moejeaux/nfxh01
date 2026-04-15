"""Strategy enable flags and engine_id mapping (KillSwitch keys)."""

from __future__ import annotations

from typing import Any


def default_strategies_block(config: dict) -> dict[str, Any]:
    """Backward-compatible defaults when ``strategies:`` is absent."""
    av_interval = config.get("acevault", {}).get("cycle_interval_seconds", 15)
    return {
        "acevault": {
            "enabled": True,
            "engine_id": "acevault",
            "cycle_interval_seconds": av_interval,
        },
        "growi_hf": {
            "enabled": False,
            "engine_id": "growi",
            "cycle_interval_seconds": 60,
            "max_candidates": 10,
        },
        "mc_recovery": {
            "enabled": False,
            "engine_id": "mc",
            "cycle_interval_seconds": 120,
            "max_candidates": 3,
        },
    }


class StrategyRegistry:
    def __init__(self, config: dict) -> None:
        self._config = config
        merged = default_strategies_block(config)
        user = dict(config.get("strategies") or {})
        for key, defaults in merged.items():
            u = user.pop(key, {})
            merged[key] = {**defaults, **u}
        for key, u in user.items():
            merged[key] = u
        self._strategies = merged

    def strategy_keys(self) -> list[str]:
        return list(self._strategies.keys())

    def is_enabled(self, strategy_key: str) -> bool:
        s = self._strategies.get(strategy_key, {})
        return bool(s.get("enabled", False))

    def engine_id(self, strategy_key: str) -> str:
        return str(self._strategies.get(strategy_key, {}).get("engine_id", strategy_key))

    def cycle_interval_seconds(self, strategy_key: str) -> float:
        s = self._strategies.get(strategy_key, {})
        if "cycle_interval_seconds" in s:
            return float(s["cycle_interval_seconds"])
        if strategy_key == "acevault":
            return float(self._config.get("acevault", {}).get("cycle_interval_seconds", 15))
        return 60.0

    def raw_row(self, strategy_key: str) -> dict[str, Any]:
        return dict(self._strategies.get(strategy_key) or {})
