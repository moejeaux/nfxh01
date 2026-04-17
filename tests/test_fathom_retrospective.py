"""Tests for six-hour Fathom retrospective helpers and run (mocked externals)."""

from __future__ import annotations

import asyncio
from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.fathom.retrospective import (
    build_decisions_digest,
    compute_next_retrospective_wait_seconds,
    format_retrospective_telegram_message,
    run_embedded_retrospective_loop,
    run_six_hour_retrospective,
    serialize_decisions_for_prompt,
    try_parse_analysis_json,
)

_VALID_ADVISOR_JSON = {
    "schema_version": 1,
    "diagnosis": "test",
    "low_risk_actions": [{"action": "no_action", "target": "", "value": None}],
    "high_risk_suggestions": [],
    "confidence": 0.5,
    "evaluation_horizon": "25 trades",
    "rollback_criteria": "none",
}


async def _fake_call_with_retry(*_a, **_k):
    import json

    return (json.dumps(_VALID_ADVISOR_JSON), dict(_VALID_ADVISOR_JSON))


def test_build_decisions_digest_empty():
    d = build_decisions_digest([])
    assert d["decision_count"] == 0
    assert d["note"] == "no_trades_in_window"


def test_build_decisions_digest_with_rows():
    rows = [
        {
            "coin": "ETH",
            "outcome_recorded_at": datetime.now(timezone.utc),
            "pnl_usd": 10.0,
        },
        {
            "coin": "ETH",
            "outcome_recorded_at": datetime.now(timezone.utc),
            "pnl_usd": -5.0,
        },
    ]
    d = build_decisions_digest(rows)
    assert d["decision_count"] == 2
    assert d["closed_with_pnl_count"] == 2
    assert d["total_pnl_usd"] == 5.0


def test_try_parse_analysis_json_plain():
    assert try_parse_analysis_json('{"x": 1}') == {"x": 1}


def test_try_parse_analysis_json_embedded():
    raw = 'Intro\n{"summary": "ok", "carry_over": []}\n'
    out = try_parse_analysis_json(raw)
    assert out == {"summary": "ok", "carry_over": []}


def test_compute_next_retrospective_wait_seconds_no_prior():
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    w = compute_next_retrospective_wait_seconds(None, now, 6, 120.0, 5.0)
    assert w == 120.0


def test_compute_next_retrospective_wait_seconds_stale():
    last = datetime(2026, 1, 1, 4, 0, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    w = compute_next_retrospective_wait_seconds(last, now, 6, 120.0, 5.0)
    assert w == 5.0


def test_compute_next_retrospective_wait_seconds_within_interval():
    last = datetime(2026, 1, 1, 10, 0, tzinfo=timezone.utc)
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    w = compute_next_retrospective_wait_seconds(last, now, 6, 120.0, 5.0)
    assert w == 4 * 3600.0


def test_compute_next_retrospective_wait_seconds_naive_last_treated_utc():
    last = datetime(2026, 1, 1, 10, 0)  # naive
    now = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)
    w = compute_next_retrospective_wait_seconds(last, now, 6, 120.0, 5.0)
    assert w == 4 * 3600.0


@pytest.mark.asyncio
async def test_run_embedded_retrospective_loop_skips_disabled():
    ev = asyncio.Event()
    config = {"fathom_retrospective": {"enabled": False}}
    await run_embedded_retrospective_loop(config, ev)


@pytest.mark.asyncio
async def test_run_embedded_retrospective_loop_skips_embed_flag(monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "postgresql://u:p@localhost/db")
    ev = asyncio.Event()
    config = {
        "fathom_retrospective": {"enabled": True, "embed_in_main_process": False},
    }
    await run_embedded_retrospective_loop(config, ev)


@pytest.mark.asyncio
async def test_run_embedded_retrospective_loop_skips_no_database_url(monkeypatch):
    monkeypatch.delenv("DATABASE_URL", raising=False)
    ev = asyncio.Event()
    config = {"fathom_retrospective": {"enabled": True, "embed_in_main_process": True}}
    await run_embedded_retrospective_loop(config, ev)


def test_serialize_decisions_for_prompt_truncation():
    rows = [{"id": 1, "coin": "BTC", "regime": "r", "entry_price": 1, "exit_price": 2,
             "pnl_usd": 0, "pnl_pct": 0, "exit_reason": "x", "fathom_size_mult": 1.0}]
    s = serialize_decisions_for_prompt(rows, max_chars=50)
    assert "truncated" in s or len(s) <= 60


