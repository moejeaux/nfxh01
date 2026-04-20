from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from src.research.weekly_review import run_weekly_review


def _write_archive(archive_dir: Path) -> None:
    payload = {
        "timestamp": "2026-04-10T00:00:00+00:00",
        "metaAndAssetCtxs": [
            {
                "universe": [
                    {"name": "BTC", "maxLeverage": 10, "onlyIsolated": False},
                    {"name": "ETH", "maxLeverage": 8, "onlyIsolated": False},
                ]
            },
            [
                {
                    "dayNtlVlm": 5_000_000,
                    "openInterest": 300,
                    "midPx": "84000",
                    "markPx": "84000",
                    "oraclePx": "83950",
                    "impactPxs": ["83990", "84010"],
                    "funding": "0.0001",
                },
                {
                    "dayNtlVlm": 4_000_000,
                    "openInterest": 500,
                    "midPx": "2000",
                    "markPx": "2000",
                    "oraclePx": "1999",
                    "impactPxs": ["1999", "2001"],
                    "funding": "0.0002",
                },
            ],
        ],
    }
    (archive_dir / "snapshot.json").write_text(json.dumps(payload), encoding="utf-8")


def _write_candles(candles_dir: Path) -> None:
    rows = [
        {"symbol": "BTC", "timestamp": "2026-04-10T00:00:00+00:00", "regime_value": "ranging"},
        {"symbol": "ETH", "timestamp": "2026-04-10T00:00:00+00:00", "regime_value": "trending"},
    ]
    (candles_dir / "candles.json").write_text(json.dumps(rows), encoding="utf-8")


def _write_outcomes(outcomes_dir: Path) -> None:
    candidates = [
        {
            "timestamp": "2026-04-10T00:00:00+00:00",
            "trace_id": "t1",
            "symbol": "BTC",
            "engine_id": "acevault",
            "strategy_key": "acevault",
            "side": "short",
            "regime_value": "ranging",
            "raw_strategy_score": 1.1,
            "signal_alpha": 0.7,
            "liq_mult": 1.2,
            "regime_mult": 1.0,
            "cost_mult": 0.9,
            "final_score": 0.75,
            "market_tier": 1,
            "leverage_proposal": 7,
            "asset_max_leverage": 10,
            "hard_reject": False,
            "hard_reject_reason": None,
            "submit_eligible": True,
        }
    ]
    outcomes = [
        {
            "timestamp": "2026-04-10T01:00:00+00:00",
            "trace_id": "t1",
            "position_id": "p1",
            "symbol": "BTC",
            "engine_id": "acevault",
            "strategy_key": "acevault",
            "side": "short",
            "submitted": True,
            "entry_price": 84000.0,
            "exit_price": 83800.0,
            "position_size_usd": 100.0,
            "leverage_used": 5,
            "realized_pnl": 4.0,
            "fees": 0.1,
            "slippage_bps": 1.0,
            "realized_net_pnl": 3.9,
            "hold_time_seconds": 120,
        }
    ]
    (outcomes_dir / "opportunity_candidates.jsonl").write_text(
        "\n".join(json.dumps(x, sort_keys=True) for x in candidates) + "\n",
        encoding="utf-8",
    )
    (outcomes_dir / "opportunity_trade_outcomes.jsonl").write_text(
        "\n".join(json.dumps(x, sort_keys=True) for x in outcomes) + "\n",
        encoding="utf-8",
    )


def _write_config(path: Path) -> None:
    cfg = {
        "opportunity": {
            "hard_reject": {
                "min_day_ntl_vlm_usd": 1000,
                "min_open_interest_usd": 1000,
                "require_mid_px": True,
                "require_impact_pxs": True,
            },
            "liquidity": {
                "vlm_ref_usd": 1_000_000,
                "oi_ref_usd": 1_000_000,
                "impact_k": 10.0,
                "min_liq_mult": 0.05,
                "weights": {"volume": 0.5, "open_interest": 0.5},
            },
            "regime": {"default_mult": 1.0, "by_engine": {"acevault": {"ranging": 1.0, "trending": 1.05}}},
            "cost": {
                "impact_k": 10.0,
                "funding_k": 3000.0,
                "premium_k": 0.2,
                "min_cost_mult": 0.02,
                "floors": {"impact": 0.0001},
            },
            "tiering": {"tier1_min_liq_mult": 1.0, "tier2_min_liq_mult": 0.2},
            "final_score": {"min_submit_score": 0.1},
            "alpha": {"acevault": {"min_raw": 0.0, "max_raw": 2.0}},
            "leverage": {
                "enabled": True,
                "top_target_x": 10.0,
                "tier_caps": {"tier1": 10.0, "tier2": 5.0, "tier3": 0.0},
                "confidence_bands": {"elite_min_score": 0.8, "strong_min_score": 0.5, "medium_min_score": 0.2},
                "by_band": {
                    "1": {"elite": 10.0, "strong": 7.0, "medium": 4.0, "weak": 1.0},
                    "2": {"elite": 5.0, "strong": 3.0, "medium": 2.0, "weak": 1.0},
                },
            },
        },
        "calibration": {"enabled": True, "min_trades_for_update": 2, "lookback_days": 30},
        "research": {"enabled": True, "data_dir": "data/research"},
    }
    path.write_text(json.dumps(cfg), encoding="utf-8")


