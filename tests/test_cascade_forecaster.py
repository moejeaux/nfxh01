"""Liquidation cascade forecaster: model, holder, scoring, classification, API integration."""

from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest

from src.market.cascade_risk import (
    SAFE_DEFAULT,
    CascadeLevel,
    CascadeRisk,
    CascadeRiskHolder,
)
from src.market.cascade_forecaster import CascadeForecaster


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

def _base_config(*, enabled: bool = True) -> dict:
    return {
        "cascade_forecaster": {
            "enabled": enabled,
            "book_probe_coin": "BTC",
            "weights": {
                "oi_delta": 0.30,
                "funding": 0.20,
                "premium": 0.15,
                "oi_cap": 0.10,
                "book_thin": 0.25,
            },
            "normalization": {
                "oi_delta_extreme_pct": 0.05,
                "funding_extreme": 0.001,
                "premium_extreme": 0.005,
                "premium_per_asset_cap": 0.05,
                "premium_aggregation": "p95",
                "premium_mean_top_n": 10,
                "oi_cap_extreme_count": 10,
            },
            "thresholds": {
                "low": 0.15,
                "elevated": 0.40,
                "high": 0.65,
                "critical": 0.85,
            },
        },
    }


def _asset_ctx(
    *,
    oi: float = 1_000_000.0,
    funding: float = 0.0001,
    mark_px: float = 100.0,
    oracle_px: float = 100.0,
) -> dict:
    """HL asset ctx row (no ``coin``; name comes from meta universe index)."""
    return {
        "openInterest": str(oi),
        "funding": str(funding),
        "markPx": str(mark_px),
        "oraclePx": str(oracle_px),
        "premium": str(mark_px / oracle_px - 1.0) if oracle_px else "0",
    }


def _meta(names: list[str]) -> dict:
    return {"universe": [{"name": n} for n in names]}


def _mock_hl(
    *,
    ctxs: list[dict] | None = None,
    universe: list[str] | None = None,
    oi_cap: list[str] | None = None,
    l2_bids: list[dict] | None = None,
    l2_asks: list[dict] | None = None,
) -> MagicMock:
    hl = MagicMock()
    asset_ctxs = ctxs if ctxs is not None else [
        _asset_ctx(oi=5_000_000, funding=0.0001, mark_px=65000, oracle_px=65000),
        _asset_ctx(oi=3_000_000, funding=0.00015, mark_px=3200, oracle_px=3200),
    ]
    names = universe if universe is not None else ["BTC", "ETH"]
    if len(names) != len(asset_ctxs):
        names = [f"C{i}" for i in range(len(asset_ctxs))]
    hl.meta_and_asset_ctxs.return_value = (_meta(names), asset_ctxs)
    hl.post.return_value = oi_cap if oi_cap is not None else []
    bids = l2_bids if l2_bids is not None else [{"sz": "10.0"}] * 10
    asks = l2_asks if l2_asks is not None else [{"sz": "10.0"}] * 10
    hl.l2_snapshot.return_value = {"levels": [bids, asks]}
    return hl


# ===========================================================================
# CascadeRisk model tests
# ===========================================================================

