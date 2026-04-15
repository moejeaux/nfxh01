"""Growi HF — systematic mean-reversion profile (skeleton)."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)


class GrowiHFEngine:
    def __init__(self, config: dict) -> None:
        self._config = config

    async def run_cycle(self) -> list[Any]:
        logger.info("GROWI_HF_CYCLE skeleton=true skipped_entries=True")
        return []