def test_weekly_review_creates_outputs(tmp_path):
    archive = tmp_path / "archive"
    candles = tmp_path / "candles"
    outcomes = tmp_path / "outcomes"
    out = tmp_path / "out"
    for p in (archive, candles, outcomes, out):
        p.mkdir()
    _write_archive(archive)
    _write_candles(candles)
    _write_outcomes(outcomes)
    cfg = tmp_path / "config.yaml"
    _write_config(cfg)

    paths = run_weekly_review(
        archive_dir=str(archive),
        candles_dir=str(candles),
        outcomes_dir=str(outcomes),
        output_dir=str(out),
        start_date="2026-04-01",
        end_date="2026-04-30",
        lookback_days=14,
        config_path=str(cfg),
    )
    for v in paths.values():
        assert Path(v).is_file()


def test_weekly_review_handles_missing_candles_gracefully(tmp_path):
    archive = tmp_path / "archive"
    outcomes = tmp_path / "outcomes"
    out = tmp_path / "out"
    for p in (archive, outcomes, out):
        p.mkdir()
    _write_archive(archive)
    _write_outcomes(outcomes)
    cfg = tmp_path / "config.yaml"
    _write_config(cfg)

    paths = run_weekly_review(
        archive_dir=str(archive),
        candles_dir=str(tmp_path / "missing_candles"),
        outcomes_dir=str(outcomes),
        output_dir=str(out),
        start_date=None,
        end_date=None,
        lookback_days=None,
        config_path=str(cfg),
    )
    md = Path(paths["weekly_review"]).read_text(encoding="utf-8")
    assert "reduced_regime_coverage" in md


def test_weekly_review_handles_insufficient_outcomes(tmp_path):
    archive = tmp_path / "archive"
    candles = tmp_path / "candles"
    outcomes = tmp_path / "outcomes"
    out = tmp_path / "out"
    for p in (archive, candles, outcomes, out):
        p.mkdir()
    _write_archive(archive)
    _write_candles(candles)
    # No outcomes written.
    cfg = tmp_path / "config.yaml"
    _write_config(cfg)

    paths = run_weekly_review(
        archive_dir=str(archive),
        candles_dir=str(candles),
        outcomes_dir=str(outcomes),
        output_dir=str(out),
        start_date=None,
        end_date=None,
        lookback_days=None,
        config_path=str(cfg),
    )
    payload = json.loads(Path(paths["calibration_recommendations"]).read_text(encoding="utf-8"))
    assert payload["status"] == "insufficient_data"


def test_weekly_review_merges_baseline_metrics_json_into_md(tmp_path):
    archive = tmp_path / "archive"
    candles = tmp_path / "candles"
    outcomes = tmp_path / "outcomes"
    out = tmp_path / "out"
    for p in (archive, candles, outcomes, out):
        p.mkdir()
    _write_archive(archive)
    _write_candles(candles)
    _write_outcomes(outcomes)
    cfg = tmp_path / "config.yaml"
    _write_config(cfg)
    baseline = {"period_label": "prior_week", "mean_pnl_bps": -1.2}
    (out / "baseline_metrics.json").write_text(json.dumps(baseline), encoding="utf-8")

    paths = run_weekly_review(
        archive_dir=str(archive),
        candles_dir=str(candles),
        outcomes_dir=str(outcomes),
        output_dir=str(out),
        start_date=None,
        end_date=None,
        lookback_days=None,
        config_path=str(cfg),
    )
    md = Path(paths["weekly_review"]).read_text(encoding="utf-8")
    assert "## Baseline metrics (baseline_metrics.json)" in md
    assert '"mean_pnl_bps": -1.2' in md
    assert "prior_week" in md


def test_weekly_review_stable_structure(tmp_path):
    archive = tmp_path / "archive"
    candles = tmp_path / "candles"
    outcomes = tmp_path / "outcomes"
    out = tmp_path / "out"
    for p in (archive, candles, outcomes, out):
        p.mkdir()
    _write_archive(archive)
    _write_candles(candles)
    _write_outcomes(outcomes)
    cfg = tmp_path / "config.yaml"
    _write_config(cfg)

    paths = run_weekly_review(
        archive_dir=str(archive),
        candles_dir=str(candles),
        outcomes_dir=str(outcomes),
        output_dir=str(out),
        start_date=None,
        end_date=None,
        lookback_days=None,
        config_path=str(cfg),
    )
    historical = json.loads(Path(paths["historical_summary"]).read_text(encoding="utf-8"))
    assert "period" in historical
    assert "backtest_metrics" in historical
    md = Path(paths["weekly_review"]).read_text(encoding="utf-8")
    assert "## Date Range Analyzed" in md
    assert "## Calibration Recommendations" in md
    assert "does not auto-apply config changes" in md


def test_weekly_review_with_llm_summary_failure_is_non_blocking(tmp_path):
    archive = tmp_path / "archive"
    candles = tmp_path / "candles"
    outcomes = tmp_path / "outcomes"
    out = tmp_path / "out"
    for p in (archive, candles, outcomes, out):
        p.mkdir()
    _write_archive(archive)
    _write_candles(candles)
    _write_outcomes(outcomes)
    cfg = tmp_path / "config.yaml"
    _write_config(cfg)

    with patch("src.research.weekly_review.run_weekly_llm_summary", side_effect=RuntimeError("offline")):
        paths = run_weekly_review(
            archive_dir=str(archive),
            candles_dir=str(candles),
            outcomes_dir=str(outcomes),
            output_dir=str(out),
            start_date=None,
            end_date=None,
            lookback_days=None,
            config_path=str(cfg),
            with_llm_summary=True,
        )
    assert Path(paths["historical_summary"]).is_file()
    assert Path(paths["weekly_review"]).is_file()

