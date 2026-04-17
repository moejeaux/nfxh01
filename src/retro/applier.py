"""Safe config applier — low-risk auto-apply with ruamel backup; learning registry rows."""

from __future__ import annotations

import logging
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ruamel.yaml import YAML

from src.db.decision_journal import DecisionJournal
from src.nxfh01.config_reload import merge_into_live_config, validate_merge_preview
from src.intelligence.recommendations import (
    LOW_RISK_ACTIONS,
    first_low_risk_action,
    validate_advisor_response,
)
from src.retro.metrics import RetroMetricsSnapshot
from src.retro.registry import new_change_id

logger = logging.getLogger(__name__)

DEFENSIVE_ACTIONS = frozenset(
    {"disable_coin", "reduce_position_size", "lower_max_concurrent_positions"}
)


def _backup_config(config_path: Path) -> Path:
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    bak = config_path.with_suffix(f".yaml.bak.{ts}")
    shutil.copy2(config_path, bak)
    logger.info("LEARN_CONFIG_BACKUP path=%s", bak)
    return bak


def _load_yaml(path: Path) -> Any:
    y = YAML()
    y.preserve_quotes = True
    with open(path, encoding="utf-8") as f:
        return y.load(f)


def _save_yaml(path: Path, data: Any) -> None:
    y = YAML()
    y.preserve_quotes = True
    y.indent(mapping=2, sequence=4, offset=2)
    with open(path, "w", encoding="utf-8") as f:
        y.dump(data, f)


def _ensure_learning_map(cfg: dict[str, Any]) -> dict[str, Any]:
    lg = cfg.setdefault("learning", {})
    if not isinstance(lg, dict):
        raise ValueError("learning must be a mapping")
    return lg


