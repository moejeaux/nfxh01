"""Learning registry — structured change records; persistence via DB in follow-up migrations."""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Literal

logger = logging.getLogger(__name__)

ResultStatus = Literal["pending", "improved", "worsened", "inconclusive"]


@dataclass
class LearningChangeRecord:
    change_id: str
    schema_version: int
    config_schema_version: int
    advisor_schema_version: int
    mode: Literal["shallow", "deep"]
    action_type: str
    target: str
    old_value: Any
    new_value: Any
    confidence: float
    applied_at: datetime
    snapshot_id: str
    evaluation_trade_checkpoints: list[int] = field(default_factory=lambda: [25, 50, 100])
    evaluation_timeout_days: int = 7
    result_status: ResultStatus = "pending"


def log_change_intent(record: LearningChangeRecord) -> None:
    logger.info(
        "LEARN_RECORD_INTENT change_id=%s mode=%s action=%s target=%s confidence=%.3f",
        record.change_id,
        record.mode,
        record.action_type,
        record.target,
        record.confidence,
    )


def new_change_id() -> str:
    return str(uuid.uuid4())