class TestCascadeRiskModel:
    def test_safe_default_is_zero_risk(self) -> None:
        assert SAFE_DEFAULT.risk_score == 0.0
        assert SAFE_DEFAULT.level == CascadeLevel.NONE
        assert SAFE_DEFAULT.error is None

    def test_model_is_frozen(self) -> None:
        risk = CascadeRisk(
            risk_score=0.5, level=CascadeLevel.ELEVATED,
            oi_delta_pct=-0.02, funding_abs=0.0005, premium_abs=0.001,
            oi_at_cap_count=3, book_thinning_score=0.3,
            updated_at=datetime.now(timezone.utc),
        )
        with pytest.raises(Exception):
            risk.risk_score = 0.9  # type: ignore[misc]

    def test_score_bounds_enforced(self) -> None:
        with pytest.raises(Exception):
            CascadeRisk(
                risk_score=1.5, level=CascadeLevel.NONE,
                oi_delta_pct=0, funding_abs=0, premium_abs=0,
                oi_at_cap_count=0, book_thinning_score=0,
                updated_at=datetime.now(timezone.utc),
            )

    def test_negative_score_rejected(self) -> None:
        with pytest.raises(Exception):
            CascadeRisk(
                risk_score=-0.1, level=CascadeLevel.NONE,
                oi_delta_pct=0, funding_abs=0, premium_abs=0,
                oi_at_cap_count=0, book_thinning_score=0,
                updated_at=datetime.now(timezone.utc),
            )

    def test_all_cascade_levels_exist(self) -> None:
        assert set(CascadeLevel) == {
            CascadeLevel.NONE,
            CascadeLevel.LOW,
            CascadeLevel.ELEVATED,
            CascadeLevel.HIGH,
            CascadeLevel.CRITICAL,
        }

    def test_error_field_preserved(self) -> None:
        risk = CascadeRisk(
            risk_score=0.0, level=CascadeLevel.NONE,
            oi_delta_pct=0, funding_abs=0, premium_abs=0,
            oi_at_cap_count=0, book_thinning_score=0,
            updated_at=datetime.now(timezone.utc),
            error="api_timeout",
        )
        assert risk.error == "api_timeout"

    def test_json_serialization(self) -> None:
        risk = CascadeRisk(
            risk_score=0.42, level=CascadeLevel.ELEVATED,
            oi_delta_pct=-0.01, funding_abs=0.0005, premium_abs=0.002,
            oi_at_cap_count=2, book_thinning_score=0.15,
            updated_at=datetime.now(timezone.utc),
        )
        payload = risk.model_dump(mode="json")
        assert payload["risk_score"] == 0.42
        assert payload["level"] == "elevated"


# ===========================================================================
# CascadeRiskHolder tests
# ===========================================================================

class TestCascadeRiskHolder:
    def test_initial_state_is_none(self) -> None:
        h = CascadeRiskHolder()
        assert h.snapshot is None
        assert h.tick_at is None
        assert h.seq == 0

    def test_set_risk_increments_seq(self) -> None:
        h = CascadeRiskHolder()
        h.set_risk(SAFE_DEFAULT)
        assert h.seq == 1
        assert h.snapshot is SAFE_DEFAULT

    def test_multiple_sets_track_latest(self) -> None:
        h = CascadeRiskHolder()
        r1 = CascadeRisk(
            risk_score=0.2, level=CascadeLevel.LOW,
            oi_delta_pct=0, funding_abs=0, premium_abs=0,
            oi_at_cap_count=0, book_thinning_score=0,
            updated_at=datetime.now(timezone.utc),
        )
        r2 = CascadeRisk(
            risk_score=0.7, level=CascadeLevel.HIGH,
            oi_delta_pct=0, funding_abs=0, premium_abs=0,
            oi_at_cap_count=0, book_thinning_score=0,
            updated_at=datetime.now(timezone.utc),
        )
        h.set_risk(r1)
        h.set_risk(r2)
        assert h.seq == 2
        assert h.snapshot is r2

    def test_set_none_clears_snapshot(self) -> None:
        h = CascadeRiskHolder()
        h.set_risk(SAFE_DEFAULT)
        h.set_risk(None)
        assert h.snapshot is None
        assert h.seq == 2

    def test_tick_at_auto_populated(self) -> None:
        h = CascadeRiskHolder()
        h.set_risk(SAFE_DEFAULT)
        assert h.tick_at is not None

    def test_tick_at_explicit(self) -> None:
        h = CascadeRiskHolder()
        t = datetime(2026, 4, 18, 12, 0, tzinfo=timezone.utc)
        h.set_risk(SAFE_DEFAULT, tick_at=t)
        assert h.tick_at == t


# ===========================================================================
# CascadeForecaster — disabled mode
# ===========================================================================

