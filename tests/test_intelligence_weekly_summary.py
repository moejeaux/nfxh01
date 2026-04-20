from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.intelligence.schemas import NON_AUTO_APPLY_NOTE, parse_weekly_summary_response
from src.intelligence.weekly_summary import run_weekly_llm_summary


def _write_json(path: Path, payload: dict) -> None:
    path.write_text(json.dumps(payload, sort_keys=True), encoding="utf-8")


def _base_config() -> dict:
    return {
        "intelligence": {
            "enabled": True,
            "provider": "fathom",
            "model": "fathom-14b",
            "timeout_seconds": 60,
            "weekly_summary": {
                "enabled": True,
                "max_recommendations": 2,
                "require_structured_output": True,
                "write_markdown": True,
            },
        }
    }


def _valid_summary(reco_count: int = 1, non_auto_note: str = NON_AUTO_APPLY_NOTE) -> dict:
    recos = []
    for idx in range(reco_count):
        recos.append(
            {
                "priority": idx + 1,
                "change_type": "threshold",
                "target": f"opportunity.threshold_{idx}",
                "recommendation": "Raise threshold slightly after manual validation.",
                "expected_benefit": "Reduce low-quality submissions.",
                "risk": "Could reduce opportunity count.",
                "confidence": "medium",
            }
        )
    return {
        "weekly_verdict": {"label": "stable", "reason": "Mixed outcomes with no large drift."},
        "top_positive_changes": [
            {"title": "Higher tier stability", "evidence": "Tier buckets held win-rate.", "confidence": "medium"}
        ],
        "top_risks": [{"title": "Band noise", "evidence": "Leverage band variance rose.", "confidence": "low"}],
        "engine_observations": [
            {
                "engine": "acevault",
                "observation": "Execution quality stable.",
                "evidence": "Reject profile flat week-over-week.",
                "confidence": "medium",
            }
        ],
        "recommended_changes": recos,
        "things_not_to_change": [{"item": "Hard risk controls", "reason": "No evidence supports relaxing controls."}],
        "data_quality_notes": [{"severity": "info", "note": "Baseline available for partial comparison."}],
        "operator_memo": {
            "summary": "System appears stable with selective tuning opportunity.",
            "next_week_focus": "Validate one threshold adjustment in research only.",
            "non_auto_apply_note": non_auto_note,
        },
    }


def _write_required_artifacts(root: Path) -> None:
    _write_json(root / "historical_summary.json", {"period": {"start": "2026-04-01", "end": "2026-04-08"}})
    _write_json(root / "calibration_recommendations.json", {"status": "recommend_only"})
    (root / "weekly_review.md").write_text("# weekly review\n\nok", encoding="utf-8")


def _write_config(path: Path, cfg: dict) -> None:
    path.write_text(json.dumps(cfg), encoding="utf-8")


class _FakeClient:
    def __init__(self, content: str | None = None, error: Exception | None = None) -> None:
        self._content = content
        self._error = error

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        if self._error:
            raise self._error
        assert "deterministic Hyperliquid trading system" in system_prompt
        assert "Artifacts:" in user_prompt
        return self._content or "{}"

    def trace_config(self) -> dict:
        return {"endpoint": "http://fake", "model": "fathom-14b", "timeout_seconds": 60, "auth_configured": False}

    @staticmethod
    def sanitize_error(err: Exception) -> str:
        return str(err)


def test_schema_validation_accepts_valid():
    raw = json.dumps(_valid_summary())
    parsed = parse_weekly_summary_response(raw, max_recommendations=2)
    assert parsed.weekly_verdict.label == "stable"
    assert parsed.operator_memo.non_auto_apply_note == NON_AUTO_APPLY_NOTE


def test_schema_validation_rejects_bad_non_auto_apply_note():
    raw = json.dumps(_valid_summary(non_auto_note="auto apply this"))
    with pytest.raises(ValueError):
        parse_weekly_summary_response(raw, max_recommendations=2)


def test_recommendation_cap_respected():
    raw = json.dumps(_valid_summary(reco_count=3))
    with pytest.raises(ValueError):
        parse_weekly_summary_response(raw, max_recommendations=2)


def test_missing_optional_artifacts_and_markdown_generation(tmp_path, monkeypatch):
    _write_required_artifacts(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, _base_config())
    content = json.dumps(_valid_summary())
    monkeypatch.setattr(
        "src.intelligence.weekly_summary.FathomClient.from_config",
        lambda _cfg: _FakeClient(content=content),
    )

    out = run_weekly_llm_summary(
        input_dir=str(tmp_path),
        output_dir=str(tmp_path),
        config_path=str(cfg_path),
    )
    assert out["status"] == "ok"
    md_path = Path(out["llm_weekly_summary_md"])
    md = md_path.read_text(encoding="utf-8")
    assert "## Verdict" in md
    assert "## Recommended changes" in md
    assert "## Do not change yet" in md


def test_graceful_model_failure_fallback(tmp_path, monkeypatch):
    _write_required_artifacts(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, _base_config())
    monkeypatch.setattr(
        "src.intelligence.weekly_summary.FathomClient.from_config",
        lambda _cfg: _FakeClient(error=RuntimeError("llm offline")),
    )
    out = run_weekly_llm_summary(
        input_dir=str(tmp_path),
        output_dir=str(tmp_path),
        config_path=str(cfg_path),
    )
    assert out["status"] == "fallback"
    payload = json.loads(Path(out["llm_weekly_summary_json"]).read_text(encoding="utf-8"))
    assert payload["operator_memo"]["non_auto_apply_note"] == NON_AUTO_APPLY_NOTE
    assert payload["status"] == "fallback"


def test_no_auto_apply_behavior_in_output(tmp_path, monkeypatch):
    _write_required_artifacts(tmp_path)
    cfg_path = tmp_path / "config.yaml"
    _write_config(cfg_path, _base_config())
    content = json.dumps(_valid_summary())
    monkeypatch.setattr(
        "src.intelligence.weekly_summary.FathomClient.from_config",
        lambda _cfg: _FakeClient(content=content),
    )
    out = run_weekly_llm_summary(
        input_dir=str(tmp_path),
        output_dir=str(tmp_path),
        config_path=str(cfg_path),
    )
    payload = json.loads(Path(out["llm_weekly_summary_json"]).read_text(encoding="utf-8"))
    assert payload["operator_memo"]["non_auto_apply_note"] == NON_AUTO_APPLY_NOTE
