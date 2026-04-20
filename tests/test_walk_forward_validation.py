from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from src.calibration.score_calibrator import calibrate_score_components
from src.research.walk_forward_validation import (
    apply_calibration_deltas_in_memory,
    build_labeled_ranker_rows,
    compute_week_blocks,
    filter_rows_by_time_range,
    run_walk_forward_validation,
)


def _ranker_cfg():
    return {
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
            "regime": {"default_mult": 1.0, "by_engine": {"acevault": {"ranging": 1.0}}},
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
        "calibration": {"enabled": True, "min_trades_for_update": 2, "lookback_days": 7},
        "research": {"enabled": True, "data_dir": "data/research"},
    }


def _archive_payload(ts_iso: str) -> dict:
    return {
        "timestamp": ts_iso,
        "metaAndAssetCtxs": [
            {"universe": [{"name": "BTC", "maxLeverage": 10, "onlyIsolated": False}]},
            [
                {
                    "dayNtlVlm": 2_000_000,
                    "openInterest": 400,
                    "midPx": "84000",
                    "markPx": "84000",
                    "oraclePx": "83980",
                    "impactPxs": ["83990", "84010"],
                    "funding": "0.0001",
                }
            ],
        ],
    }


def test_compute_week_blocks_four_sequential_windows():
    end = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    blocks, meta = compute_week_blocks(end_date=end, total_days=30, block_days=7)
    assert len(blocks) == 4
    assert blocks[0][2] == "week_1"
    assert blocks[3][2] == "week_4"
    assert blocks[3][1] == end
    assert meta["remainder_days_before_first_fold"] == pytest.approx(2.0)
    for i in range(3):
        assert blocks[i][1] == blocks[i + 1][0]


def test_compute_week_blocks_rejects_short_window():
    end = datetime(2026, 4, 19, tzinfo=timezone.utc)
    with pytest.raises(ValueError, match="total_days"):
        compute_week_blocks(end_date=end, total_days=20, block_days=7)


def test_filter_rows_no_leakage_between_weeks():
    end = datetime(2026, 4, 19, tzinfo=timezone.utc)
    blocks, _ = compute_week_blocks(end_date=end, total_days=30, block_days=7)
    w1s, w1e, _ = blocks[0]
    w2s, w2e, _ = blocks[1]
    rows = [
        {"timestamp": (w1s + timedelta(hours=1)).isoformat(), "k": 1},
        {"timestamp": (w2s + timedelta(hours=1)).isoformat(), "k": 2},
    ]
    a = filter_rows_by_time_range(rows, w1s, w1e)
    b = filter_rows_by_time_range(rows, w2s, w2e)
    assert len(a) == 1 and a[0]["k"] == 1
    assert len(b) == 1 and b[0]["k"] == 2


def test_apply_calibration_deltas_in_memory_smoke():
    cfg = _ranker_cfg()
    deltas = {
        "opportunity.liquidity.weights": {"volume": 0.45, "open_interest": 0.55, "reason": "x"},
        "opportunity.unknown.key": {"foo": 1},
    }
    out, applied, skipped = apply_calibration_deltas_in_memory(cfg, deltas)
    assert "opportunity.liquidity.weights" in applied
    assert any(s.startswith("opportunity.unknown.key") for s in skipped)
    assert out["opportunity"]["liquidity"]["weights"]["volume"] == 0.45
    assert cfg["opportunity"]["liquidity"]["weights"]["volume"] == 0.5


def test_calibrate_reference_time_controls_cutoff():
    cfg = {
        "calibration": {"min_trades_for_update": 2, "lookback_days": 7},
    }
    candidates = [{"trace_id": "a", "final_score": 0.5, "liq_mult": 1, "cost_mult": 1, "regime_mult": 1, "market_tier": 1, "leverage_proposal": 2, "hard_reject": False}]
    old_out = {"trace_id": "a", "timestamp": "2026-01-01T00:00:00+00:00", "realized_net_pnl": 1.0}
    new_out = {"trace_id": "a", "timestamp": "2026-04-18T00:00:00+00:00", "realized_net_pnl": 2.0}
    ref = datetime(2026, 4, 19, tzinfo=timezone.utc)
    r_old = calibrate_score_components(cfg=cfg, candidates=candidates, outcomes=[old_out], reference_time=ref)
    assert r_old.summary_metrics["paired_trade_count"] == 0
    r_new = calibrate_score_components(cfg=cfg, candidates=candidates, outcomes=[new_out], reference_time=ref)
    assert r_new.summary_metrics["paired_trade_count"] == 1