class TestForecasterDisabled:
    def test_disabled_returns_safe_default(self) -> None:
        cfg = _base_config(enabled=False)
        hl = _mock_hl()
        f = CascadeForecaster(cfg, hl)
        result = f.assess()
        assert result.risk_score == 0.0
        assert result.level == CascadeLevel.NONE
        hl.meta_and_asset_ctxs.assert_not_called()

    def test_missing_config_section_returns_safe(self) -> None:
        f = CascadeForecaster({}, _mock_hl())
        result = f.assess()
        assert result.risk_score == 0.0


# ===========================================================================
# CascadeForecaster — scoring
# ===========================================================================

class TestForecasterScoring:
    def test_calm_market_low_score(self) -> None:
        cfg = _base_config()
        hl = _mock_hl(
            ctxs=[
                _asset_ctx(oi=5_000_000, funding=0.00005, mark_px=65000, oracle_px=65000),
                _asset_ctx(oi=3_000_000, funding=0.00005, mark_px=3200, oracle_px=3200),
            ],
            oi_cap=[],
        )
        f = CascadeForecaster(cfg, hl)
        # First call seeds OI baseline — always returns 0 delta
        r1 = f.assess()
        assert r1.risk_score < 0.30

    def test_extreme_funding_raises_score(self) -> None:
        cfg = _base_config()
        hl = _mock_hl(
            ctxs=[
                _asset_ctx(oi=5_000_000, funding=0.005, mark_px=65000, oracle_px=65000),
            ],
            universe=["BTC"],
            oi_cap=[],
        )
        f = CascadeForecaster(cfg, hl)
        r = f.assess()
        assert r.funding_abs == 0.005
        assert r.risk_score > 0.0

    def test_high_premium_raises_score(self) -> None:
        cfg = _base_config()
        hl = _mock_hl(
            ctxs=[
                _asset_ctx(oi=5_000_000, funding=0.0001, mark_px=65500, oracle_px=65000),
            ],
            universe=["BTC"],
        )
        f = CascadeForecaster(cfg, hl)
        r = f.assess()
        assert r.premium_abs > 0.0

    def test_many_oi_caps_raises_score(self) -> None:
        cfg = _base_config()
        hl = _mock_hl(oi_cap=["BADGER", "CANTO", "FTM", "LOOM", "PURR", "SUI", "WIF", "PEPE", "ARB", "OP"])
        f = CascadeForecaster(cfg, hl)
        r = f.assess()
        assert r.oi_at_cap_count == 10

    def test_book_thinning_detected(self) -> None:
        cfg = _base_config()
        thick_bids = [{"sz": "50.0"}] * 10
        thick_asks = [{"sz": "50.0"}] * 10
        hl_thick = _mock_hl(l2_bids=thick_bids, l2_asks=thick_asks)

        f = CascadeForecaster(cfg, hl_thick)
        f.assess()  # seed baseline

        thin_bids = [{"sz": "5.0"}] * 10
        thin_asks = [{"sz": "5.0"}] * 10
        hl_thick.l2_snapshot.return_value = {"levels": [thin_bids, thin_asks]}
        r = f.assess()
        assert r.book_thinning_score > 0.5

    def test_oi_delta_negative_on_liquidation_unwind(self) -> None:
        cfg = _base_config()
        initial_ctxs = [
            _asset_ctx(oi=5_000_000),
            _asset_ctx(oi=3_000_000),
        ]
        hl = _mock_hl(ctxs=initial_ctxs)
        f = CascadeForecaster(cfg, hl)
        f.assess()  # seed OI baseline

        f._prev_oi_mono -= 5.0  # simulate time passing

        dropped_ctxs = [
            _asset_ctx(oi=4_000_000),
            _asset_ctx(oi=2_500_000),
        ]
        hl.meta_and_asset_ctxs.return_value = (_meta(["BTC", "ETH"]), dropped_ctxs)
        r = f.assess()
        assert r.oi_delta_pct < 0.0

    def test_score_clamped_to_zero_one(self) -> None:
        cfg = _base_config()
        extreme_ctxs = [
            _asset_ctx(oi=1, funding=1.0, mark_px=200000, oracle_px=65000),
        ]
        hl = _mock_hl(ctxs=extreme_ctxs, universe=["BTC"], oi_cap=["A"] * 50)
        f = CascadeForecaster(cfg, hl)
        r = f.assess()
        assert 0.0 <= r.risk_score <= 1.0


