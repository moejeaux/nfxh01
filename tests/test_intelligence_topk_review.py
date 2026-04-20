from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.intelligence.schemas import parse_topk_review_response
from src.intelligence.topk_review import build_topk_candidate_payload, run_topk_advisory_review


class _FakeClient:
    def __init__(self, *, raw: str | None = None, error: Exception | None = None) -> None:
        self._raw = raw
        self._error = error

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        if self._error:
            raise self._error
        assert "advisory reviewer" in system_prompt
        assert "Candidate payload" in user_prompt
        return self._raw or "{}"

    def sanitize_error(self, err: Exception) -> str:
        return str(err)


def _config(enabled: bool = True, write_artifacts: bool = False) -> dict:
    return {
        "research": {"data_dir": "data/research"},
        "intelligence": {
            "enabled": enabled,
            "model": "fathom-14b",
            "timeout_seconds": 30,
            "topk_review": {
                "enabled": enabled,
                "top_k": 5,
                "advisory_only": True,
                "write_artifacts": write_artifacts,
            },
        },
    }


def _candidates() -> list[dict]:
    return [
        {
            "symbol": "SOL",
            "engine": "acevault",
            "raw_score": 2.0,
            "signal_alpha": 0.8,
            "liq_mult": 1.2,
            "regime_mult": 1.0,
            "cost_mult": 0.95,
            "final_score": 0.912,
            "market_tier": 1,
            "leverage_proposal": 5,
            "funding": 0.0001,
            "premium": 0.0002,
            "impact_proxy": 0.1,
            "volume_band": "high",
            "open_interest_band": "high",
        },
        {
            "symbol": "ARB",
            "engine": "acevault",
            "raw_score": 1.8,
            "signal_alpha": 0.76,
            "liq_mult": 1.1,
            "regime_mult": 1.0,
            "cost_mult": 0.9,
            "final_score": 0.7524,
            "market_tier": 2,
            "leverage_proposal": 3,
        },
    ]


def _valid_response() -> str:
    return json.dumps(
        {
            "review_status": "ok",
            "top_choice": {
                "symbol": "SOL",
                "reason": "Best score-quality balance.",
                "confidence": "medium",
            },
            "ranking_adjustments": [
                {
                    "symbol": "ARB",
                    "suggested_rank": 2,
                    "current_rank": 2,
                    "reason": "No change needed.",
                    "confidence": "low",
                }
            ],
            "caution_flags": [
                {
                    "symbol": "ARB",
                    "flag_type": "score_too_close",
                    "message": "Relative separation is modest.",
                    "confidence": "medium",
                }
            ],
            "overall_assessment": {
                "summary": "SOL leads, but separation is not extreme.",
                "actionability": "cautious",
                "confidence": "medium",
            },
        }
    )


def test_topk_payload_generation_is_bounded():
    payload, rank = build_topk_candidate_payload(_candidates(), top_k=1)
    assert len(payload) == 1
    assert payload[0]["symbol"] == "SOL"
    assert rank == {"SOL": 1}


def test_topk_schema_validation_rejects_unknown_symbol():
    bad = json.dumps(
        {
            "review_status": "ok",
            "top_choice": {"symbol": "DOGE", "reason": "x", "confidence": "low"},
            "ranking_adjustments": [],
            "caution_flags": [],
            "overall_assessment": {"summary": "x", "actionability": "weak_signal", "confidence": "low"},
        }
    )
    with pytest.raises(ValueError):
        parse_topk_review_response(bad, allowed_symbols={"SOL", "ARB"}, top_k=2)


def test_topk_graceful_failure_timeout_behavior():
    cands = _candidates()
    out = run_topk_advisory_review(
        candidates=cands,
        config=_config(enabled=True, write_artifacts=False),
        client=_FakeClient(error=TimeoutError("timeout")),
    )
    assert out["enabled"] is True
    assert out["status"] == "fallback"
    assert out["llm_review_status"] == "inconclusive"


def test_topk_advisory_artifact_writing(tmp_path):
    cands = _candidates()
    out = run_topk_advisory_review(
        candidates=cands,
        config=_config(enabled=True, write_artifacts=True),
        client=_FakeClient(raw=_valid_response()),
        artifact_dir=str(tmp_path),
    )
    artifacts = out.get("artifacts") or {}
    j = artifacts.get("topk_review_json")
    m = artifacts.get("topk_review_md")
    assert isinstance(j, str) and Path(j).is_file()
    assert isinstance(m, str) and Path(m).is_file()
    payload = json.loads(Path(j).read_text(encoding="utf-8"))
    assert payload["engine"] == "acevault"
    assert isinstance(payload.get("timestamp"), str)
    assert payload.get("prompt_template_id") == "topk_review_prompt_v1"
    assert isinstance(payload.get("input_payload_hash"), str)
    assert isinstance(payload.get("deterministic_candidate_ranks"), list)


def test_default_mode_does_not_affect_deterministic_ranking():
    cands = _candidates()
    before = [c["symbol"] for c in cands]
    out = run_topk_advisory_review(
        candidates=cands,
        config=_config(enabled=False, write_artifacts=False),
        client=_FakeClient(raw=_valid_response()),
    )
    after = [c["symbol"] for c in cands]
    assert before == after
    assert out["enabled"] is False
