"""Retro advisor: schema repair text and call_with_retry deterministic fallback."""

from __future__ import annotations

import json

import pytest

from src.intelligence.recommendations import validate_advisor_response
from src.retro import advisor as advisor_mod


def test_schema_repair_suffix_includes_minimal_example() -> None:
    suffix = advisor_mod._schema_repair_suffix(["missing confidence"], 2)
    assert "schema_version" in suffix
    assert "2" in suffix
    ex = advisor_mod._minimal_advisor_example_json(2)
    assert ex in suffix
    obj = json.loads(ex)
    assert validate_advisor_response(obj)[0] is True


@pytest.mark.asyncio
async def test_call_with_retry_returns_validated_fallback_when_model_invalid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def always_bad(*_a, **_k) -> str:
        return "not valid json {"

    monkeypatch.setattr(advisor_mod, "call_retro_ollama", always_bad)

    config = {
        "fathom": {"retro_timeout_seconds": 1.0},
        "retro": {
            "ollama_json_response_format": False,
            "advisor_schema_version": 1,
            "advisor_retry_temperature": 0.0,
        },
    }
    raw, obj = await advisor_mod.call_with_retry(
        config,
        "http://127.0.0.1:11434",
        "prompt",
        model="m",
        num_predict=32,
        temperature=0.1,
        timeout_s=1.0,
    )
    assert obj is not None
    assert validate_advisor_response(obj)[0] is True
    assert json.loads(raw) == obj
    assert obj["low_risk_actions"][0]["action"] == "no_action"
    assert obj["schema_version"] == 1
