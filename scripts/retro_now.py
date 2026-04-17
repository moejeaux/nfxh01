#!/usr/bin/env python3
"""Run one Fathom retrospective cycle (same path as embedded loop, standalone process)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from src.fathom.retrospective import run_six_hour_retrospective
from src.nxfh01.runtime import load_config


async def _amain() -> int:
    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = load_config()
    db_url = os.getenv("DATABASE_URL")
    if not db_url:
        logging.getLogger(__name__).error("RETRO_NOW_ABORT reason=DATABASE_URL_missing")
        return 1
    ollama = os.getenv("OLLAMA_BASE_URL", "http://127.0.0.1:11434")
    return await run_six_hour_retrospective(cfg, db_url, ollama)


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
