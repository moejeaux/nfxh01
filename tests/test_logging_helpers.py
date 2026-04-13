from __future__ import annotations

import logging

from src.nxfh01.logging.structured import log_acevault_no_candidates, log_risk_rejected


def test_risk_rejected_log_includes_prefix(caplog):
    caplog.set_level(logging.INFO)
    log_risk_rejected("TEST_REASON", engine=1)
    assert any("RISK_REJECTED" in r.message and "reason=TEST_REASON" in r.message for r in caplog.records)


def test_acevault_no_candidates_prefix(caplog):
    caplog.set_level(logging.INFO)
    log_acevault_no_candidates(symbol="BTC")
    assert any("ACEVAULT_NO_CANDIDATES" in r.message for r in caplog.records)