def test_run_walk_forward_deterministic_twice(tmp_path):
    archive = tmp_path / "archive"
    out = tmp_path / "out1"
    out2 = tmp_path / "out2"
    data = tmp_path / "data"
    for p in (archive, out, out2, data):
        p.mkdir()
    end = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    t0 = end - timedelta(days=28)
    lines = []
    for i in range(29):
        ts = (t0 + timedelta(days=i)).isoformat()
        lines.append(json.dumps(_archive_payload(ts)))
    (archive / "snap.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")

    candidates = []
    outcomes = []
    tid = 0
    for wi, (ws, we) in enumerate(
        [
            (
                end - timedelta(days=28),
                end - timedelta(days=21),
            ),
            (end - timedelta(days=21), end - timedelta(days=14)),
            (end - timedelta(days=14), end - timedelta(days=7)),
            (end - timedelta(days=7), end),
        ]
    ):
        for _ in range(6):
            tid += 1
            ts_c = ws + timedelta(hours=12)
            ts_o = ws + timedelta(days=1)
            candidates.append(
                {
                    "timestamp": ts_c.isoformat(),
                    "trace_id": f"t{tid}",
                    "symbol": "BTC",
                    "engine_id": "acevault",
                    "strategy_key": "acevault",
                    "side": "short",
                    "regime_value": "ranging",
                    "raw_strategy_score": 1.0,
                    "signal_alpha": 0.7,
                    "liq_mult": 1.2,
                    "regime_mult": 1.0,
                    "cost_mult": 0.9,
                    "final_score": 0.75,
                    "market_tier": 1,
                    "leverage_proposal": 5,
                    "asset_max_leverage": 10,
                    "hard_reject": False,
                    "hard_reject_reason": None,
                    "submit_eligible": True,
                }
            )
            outcomes.append(
                {
                    "timestamp": ts_o.isoformat(),
                    "trace_id": f"t{tid}",
                    "position_id": f"p{tid}",
                    "symbol": "BTC",
                    "engine_id": "acevault",
                    "strategy_key": "acevault",
                    "side": "short",
                    "submitted": True,
                    "entry_price": 84000.0,
                    "exit_price": 83800.0,
                    "position_size_usd": 100.0,
                    "leverage_used": 5,
                    "realized_pnl": 1.0,
                    "fees": 0.1,
                    "realized_net_pnl": 0.5 + 0.01 * tid,
                    "hold_time_seconds": 120,
                }
            )

    (data / "opportunity_candidates.jsonl").write_text(
        "\n".join(json.dumps(c, sort_keys=True) for c in candidates) + "\n",
        encoding="utf-8",
    )
    (data / "opportunity_trade_outcomes.jsonl").write_text(
        "\n".join(json.dumps(o, sort_keys=True) for o in outcomes) + "\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(json.dumps(_ranker_cfg()), encoding="utf-8")

    kwargs = dict(
        archive_dir=str(archive),
        candles_dir=None,
        outcomes_dir=str(data),
        output_dir=str(out),
        config_path=str(cfg_path),
        end_date=end,
        total_days=30,
        block_days=7,
        min_candidates_per_fold=2,
        allow_api_backfill=False,
    )
    run_walk_forward_validation(**kwargs)
    run_walk_forward_validation(**{**kwargs, "output_dir": str(out2)})
    a = (out / "walk_forward_summary.json").read_text(encoding="utf-8")
    b = (out2 / "walk_forward_summary.json").read_text(encoding="utf-8")
    assert a == b


def test_run_walk_forward_insufficient_labels_raises(tmp_path):
    archive = tmp_path / "archive"
    data = tmp_path / "data"
    out = tmp_path / "out"
    for p in (archive, data, out):
        p.mkdir()
    end = datetime(2026, 4, 19, tzinfo=timezone.utc)
    (archive / "snap.json").write_text(json.dumps(_archive_payload("2026-04-18T00:00:00+00:00")), encoding="utf-8")
    (data / "opportunity_candidates.jsonl").write_text("", encoding="utf-8")
    (data / "opportunity_trade_outcomes.jsonl").write_text("", encoding="utf-8")
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(json.dumps(_ranker_cfg()), encoding="utf-8")
    with pytest.raises(ValueError, match="insufficient labeled rows"):
        run_walk_forward_validation(
            archive_dir=str(archive),
            candles_dir=None,
            outcomes_dir=str(data),
            output_dir=str(out),
            config_path=str(cfg_path),
            end_date=end,
            min_candidates_per_fold=5,
        )


def test_build_labeled_ranker_rows_joins_trace(tmp_path):
    hist = [
        {
            "timestamp": "2026-04-10T00:00:00+00:00",
            "symbol": "BTC",
            "asset_max_leverage": 10,
            "only_isolated": False,
            "day_ntl_vlm": 2e6,
            "open_interest": 400,
            "mid_px": 84000.0,
            "mark_px": 84000.0,
            "oracle_px": 83980.0,
            "impact_pxs": [83990.0, 84010.0],
            "funding": 0.0001,
            "premium": 0.1,
            "regime_value": "ranging",
        }
    ]
    cands = [
        {
            "timestamp": "2026-04-10T12:00:00+00:00",
            "trace_id": "x1",
            "symbol": "BTC",
            "engine_id": "acevault",
            "side": "short",
            "regime_value": "ranging",
            "raw_strategy_score": 1.2,
        }
    ]
    outs = [{"trace_id": "x1", "timestamp": "2026-04-11T00:00:00+00:00", "realized_net_pnl": 3.0}]
    rows = build_labeled_ranker_rows(hist, cands, outs)
    assert len(rows) == 1
    assert rows[0]["realized_net_pnl"] == 3.0


def test_oos_rows_exclude_calibration_week(tmp_path):
    """Week-2 test rows must not include timestamps inside week-1 calibration window."""
    end = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    blocks, _ = compute_week_blocks(end_date=end, total_days=30, block_days=7)
    w2s, w2e, _ = blocks[1]
    labeled = [
        {"timestamp": (blocks[0][0] + timedelta(hours=2)).isoformat(), "symbol": "BTC", "realized_net_pnl": 1.0},
        {"timestamp": (w2s + timedelta(hours=2)).isoformat(), "symbol": "BTC", "realized_net_pnl": 2.0},
    ]
    oos = filter_rows_by_time_range(labeled, w2s, w2e)
    assert len(oos) == 1 and oos[0]["realized_net_pnl"] == 2.0


def test_fold_csv_written(tmp_path):
    archive = tmp_path / "archive"
    out = tmp_path / "out"
    data = tmp_path / "data"
    for p in (archive, out, data):
        p.mkdir()
    end = datetime(2026, 4, 19, 12, 0, tzinfo=timezone.utc)
    t0 = end - timedelta(days=28)
    lines = [json.dumps(_archive_payload((t0 + timedelta(days=i)).isoformat())) for i in range(29)]
    (archive / "snap.jsonl").write_text("\n".join(lines) + "\n", encoding="utf-8")
    candidates = []
    outcomes = []
    tid = 0
    for ws, we in [
        (end - timedelta(days=28), end - timedelta(days=21)),
        (end - timedelta(days=21), end - timedelta(days=14)),
        (end - timedelta(days=14), end - timedelta(days=7)),
        (end - timedelta(days=7), end),
    ]:
        for _ in range(6):
            tid += 1
            ts_c = ws + timedelta(hours=12)
            ts_o = ws + timedelta(days=1)
            candidates.append(
                {
                    "timestamp": ts_c.isoformat(),
                    "trace_id": f"t{tid}",
                    "symbol": "BTC",
                    "engine_id": "acevault",
                    "strategy_key": "acevault",
                    "side": "short",
                    "regime_value": "ranging",
                    "raw_strategy_score": 1.0,
                    "signal_alpha": 0.7,
                    "liq_mult": 1.2,
                    "regime_mult": 1.0,
                    "cost_mult": 0.9,
                    "final_score": 0.75,
                    "market_tier": 1,
                    "leverage_proposal": 5,
                    "asset_max_leverage": 10,
                    "hard_reject": False,
                    "hard_reject_reason": None,
                    "submit_eligible": True,
                }
            )
            outcomes.append(
                {
                    "timestamp": ts_o.isoformat(),
                    "trace_id": f"t{tid}",
                    "position_id": f"p{tid}",
                    "symbol": "BTC",
                    "engine_id": "acevault",
                    "strategy_key": "acevault",
                    "side": "short",
                    "submitted": True,
                    "entry_price": 84000.0,
                    "exit_price": 83800.0,
                    "position_size_usd": 100.0,
                    "leverage_used": 5,
                    "realized_pnl": 1.0,
                    "fees": 0.1,
                    "realized_net_pnl": 1.0,
                    "hold_time_seconds": 120,
                }
            )
    (data / "opportunity_candidates.jsonl").write_text(
        "\n".join(json.dumps(c, sort_keys=True) for c in candidates) + "\n",
        encoding="utf-8",
    )
    (data / "opportunity_trade_outcomes.jsonl").write_text(
        "\n".join(json.dumps(o, sort_keys=True) for o in outcomes) + "\n",
        encoding="utf-8",
    )
    cfg_path = tmp_path / "cfg.yaml"
    cfg_path.write_text(json.dumps(_ranker_cfg()), encoding="utf-8")
    run_walk_forward_validation(
        archive_dir=str(archive),
        outcomes_dir=str(data),
        output_dir=str(out),
        config_path=str(cfg_path),
        end_date=end,
        min_candidates_per_fold=2,
    )
    assert (out / "walk_forward_folds.csv").is_file()
    assert (out / "walk_forward_report.md").is_file()
    assert "week_2" in (out / "walk_forward_report.md").read_text(encoding="utf-8")