@pytest.mark.asyncio
async def test_run_six_hour_retrospective_success(monkeypatch):
    config = {
        "fathom_retrospective": {
            "enabled": True,
            "deep_model": "test-model",
            "lookback_hours": 6,
            "max_decision_rows": 10,
            "timeout_seconds": 30,
            "num_predict": 100,
            "temperature": 0.0,
            "previous_runs_in_prompt": 0,
            "max_decisions_prompt_chars": 5000,
        },
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 15,
        },
    }

    async def fake_fetch(_hl):
        return {
            "btc_1h_return": 0.001,
            "btc_4h_return": 0.002,
            "btc_vol_1h": 0.01,
            "funding_rate": 0.01,
        }

    monkeypatch.setattr("src.fathom.retrospective.fetch_real_market_data", fake_fetch)
    monkeypatch.setattr("src.fathom.retrospective._make_hl_client", lambda _c: MagicMock())

    journal = MagicMock()
    journal.connect = AsyncMock()
    journal.close = AsyncMock()
    journal.is_connected = MagicMock(return_value=True)
    journal.fetch_decisions_in_window = AsyncMock(return_value=[])
    journal.get_recent_retrospectives = AsyncMock(return_value=[])
    journal.insert_retrospective_run = AsyncMock(return_value="new-uuid")
    journal.learning_effectiveness_ratio = AsyncMock(return_value={})
    journal.get_last_auto_apply_time = AsyncMock(return_value=None)

    monkeypatch.setattr("src.fathom.retrospective.DecisionJournal", lambda *a, **k: journal)
    monkeypatch.setattr("src.fathom.retrospective.call_with_retry", _fake_call_with_retry)
    monkeypatch.setattr(
        "src.fathom.retrospective.maybe_apply_retro_analysis", AsyncMock()
    )
    monkeypatch.setattr(
        "src.fathom.retrospective.evaluate_pending_learning_changes", AsyncMock()
    )

    code = await run_six_hour_retrospective(
        config, "postgresql://x/x", "http://localhost:11434"
    )

    assert code == 0
    journal.insert_retrospective_run.assert_called_once()
    journal.close.assert_called()


@pytest.mark.asyncio
async def test_run_disabled_returns_zero():
    config = {"fathom_retrospective": {"enabled": False}}
    code = await run_six_hour_retrospective(
        config, "postgresql://x/x", "http://localhost:11434"
    )
    assert code == 0


@pytest.mark.asyncio
async def test_run_six_hour_shared_journal_does_not_close(monkeypatch):
    config = {
        "fathom_retrospective": {
            "enabled": True,
            "deep_model": "test-model",
            "lookback_hours": 6,
            "max_decision_rows": 10,
            "timeout_seconds": 30,
            "num_predict": 100,
            "temperature": 0.0,
            "previous_runs_in_prompt": 0,
            "max_decisions_prompt_chars": 5000,
        },
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 15,
        },
    }

    async def fake_fetch(_hl):
        return {
            "btc_1h_return": 0.001,
            "btc_4h_return": 0.002,
            "btc_vol_1h": 0.01,
            "funding_rate": 0.01,
        }

    monkeypatch.setattr("src.fathom.retrospective.fetch_real_market_data", fake_fetch)
    shared = MagicMock()
    shared.is_connected.return_value = True
    shared.fetch_decisions_in_window = AsyncMock(return_value=[])
    shared.get_recent_retrospectives = AsyncMock(return_value=[])
    shared.insert_retrospective_run = AsyncMock(return_value="shared-uuid")
    shared.close = AsyncMock()
    shared.learning_effectiveness_ratio = AsyncMock(return_value={})
    shared.get_last_auto_apply_time = AsyncMock(return_value=None)

    monkeypatch.setattr("src.fathom.retrospective.call_with_retry", _fake_call_with_retry)
    monkeypatch.setattr(
        "src.fathom.retrospective.maybe_apply_retro_analysis", AsyncMock()
    )
    monkeypatch.setattr(
        "src.fathom.retrospective.evaluate_pending_learning_changes", AsyncMock()
    )

    code = await run_six_hour_retrospective(
        config,
        "postgresql://x/x",
        "http://localhost:11434",
        shared_journal=shared,
        hl_client=MagicMock(),
    )

    assert code == 0
    shared.close.assert_not_called()
    shared.insert_retrospective_run.assert_called_once()


def test_format_retrospective_telegram_message_json():
    ws = datetime(2025, 1, 1, tzinfo=timezone.utc)
    we = datetime(2025, 1, 2, tzinfo=timezone.utc)
    msg = format_retrospective_telegram_message(
        run_id="abc",
        window_start=ws,
        window_end=we,
        analysis_json={
            "summary": "Test summary",
            "recommended_changes": ["a", "b"],
        },
        analysis_text="",
        max_chars=3500,
    )
    assert "abc" in msg
    assert "Test summary" in msg
    assert "a" in msg


def test_format_retrospective_telegram_message_fallback_text():
    ws = datetime(2025, 1, 1, tzinfo=timezone.utc)
    we = datetime(2025, 1, 2, tzinfo=timezone.utc)
    msg = format_retrospective_telegram_message(
        run_id="x",
        window_start=ws,
        window_end=we,
        analysis_json=None,
        analysis_text="long prose " * 500,
        max_chars=500,
    )
    assert len(msg) <= 500
    assert "JSON parse failed" in msg or "excerpt" in msg


