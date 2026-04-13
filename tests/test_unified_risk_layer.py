from __future__ import annotations

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
