from __future__ import annotations

import asyncio
import logging
import signal

from src.retro.loop import run_embedded_retrospective_loop
from src.nxfh01.runtime import (
    VERSION,
    _log_startup_sequence,
    build_context,
    load_config,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

__all__ = ["VERSION", "_log_startup_sequence", "build_context", "load_config", "main"]


async def main() -> None:
    config = load_config()
    ctx = await build_context(config)
    _log_startup_sequence(ctx)

    orchestrator = ctx["orchestrator"]
    tick_interval = float(ctx["tick_interval_seconds"])

    shutdown_event = asyncio.Event()

    def handle_shutdown(sig, frame):
        logger.info("NXFH01_SHUTDOWN_INITIATED signal=%s", sig)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    retro_task = asyncio.create_task(
        run_embedded_retrospective_loop(
            config,
            shutdown_event,
            shared_journal=ctx.get("journal"),
            hl_client=ctx.get("hl_client"),
            kill_switch=ctx.get("kill_switch"),
        ),
        name="fathom_retrospective_embedded",
    )

    try:
        while not shutdown_event.is_set():
            try:
                summary = await orchestrator.run_tick()
                ran = sum(1 for r in summary.strategy_results if r.ran)
                logger.info(
                    "NXFH01_CYCLE_COMPLETE strategies_ran=%d raw_events=%d tick_ms=%.2f "
                    "track_a_submitted=%d track_a_registered=%d",
                    ran,
                    sum(r.raw_result_count for r in summary.strategy_results),
                    summary.tick_duration_ms,
                    summary.track_a_submitted,
                    summary.track_a_registered,
                )
            except Exception as e:
                logger.error("NXFH01_CYCLE_ERROR error=%s", e, exc_info=True)

            try:
                await asyncio.wait_for(
                    shutdown_event.wait(),
                    timeout=tick_interval,
                )
            except asyncio.TimeoutError:
                pass
    finally:
        retro_task.cancel()
        try:
            await retro_task
        except asyncio.CancelledError:
            pass

    logger.info("NXFH01_SHUTDOWN_COMPLETE")


if __name__ == "__main__":
    asyncio.run(main())
