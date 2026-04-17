"""Unified retrospective scheduler: shallow + deep, single lock, no overlapping runs."""

from __future__ import annotations

import asyncio
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any

from src.db.decision_journal import DecisionJournal
from src.fathom.retrospective import (
    compute_next_retrospective_wait_seconds,
    run_six_hour_retrospective,
)

logger = logging.getLogger(__name__)


async def _wait_retro_shutdown(
    shutdown_event: asyncio.Event, seconds: float
) -> bool:
    if seconds <= 0:
        return shutdown_event.is_set()
    try:
        await asyncio.wait_for(shutdown_event.wait(), timeout=seconds)
        return True
    except asyncio.TimeoutError:
        return False


async def run_unified_retrospective_loop(
    config: dict,
    shutdown_event: asyncio.Event,
    *,
    shared_journal: DecisionJournal | None = None,
    hl_client: Any | None = None,
    kill_switch: Any | None = None,
) -> None:
    """Shallow default 60m, deep default 6h; one lock; deep may skip shallow once if healthy."""
    retro = config.get("retro") or {}
    fr = config.get("fathom_retrospective") or {}
    if not retro.get("enabled", False):
        logger.info("RETRO_UNIFIED_SKIP reason=retro.enabled_false")
        return
    if not fr.get("embed_in_main_process", True):
        logger.info("RETRO_EMBED_SKIP reason=embed_in_main_process_false")
        return

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.warning("RETRO_EMBED_SKIP reason=DATABASE_URL_missing")
        return

    shallow_min = int(retro.get("shallow_interval_minutes", 60))
    deep_h = int(retro.get("deep_interval_hours", fr.get("run_interval_hours", 6)))
    initial_delay = float(retro.get("initial_delay_seconds", fr.get("initial_delay_seconds", 120)))
    catch_up_min = float(retro.get("catch_up_min_delay_seconds", fr.get("catch_up_min_delay_seconds", 5)))
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    lock = asyncio.Lock()
    skip_next_shallow = False
    next_deep: datetime | None = None
    next_shallow: datetime | None = None

    logger.info(
        "RETRO_UNIFIED_STARTED shallow_min=%d deep_h=%d",
        shallow_min,
        deep_h,
    )

    first = True
    try:
        while not shutdown_event.is_set():
            now = datetime.now(timezone.utc)
            if first:
                last_at: datetime | None = None
                if shared_journal is not None and shared_journal.is_connected():
                    try:
                        recent = await shared_journal.get_recent_retrospectives(1)
                        if recent:
                            ca = recent[0].get("created_at")
                            if isinstance(ca, datetime):
                                last_at = ca
                    except Exception as e:
                        logger.warning("RETRO_EMBED_LAST_RUN_QUERY_FAILED error=%s", e)
                else:
                    j = DecisionJournal(db_url)
                    try:
                        await j.connect(config)
                        recent = await j.get_recent_retrospectives(1)
                        if recent:
                            ca = recent[0].get("created_at")
                            if isinstance(ca, datetime):
                                last_at = ca
                    except Exception as e:
                        logger.warning("RETRO_EMBED_LAST_RUN_QUERY_FAILED error=%s", e)
                    finally:
                        await j.close()

                wait = compute_next_retrospective_wait_seconds(
                    last_at,
                    now,
                    deep_h,
                    initial_delay,
                    catch_up_min,
                )
                if await _wait_retro_shutdown(shutdown_event, wait):
                    break
                t0 = datetime.now(timezone.utc)
                next_deep = t0 + timedelta(hours=deep_h)
                next_shallow = t0 + timedelta(minutes=shallow_min)
                first = False
                async with lock:
                    meta: dict[str, Any] = {}
                    await run_six_hour_retrospective(
                        config,
                        db_url,
                        ollama_url,
                        shared_journal=shared_journal,
                        hl_client=hl_client,
                        mode="deep",
                        out_meta=meta,
                        kill_switch=kill_switch,
                    )
                    skip_next_shallow = bool(meta.get("skipped_fathom_healthy"))
                continue

            assert next_deep is not None and next_shallow is not None
            now = datetime.now(timezone.utc)
            if now >= next_deep:
                async with lock:
                    meta = {}
                    await run_six_hour_retrospective(
                        config,
                        db_url,
                        ollama_url,
                        shared_journal=shared_journal,
                        hl_client=hl_client,
                        mode="deep",
                        out_meta=meta,
                        kill_switch=kill_switch,
                    )
                    skip_next_shallow = bool(meta.get("skipped_fathom_healthy"))
                next_deep = now + timedelta(hours=deep_h)
                next_shallow = now + timedelta(minutes=shallow_min)
                continue

            if now >= next_shallow:
                if skip_next_shallow:
                    logger.info(
                        "RETRO_SHALLOW_SKIPPED reason=after_deep_healthy_skip "
                        "reset_skip_flag=true"
                    )
                    skip_next_shallow = False
                else:
                    async with lock:
                        await run_six_hour_retrospective(
                            config,
                            db_url,
                            ollama_url,
                            shared_journal=shared_journal,
                            hl_client=hl_client,
                            mode="shallow",
                            out_meta={},
                            kill_switch=kill_switch,
                        )
                next_shallow = now + timedelta(minutes=shallow_min)
                continue

            sleep_s = min(
                (next_deep - now).total_seconds(),
                (next_shallow - now).total_seconds(),
            )
            sleep_s = max(1.0, sleep_s)
            if await _wait_retro_shutdown(shutdown_event, sleep_s):
                break
    except asyncio.CancelledError:
        logger.info("RETRO_UNIFIED_CANCELLED")
        raise


