"""Fail-fast validation for orchestration and strategy blocks (structure only; thresholds live in YAML)."""

from __future__ import annotations

from typing import Any

from src.exits.policy_config import validate_exit_policy_config
from src.nxfh01.orchestration.strategy_registry import StrategyRegistry


def _engine_ids_declared_in_engines_block(config: dict) -> set[str]:
    out: set[str] = set()
    for k, v in (config.get("engines") or {}).items():
        if k in ("planned_count", "acevault_engine_id"):
            continue
        if isinstance(v, dict) and ("loss_pct" in v or "cooldown_hours" in v):
            out.add(k)
    return out


def validate_multi_strategy_config(config: dict) -> None:
    """
    Raise ``ValueError`` with a clear message if orchestration config is inconsistent.

    Does not substitute business judgment for risk thresholds — only structural coherence.
    """
    validate_exit_policy_config(config)

    orch = config.get("orchestration")
    if orch is None:
        return

    if not isinstance(orch, dict):
        raise ValueError("orchestration must be a mapping")

    tick = orch.get("tick_interval_seconds")
    if tick is not None:
        t = float(tick)
        if t <= 0:
            raise ValueError("orchestration.tick_interval_seconds must be positive")

    order = list(orch.get("execution_order") or [])
    if len(order) != len(set(order)):
        raise ValueError("orchestration.execution_order must not contain duplicate entries")

    reg = StrategyRegistry(config)
    known_sk = set(reg.strategy_keys())

    for sk in order:
        if sk not in known_sk:
            raise ValueError(
                "orchestration.execution_order references unknown strategy_key=%r "
                "(known=%s)" % (sk, sorted(known_sk))
            )

    conflict = orch.get("conflict") or {}
    pri = list(conflict.get("priority") or [])
    for sk in pri:
        if sk not in known_sk:
            raise ValueError(
                "orchestration.conflict.priority references unknown strategy_key=%r" % (sk,)
            )

    mode = conflict.get("mode", "skip_opposing")
    if mode not in ("skip_opposing", "priority"):
        raise ValueError(
            "orchestration.conflict.mode must be 'skip_opposing' or 'priority', got %r" % (mode,)
        )

    declared_engines = _engine_ids_declared_in_engines_block(config)
    for sk in known_sk:
        row = reg.raw_row(sk)
        eid = row.get("engine_id")
        if not eid:
            continue
        if declared_engines and eid not in declared_engines:
            raise ValueError(
                "strategies.%s.engine_id=%r has no matching entry under engines: "
                "(declared engine ids=%s)"
                % (sk, eid, sorted(declared_engines))
            )
