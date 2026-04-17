"""Advisor taxonomy validation for retrospective JSON."""

from __future__ import annotations

from src.intelligence.recommendations import (
    LOW_RISK_ACTIONS,
    validate_advisor_response,
)


def _minimal_valid_payload(low_risk: list) -> dict:
    return {
        "schema_version": 1,
        "diagnosis": "d",
        "low_risk_actions": low_risk,
        "high_risk_suggestions": [],
        "confidence": 0.9,
        "evaluation_horizon": "25 trades",
        "rollback_criteria": "none",
    }


def test_reduce_fathom_action_is_allowed_in_low_risk_list():
    assert "reduce_fathom_acevault_max_mult" in LOW_RISK_ACTIONS


def test_validate_accepts_reduce_fathom_action():
    obj = _minimal_valid_payload(
        [
            {
                "action": "reduce_fathom_acevault_max_mult",
                "target": "acevault",
                "value": 0.9,
            }
        ]
    )
    ok, errs = validate_advisor_response(obj)
    assert ok, errs
