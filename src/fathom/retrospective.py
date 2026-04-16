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

import httpx
import yaml
from dotenv import load_dotenv
from hyperliquid.info import Info

from src.db.decision_journal import DecisionJournal
from src.market_data.hyperliquid_btc import fetch_real_market_data
from src.notifications.telegram import TelegramBot
from src.regime.detector import RegimeDetector

logger = logging.getLogger(__name__)

_RETRO_LOG = "FATHOM_RETRO"


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


def try_parse_analysis_json(raw: str) -> dict[str, Any] | None:
    """Best-effort JSON object from model output."""
    s = raw.strip()
    if not s:
        return None
    try:
        obj = json.loads(s)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    i = s.find("{")
    j = s.rfind("}")
    if i >= 0 and j > i:
        try:
            obj = json.loads(s[i : j + 1])
            return obj if isinstance(obj, dict) else None
        except json.JSONDecodeError:
            return None
    return None


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
) -> None:
    """Background task: run six-hour retrospective on a fixed cadence while the agent runs."""
    fr = config.get("fathom_retrospective") or {}
    if not fr.get("enabled", False):
        logger.info("%s_EMBED_SKIP reason=fathom_retrospective.disabled", _RETRO_LOG)
        return
    if not fr.get("embed_in_main_process", True):
        logger.info("%s_EMBED_SKIP reason=embed_in_main_process.false", _RETRO_LOG)
        return

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.warning("%s_EMBED_SKIP reason=DATABASE_URL_missing", _RETRO_LOG)
        return

    interval_h = int(fr.get("run_interval_hours", 6))
    initial_delay = float(fr.get("initial_delay_seconds", 120))
    catch_up_min = float(fr.get("catch_up_min_delay_seconds", 5))
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    logger.info(
        "%s_EMBED_STARTED interval_hours=%d initial_delay_s=%.0f",
        _RETRO_LOG,
        interval_h,
        initial_delay,
    )

    first = True
    try:
        while not shutdown_event.is_set():
            if first:
                last_at: datetime | None = None
                j = DecisionJournal(db_url)
                try:
                    await j.connect()
                    recent = await j.get_recent_retrospectives(1)
                    if recent:
                        ca = recent[0].get("created_at")
                        if isinstance(ca, datetime):
                            last_at = ca
                except Exception as e:
                    logger.warning("%s_EMBED_LAST_RUN_QUERY_FAILED error=%s", _RETRO_LOG, e)
                finally:
                    await j.close()

                now = datetime.now(timezone.utc)
                wait = compute_next_retrospective_wait_seconds(
                    last_at,
                    now,
                    interval_h,
                    initial_delay,
                    catch_up_min,
                )
                first = False
            else:
                wait = float(interval_h * 3600)

            if await _wait_retro_shutdown(shutdown_event, wait):
                break

            try:
                code = await run_six_hour_retrospective(config, db_url, ollama_url)
                logger.info("%s_EMBED_RUN_DONE exit_code=%d", _RETRO_LOG, code)
            except asyncio.CancelledError:
                logger.info("%s_EMBED_CANCELLED during_run", _RETRO_LOG)
                raise
            except Exception as e:
                logger.error("%s_EMBED_RUN_FAILED error=%s", _RETRO_LOG, e, exc_info=True)
    except asyncio.CancelledError:
        logger.info("%s_EMBED_CANCELLED", _RETRO_LOG)
        raise


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


def _make_hl_client() -> Info:
    return Info(base_url="https://api.hyperliquid.xyz", skip_ws=True)


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
    header = (
        f"Fathom 6h retrospective\n"
        f"run_id: {run_id}\n"
        f"window_utc: {window_start.isoformat()} .. {window_end.isoformat()}\n"
    )
    body_parts: list[str] = []

    if analysis_json:
        if analysis_json.get("summary"):
            body_parts.append("Summary:\n" + str(analysis_json["summary"]))
        for label, key in (
            ("Market factors", "market_factors"),
            ("Recommended changes", "recommended_changes"),
            ("Carry over", "carry_over"),
        ):
            val = analysis_json.get(key)
            if val:
                if isinstance(val, list):
                    lines = "\n".join(f"  - {x}" for x in val[:30])
                    body_parts.append(f"{label}:\n{lines}")
                else:
                    body_parts.append(f"{label}:\n  {val}")
    else:
        snippet = (analysis_text or "")[: max(0, cap - len(header) - 80)]
        body_parts.append("Summary: (JSON parse failed; excerpt below)\n" + snippet)

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


async def run_six_hour_retrospective(
    config: dict,
    database_url: str,
    ollama_base_url: str,
) -> int:
    """Run one retrospective; return process exit code (0 ok, 1 error)."""
    fr = config.get("fathom_retrospective") or {}
    if not fr.get("enabled", False):
        logger.info("%s_DISABLED config=fathom_retrospective.enabled", _RETRO_LOG)
        return 0

    lookback = int(fr.get("lookback_hours", 6))
    max_rows = int(fr.get("max_decision_rows", 200))
    deep_model = str(fr.get("deep_model", "fathom-r1-14b"))
    timeout_s = float(fr.get("timeout_seconds", 600))
    num_predict = int(fr.get("num_predict", 2048))
    temperature = float(fr.get("temperature", 0.2))
    prev_n = int(fr.get("previous_runs_in_prompt", 1))
    max_prompt_chars = int(fr.get("max_decisions_prompt_chars", 12000))

    window_end = datetime.now(timezone.utc)
    window_start = window_end - timedelta(hours=lookback)

    logger.info(
        "%s_START window_start=%s window_end=%s lookback_hours=%d model=%s",
        _RETRO_LOG,
        window_start.isoformat(),
        window_end.isoformat(),
        lookback,
        deep_model,
    )

    journal = DecisionJournal(database_url)
    await journal.connect()

    try:
        market_data: dict[str, Any]
        try:
            hl = _make_hl_client()
            market_data = await fetch_real_market_data(hl)
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

        prompt = _build_prompt(
            window_start=window_start,
            window_end=window_end,
            market_data=market_data,
            regime_label=regime_label,
            regime_confidence=regime_confidence,
            digest=digest,
            decisions_block=decisions_block,
            previous_review=previous_review,
        )

        url = ollama_base_url.rstrip("/") + "/api/chat"
        payload = {
            "model": deep_model,
            "stream": False,
            "options": {"num_predict": num_predict, "temperature": temperature},
            "messages": [
                {
                    "role": "system",
                    "content": "You are a disciplined trading systems analyst. Be specific and cite metrics from the user message.",
                },
                {"role": "user", "content": prompt},
            ],
        }

        raw_text = ""
        try:
            async with httpx.AsyncClient(timeout=timeout_s) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                raw_text = (resp.json().get("message") or {}).get("content") or ""
                raw_text = str(raw_text).strip()
        except Exception as e:
            logger.error("%s_OLLAMA_FAILED error=%s", _RETRO_LOG, e)
            return 1

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

        return 0
    finally:
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