# ===========================================================================
# CascadeForecaster — classification
# ===========================================================================

class TestForecasterClassification:
    def _make_forecaster(self) -> CascadeForecaster:
        return CascadeForecaster(_base_config(), _mock_hl())

    def test_none_level(self) -> None:
        f = self._make_forecaster()
        assert f._classify(0.0) == CascadeLevel.NONE
        assert f._classify(0.14) == CascadeLevel.NONE

    def test_low_level(self) -> None:
        f = self._make_forecaster()
        assert f._classify(0.15) == CascadeLevel.LOW
        assert f._classify(0.39) == CascadeLevel.LOW

    def test_elevated_level(self) -> None:
        f = self._make_forecaster()
        assert f._classify(0.40) == CascadeLevel.ELEVATED
        assert f._classify(0.64) == CascadeLevel.ELEVATED

    def test_high_level(self) -> None:
        f = self._make_forecaster()
        assert f._classify(0.65) == CascadeLevel.HIGH
        assert f._classify(0.84) == CascadeLevel.HIGH

    def test_critical_level(self) -> None:
        f = self._make_forecaster()
        assert f._classify(0.85) == CascadeLevel.CRITICAL
        assert f._classify(1.0) == CascadeLevel.CRITICAL


# ===========================================================================
# CascadeForecaster — error handling
# ===========================================================================

class TestForecasterErrorHandling:
    def test_meta_ctxs_exception_returns_safe(self) -> None:
        hl = _mock_hl()
        hl.meta_and_asset_ctxs.side_effect = ConnectionError("HL down")
        f = CascadeForecaster(_base_config(), hl)
        r = f.assess()
        assert r.risk_score == 0.0
        assert r.error is not None
        assert "HL down" in r.error

    def test_oi_cap_exception_returns_empty(self) -> None:
        hl = _mock_hl()
        hl.post.side_effect = ConnectionError("timeout")
        f = CascadeForecaster(_base_config(), hl)
        r = f.assess()
        assert r.oi_at_cap_count == 0
        assert r.error is None

    def test_l2_exception_returns_zero_thinning(self) -> None:
        hl = _mock_hl()
        hl.l2_snapshot.side_effect = ConnectionError("book fail")
        f = CascadeForecaster(_base_config(), hl)
        r = f.assess()
        assert r.book_thinning_score == 0.0

    def test_empty_ctxs_returns_safe(self) -> None:
        hl = _mock_hl(ctxs=[])
        f = CascadeForecaster(_base_config(), hl)
        r = f.assess()
        assert r.funding_abs == 0.0
        assert r.premium_abs == 0.0

    def test_malformed_meta_response_degrades_gracefully(self) -> None:
        hl = _mock_hl()
        hl.meta_and_asset_ctxs.return_value = "not_a_tuple"
        f = CascadeForecaster(_base_config(), hl)
        r = f.assess()
        assert r.risk_score == 0.0
        assert r.funding_abs == 0.0
        assert r.premium_abs == 0.0


# ===========================================================================
# CascadeForecaster — weight and normalization config
# ===========================================================================

