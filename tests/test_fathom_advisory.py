from __future__ import annotations

import time
from decimal import Decimal

from src.nxfh01.advisory.fathom import FathomAdvisory, apply_fathom_payload
from src.nxfh01.config_loader import load_config
from src.nxfh01.contracts.engine import EngineId


def test_clamp_acevault_and_majors():
    cfg = load_config()
    adv = FathomAdvisory(cfg)
    ace = adv.clamp_size_multiplier(Decimal("99"), EngineId.ACEVAULT, is_major_asset=False)
    assert ace.size_multiplier == Decimal("1.5")
    maj = adv.clamp_size_multiplier(Decimal("99"), EngineId.ACEVAULT, is_major_asset=True)
    assert maj.size_multiplier == Decimal("2")


def test_timeout_returns_default():
    cfg = {
        "fathom": {
            "timeout_seconds": 0.05,
            "acevault_max_mult": 1.5,
            "majors_max_mult": 2.0,
        }
    }
    adv = FathomAdvisory(cfg)

    def slow():
        time.sleep(0.2)
        return "x"

    out = adv.call_with_timeout(slow, default=Decimal("1"))
    assert out == Decimal("1")


def test_block_trade_ignored_for_sizing():
    cfg = load_config()
    adv = FathomAdvisory(cfg)
    out = apply_fathom_payload(
        {"block_trade": True, "size_multiplier": "1.4"},
        adv,
        EngineId.ACEVAULT,
        is_major_asset=False,
    )
    assert out.size_multiplier == Decimal("1.4")


def test_bad_multiplier_defaults():
    cfg = load_config()
    adv = FathomAdvisory(cfg)
    out = apply_fathom_payload(
        {"size_multiplier": "not-a-number"},
        adv,
        EngineId.ACEVAULT,
        is_major_asset=False,
        default_mult=Decimal("1"),
    )
    assert out.size_multiplier == Decimal("1")
