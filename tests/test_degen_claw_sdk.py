"""ACP SDK HTTP path (DegenClawAcp + buyer sidecar contract)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.acp.degen_claw import AcpTradeRequest, DegenClawAcp


@pytest.fixture
def sdk_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACP_SDK_URL", "http://127.0.0.1:8765")
    monkeypatch.setenv("ACP_PROVIDER_WALLET", "0xprovider1234567890123456789012345678901234")


def test_sdk_submit_trade_success(sdk_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "jobId": "1001"}
    mock_resp.text = ""

    client_inst = MagicMock()
    client_inst.post.return_value = mock_resp
    client_inst.__enter__.return_value = client_inst
    client_inst.__exit__.return_value = None

    cfg = {"acp": {"min_trade_size_usd": 10.0}}
    with patch("src.acp.degen_claw.httpx.Client", return_value=client_inst):
        acp = DegenClawAcp(config=cfg)
        assert acp.get_acp_state()["backend"] == "sdk"
        r = acp.submit_trade(
            AcpTradeRequest(coin="BTC", side="long", size_usd=50.0, leverage=2),
        )
    assert r.success is True
    assert r.job_id == "1001"
    assert r.filled_via == "degen"
    call_kw = client_inst.post.call_args
    assert call_kw[0][0] == "http://127.0.0.1:8765/v1/job"
    body = call_kw[1]["json"]
    assert body["offeringName"] == "perp_trade"
    assert body["providerAddress"] == "0xprovider1234567890123456789012345678901234"
    assert body["requirementData"]["action"] == "open"
    assert body["requirementData"]["pair"] == "BTC"
    assert body["requirementData"]["leverage"] == 2


def test_sdk_uses_buyer_secret_header(sdk_env: None, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACP_BUYER_SECRET", "test-secret")

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "jobId": "2"}
    mock_resp.text = ""

    client_inst = MagicMock()
    client_inst.post.return_value = mock_resp
    client_inst.__enter__.return_value = client_inst
    client_inst.__exit__.return_value = None

    with patch("src.acp.degen_claw.httpx.Client", return_value=client_inst):
        acp = DegenClawAcp(config={})
        acp.submit_trade(AcpTradeRequest(coin="ETH", side="short", size_usd=25.0))

    headers = client_inst.post.call_args[1]["headers"]
    assert headers.get("X-Acp-Buyer-Token") == "test-secret"


def test_sdk_error_response(sdk_env: None) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 500
    mock_resp.json.return_value = {"ok": False, "error": "No agent found"}
    mock_resp.text = ""

    client_inst = MagicMock()
    client_inst.post.return_value = mock_resp
    client_inst.__enter__.return_value = client_inst
    client_inst.__exit__.return_value = None

    with patch("src.acp.degen_claw.httpx.Client", return_value=client_inst):
        acp = DegenClawAcp(config={})
        r = acp.submit_trade(
            AcpTradeRequest(coin="BTC", side="long", size_usd=100.0),
        )
    assert r.success is False
    assert "No agent found" in (r.error or "")


def test_config_min_trade_from_yaml(sdk_env: None) -> None:
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {"ok": True, "jobId": "1"}
    mock_resp.text = ""
    client_inst = MagicMock()
    client_inst.post.return_value = mock_resp
    client_inst.__enter__.return_value = client_inst
    client_inst.__exit__.return_value = None
    with patch("src.acp.degen_claw.httpx.Client", return_value=client_inst):
        acp = DegenClawAcp(config={"acp": {"min_trade_size_usd": 99.0}})
        r = acp.submit_trade(
            AcpTradeRequest(coin="X", side="long", size_usd=50.0),
        )
    assert r.success is False
    assert "99" in (r.error or "")
