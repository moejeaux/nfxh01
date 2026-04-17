#!/usr/bin/env python3
"""Evaluate pending learning registry rows (checkpoint PF vs baseline)."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from src.db.decision_journal import DecisionJournal
from src.intelligence.rollback import evaluate_pending_learning_changes
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
        logging.getLogger(__name__).error("EVAL_CHANGE_ABORT reason=DATABASE_URL_missing")
        return 1
    journal = DecisionJournal(db_url)
    await journal.connect(cfg)
    try:
        await evaluate_pending_learning_changes(cfg, journal)
    finally:
        await journal.close()
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_amain()))


if __name__ == "__main__":
    main()
