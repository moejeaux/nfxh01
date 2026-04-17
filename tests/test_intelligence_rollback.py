"""Tests for learning registry checkpoint evaluation."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

import pytest

from src.intelligence.rollback import (
    _parse_checkpoints,
    evaluate_pending_learning_changes,
)


def test_parse_checkpoints_defaults():
    assert _parse_checkpoints(None) == [25, 50, 100]
    assert _parse_checkpoints([100, 25]) == [25, 100]


@pytest.mark.asyncio
async def test_evaluate_pending_worsened():
    config = {
        "learning": {
            "evaluation_trade_checkpoints": [10],
            "revert_pf_worsen_ratio": 0.9,
            "improve_pf_ratio": 1.1,
            "inconclusive_timeout_days": 7,
            "evaluation_max_closed_rows": 5000,
        }
    }
    cid = str(uuid4())
    journal = MagicMock()
    journal.is_connected.return_value = True

    rows = [{"outcome_recorded_at": datetime.now(timezone.utc), "pnl_usd": 1.0} for _ in range(10)]
    rows += [{"outcome_recorded_at": datetime.now(timezone.utc), "pnl_usd": -1.0} for _ in range(20)]

    journal.fetch_closed_decisions_for_metrics = AsyncMock(return_value=rows)
    journal.fetch_pending_learning_evaluations = AsyncMock(
        return_value=[
            {
                "change_id": cid,
                "closing_trade_count_at_apply": 0,
                "baseline_profit_factor": 2.0,
                "created_at": datetime.now(timezone.utc),
            }
        ]
    )
    journal.update_learning_result = AsyncMock()

    await evaluate_pending_learning_changes(config, journal)

    journal.update_learning_result.assert_called_once()
    assert journal.update_learning_result.call_args[1]["result_status"] == "worsened"


@pytest.mark.asyncio
async def test_evaluate_pending_improved():
    config = {
        "learning": {
            "evaluation_trade_checkpoints": [5],
            "revert_pf_worsen_ratio": 0.85,
            "improve_pf_ratio": 1.05,
            "inconclusive_timeout_days": 7,
            "evaluation_max_closed_rows": 5000,
        }
    }
    cid = str(uuid4())
    journal = MagicMock()
    journal.is_connected.return_value = True

    rows = [{"outcome_recorded_at": datetime.now(timezone.utc), "pnl_usd": 2.0} for _ in range(20)]
    rows += [{"outcome_recorded_at": datetime.now(timezone.utc), "pnl_usd": -1.0} for _ in range(5)]

    journal.fetch_closed_decisions_for_metrics = AsyncMock(return_value=rows)
    journal.fetch_pending_learning_evaluations = AsyncMock(
        return_value=[
            {
                "change_id": cid,
                "closing_trade_count_at_apply": 0,
                "baseline_profit_factor": 1.0,
                "created_at": datetime.now(timezone.utc),
            }
        ]
    )
    journal.update_learning_result = AsyncMock()

    await evaluate_pending_learning_changes(config, journal)

    assert journal.update_learning_result.call_args[1]["result_status"] == "improved"


@pytest.mark.asyncio
async def test_evaluate_pending_inconclusive_ambiguous_at_max_checkpoint():
    config = {
        "learning": {
            "evaluation_trade_checkpoints": [10, 20],
            "revert_pf_worsen_ratio": 0.85,
            "improve_pf_ratio": 1.05,
            "inconclusive_timeout_days": 7,
            "evaluation_max_closed_rows": 5000,
        }
    }
    cid = str(uuid4())
    journal = MagicMock()
    journal.is_connected.return_value = True

    rows = [{"outcome_recorded_at": datetime.now(timezone.utc), "pnl_usd": 1.0} for _ in range(12)]
    rows += [{"outcome_recorded_at": datetime.now(timezone.utc), "pnl_usd": -1.0} for _ in range(12)]

    journal.fetch_closed_decisions_for_metrics = AsyncMock(return_value=rows)
    journal.fetch_pending_learning_evaluations = AsyncMock(
        return_value=[
            {
                "change_id": cid,
                "closing_trade_count_at_apply": 0,
                "baseline_profit_factor": 1.0,
                "created_at": datetime.now(timezone.utc),
            }
        ]
    )
    journal.update_learning_result = AsyncMock()

    await evaluate_pending_learning_changes(config, journal)

    assert journal.update_learning_result.call_args[1]["result_status"] == "inconclusive"
