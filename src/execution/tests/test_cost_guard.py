import copy
from types import SimpleNamespace
from typing import Any

import pytest

from src.execution.cost_guard import CostGuard


@pytest.fixture
def cost_guard_config() -> dict[str, Any]:
    return {
        "execution": {
            "max_spread_bps": 12,
            "max_slippage_bps": 15,
            "max_total_round_trip_cost_bps": 35,
            "entry_fee_bps": 4,
            "exit_fee_bps": 4,
            "fallback_spread_bps": 10,
            "fallback_slippage_bps": 12,
        }
    }


@pytest.fixture
def mock_hl_client():
    class _Info:
        def __init__(self) -> None:
            self.by_coin: dict[str, Any] = {}
            self.default: Any = None
            self.exc: BaseException | None = None

        def l2_snapshot(self, *, coin: str) -> Any:
            if self.exc is not None:
                raise self.exc
            if coin in self.by_coin:
                return self.by_coin[coin]
            return self.default

    return SimpleNamespace(info=_Info())


def test_approves_low_cost_entry(cost_guard_config, mock_hl_client):
    mock_hl_client.info.by_coin["BTC"] = {
        "levels": [
            [{"px": "10000", "sz": "100"}],
            [{"px": "10001", "sz": "100"}],
        ]
    }
    guard = CostGuard(cost_guard_config, mock_hl_client)
    ok, detail = guard.should_allow_entry("BTC", 100.0)
    assert ok is True
    assert detail["reason"] == "approved"
    assert detail["spread_bps"] < cost_guard_config["execution"]["max_spread_bps"]
    assert detail["slippage_bps"] < cost_guard_config["execution"]["max_slippage_bps"]
    assert detail["total_cost_bps"] <= cost_guard_config["execution"]["max_total_round_trip_cost_bps"]


def test_rejects_high_spread(cost_guard_config, mock_hl_client):
    mock_hl_client.info.by_coin["ETH"] = {
        "levels": [
            [{"px": "100", "sz": "10"}],
            [{"px": "101.01", "sz": "10"}],
        ]
    }
    guard = CostGuard(cost_guard_config, mock_hl_client)
    ok, detail = guard.should_allow_entry("ETH", 50.0)
    assert ok is False
    assert detail["reason"] == "spread_limit"
    assert detail["spread_bps"] > cost_guard_config["execution"]["max_spread_bps"]


def test_rejects_high_slippage(cost_guard_config, mock_hl_client):
    mock_hl_client.info.by_coin["SOL"] = {
        "levels": [
            [{"px": "99.9", "sz": "100"}],
            [
                {"px": "100", "sz": "1"},
                {"px": "200", "sz": "100"},
            ],
        ]
    }
    guard = CostGuard(cost_guard_config, mock_hl_client)
    ok, detail = guard.should_allow_entry("SOL", 1000.0)
    assert ok is False
    assert detail["reason"] == "slippage_limit"
    assert detail["slippage_bps"] > cost_guard_config["execution"]["max_slippage_bps"]


def test_rejects_high_total_cost(cost_guard_config, mock_hl_client):
    cfg = copy.deepcopy(cost_guard_config)
    cfg["execution"] = {
        **cfg["execution"],
        "max_spread_bps": 50,
        "max_slippage_bps": 50,
        "max_total_round_trip_cost_bps": 17,
    }
    mock_hl_client.info.by_coin["ARB"] = {
        "levels": [
            [{"px": "99.95", "sz": "100"}],
            [
                {"px": "100", "sz": "1"},
                {"px": "100.1", "sz": "1000"},
            ],
        ]
    }
    guard = CostGuard(cfg, mock_hl_client)
    ok, detail = guard.should_allow_entry("ARB", 200.0)
    assert ok is False
    assert detail["reason"] == "total_cost_limit"
    assert detail["spread_bps"] <= cfg["execution"]["max_spread_bps"]
    assert detail["slippage_bps"] <= cfg["execution"]["max_slippage_bps"]
    assert detail["total_cost_bps"] > cfg["execution"]["max_total_round_trip_cost_bps"]


def test_fallback_spread_used_when_book_missing(cost_guard_config, mock_hl_client):
    mock_hl_client.info.by_coin["BTC"] = None
    guard = CostGuard(cost_guard_config, mock_hl_client)
    spread = guard._estimate_spread_bps("BTC")
    assert spread == float(cost_guard_config["execution"]["fallback_spread_bps"])


def test_fallback_slippage_used_when_depth_missing(cost_guard_config, mock_hl_client):
    mock_hl_client.info.by_coin["BTC"] = {
        "levels": [
            [{"px": "100", "sz": "100"}],
            [{"px": "100", "sz": "0.0001"}],
        ]
    }
    guard = CostGuard(cost_guard_config, mock_hl_client)
    slip = guard._estimate_slippage_bps("BTC", 1_000_000.0)
    assert slip == float(cost_guard_config["execution"]["fallback_slippage_bps"])


def test_returns_detail_dict_keys(cost_guard_config, mock_hl_client):
    mock_hl_client.info.by_coin["BTC"] = {
        "levels": [
            [{"px": "10000", "sz": "100"}],
            [{"px": "10001", "sz": "100"}],
        ]
    }
    guard = CostGuard(cost_guard_config, mock_hl_client)
    _ok, detail = guard.should_allow_entry("BTC", 10.0)
    assert set(detail.keys()) == {
        "spread_bps",
        "slippage_bps",
        "total_cost_bps",
        "reason",
    }
