from datetime import datetime
from unittest.mock import AsyncMock, Mock

import httpx
import pytest

from src.engines.acevault.models import AceSignal
from src.fathom.advisor import FathomAdvisor
from src.regime.models import RegimeState, RegimeType


@pytest.fixture
def mock_config():
    return {
        "fathom": {
            "model": "llama3.2:3b",
            "fast_model": "llama3.2:3b",
            "timeout_seconds": 15,
            "acevault_min_mult": 0.90,
            "acevault_max_mult": 1.15,
        }
    }


@pytest.fixture
def advisor(mock_config):
    return FathomAdvisor(mock_config, "http://localhost:11434")


@pytest.fixture
def sample_signal():
    return AceSignal(
        coin="ETH",
        side="SHORT",
        entry_price=3000.0,
        stop_loss_price=3009.0,
        take_profit_price=2970.0,
        position_size_usd=500.0,
        weakness_score=0.75,
        regime_at_entry="trending_down",
        timestamp=datetime.now(),
    )


@pytest.fixture
def sample_regime_state():
    return RegimeState(
        regime=RegimeType.TRENDING_DOWN,
        confidence=0.85,
        timestamp=datetime.now(),
        indicators_snapshot={},
    )


@pytest.mark.asyncio
async def test_returns_deterministic_on_timeout(advisor, sample_signal, sample_regime_state):
    # Mock the actual call to return None (which happens when timeout occurs)
    advisor._call_fathom = AsyncMock(return_value=None)

    result = await advisor.advise_acevault(sample_signal, sample_regime_state, "prior context")

    assert result["source"] == "deterministic"
    assert result["size_mult"] == 1.0
    assert result["size_mult_raw"] == 1.0
    assert result["reasoning"] == "fathom_unavailable"


@pytest.mark.asyncio
async def test_returns_deterministic_on_connection_error(advisor, sample_signal, sample_regime_state):
    # Mock the actual call to return None (which happens when connection error occurs)
    advisor._call_fathom = AsyncMock(return_value=None)

    result = await advisor.advise_acevault(sample_signal, sample_regime_state, "prior context")

    assert result["source"] == "deterministic"
    assert result["size_mult"] == 1.0
    assert result["size_mult_raw"] == 1.0
    assert result["reasoning"] == "fathom_unavailable"


@pytest.mark.asyncio
async def test_parses_multiplier_correctly(advisor, sample_signal, sample_regime_state):
    advisor._call_fathom = AsyncMock(return_value="MULTIPLIER: 1.10 Strong trend")

    result = await advisor.advise_acevault(sample_signal, sample_regime_state, "prior context")

    assert result["source"] == "fathom"
    assert result["size_mult"] == 1.1
    assert result["size_mult_raw"] == 1.1
    assert result["reasoning"] == "Strong trend"


@pytest.mark.asyncio
async def test_clamps_multiplier_above_config_max(advisor, sample_signal, sample_regime_state):
    advisor._call_fathom = AsyncMock(return_value="MULTIPLIER: 2.0")

    result = await advisor.advise_acevault(sample_signal, sample_regime_state, "prior context")

    assert result["source"] == "fathom"
    assert result["size_mult"] == 1.15
    assert result["size_mult_raw"] == 2.0
    assert result["reasoning"] == "no_reasoning_provided"


@pytest.mark.asyncio
async def test_clamps_multiplier_below_config_min(advisor, sample_signal, sample_regime_state):
    advisor._call_fathom = AsyncMock(return_value="MULTIPLIER: 0.5\nREASON: test.")

    result = await advisor.advise_acevault(sample_signal, sample_regime_state, "prior context")

    assert result["source"] == "fathom"
    assert result["size_mult"] == 0.90
    assert result["size_mult_raw"] == 0.5
    assert result["reasoning"] == "test."


@pytest.mark.asyncio
async def test_preserves_multiplier_between_min_and_max(advisor, sample_signal, sample_regime_state):
    advisor._call_fathom = AsyncMock(return_value="MULTIPLIER: 0.92\nREASON: cautious.")

    result = await advisor.advise_acevault(sample_signal, sample_regime_state, "prior context")

    assert result["source"] == "fathom"
    assert result["size_mult"] == 0.92
    assert result["size_mult_raw"] == 0.92
    assert result["reasoning"] == "cautious."


@pytest.mark.asyncio
async def test_parse_failure_returns_default(advisor, sample_signal, sample_regime_state):
    advisor._call_fathom = AsyncMock(return_value="garbage response with no multiplier")

    result = await advisor.advise_acevault(sample_signal, sample_regime_state, "prior context")

    assert result["source"] == "deterministic"
    assert result["size_mult"] == 1.0
    assert result["size_mult_raw"] == 1.0
    assert result["reasoning"] == "fathom_unavailable"


def test_prompt_includes_regime(advisor, sample_signal, sample_regime_state):
    prompt = advisor._build_acevault_prompt(sample_signal, sample_regime_state, "prior context")

    assert "trending_down" in prompt
    assert "confidence=0.85" in prompt


def test_prompt_includes_prior_context(advisor, sample_signal, sample_regime_state):
    prior_context = "Previous decision: increased size to 1.2x"
    prompt = advisor._build_acevault_prompt(sample_signal, sample_regime_state, prior_context)

    assert prior_context in prompt