@pytest.mark.asyncio
async def test_telegram_skipped_when_disabled(monkeypatch):
    config = {
        "fathom_retrospective": {
            "enabled": True,
            "telegram_notify": False,
            "deep_model": "m",
            "lookback_hours": 6,
            "max_decision_rows": 10,
            "timeout_seconds": 30,
            "num_predict": 50,
            "temperature": 0.0,
            "previous_runs_in_prompt": 0,
            "max_decisions_prompt_chars": 5000,
        },
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 15,
        },
    }

    async def _fake_fetch(_h):
        return {
            "btc_1h_return": 0.0,
            "btc_4h_return": 0.0,
            "btc_vol_1h": 0.01,
            "funding_rate": 0.0,
        }

    monkeypatch.setattr("src.fathom.retrospective.fetch_real_market_data", _fake_fetch)
    monkeypatch.setattr(
        "src.fathom.retrospective._make_hl_client", lambda _c: MagicMock()
    )
    journal = MagicMock()
    journal.connect = AsyncMock()
    journal.close = AsyncMock()
    journal.is_connected = MagicMock(return_value=True)
    journal.fetch_decisions_in_window = AsyncMock(return_value=[])
    journal.get_recent_retrospectives = AsyncMock(return_value=[])
    journal.insert_retrospective_run = AsyncMock(return_value="id1")
    journal.learning_effectiveness_ratio = AsyncMock(return_value={})
    journal.get_last_auto_apply_time = AsyncMock(return_value=None)
    monkeypatch.setattr("src.fathom.retrospective.DecisionJournal", lambda *a, **k: journal)
    monkeypatch.setattr("src.fathom.retrospective.call_with_retry", _fake_call_with_retry)
    monkeypatch.setattr(
        "src.fathom.retrospective.maybe_apply_retro_analysis", AsyncMock()
    )
    monkeypatch.setattr(
        "src.fathom.retrospective.evaluate_pending_learning_changes", AsyncMock()
    )
    tg_cls = MagicMock()
    with patch("src.fathom.retrospective.TelegramBot", tg_cls):
        code = await run_six_hour_retrospective(
            config, "postgresql://x/x", "http://localhost:11434"
        )
    assert code == 0
    tg_cls.assert_not_called()


@pytest.mark.asyncio
async def test_telegram_sent_when_enabled(monkeypatch):
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "t")
    monkeypatch.setenv("TELEGRAM_CHAT_ID", "123")
    mock_notify = MagicMock(return_value=True)
    mock_bot = MagicMock()
    mock_bot.notify = mock_notify

    config = {
        "fathom_retrospective": {
            "enabled": True,
            "telegram_notify": True,
            "deep_model": "m",
            "lookback_hours": 6,
            "max_decision_rows": 10,
            "timeout_seconds": 30,
            "num_predict": 50,
            "temperature": 0.0,
            "previous_runs_in_prompt": 0,
            "max_decisions_prompt_chars": 5000,
            "telegram_message_max_chars": 3500,
        },
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 15,
        },
    }
    async def _fake_fetch(_h):
        return {
            "btc_1h_return": 0.0,
            "btc_4h_return": 0.0,
            "btc_vol_1h": 0.01,
            "funding_rate": 0.0,
        }

    monkeypatch.setattr("src.fathom.retrospective.fetch_real_market_data", _fake_fetch)
    monkeypatch.setattr(
        "src.fathom.retrospective._make_hl_client", lambda _c: MagicMock()
    )
    journal = MagicMock()
    journal.connect = AsyncMock()
    journal.close = AsyncMock()
    journal.is_connected = MagicMock(return_value=True)
    journal.fetch_decisions_in_window = AsyncMock(return_value=[])
    journal.get_recent_retrospectives = AsyncMock(return_value=[])
    journal.insert_retrospective_run = AsyncMock(return_value="run-uuid-1")
    journal.learning_effectiveness_ratio = AsyncMock(return_value={})
    journal.get_last_auto_apply_time = AsyncMock(return_value=None)
    monkeypatch.setattr("src.fathom.retrospective.DecisionJournal", lambda *a, **k: journal)
    monkeypatch.setattr("src.fathom.retrospective.call_with_retry", _fake_call_with_retry)
    monkeypatch.setattr(
        "src.fathom.retrospective.maybe_apply_retro_analysis", AsyncMock()
    )
    monkeypatch.setattr(
        "src.fathom.retrospective.evaluate_pending_learning_changes", AsyncMock()
    )

    with patch("src.fathom.retrospective.TelegramBot", return_value=mock_bot):
        code = await run_six_hour_retrospective(
            config, "postgresql://x/x", "http://localhost:11434"
        )

    assert code == 0
    mock_notify.assert_called_once()
    call_text = mock_notify.call_args[0][0]
    assert "run-uuid-1" in call_text
    assert "s" in call_text
