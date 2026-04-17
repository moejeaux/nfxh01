"""Scheduled six-hour Fathom retrospective: journal window + market snapshot -> 14b analysis."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from src.nxfh01.config_paths import find_config_yaml
from src.retro.advisor import (
    build_strict_retro_user_prompt,
    call_with_retry,
)
from src.retro.analysis_parse import try_parse_analysis_json
from src.retro.applier import maybe_apply_retro_analysis
from src.retro.metrics import (
    build_extended_performance_snapshot,
    build_metrics_from_decision_rows,
)

import yaml
from dotenv import load_dotenv

from src.db.decision_journal import DecisionJournal
from src.intelligence.rollback import evaluate_pending_learning_changes
from src.market_data.hl_rate_limited_info import RateLimitedInfo
from src.market_data.hyperliquid_btc import fetch_real_market_data
from src.notifications.telegram import TelegramBot
from src.regime.detector import RegimeDetector

logger = logging.getLogger(__name__)

_RETRO_LOG = "RETRO"


def _load_config() -> dict:
    root = Path(__file__).resolve().parents[2]
    with open(root / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


def build_decisions_digest(rows: list[dict[str, Any]]) -> dict[str, Any]:
    """Compact stats + sample for prompts; all derived from DB rows."""
    if not rows:
        return {
            "decision_count": 0,
            "closed_with_pnl_count": 0,
            "total_pnl_usd": 0.0,
            "by_coin": {},
            "note": "no_trades_in_window",
        }

    closed = [r for r in rows if r.get("outcome_recorded_at") is not None]
    pnl_vals = [float(r["pnl_usd"] or 0) for r in closed]
    by_coin: dict[str, dict[str, float]] = {}
    for r in rows:
        c = str(r.get("coin") or "?")
        bucket = by_coin.setdefault(c, {"n": 0, "pnl_usd": 0.0})
        bucket["n"] += 1
        if r.get("pnl_usd") is not None:
            bucket["pnl_usd"] += float(r["pnl_usd"])

    return {
        "decision_count": len(rows),
        "closed_with_pnl_count": len(closed),
        "total_pnl_usd": sum(pnl_vals),
        "win_count": sum(1 for x in pnl_vals if x > 0),
        "loss_count": sum(1 for x in pnl_vals if x < 0),
        "by_coin": by_coin,
    }


def serialize_decisions_for_prompt(rows: list[dict[str, Any]], max_chars: int) -> str:
    """Compact lines for LLM; cap total size."""
    lines: list[str] = []
    for r in rows:
        line = (
            f"id={r.get('id')} coin={r.get('coin')} regime={r.get('regime')} "
            f"entry={r.get('entry_price')} exit={r.get('exit_price')} "
            f"pnl_usd={r.get('pnl_usd')} pnl_pct={r.get('pnl_pct')} "
            f"exit_reason={r.get('exit_reason')} fathom_mult={r.get('fathom_size_mult')}"
        )
        lines.append(line)
    text = "\n".join(lines)
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "\n…[truncated]"


def compute_next_retrospective_wait_seconds(
    last_run_at: datetime | None,
    now: datetime,
    interval_hours: int,
    initial_delay_seconds: float,
    catch_up_min_delay_seconds: float,
) -> float:
    """Seconds to wait before the next retrospective (first run or steady cadence)."""
    interval = max(1, int(interval_hours)) * 3600.0
    catch_up = max(0.0, float(catch_up_min_delay_seconds))
    initial = max(0.0, float(initial_delay_seconds))
    if last_run_at is None:
        return initial
    last = last_run_at
    if last.tzinfo is None:
        last = last.replace(tzinfo=timezone.utc)
    else:
        last = last.astimezone(timezone.utc)
    now_utc = now if now.tzinfo else now.replace(tzinfo=timezone.utc)
    if now_utc.tzinfo:
        now_utc = now_utc.astimezone(timezone.utc)
    elapsed = (now_utc - last).total_seconds()
    if elapsed >= interval:
        return catch_up
    return max(catch_up, interval - elapsed)


async def _wait_retro_shutdown(
    shutdown_event: asyncio.Event, seconds: float
) -> bool:
    """Return True if shutdown was signaled during the wait."""
    if seconds <= 0:
        return shutdown_event.is_set()
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def run_embedded_retrospective_loop(
    config: dict,
    shutdown_event: asyncio.Event,
    *,
    shared_journal: DecisionJournal | None = None,
    hl_client: Any | None = None,
    kill_switch: Any | None = None,
) -> None:
    """Delegate to unified retro scheduler in ``src.retro.loop`` (single service, shallow+deep)."""
    from src.retro.loop import run_embedded_retrospective_loop as _embedded

    await _embedded(
        config,
        shutdown_event,
        shared_journal=shared_journal,
        hl_client=hl_client,
        kill_switch=kill_switch,
    )


def _build_prompt(
    *,
    window_start: datetime,
    window_end: datetime,
    market_data: dict[str, Any],
    regime_label: str,
    regime_confidence: float,
    digest: dict[str, Any],
    decisions_block: str,
    previous_review: str | None,
) -> str:
    prev = previous_review or "(no prior retrospective in database)"
    return f"""You are the strategic review layer for an automated Hyperliquid perps system (AceVault).