async def _legacy_six_hour_embedded_loop(
    config: dict,
    shutdown_event: asyncio.Event,
    *,
    shared_journal: DecisionJournal | None = None,
    hl_client: Any | None = None,
    kill_switch: Any | None = None,
) -> None:
    fr = config.get("fathom_retrospective") or {}
    if not fr.get("enabled", False):
        logger.info("RETRO_EMBED_SKIP reason=fathom_retrospective_disabled")
        return
    if not fr.get("embed_in_main_process", True):
        logger.info("RETRO_EMBED_SKIP reason=embed_in_main_process_false")
        return

    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logger.warning("RETRO_EMBED_SKIP reason=DATABASE_URL_missing")
        return

    interval_h = int(fr.get("run_interval_hours", 6))
    initial_delay = float(fr.get("initial_delay_seconds", 120))
    catch_up_min = float(fr.get("catch_up_min_delay_seconds", 5))
    ollama_url = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")

    logger.info(
        "RETRO_LEGACY_EMBED_STARTED interval_hours=%d initial_delay_s=%.0f",
        interval_h,
        initial_delay,
    )

    first = True
    try:
        while not shutdown_event.is_set():
            if first:
                last_at: datetime | None = None
                if shared_journal is not None and shared_journal.is_connected():
                    try:
                        recent = await shared_journal.get_recent_retrospectives(1)
                        if recent:
                            ca = recent[0].get("created_at")
                            if isinstance(ca, datetime):
                                last_at = ca
                    except Exception as e:
                        logger.warning("RETRO_EMBED_LAST_RUN_QUERY_FAILED error=%s", e)
                else:
                    j = DecisionJournal(db_url)
                    try:
                        await j.connect(config)
                        recent = await j.get_recent_retrospectives(1)
                        if recent:
                            ca = recent[0].get("created_at")
                            if isinstance(ca, datetime):
                                last_at = ca
                    except Exception as e:
                        logger.warning("RETRO_EMBED_LAST_RUN_QUERY_FAILED error=%s", e)
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
                code = await run_six_hour_retrospective(
                    config,
                    db_url,
                    ollama_url,
                    shared_journal=shared_journal,
                    hl_client=hl_client,
                    mode="deep",
                    kill_switch=kill_switch,
                )
                logger.info("RETRO_EMBED_RUN_DONE exit_code=%d", code)
            except asyncio.CancelledError:
                logger.info("RETRO_EMBED_CANCELLED during_run")
                raise
            except Exception as e:
                logger.error("RETRO_EMBED_RUN_FAILED error=%s", e, exc_info=True)
    except asyncio.CancelledError:
        logger.info("RETRO_EMBED_CANCELLED")
        raise


async def run_embedded_retrospective_loop(
    config: dict,
    shutdown_event: asyncio.Event,
    *,
    shared_journal: DecisionJournal | None = None,
    hl_client: Any | None = None,
    kill_switch: Any | None = None,
) -> None:
    """Main process entry: unified retro when ``retro.enabled``, else legacy 6h-only loop."""
    retro = config.get("retro") or {}
    if retro.get("enabled", False):
        await run_unified_retrospective_loop(
            config,
            shutdown_event,
            shared_journal=shared_journal,
            hl_client=hl_client,
            kill_switch=kill_switch,
        )
    else:
        await _legacy_six_hour_embedded_loop(
            config,
            shutdown_event,
            shared_journal=shared_journal,
            hl_client=hl_client,
            kill_switch=kill_switch,
        )
