from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_PARTIAL_TP_EXECUTION_SUPPORTED = False


def deep_merge_dict(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Recursively merge *override* into a shallow copy of *base*."""
    out = dict(base)
    for k, v in override.items():
        if k in out and isinstance(out[k], dict) and isinstance(v, dict):
            out[k] = deep_merge_dict(out[k], v)
        else:
            out[k] = v
    return out


def _normalize_regime_key(raw: str | None) -> str | None:
    if raw is None:
        return None
    return raw.strip().lower().replace("-", "_")


def _engine_block(config: dict[str, Any], strategy_key: str) -> dict[str, Any]:
    """Return the top-level engine config block for *strategy_key* (e.g. ``config["acevault"]``)."""
    strategies = config.get("strategies") or {}
    row = strategies.get(strategy_key) or {}
    engine_id = row.get("engine_id", strategy_key)
    return config.get(engine_id) or {}


def resolve_engine_exit_overrides(
    config: dict[str, Any],
    strategy_key: str,
    regime: str | None,
) -> dict[str, Any]:
    """Extract exit overrides for *regime* from the engine block's ``exit_overrides`` section.

    Returns an empty dict when no overrides apply (no matching regime, no
    ``exit_overrides`` block, or regime is ``None``).
    """
    regime_norm = _normalize_regime_key(regime)
    if regime_norm is None:
        return {}
    eb = _engine_block(config, strategy_key)
    overrides_by_regime = eb.get("exit_overrides") or {}
    return dict(overrides_by_regime.get(regime_norm) or {})


def resolve_engine_exit_config(
    config: dict[str, Any],
    strategy_key: str,
    regime: str | None,
) -> dict[str, Any]:
    """Return effective scalar exit config (SL/TP distances) for an engine+regime.

    Starts from the engine block defaults and merges any regime-specific overrides.
    Only scalar keys are relevant here (``stop_loss_distance_pct``, ``take_profit_distance_pct``).
    """
    eb = _engine_block(config, strategy_key)
    base = {
        "stop_loss_distance_pct": eb.get("stop_loss_distance_pct"),
        "take_profit_distance_pct": eb.get("take_profit_distance_pct"),
    }
    overrides = resolve_engine_exit_overrides(config, strategy_key, regime)
    for k in ("stop_loss_distance_pct", "take_profit_distance_pct"):
        if k in overrides:
            base[k] = overrides[k]
    return base


def resolve_exit_policy(
    config: dict[str, Any],
    strategy_key: str,
    *,
    regime: str | None = None,
) -> dict[str, Any]:
    """Merge global ``exits`` with strategy-level and engine/regime-level overrides.

    Resolution order (later wins):
      1. ``config["exits"]`` — global defaults
      2. ``config["strategies"][strategy_key]["exits"]`` — strategy overrides
      3. ``config[engine_id]["exit_overrides"][regime]`` — regime-specific overrides

    Only dict-valued keys are deep-merged; scalars are replaced outright.
    """
    root = dict(config.get("exits") or {})
    strategies = config.get("strategies") or {}
    sk_row = strategies.get(strategy_key) or {}
    strategy_exits = sk_row.get("exits") or {}
    if strategy_exits:
        root = deep_merge_dict(root, dict(strategy_exits))

    regime_overrides = resolve_engine_exit_overrides(config, strategy_key, regime)
    exit_keys = {
        k for k in regime_overrides
        if k not in ("stop_loss_distance_pct", "take_profit_distance_pct")
    }
    if exit_keys:
        regime_exit_dict = {k: regime_overrides[k] for k in exit_keys}
        root = deep_merge_dict(root, regime_exit_dict)

    return root


def validate_partial_tp_config(partial: dict[str, Any] | None, context: str = "") -> None:
    """Validate a ``partial_tp`` dict.

    Raises ``ValueError`` if:
    - ``enabled: true`` and execution plumbing does not yet support sized closes
    - ``levels`` entries are malformed (negative trigger_r, fraction out of range, sum > 1)
    """
    if partial is None or not isinstance(partial, dict):
        return

    enabled = partial.get("enabled", False)
    if enabled and not _PARTIAL_TP_EXECUTION_SUPPORTED:
        raise ValueError(
            f"partial_tp.enabled=true is not allowed{' in ' + context if context else ''}: "
            "execution layer does not support sized partial closes. "
            "Set partial_tp.enabled: false or remove the key."
        )

    levels = partial.get("levels")
    if levels is None:
        return
    if not isinstance(levels, list):
        raise ValueError(
            f"partial_tp.levels must be a list{' in ' + context if context else ''}, "
            f"got {type(levels).__name__}"
        )

    total_fraction = 0.0
    prev_trigger: float | None = None
    for i, lvl in enumerate(levels):
        if not isinstance(lvl, dict):
            raise ValueError(f"partial_tp.levels[{i}] must be a dict{' in ' + context if context else ''}")
        tr = lvl.get("trigger_r")
        sf = lvl.get("size_fraction")
        if tr is None or sf is None:
            raise ValueError(
                f"partial_tp.levels[{i}] must have trigger_r and size_fraction"
                f"{' in ' + context if context else ''}"
            )
        if float(tr) <= 0:
            raise ValueError(f"partial_tp.levels[{i}].trigger_r must be positive{' in ' + context if context else ''}")
        if not (0 < float(sf) <= 1.0):
            raise ValueError(
                f"partial_tp.levels[{i}].size_fraction must be in (0, 1]{' in ' + context if context else ''}"
            )
        if prev_trigger is not None and float(tr) < prev_trigger:
            raise ValueError(
                f"partial_tp.levels must be sorted by trigger_r (level {i} = {tr} < {prev_trigger})"
                f"{' in ' + context if context else ''}"
            )
        prev_trigger = float(tr)
        total_fraction += float(sf)

    if total_fraction > 1.0 + 1e-9:
        raise ValueError(
            f"partial_tp.levels total size_fraction={total_fraction:.4f} exceeds 1.0"
            f"{' in ' + context if context else ''}"
        )


def validate_exit_policy_config(config: dict[str, Any]) -> None:
    """Top-level validation of all exit-related config sections.

    Called at startup to fail fast on malformed exit configs.
    """
    root_exits = config.get("exits") or {}
    root_partial = root_exits.get("partial_tp")
    validate_partial_tp_config(root_partial, context="exits.partial_tp")

    for sk, row in (config.get("strategies") or {}).items():
        if not isinstance(row, dict):
            continue
        strat_exits = row.get("exits") or {}
        sp = strat_exits.get("partial_tp")
        validate_partial_tp_config(sp, context=f"strategies.{sk}.exits.partial_tp")

    for sk, row in (config.get("strategies") or {}).items():
        if not isinstance(row, dict):
            continue
        engine_id = row.get("engine_id", sk)
        eb = config.get(engine_id) or {}
        for regime_key, overrides in (eb.get("exit_overrides") or {}).items():
            if not isinstance(overrides, dict):
                continue
            ep = overrides.get("partial_tp")
            validate_partial_tp_config(
                ep, context=f"{engine_id}.exit_overrides.{regime_key}.partial_tp"
            )