async def maybe_apply_retro_analysis(
    *,
    config: dict[str, Any],
    config_path: Path,
    analysis_json: dict[str, Any],
    journal: DecisionJournal,
    retrospective_run_id: str | None,
    mode: str,
    kill_switch_any_active: bool,
    metrics: RetroMetricsSnapshot,
    baseline_pf: float,
) -> None:
    """Auto-apply at most one low-risk action when policy allows; always validate + log."""
    ok, errs = validate_advisor_response(analysis_json)
    if not ok:
        logger.warning("RETRO_APPLIER_SKIP reason=invalid_json errs=%s", errs)
        return

    retro = config.get("retro") or {}
    learn = config.get("learning") or {}
    expected_sv = int(retro.get("advisor_schema_version", 1))
    try:
        if int(analysis_json.get("schema_version", -1)) != expected_sv:
            logger.warning(
                "RETRO_APPLIER_SKIP reason=schema_version_mismatch expected=%s got=%s",
                expected_sv,
                analysis_json.get("schema_version"),
            )
            return
    except (TypeError, ValueError):
        logger.warning("RETRO_APPLIER_SKIP reason=schema_version_invalid")
        return

    min_tr = int(learn.get("min_trades_before_auto_apply", 30))
    if metrics.closing_trade_count < min_tr:
        logger.info(
            "RETRO_APPLIER_SKIP reason=insufficient_trades n=%d min=%d",
            metrics.closing_trade_count,
            min_tr,
        )
        return

    min_conf = float(learn.get("auto_apply_min_confidence", 0.75))
    try:
        conf = float(analysis_json.get("confidence", 0))
    except (TypeError, ValueError):
        conf = 0.0
    if conf < min_conf:
        logger.info("RETRO_APPLIER_SKIP reason=low_confidence conf=%.3f min=%.3f", conf, min_conf)
        return

    first = first_low_risk_action(analysis_json)
    if first is None:
        logger.info("RETRO_APPLIER_SKIP reason=no_low_risk_action")
        return

    action = str(first.get("action") or "").strip()
    if action not in LOW_RISK_ACTIONS or action == "no_action":
        logger.info("RETRO_APPLIER_SKIP reason=action_not_applicable action=%s", action)
        return

    if kill_switch_any_active and action not in DEFENSIVE_ACTIONS:
        logger.info(
            "RETRO_APPLIER_SKIP reason=kill_switch_only_defensive action=%s",
            action,
        )
        return

    cooldown_min = float((retro.get("healthy_gate") or {}).get("cooldown_minutes_after_change", 0))
    if cooldown_min > 0 and journal.is_connected():
        last = await journal.get_last_auto_apply_time()
        if last is not None:
            if last.tzinfo is None:
                last = last.replace(tzinfo=timezone.utc)
            delta = (datetime.now(timezone.utc) - last).total_seconds() / 60.0
            if delta < cooldown_min:
                logger.info(
                    "RETRO_APPLIER_SKIP reason=cooldown_minutes delta=%.1f min=%.1f",
                    delta,
                    cooldown_min,
                )
                return

    target = str(first.get("target") or "").strip()
    value = first.get("value")

    data = _load_yaml(config_path)
    if not isinstance(data, dict):
        raise ValueError("config root must be mapping")

    old_snap: dict[str, Any] = {}
    new_snap: dict[str, Any] = {}

    try:
        if action == "disable_coin":
            coin = (value or target or "").strip().upper()
            if not coin:
                logger.warning("RETRO_APPLIER_SKIP reason=disable_coin_missing_symbol")
                return
            lg = _ensure_learning_map(data)
            cur = list(lg.get("disabled_coins") or [])
            old_snap = {"disabled_coins": list(cur)}
            if coin not in {str(x).upper() for x in cur}:
                cur.append(coin)
            lg["disabled_coins"] = cur
            new_snap = {"disabled_coins": list(cur)}

        elif action == "reduce_position_size":
            strat = str(value or target or "acevault").strip().lower()
            factor = 0.9
            if isinstance(first.get("value"), (int, float)) and 0 < float(first["value"]) < 1:
                factor = float(first["value"])
            key = "acevault" if strat == "acevault" else "growi_hf" if "growi" in strat else "mc_recovery"
            sec = data.get(key)
            if not isinstance(sec, dict):
                logger.warning("RETRO_APPLIER_SKIP reason=unknown_strategy_section key=%s", key)
                return
            k = "default_position_size_usd"
            old_v = float(sec.get(k, 0))
            old_snap = {key: {k: old_v}}
            sec[k] = round(old_v * factor, 2)
            new_snap = {key: {k: sec[k]}}

        elif action == "raise_min_signal_score":
            delta = float(value) if value is not None else 0.02
            sec = data.get("acevault")
            if not isinstance(sec, dict):
                return
            k = "min_weakness_score"
            old_v = float(sec.get(k, 0.38))
            old_snap = {"acevault": {k: old_v}}
            sec[k] = min(0.99, round(old_v + delta, 4))
            new_snap = {"acevault": {k: sec[k]}}

        elif action == "lower_max_concurrent_positions":
            sec = data.get("acevault")
            if not isinstance(sec, dict):
                return
            k = "max_concurrent_positions"
            old_v = int(sec.get(k, 5))
            old_snap = {"acevault": {k: old_v}}
            sec[k] = max(1, old_v - 1)
            new_snap = {"acevault": {k: sec[k]}}

        else:
            return
    except Exception as e:
        logger.error("RETRO_APPLIER_PATCH_FAILED error=%s", e, exc_info=True)
        return

    try:
        validate_merge_preview(config, data)
    except ValueError as e:
        logger.error(
            "RETRO_APPLIER_ABORT reason=config_invalid_after_patch error=%s",
            e,
        )
        return
    except Exception as e:
        logger.error(
            "RETRO_APPLIER_ABORT reason=config_preview_failed error=%s",
            e,
            exc_info=True,
        )
        return

    _backup_config(config_path)
    _save_yaml(config_path, data)

    learn_reload = (config.get("learning") or {}).get(
        "reload_runtime_config_after_apply", True
    )
    if learn_reload:
        merge_into_live_config(config, data)
        logger.info("RISK_CONFIG_HOT_RELOAD merged_into_process=true")
        logger.info(
            "LEARN_CONFIG_WRITTEN path=%s action=%s restart_required=false",
            config_path,
            action,
        )
    else:
        logger.info(
            "LEARN_CONFIG_WRITTEN path=%s action=%s restart_required=true "
            "reason=hot_reload_disabled",
            config_path,
            action,
        )

    if not journal.is_connected():
        logger.warning("LEARN_REGISTRY_SKIP reason=journal_disconnected")
        return

    cid = new_change_id()
    await journal.insert_learning_change(
        retrospective_run_id=retrospective_run_id,
        change_id=cid,
        schema_version=int(analysis_json.get("schema_version", 1)),
        config_schema_version=int(retro.get("config_schema_version", 1)),
        advisor_schema_version=int(retro.get("advisor_schema_version", 1)),
        retro_mode=mode,
        action_type=action,
        target_key=target or action,
        old_value=old_snap,
        new_value=new_snap,
        confidence=conf,
        auto_applied=True,
        closing_trade_count_at_apply=metrics.closing_trade_count,
        baseline_profit_factor=baseline_pf,
    )


def apply_retro_recommendations_safe(
    config: dict[str, Any],
    recommendations: dict[str, Any],
    *,
    kill_switch_active: bool = False,
) -> None:
    """Sync stub for legacy callers — full path uses ``maybe_apply_retro_analysis``."""
    logger.info(
        "LEARN_APPLIER_LEGACY noop=%s keys=%s",
        kill_switch_active,
        list(recommendations.keys()) if recommendations else [],
    )