class TestForecasterConfigDriven:
    def test_custom_thresholds(self) -> None:
        cfg = _base_config()
        cfg["cascade_forecaster"]["thresholds"] = {
            "low": 0.05,
            "elevated": 0.20,
            "high": 0.50,
            "critical": 0.70,
        }
        f = CascadeForecaster(cfg, _mock_hl())
        assert f._classify(0.05) == CascadeLevel.LOW
        assert f._classify(0.70) == CascadeLevel.CRITICAL

    def test_zero_weight_disables_signal(self) -> None:
        cfg = _base_config()
        cfg["cascade_forecaster"]["weights"] = {
            "oi_delta": 0.0,
            "funding": 0.0,
            "premium": 0.0,
            "oi_cap": 0.0,
            "book_thin": 1.0,
        }
        f = CascadeForecaster(cfg, _mock_hl())
        score = f._score(
            oi_delta_pct=-0.10,
            funding_abs=0.01,
            premium_abs=0.05,
            oi_at_cap_count=20,
            book_thinning_score=0.0,
        )
        assert score == 0.0

    def test_normalization_scales_signals(self) -> None:
        cfg = _base_config()
        cfg["cascade_forecaster"]["normalization"]["funding_extreme"] = 0.01
        f = CascadeForecaster(cfg, _mock_hl())
        s_tight = f._score(
            oi_delta_pct=0, funding_abs=0.005,
            premium_abs=0, oi_at_cap_count=0, book_thinning_score=0,
        )
        cfg["cascade_forecaster"]["normalization"]["funding_extreme"] = 0.001
        f2 = CascadeForecaster(cfg, _mock_hl())
        s_loose = f2._score(
            oi_delta_pct=0, funding_abs=0.005,
            premium_abs=0, oi_at_cap_count=0, book_thinning_score=0,
        )
        assert s_loose > s_tight

    def test_universe_index_names_used_not_ctx_coin(self) -> None:
        """HL pairs ctxs[i] with meta.universe[i].name; ctx rows omit coin."""
        cfg = _base_config()
        names = ["BTC", "ETH"]
        ctxs = [
            _asset_ctx(oi=9_000_000, funding=0.0001, mark_px=100, oracle_px=100),
            _asset_ctx(oi=1_000_000, funding=0.0001, mark_px=100, oracle_px=100),
        ]
        hl = MagicMock()
        hl.meta_and_asset_ctxs.return_value = (_meta(names), ctxs)
        hl.post.return_value = []
        hl.l2_snapshot.return_value = {"levels": [[{"sz": "10.0"}] * 10, [{"sz": "10.0"}] * 10]}
        f = CascadeForecaster(cfg, hl)
        f.assess()
        f._prev_oi_mono -= 2.0
        ctxs2 = [
            _asset_ctx(oi=8_000_000, funding=0.0001, mark_px=100, oracle_px=100),
            _asset_ctx(oi=1_000_000, funding=0.0001, mark_px=100, oracle_px=100),
        ]
        hl.meta_and_asset_ctxs.return_value = (_meta(names), ctxs2)
        r = f.assess()
        assert r.oi_delta_pct < 0.0

    def test_pathological_premium_capped_p95_not_elevated(self) -> None:
        """One broken mark/oracle row must not dominate default p95 aggregate."""
        cfg = _base_config()
        cfg["cascade_forecaster"]["weights"] = {
            "oi_delta": 0.0,
            "funding": 0.0,
            "premium": 1.0,
            "oi_cap": 0.0,
            "book_thin": 0.0,
        }
        n = 21
        names = [f"C{i}" for i in range(n - 1)] + ["PATH"]
        ctxs = [
            _asset_ctx(oi=1e6, funding=0.0, mark_px=100.0, oracle_px=100.0)
            for _ in range(n - 1)
        ]
        ctxs.append(
            _asset_ctx(oi=1e6, funding=0.0, mark_px=200000.0, oracle_px=100.0)
        )
        hl = _mock_hl(ctxs=ctxs, universe=names, oi_cap=[])
        f = CascadeForecaster(cfg, hl)
        r = f.assess()
        assert r.premium_abs == 0.0
        assert r.risk_score < float(cfg["cascade_forecaster"]["thresholds"]["elevated"])

    def test_premium_max_aggregation_uses_cap(self) -> None:
        cfg = _base_config()
        cfg["cascade_forecaster"]["normalization"]["premium_aggregation"] = "max"
        cfg["cascade_forecaster"]["weights"] = {
            "oi_delta": 0.0,
            "funding": 0.0,
            "premium": 1.0,
            "oi_cap": 0.0,
            "book_thin": 0.0,
        }
        hl = _mock_hl(
            ctxs=[_asset_ctx(oi=1e6, funding=0.0, mark_px=200000.0, oracle_px=100.0)],
            universe=["PATH"],
            oi_cap=[],
        )
        f = CascadeForecaster(cfg, hl)
        r = f.assess()
        assert abs(r.premium_abs - 0.05) < 1e-9