Review the trading WINDOW and CURRENT MARKET SNAPSHOT. Output actionable feedback to refine strategy.
You do not execute trades or change config directly — operators apply your recommendations manually.

=== TIME WINDOW (UTC) ===
start: {window_start.isoformat()}
end: {window_end.isoformat()}

=== CURRENT MARKET / REGIME ===
btc_1h_return={market_data.get('btc_1h_return')}
btc_4h_return={market_data.get('btc_4h_return')}
btc_vol_1h={market_data.get('btc_vol_1h')}
funding_rate_pct={market_data.get('funding_rate')}
detected_regime={regime_label}
regime_confidence={regime_confidence:.2f}

=== WINDOW DIGEST (aggregates) ===
{json.dumps(digest, indent=2)}

=== PRIOR SIX-HOUR REVIEW (continuity) ===
{prev}

=== DECISIONS IN WINDOW (newest first; truncated if long) ===
{decisions_block}

=== TASK ===
1) Summarize what the data shows about performance in this window vs current conditions.
2) List key market factors (volatility, trend, funding) that matter for the next window.
3) Give concrete recommended improvements to refine strategy (parameters, filters, risk posture) — advisory only.
4) List carry-over items to check on the next run.

Respond with a short prose section, then a FINAL JSON object only (no markdown fences) with this shape:
{{"summary": "...", "market_factors": ["..."], "recommended_changes": ["..."], "carry_over": ["..."]}}
"""


def _make_hl_client(config: dict) -> RateLimitedInfo:
    hl = config["hyperliquid_api"]
    return RateLimitedInfo(
        base_url=hl["api_base_url"],
        skip_ws=True,
        rate_config=hl,
    )


def _format_telegram_action_line(item: Any) -> str:
    if isinstance(item, dict):
        parts = [
            str(item.get("action", "?")),
        ]
        if item.get("target") not in (None, ""):
            parts.append(f"target={item['target']!r}")
        if "value" in item:
            parts.append(f"value={item['value']!r}")
        return "  - " + " ".join(parts)
    return f"  - {item!s}"


def _format_high_risk_line(item: Any) -> str:
    if isinstance(item, dict):
        a = str(item.get("action", "?"))
        d = str(item.get("detail", "")).strip()
        if d:
            return f"  - {a}: {d}"
        return f"  - {a}"
    return f"  - {item!s}"


def _append_list_section(
    body_parts: list[str],
    label: str,
    val: Any,
    *,
    max_items: int = 12,
    formatter: Any = None,
) -> None:
    if val is None:
        return
    if isinstance(val, list):
        if not val:
            body_parts.append(f"{label}:\n  (none)")
            return
        lines = "\n".join(
            formatter(x) if formatter else f"  - {x!s}"
            for x in val[:max_items]
        )
        body_parts.append(f"{label}:\n{lines}")
        if len(val) > max_items:
            body_parts.append(f"  … +{len(val) - max_items} more")
    else:
        body_parts.append(f"{label}:\n  {val!s}")


def format_retrospective_telegram_message(
    *,
    run_id: str,
    window_start: datetime,
    window_end: datetime,
    analysis_json: dict[str, Any] | None,
    analysis_text: str,
    max_chars: int,
) -> str:
    """Plain-text synopsis for Telegram; capped below Telegram 4096 limit."""
    cap = min(int(max_chars), 4096)
    try:
        dur_h = (window_end - window_start).total_seconds() / 3600.0
    except Exception:
        dur_h = 0.0
    header = (
        f"Fathom 6h retrospective\n"
        f"run_id: {run_id}\n"
        f"window_utc: {window_start.isoformat()} .. {window_end.isoformat()}\n"
        f"lookback_h: {dur_h:.2f}\n"
    )
    body_parts: list[str] = []

    if analysis_json:
        sv = analysis_json.get("schema_version")
        if sv is not None:
            body_parts.append(f"schema_version: {sv}")

        for key, title in (
            ("diagnosis", "Diagnosis"),
            ("summary", "Summary"),
        ):
            v = analysis_json.get(key)
            if v is not None and str(v).strip():
                body_parts.append(f"{title}:\n{str(v).strip()}")

        conf = analysis_json.get("confidence")
        if conf is not None:
            try:
                body_parts.append(f"confidence: {float(conf):.3f}")
            except (TypeError, ValueError):
                body_parts.append(f"confidence: {conf!s}")

        eh = analysis_json.get("evaluation_horizon")
        if eh is not None and str(eh).strip():
            body_parts.append(f"evaluation_horizon: {eh!s}")

        rc = analysis_json.get("rollback_criteria")
        if rc is not None and str(rc).strip():
            body_parts.append(f"rollback_criteria:\n{str(rc).strip()}")

        _append_list_section(
            body_parts,
            "Low-risk actions",
            analysis_json.get("low_risk_actions"),
            formatter=_format_telegram_action_line,
        )
        _append_list_section(
            body_parts,
            "High-risk suggestions",
            analysis_json.get("high_risk_suggestions"),
            formatter=_format_high_risk_line,
        )
        for label, key in (
            ("Market factors", "market_factors"),
            ("Recommended changes", "recommended_changes"),
            ("Carry over", "carry_over"),
        ):
            _append_list_section(body_parts, label, analysis_json.get(key))

    else:
        snippet = (analysis_text or "")[: max(0, cap - len(header) - 80)]
        body_parts.append("Summary: (JSON parse failed; excerpt below)\n" + snippet)

    joined = "\n\n".join(body_parts)
    sparse = len(joined.strip()) < 120
    raw = (analysis_text or "").strip()
    if analysis_json and raw and sparse:
        excerpt_budget = max(0, cap - len(header) - len(joined) - 120)
        if excerpt_budget > 80:
            ex = raw[:excerpt_budget]
            body_parts.append(
                "--- Model output (excerpt; JSON body was empty or minimal) ---\n" + ex
            )

    text = header + "\n" + "\n\n".join(body_parts)
    if len(text) > cap:
        text = text[: cap - 3] + "..."
    return text


async def _maybe_send_telegram_retrospective(
    fr: dict[str, Any],
    *,
    run_id: str,
    window_start: datetime,
    window_end: datetime,
    analysis_json: dict[str, Any] | None,
    analysis_text: str,
) -> None:
    """Notify Telegram if enabled; failures are logged only (exit code unchanged)."""
    if not fr.get("telegram_notify", False):
        logger.info("%s_TELEGRAM_SKIPPED reason=config_disabled", _RETRO_LOG)
        return

    token = (os.getenv("TELEGRAM_BOT_TOKEN") or "").strip()
    chat_id = (os.getenv("TELEGRAM_CHAT_ID") or "").strip()
    if not token or not chat_id:
        logger.info("%s_TELEGRAM_SKIPPED reason=missing_credentials", _RETRO_LOG)
        return

    max_chars = int(fr.get("telegram_message_max_chars", 3500))
    msg = format_retrospective_telegram_message(
        run_id=run_id,
        window_start=window_start,
        window_end=window_end,
        analysis_json=analysis_json,
        analysis_text=analysis_text,
        max_chars=max_chars,
    )

    bot = TelegramBot(token, chat_id)
    try:
        ok = await asyncio.to_thread(bot.notify, msg)
        if ok:
            logger.info("%s_TELEGRAM_SENT ok=true", _RETRO_LOG)
        else:
            logger.warning("%s_TELEGRAM_FAILED ok=false", _RETRO_LOG)
    except Exception as e:
        logger.warning("%s_TELEGRAM_FAILED error=%s", _RETRO_LOG, e)


def _kill_switch_any_active(kill_switch: Any | None) -> bool:
    if kill_switch is None:
        return False
    for eid in ("acevault", "growi", "mc"):
        try:
            if kill_switch.is_active(eid):
                return True
        except Exception:
            continue
    return False


async def run_six_hour_retrospective(
    config: dict,
    database_url: str,
    ollama_base_url: str,
    *,
    shared_journal: DecisionJournal | None = None,
    hl_client: Any | None = None,
    mode: str = "deep",
    lookback_hours: int | None = None,
    out_meta: dict[str, Any] | None = None,
    kill_switch: Any | None = None,
) -> int:
    """Run one retrospective; return process exit code (0 ok, 1 error)."""
    fr = config.get("fathom_retrospective") or {}
    retro = config.get("retro") or {}
    if not fr.get("enabled", False) and not retro.get("enabled", False):
        logger.info("%s_DISABLED config=retro_and_fathom_retrospective", _RETRO_LOG)
        return 0

    if lookback_hours is not None:
        lookback = int(lookback_hours)
    elif mode == "shallow":
        lookback = int(retro.get("shallow_lookback_hours", fr.get("lookback_hours", 6)))
    else:
        lookback = int(retro.get("deep_lookback_hours", fr.get("lookback_hours", 6)))
    max_rows = int((retro.get("max_decision_rows") if retro else None) or fr.get("max_decision_rows", 200))
    deep_model = str(
        retro.get("deep_model") or fr.get("deep_model", "fathom-r1-14b")
    )
    fathom_cfg = config.get("fathom") or {}
    timeout_s = float(
        fathom_cfg.get("retro_timeout_seconds", fr.get("timeout_seconds", 600))
    )
    num_predict = int(fr.get("num_predict", 2048))
    temperature = float(fr.get("temperature", 0.2))
    prev_n = int(fr.get("previous_runs_in_prompt", 1))
    max_prompt_chars = int(fr.get("max_decisions_prompt_chars", 12000))

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=lookback)

    logger.info(
        "%s_START mode=%s window_start=%s window_end=%s lookback_hours=%d model=%s",
        _RETRO_LOG,
        mode,
        window_start.isoformat(),
        window_end.isoformat(),
        lookback,
        deep_model,
    )

    own_journal = shared_journal is None
    if own_journal:
        journal = DecisionJournal(database_url)
        await journal.connect(config)
    else:
        journal = shared_journal
        if not journal.is_connected():
            logger.error("%s_ABORT reason=shared_journal_not_connected", _RETRO_LOG)
            return 1

    try:
        if journal.is_connected():
            try:
                await evaluate_pending_learning_changes(config, journal)
            except Exception as e:
                logger.warning("%s_LEARN_EVAL_FAILED error=%s", _RETRO_LOG, e)

        market_data: dict[str, Any]
        try:
            hl_for_fetch = hl_client if hl_client is not None else _make_hl_client(config)
            market_data = await fetch_real_market_data(hl_for_fetch)
        except Exception as e:
            logger.warning("%s_MARKET_INIT_FAILED error=%s using_fallback", _RETRO_LOG, e)
            market_data = {
                "btc_1h_return": 0.0,
                "btc_4h_return": 0.0,
                "btc_vol_1h": 0.004,
                "funding_rate": 0.0,
            }

        regime_detector = RegimeDetector(config, data_fetcher=None)
        rs = regime_detector.detect(market_data)
        regime_label = rs.regime.value
        regime_confidence = rs.confidence

        rows = await journal.fetch_decisions_in_window(
            window_start, window_end, max_rows
        )
        if not rows:
            logger.info("%s_WINDOW_EMPTY trades_in_window=0", _RETRO_LOG)

        digest = build_decisions_digest(rows)
        decisions_block = serialize_decisions_for_prompt(rows, max_prompt_chars)

        from src.retro.gate import evaluate_retro_skip

        metrics = build_metrics_from_decision_rows(
            rows,
            recent_hours=float(retro.get("shallow_lookback_hours", 24)),
        )
        cooldown_active = False
        hg = retro.get("healthy_gate") or {}
        cm = float(hg.get("cooldown_minutes_after_change", 0))
        if cm > 0 and journal.is_connected():
            try:
                last = await journal.get_last_auto_apply_time()
                if last is not None:
                    if last.tzinfo is None:
                        last = last.replace(tzinfo=timezone.utc)
                    delta_min = (
                        datetime.now(timezone.utc) - last
                    ).total_seconds() / 60.0
                    if delta_min < cm:
                        cooldown_active = True
            except Exception as e:
                logger.warning("%s_COOLDOWN_QUERY_FAILED error=%s", _RETRO_LOG, e)

        skip_dec = evaluate_retro_skip(
            config,
            metrics,
            mode=mode,
            cooldown_active=cooldown_active,
        )
        if skip_dec.skip_fathom:
            if out_meta is not None:
                out_meta["skipped_fathom_healthy"] = True
                out_meta["skip_reason"] = skip_dec.reason
            return 0

        last_runs = await journal.get_recent_retrospectives(1)
        previous_run_id = (
            str(last_runs[0]["id"]) if last_runs else None
        )

        previous_review: str | None = None
        if prev_n > 0:
            prompt_runs = await journal.get_recent_retrospectives(prev_n)
            parts: list[str] = []
            for pr in prompt_runs:
                if pr.get("analysis_text"):
                    parts.append(str(pr["analysis_text"])[:8000])
                if pr.get("analysis_json"):
                    parts.append(json.dumps(pr["analysis_json"], indent=2)[:4000])
            previous_review = "\n---\n".join(parts) if parts else None

        learning_eff: dict[str, Any] = {}
        if journal.is_connected():
            try:
                learning_eff = await journal.learning_effectiveness_ratio()
            except Exception as e:
                logger.warning("%s_LEARNING_EFF_FAILED error=%s", _RETRO_LOG, e)

        performance_snapshot = build_extended_performance_snapshot(
            rows,
            config,
            recent_hours=float(retro.get("shallow_lookback_hours", 24)),
            learning_effectiveness=learning_eff,
        )
        performance_snapshot["regime"] = regime_label
        performance_snapshot["regime_confidence"] = regime_confidence
        performance_snapshot["market_data"] = dict(market_data)

        adv_sv = int((retro or {}).get("advisor_schema_version", 1))
        user_prompt = build_strict_retro_user_prompt(
            mode=mode,
            window_start=window_start,
            window_end=window_end,
            performance_snapshot=performance_snapshot,
            decisions_block=decisions_block,
            previous_review=previous_review,
            advisor_schema_version=adv_sv,
        )

        raw_text = ""
        analysis_json: dict[str, Any] | None = None
        try:
            raw_text, analysis_json = await call_with_retry(
                config,
                ollama_base_url,
                user_prompt,
                model=deep_model,
                num_predict=num_predict,
                temperature=temperature,
                timeout_s=timeout_s,
            )
        except Exception as e:
            logger.error("%s_OLLAMA_FAILED error=%s", _RETRO_LOG, e)
            return 1

        if analysis_json is None:
            analysis_json = try_parse_analysis_json(raw_text)
        if analysis_json is None:
            logger.warning("%s_PARSE_FALLBACK storing_raw_text_only", _RETRO_LOG)

        new_id = await journal.insert_retrospective_run(
            window_start=window_start,
            window_end=window_end,
            market_snapshot=dict(market_data),
            decisions_digest=digest,
            analysis_text=raw_text,
            analysis_json=analysis_json,
            previous_run_id=previous_run_id,
            model_used=deep_model,
        )

        logger.info(
            "%s_COMPLETE id=%s analysis_json=%s",
            _RETRO_LOG,
            new_id,
            "yes" if analysis_json else "no",
        )

        await _maybe_send_telegram_retrospective(
            fr,
            run_id=new_id,
            window_start=window_start,
            window_end=window_end,
            analysis_json=analysis_json,
            analysis_text=raw_text,
        )

        if analysis_json and isinstance(analysis_json, dict):
            try:
                cfg_path = find_config_yaml()
                bp = metrics.global_profit_factor
                if bp == float("inf"):
                    bp = 2.0
                await maybe_apply_retro_analysis(
                    config=config,
                    config_path=cfg_path,
                    analysis_json=analysis_json,
                    journal=journal,
                    retrospective_run_id=new_id,
                    mode=mode,
                    kill_switch_any_active=_kill_switch_any_active(kill_switch),
                    metrics=metrics,
                    baseline_pf=float(bp),
                )
            except Exception as e:
                logger.warning("%s_APPLIER_FAILED error=%s", _RETRO_LOG, e, exc_info=True)

        return 0
    finally:
        if own_journal:
            await journal.close()


def main() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.error("%s_ABORT reason=DATABASE_URL_missing", _RETRO_LOG)
        return 1

    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    config = _load_config()
    return asyncio.run(run_six_hour_retrospective(config, db_url, ollama_url))


if __name__ == "__main__":
    raise SystemExit(main())
