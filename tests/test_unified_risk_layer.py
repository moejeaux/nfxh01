from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from src.nxfh01.config_loader import load_config
from src.nxfh01.contracts.engine import EngineId
from src.nxfh01.contracts.intent import OrderIntent
from src.nxfh01.positions.acevault_stop import AceVaultStop
from src.nxfh01.risk.unified_risk_layer import UnifiedRiskLayer


@pytest.fixture
def cfg():
    return load_config()


@pytest.fixture
def risk(cfg):
    return UnifiedRiskLayer(cfg)


def test_acevault_requires_stop(risk):
    intent = OrderIntent(
        engine_id=EngineId.ACEVAULT,
        asset="BTC",
        is_long=True,
        entry_px=Decimal("100"),
        acevault_stop=None,
    )
    d = risk.validate(intent)
    assert d.allowed is False
    assert d.reason_code == "ACEVAULT_STOP_REQUIRED"


def test_acevault_accepts_canonical_stop(risk, cfg):
    pct = Decimal(str(cfg["acevault"]["stop_loss_distance_pct"]))
    entry = Decimal("100")
    stop = AceVaultStop.from_entry(entry, True, pct)
    intent = OrderIntent(
        engine_id=EngineId.ACEVAULT,
        asset="BTC",
        is_long=True,
        entry_px=entry,
        acevault_stop=stop,
    )
    d = risk.validate(intent)
    assert d.allowed is True
    assert d.reason_code == "OK"


def test_bypass_risk_rejected(risk):
    stop = AceVaultStop.from_entry(Decimal("100"), True, Decimal("0.3"))
    intent = OrderIntent(
        engine_id=EngineId.ACEVAULT,
        asset="BTC",
        is_long=True,
        entry_px=Decimal("100"),
        acevault_stop=stop,
        bypass_risk=True,
    )
    d = risk.validate(intent)
    assert d.allowed is False
    assert d.reason_code == "BYPASS_FORBIDDEN"


def test_non_canonical_stop_rejected(risk, cfg):
    pct = Decimal(str(cfg["acevault"]["stop_loss_distance_pct"]))
    bad = AceVaultStop(
        entry_px=Decimal("100"),
        stop_px=Decimal("50"),
        distance_pct=pct,
    )
    intent = OrderIntent(
        engine_id=EngineId.ACEVAULT,
        asset="BTC",
        is_long=True,
        entry_px=Decimal("100"),
        acevault_stop=bad,
    )
    d = risk.validate(intent)
    assert d.allowed is False
    assert d.reason_code == "STOP_NOT_CANONICAL"


def test_other_engine_does_not_require_ace_stop(risk):
    intent = OrderIntent(
        engine_id=EngineId.ENGINE_2,
        asset="BTC",
        is_long=True,
        entry_px=Decimal("100"),
        acevault_stop=None,
    )
    d = risk.validate(intent)
    assert d.allowed is True


def test_acevault_relaxed_bounded_stop_when_profitability_enabled(cfg):
    cfg2 = copy.deepcopy(cfg)
    cfg2["acevault"]["acevault_profitability"] = {"enabled": True}
    risk = UnifiedRiskLayer(cfg2)
    entry = Decimal("100")
    d_pct = Decimal("0.2")
    stop = AceVaultStop.from_entry(entry, False, d_pct)
    intent = OrderIntent(
        engine_id=EngineId.ACEVAULT,
        asset="ETH",
        is_long=False,
        entry_px=entry,
        acevault_stop=stop,
    )
    out = risk.validate(intent)
    assert out.allowed is True
    assert out.reason_code == "OK"


def test_acevault_rejects_stop_distance_above_cap_when_profitability_enabled(cfg):
    cfg2 = copy.deepcopy(cfg)
    cfg2["acevault"]["acevault_profitability"] = {"enabled": True}
    risk = UnifiedRiskLayer(cfg2)
    cap = Decimal(str(cfg2["acevault"]["stop_loss_distance_pct"]))
    wide = cap + Decimal("0.01")
    entry = Decimal("100")
    stop = AceVaultStop.from_entry(entry, False, wide)
    intent = OrderIntent(
        engine_id=EngineId.ACEVAULT,
        asset="ETH",
        is_long=False,
        entry_px=entry,
        acevault_stop=stop,
    )
    out = risk.validate(intent)
    assert out.allowed is False
    assert out.reason_code == "ACEVAULT_STOP_EXCEEDS_CAP"


def test_acevault_exact_match_still_required_when_profitability_disabled(cfg):
    risk = UnifiedRiskLayer(cfg)
    assert not (cfg.get("acevault") or {}).get("acevault_profitability", {}).get("enabled", False)
    entry = Decimal("100")
    tight = AceVaultStop.from_entry(entry, False, Decimal("0.2"))
    intent = OrderIntent(
        engine_id=EngineId.ACEVAULT,
        asset="ETH",
        is_long=False,
        entry_px=entry,
        acevault_stop=tight,
    )
    out = risk.validate(intent)
    assert out.allowed is False
    assert out.reason_code == "ACEVAULT_STOP_CONFIG_MISMATCH"
