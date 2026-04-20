"""Weekly walk-forward ranker/calibration validation (research-only, no disk config writes)."""

from __future__ import annotations

import argparse
import copy
import csv
import json
import logging
from bisect import bisect_right
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import httpx
import yaml

from src.calibration.opportunity_outcomes import OpportunityOutcomeStore
from src.calibration.score_calibrator import calibrate_score_components
from src.nxfh01.runtime import load_config
from src.research.historical_ranker_dataset import _parse_ts as hist_parse_ts
from src.research.historical_ranker_dataset import build_historical_ranker_dataset
from src.research.ranker_backtest import RankerBacktestResult, run_ranker_backtest
from src.research.weekly_review import _load_candle_rows

logger = logging.getLogger(__name__)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    raw = value.strip()
    if not raw:
        return None
    if len(raw) == 10:
        raw = raw + "T00:00:00+00:00"
    raw = raw.replace("Z", "+00:00")
    dt = datetime.fromisoformat(raw)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _row_ts(row: dict[str, Any]) -> datetime | None:
    return hist_parse_ts(row.get("timestamp"))


def compute_week_blocks(
    *, end_date: datetime, total_days: int, block_days: int
) -> tuple[list[tuple[datetime, datetime, str]], dict[str, Any]]:
    """Four sequential blocks: week_1 oldest .. week_4 ending at end_date.

    The four test folds occupy ``4 * block_days`` ending at ``end_date``.
    ``total_days`` is the wider catalog window; must be >= ``4 * block_days``.
    """
    if end_date.tzinfo is None:
        end_date = end_date.replace(tzinfo=timezone.utc)
    fold_span_days = 4 * int(block_days)
    if int(total_days) < fold_span_days:
        raise ValueError(
            f"total_days ({total_days}) must be >= 4 * block_days ({fold_span_days}) "
            "to define four weekly folds"
        )
    window_start = end_date - timedelta(days=int(total_days))
    fold_start = end_date - timedelta(days=fold_span_days)
    remainder_before_fold = (fold_start - window_start).total_seconds() / 86400.0
    blocks: list[tuple[datetime, datetime, str]] = []
    for k in range(4):
        ws = end_date - timedelta(days=(4 - k) * int(block_days))
        we = end_date - timedelta(days=(3 - k) * int(block_days))
        blocks.append((ws, we, f"week_{k + 1}"))
    meta = {
        "window_start": window_start.isoformat(),
        "window_end": end_date.isoformat(),
        "fold_span_start": fold_start.isoformat(),
        "remainder_days_before_first_fold": remainder_before_fold,
        "blocks": [
            {"label": lab, "start": a.isoformat(), "end": b.isoformat()} for a, b, lab in blocks
        ],
    }
    return blocks, meta


def filter_rows_by_time_range(
    rows: list[dict[str, Any]], start: datetime, end: datetime, *, end_inclusive: bool = False
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = _row_ts(r)
        if ts is None:
            continue
        if ts < start:
            continue
        if end_inclusive:
            if ts > end:
                continue
        else:
            if ts >= end:
                continue
        out.append(r)
    return out


def _index_hist_by_symbol(hist_rows: list[dict[str, Any]]) -> dict[str, list[tuple[datetime, dict[str, Any]]]]:
    by_sym: dict[str, list[tuple[datetime, dict[str, Any]]]] = {}
    for r in hist_rows:
        sym = str(r.get("symbol") or "").strip().upper()
        ts = _row_ts(r)
        if not sym or ts is None:
            continue
        by_sym.setdefault(sym, []).append((ts, r))
    for sym in by_sym:
        by_sym[sym].sort(key=lambda x: x[0])
    return by_sym


def _nearest_hist_row_at_or_before(
    by_sym: dict[str, list[tuple[datetime, dict[str, Any]]]], symbol: str, target: datetime
) -> dict[str, Any] | None:
    rows = by_sym.get(symbol.strip().upper())
    if not rows:
        return None
    tss = [t for t, _ in rows]
    i = bisect_right(tss, target) - 1
    if i < 0:
        return None
    base = dict(rows[i][1])
    return base


def build_labeled_ranker_rows(
    hist_rows: list[dict[str, Any]],
    candidates: list[dict[str, Any]],
    outcomes: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Join outcomes to candidates by ``trace_id``, attach market snapshot from nearest archive row."""
    by_trace_c: dict[str, dict[str, Any]] = {}
    for c in candidates:
        tid = str(c.get("trace_id") or "").strip()
        if tid:
            by_trace_c[tid] = c
    by_sym = _index_hist_by_symbol(hist_rows)
    labeled: list[dict[str, Any]] = []
    for o in outcomes:
        tid = str(o.get("trace_id") or "").strip()
        if not tid:
            continue
        c = by_trace_c.get(tid)
        if c is None:
            continue
        ts_c = hist_parse_ts(c.get("timestamp"))
        if ts_c is None:
            continue
        sym = str(c.get("symbol") or "").strip().upper()
        base = _nearest_hist_row_at_or_before(by_sym, sym, ts_c)
        if base is None:
            continue
        pnl = o.get("realized_net_pnl")
        if pnl is None:
            pnl = o.get("realized_pnl")
        try:
            float(pnl)
        except (TypeError, ValueError):
            continue
        row = dict(base)
        row["timestamp"] = c.get("timestamp") or base.get("timestamp")
        row["symbol"] = sym
        row["engine_id"] = str(c.get("engine_id") or "acevault")
        row["side"] = str(c.get("side") or "short")
        row["raw_strategy_score"] = float(c.get("raw_strategy_score") or c.get("signal_alpha") or 0.0)
        if c.get("regime_value"):
            row["regime_value"] = str(c.get("regime_value"))
        row["realized_net_pnl"] = float(pnl)
        row["_label_trace_id"] = tid
        labeled.append(row)
    return labeled


def apply_calibration_deltas_in_memory(
    base_cfg: dict[str, Any], deltas: dict[str, Any]
) -> tuple[dict[str, Any], list[str], list[str]]:
    """Apply only ``score_calibrator``-shaped deltas; skip unknown keys and strip ``reason`` fields."""
    cfg = copy.deepcopy(base_cfg)
    applied: list[str] = []
    skipped: list[str] = []

    def _opp() -> dict[str, Any]:
        cfg.setdefault("opportunity", {})
        assert isinstance(cfg["opportunity"], dict)
        return cfg["opportunity"]

    for key, payload in (deltas or {}).items():
        if not isinstance(payload, dict):
            skipped.append(f"{key}:non_object_payload")
            continue
        pl = {k: v for k, v in payload.items() if k != "reason"}
        try:
            if key == "opportunity.liquidity.weights":
                o = _opp()
                o.setdefault("liquidity", {})
                liq = o["liquidity"]
                assert isinstance(liq, dict)
                w = liq.setdefault("weights", {})
                assert isinstance(w, dict)
                if "volume" in pl:
                    w["volume"] = float(pl["volume"])
                if "open_interest" in pl:
                    w["open_interest"] = float(pl["open_interest"])
                applied.append(key)
            elif key == "opportunity.cost":
                o = _opp()
                o.setdefault("cost", {})
                cost = o["cost"]
                assert isinstance(cost, dict)
                for scale_key in ("impact_k_scale", "funding_k_scale", "premium_k_scale"):
                    if scale_key in pl:
                        base_key = scale_key.replace("_scale", "")
                        cur = float(cost.get(base_key, 0.0) or 0.0)
                        cost[base_key] = cur * float(pl[scale_key])
                applied.append(key)
            elif key == "opportunity.hard_reject":
                o = _opp()
                o.setdefault("hard_reject", {})
                hr = o["hard_reject"]
                assert isinstance(hr, dict)
                if "min_day_ntl_vlm_usd_scale" in pl:
                    cur = float(hr.get("min_day_ntl_vlm_usd", 0.0) or 0.0)
                    hr["min_day_ntl_vlm_usd"] = cur * float(pl["min_day_ntl_vlm_usd_scale"])
                if "min_open_interest_usd_scale" in pl:
                    cur = float(hr.get("min_open_interest_usd", 0.0) or 0.0)
                    hr["min_open_interest_usd"] = cur * float(pl["min_open_interest_usd_scale"])
                applied.append(key)
            elif key == "opportunity.tiering":
                o = _opp()
                o.setdefault("tiering", {})
                t = o["tiering"]
                assert isinstance(t, dict)
                if "tier1_min_liq_mult_scale" in pl:
                    t["tier1_min_liq_mult"] = float(t.get("tier1_min_liq_mult", 0.0)) * float(
                        pl["tier1_min_liq_mult_scale"]
                    )
                if "tier2_min_liq_mult_scale" in pl:
                    t["tier2_min_liq_mult"] = float(t.get("tier2_min_liq_mult", 0.0)) * float(
                        pl["tier2_min_liq_mult_scale"]
                    )
                applied.append(key)
            elif key == "opportunity.leverage.confidence_bands":
                o = _opp()
                o.setdefault("leverage", {})
                lev = o["leverage"]
                assert isinstance(lev, dict)
                bands = lev.setdefault("confidence_bands", {})
                assert isinstance(bands, dict)
                if "elite_min_score_shift" in pl:
                    bands["elite_min_score"] = float(bands.get("elite_min_score", 0.0)) + float(
                        pl["elite_min_score_shift"]
                    )
                if "strong_min_score_shift" in pl:
                    bands["strong_min_score"] = float(bands.get("strong_min_score", 0.0)) + float(
                        pl["strong_min_score_shift"]
                    )
                applied.append(key)
            else:
                skipped.append(f"{key}:unknown_delta_key")
        except (TypeError, ValueError, KeyError, AssertionError) as e:
            skipped.append(f"{key}:apply_failed:{e}")

    return cfg, applied, skipped


def _profit_factor(pnls: list[float]) -> float | None:
    gw = sum(x for x in pnls if x > 0)
    gl = sum(-x for x in pnls if x < 0)
    if gl <= 0:
        return None if abs(gw) < 1e-12 else float("inf")
    return gw / gl


def summarize_backtest(bt: RankerBacktestResult) -> dict[str, Any]:
    pnls: list[float] = []
    scores: list[float] = []
    levs: list[int] = []
    tier_counts: dict[str, int] = {}
    for r in bt.rows:
        v = r.get("realized_net_pnl")
        if v is None:
            v = r.get("realized_pnl")
        try:
            pnls.append(float(v))
        except (TypeError, ValueError):
            continue
        scores.append(float(r.get("final_score", 0.0)))
        levs.append(int(r.get("leverage_proposal", 1)))
        tier_counts[f"tier_{int(r.get('market_tier', 3))}"] = tier_counts.get(f"tier_{int(r.get('market_tier', 3))}", 0) + 1

    wins = sum(1 for x in pnls if x > 0)
    m = bt.metrics
    return {
        "candidate_count": int(m.get("total_rows", 0)),
        "hard_reject_count": int(round(float(m.get("hard_reject_frequency", 0.0)) * int(m.get("total_rows", 0)))),
        "hard_reject_frequency": float(m.get("hard_reject_frequency", 0.0)),
        "candidate_drop_rate": float(m.get("candidate_drop_rate", 0.0)),
        "avg_final_score": (sum(scores) / len(scores)) if scores else None,
        "avg_leverage_proposal": (sum(levs) / len(levs)) if levs else None,
        "mean_pnl_labeled": (sum(pnls) / len(pnls)) if pnls else None,
        "win_rate_labeled": (wins / len(pnls)) if pnls else None,
        "profit_factor_labeled": _profit_factor(pnls),
        "labeled_trade_rows": len(pnls),
        "tier_mix_counts": tier_counts,
        "score_bucket_performance": m.get("score_bucket_performance") or {},
        "tier_performance": m.get("tier_performance") or {},
        "leverage_band_performance": m.get("leverage_band_performance") or {},
    }


def _hl_post_info(url: str, body: dict[str, Any], timeout: float) -> Any:
    with httpx.Client(timeout=timeout) as client:
        r = client.post(url, json=body)
        r.raise_for_status()
        return r.json()


def fetch_candle_rows_from_hyperliquid(
    *,
    symbols: list[str],
    start: datetime,
    end: datetime,
    api_url: str = "https://api.hyperliquid.xyz/info",
    interval: str = "1h",
    timeout: float = 30.0,
) -> tuple[list[dict[str, Any]], str]:
    """Backfill candle-derived regime stubs via ``candleSnapshot`` (official API only)."""
    rows: list[dict[str, Any]] = []
    start_ms = int(start.timestamp() * 1000)
    end_ms = int(end.timestamp() * 1000)
    for sym in sorted({s.strip().upper() for s in symbols if str(s).strip()}):
        payload = {"type": "candleSnapshot", "req": {"coin": sym, "interval": interval, "startTime": start_ms, "endTime": end_ms}}
        data = _hl_post_info(api_url, payload, timeout=timeout)
        if not isinstance(data, list):
            continue
        for bar in data:
            if not isinstance(bar, dict):
                continue
            t_open = bar.get("t")
            if t_open is None:
                continue
            ts = datetime.fromtimestamp(int(t_open) / 1000.0, tz=timezone.utc)
            rows.append(
                {
                    "symbol": sym,
                    "timestamp": ts.isoformat(),
                    "regime_value": "ranging",
                    "_hl_candle_source": True,
                }
            )
    return rows, "hyperliquid_api_candleSnapshot"


def fetch_meta_snapshot_archive_object(
    *,
    api_url: str = "https://api.hyperliquid.xyz/info",
    timeout: float = 30.0,
) -> dict[str, Any]:
    raw = _hl_post_info(api_url, {"type": "metaAndAssetCtxs"}, timeout=timeout)
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "metaAndAssetCtxs": raw,
        "_hl_meta_source": True,
    }


def _load_yaml_config(path: str | None) -> dict[str, Any]:
    if path is None:
        return load_config()
    p = Path(path)
    if not p.is_file():
        raise ValueError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def _json_dump(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=True, default=str)
        f.write("\n")


def _write_folds_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    fields = sorted(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k) for k in fields})


def _write_bucket_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(
            f,
            fieldnames=["fold", "variant", "bucket", "count", "mean_pnl", "win_rate"],
        )
        w.writeheader()
        for r in rows:
            w.writerow(r)


def _build_recommendation_md(
    *,
    blocks_meta: dict[str, Any],
    fold_rows: list[dict[str, Any]],
    stability_notes: list[str],
    bucket_notes: list[str],
) -> str:
    lines = [
        "# Walk-forward validation report",
        "",
        "## Evaluation window",
        f"- window_start: {blocks_meta.get('window_start')}",
        f"- window_end: {blocks_meta.get('window_end')}",
        f"- fold_span_start: {blocks_meta.get('fold_span_start')}",
        f"- remainder_days_before_first_fold: {blocks_meta.get('remainder_days_before_first_fold')}",
        "",
        "## Block boundaries",
    ]
    for b in blocks_meta.get("blocks") or []:
        lines.append(f"- **{b.get('label')}**: [{b.get('start')} , {b.get('end')})")
    lines.extend(
        [
            "",
            "## Data sufficiency",
        ]
    )
    for n in stability_notes:
        lines.append(f"- {n}")
    lines.extend(["", "## Fold summary (OOS weeks 2–4)", ""])
    for fr in fold_rows:
        lines.append(
            f"- **{fr.get('test_week')}**: baseline mean_pnl={fr.get('baseline_mean_pnl')} "
            f"updated mean_pnl={fr.get('updated_mean_pnl')} "
            f"Δ={fr.get('delta_mean_pnl')} | labeled_rows={fr.get('labeled_rows')}"
        )
    lines.extend(["", "## Score bucket / tier / leverage (CSV detail)", ""])
    for bn in bucket_notes:
        lines.append(f"- {bn}")
    lines.extend(
        [
            "",
            "## Stability / variance",
            "- Compare `delta_mean_pnl` across folds in `walk_forward_folds.csv`.",
            "- Large swing between folds suggests instability or thin labels per week.",
            "",
        ]
    )
    # Recommendation heuristics
    dmeans = [fr.get("delta_mean_pnl") for fr in fold_rows if fr.get("delta_mean_pnl") is not None]
    improved = sum(1 for x in dmeans if isinstance(x, (int, float)) and float(x) > 0)
    worsened = sum(1 for x in dmeans if isinstance(x, (int, float)) and float(x) < 0)
    lines.extend(["", "## Final recommendation", ""])
    if not fold_rows:
        lines.append("- **Insufficient folds**: keep research-only; fix data pipeline.")
    elif improved >= 2 and worsened == 0:
        lines.append(
            "- **Promote toward shadow mode**: updated config beat baseline on most folds without "
            "material degradation; continue monitoring in `opportunity.shadow_mode` before enforce."
        )
    elif improved >= 2 and worsened == 1:
        lines.append(
            "- **Recalibrate more conservatively**: mixed folds; tighten deltas or increase "
            "`min_trades_for_update` before trusting weekly auto-deltas."
        )
    elif worsened >= 2:
        lines.append(
            "- **Reject proposed update logic for production**: in-memory deltas hurt multiple OOS weeks."
        )
    else:
        lines.append(
            "- **Keep current settings / research-only**: no consistent OOS edge from weekly calibration "
            "in this window; revise inputs or extend history when available."
        )
    lines.extend(
        [
            "",
            "### Questions answered",
            "",
            "- **Did weekly calibration help overall?** See fold Δ mean_pnl; summary JSON aggregates `aggregate`.",
            "- **Consistently or one week?** Compare per-fold `delta_mean_pnl` signs.",
            "- **Overfit / degradation?** Any strongly negative `delta_mean_pnl` or collapsing `labeled_rows` suggests overfit or harm.",
            "- **Move toward production?** Use recommendation above; execution path unchanged until separate rollout.",
            "",
        ]
    )
    return "\n".join(lines)


def run_walk_forward_validation(
    *,
    archive_dir: str,
    candles_dir: str | None = None,
    outcomes_dir: str | None = None,
    output_dir: str,
    config_path: str | None,
    end_date: datetime | None,
    total_days: int = 30,
    block_days: int = 7,
    min_candidates_per_fold: int = 5,
    allow_api_backfill: bool = False,
    hl_info_url: str = "https://api.hyperliquid.xyz/info",
    default_engine_id: str = "acevault",
    default_side: str = "short",
) -> dict[str, Any]:
    """Run 3 calibration folds and 3 OOS evaluations (weeks 2–4)."""
    cfg_base = _load_yaml_config(config_path)
    end = end_date or datetime.now(timezone.utc)
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)

    blocks, blocks_meta = compute_week_blocks(end_date=end, total_days=total_days, block_days=block_days)
    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)

    data_sources_log: list[dict[str, Any]] = []
    candle_rows: list[dict[str, Any]] = []
    candle_gaps: list[str] = []
    if candles_dir:
        candle_rows, candle_gaps = _load_candle_rows(candles_dir)
        data_sources_log.append({"component": "candles", "source": "local_dir", "path": candles_dir, "gaps": candle_gaps})

    hist_rows: list[dict[str, Any]] = []
    archive_used = Path(archive_dir)
    tmp: Path | None = None
    has_archive_files = archive_used.is_dir() and (
        any(archive_used.rglob("*.json"))
        or any(archive_used.rglob("*.jsonl"))
        or any(archive_used.rglob("*.json.gz"))
        or any(archive_used.rglob("*.jsonl.gz"))
    )
    if has_archive_files:
        hist_rows = build_historical_ranker_dataset(archive_dir, candle_rows=candle_rows or None)
        data_sources_log.append(
            {"component": "ranker_features", "source": "local_archive", "path": str(archive_dir), "rows": len(hist_rows)}
        )
        logger.info(
            "RISK_RESEARCH_WALK_FORWARD_DATA_SOURCE component=ranker_features source=local_archive rows=%d",
            len(hist_rows),
        )
    else:
        if not allow_api_backfill:
            raise ValueError(
                f"archive_dir unusable or empty: {archive_dir}; provide a valid archive or pass "
                "--allow-api-backfill for a single current metaAndAssetCtxs snapshot (degraded, point-in-time only)"
            )
        snap = fetch_meta_snapshot_archive_object(api_url=hl_info_url)
        tmp = out_root / "_api_backfill_archive"
        tmp.mkdir(parents=True, exist_ok=True)
        (tmp / "meta_backfill.json").write_text(
            json.dumps(snap, default=str, ensure_ascii=True),
            encoding="utf-8",
        )
        hist_rows = build_historical_ranker_dataset(tmp, candle_rows=candle_rows or None)
        data_sources_log.append(
            {
                "component": "ranker_features",
                "source": "hyperliquid_api_metaAndAssetCtxs",
                "path": str(tmp / "meta_backfill.json"),
                "rows": len(hist_rows),
                "warning": "single_point_in_time_snapshot",
            }
        )
        logger.warning(
            "RISK_RESEARCH_WALK_FORWARD_DATA_SOURCE component=ranker_features "
            "source=hyperliquid_api_metaAndAssetCtxs rows=%d (point-in-time)",
            len(hist_rows),
        )

    if candle_gaps and not candle_rows and allow_api_backfill and hist_rows:
        syms = sorted({str(r.get("symbol")) for r in hist_rows if r.get("symbol")})[:40]
        fold_start = datetime.fromisoformat(blocks_meta["fold_span_start"].replace("Z", "+00:00"))
        extra, src = fetch_candle_rows_from_hyperliquid(
            symbols=syms, start=fold_start, end=end, api_url=hl_info_url
        )
        candle_rows = extra
        data_sources_log.append({"component": "candles", "source": src, "rows": len(candle_rows)})
        logger.info(
            "RISK_RESEARCH_WALK_FORWARD_DATA_SOURCE component=candles source=%s rows=%d",
            src,
            len(candle_rows),
        )
        archive_for_rebuild = str(archive_dir) if has_archive_files else str(tmp)
        hist_rows = build_historical_ranker_dataset(archive_for_rebuild, candle_rows=candle_rows)

    data_dir = outcomes_dir or str((cfg_base.get("research") or {}).get("data_dir", "data/research"))
    store_cfg = {
        "research": {
            "enabled": True,
            "data_dir": data_dir,
            "save_candidate_logs": True,
            "save_trade_outcomes": True,
        },
        "calibration": cfg_base.get("calibration") or {"enabled": True},
    }
    store = OpportunityOutcomeStore(store_cfg)
    candidates_all = store.load_candidates()
    outcomes_all = store.load_trade_outcomes()
    data_sources_log.append(
        {
            "component": "labels",
            "source": "opportunity_outcome_store",
            "path": str(data_dir),
            "candidates": len(candidates_all),
            "outcomes": len(outcomes_all),
        }
    )

    labeled_all = build_labeled_ranker_rows(hist_rows, candidates_all, outcomes_all)
    if len(labeled_all) < min_candidates_per_fold:
        raise ValueError(
            f"insufficient labeled rows ({len(labeled_all)}) for walk-forward; need at least "
            f"min_candidates_per_fold={min_candidates_per_fold}. Check archive coverage, outcomes_dir, "
            "and trace_id alignment between candidates and outcomes."
        )

    fold_cfg = copy.deepcopy(cfg_base)
    fold_cfg.setdefault("calibration", {})
    fold_cfg["calibration"]["lookback_days"] = int(block_days)

    fold_output_rows: list[dict[str, Any]] = []
    bucket_csv_rows: list[dict[str, Any]] = []
    tier_csv_rows: list[dict[str, Any]] = []
    lev_csv_rows: list[dict[str, Any]] = []
    deltas_log: list[dict[str, Any]] = []
    stability_notes: list[str] = []
    if candle_gaps:
        stability_notes.append(f"candle_gaps: {candle_gaps}")

    for cal_idx in range(3):
        w_cal_start, w_cal_end, w_cal_label = blocks[cal_idx]
        w_test_start, w_test_end, w_test_label = blocks[cal_idx + 1]

        c_fold = filter_rows_by_time_range(candidates_all, w_cal_start, w_cal_end)
        o_fold = filter_rows_by_time_range(outcomes_all, w_cal_start, w_cal_end)
        ref_time = w_cal_end
        cal_res = calibrate_score_components(
            cfg=fold_cfg,
            candidates=c_fold,
            outcomes=o_fold,
            reference_time=ref_time,
        )
        deltas = (cal_res.recommendations or {}).get("deltas") or {}
        cfg_updated, applied, skipped = apply_calibration_deltas_in_memory(cfg_base, deltas)
        deltas_log.append(
            {
                "calibration_week": w_cal_label,
                "test_week": w_test_label,
                "calibration_status": cal_res.recommendations.get("status"),
                "deltas": deltas,
                "applied_keys": applied,
                "skipped_keys": skipped,
            }
        )

        test_rows = filter_rows_by_time_range(labeled_all, w_test_start, w_test_end)
        if len(test_rows) < min_candidates_per_fold:
            raise ValueError(
                f"insufficient labeled rows in {w_test_label} for OOS evaluation: "
                f"got {len(test_rows)}, require min_candidates_per_fold={min_candidates_per_fold}"
            )

        bt_base = run_ranker_backtest(
            test_rows, cfg=cfg_base, default_engine_id=default_engine_id, default_side=default_side
        )
        bt_new = run_ranker_backtest(
            test_rows, cfg=cfg_updated, default_engine_id=default_engine_id, default_side=default_side
        )
        sb = summarize_backtest(bt_base)
        su = summarize_backtest(bt_new)
        dmean = None
        if sb.get("mean_pnl_labeled") is not None and su.get("mean_pnl_labeled") is not None:
            dmean = float(su["mean_pnl_labeled"]) - float(sb["mean_pnl_labeled"])

        fold_output_rows.append(
            {
                "calibration_week": w_cal_label,
                "test_week": w_test_label,
                "labeled_rows": len(test_rows),
                "baseline_mean_pnl": sb.get("mean_pnl_labeled"),
                "updated_mean_pnl": su.get("mean_pnl_labeled"),
                "delta_mean_pnl": dmean,
                "baseline_profit_factor": sb.get("profit_factor_labeled"),
                "updated_profit_factor": su.get("profit_factor_labeled"),
                "baseline_win_rate": sb.get("win_rate_labeled"),
                "updated_win_rate": su.get("win_rate_labeled"),
                "baseline_drop_rate": sb.get("candidate_drop_rate"),
                "updated_drop_rate": su.get("candidate_drop_rate"),
                "baseline_hard_reject_freq": sb.get("hard_reject_frequency"),
                "updated_hard_reject_freq": su.get("hard_reject_frequency"),
                "baseline_avg_score": sb.get("avg_final_score"),
                "updated_avg_score": su.get("avg_final_score"),
                "baseline_avg_leverage": sb.get("avg_leverage_proposal"),
                "updated_avg_leverage": su.get("avg_leverage_proposal"),
            }
        )

        for variant, bt in (("baseline", bt_base), ("updated", bt_new)):
            for bucket, perf in (bt.metrics.get("score_bucket_performance") or {}).items():
                bucket_csv_rows.append(
                    {
                        "fold": w_test_label,
                        "variant": variant,
                        "bucket": bucket,
                        "count": (perf or {}).get("count"),
                        "mean_pnl": (perf or {}).get("mean_pnl"),
                        "win_rate": (perf or {}).get("win_rate"),
                    }
                )
            for bucket, perf in (bt.metrics.get("tier_performance") or {}).items():
                tier_csv_rows.append(
                    {
                        "fold": w_test_label,
                        "variant": variant,
                        "bucket": bucket,
                        "count": (perf or {}).get("count"),
                        "mean_pnl": (perf or {}).get("mean_pnl"),
                        "win_rate": (perf or {}).get("win_rate"),
                    }
                )
            for bucket, perf in (bt.metrics.get("leverage_band_performance") or {}).items():
                lev_csv_rows.append(
                    {
                        "fold": w_test_label,
                        "variant": variant,
                        "bucket": bucket,
                        "count": (perf or {}).get("count"),
                        "mean_pnl": (perf or {}).get("mean_pnl"),
                        "win_rate": (perf or {}).get("win_rate"),
                    }
                )

    summary = {
        "blocks_meta": blocks_meta,
        "data_sources": data_sources_log,
        "folds": fold_output_rows,
        "aggregate": {
            "folds_count": len(fold_output_rows),
            "mean_delta_mean_pnl": (
                sum(float(fr["delta_mean_pnl"]) for fr in fold_output_rows if fr.get("delta_mean_pnl") is not None)
                / len(fold_output_rows)
                if fold_output_rows
                else None
            ),
        },
        "calibration_results": deltas_log,
    }
    _json_dump(out_root / "walk_forward_summary.json", summary)
    _json_dump(out_root / "walk_forward_config_deltas.json", {"folds": deltas_log})
    _write_folds_csv(out_root / "walk_forward_folds.csv", fold_output_rows)
    _write_bucket_csv(out_root / "walk_forward_score_buckets.csv", bucket_csv_rows)
    _write_bucket_csv(out_root / "walk_forward_tier_metrics.csv", tier_csv_rows)
    _write_bucket_csv(out_root / "walk_forward_leverage_metrics.csv", lev_csv_rows)

    hr_rows: list[dict[str, Any]] = []
    for fr in fold_output_rows:
        hr_rows.append(
            {
                "test_week": fr.get("test_week"),
                "baseline_hard_reject_freq": fr.get("baseline_hard_reject_freq"),
                "updated_hard_reject_freq": fr.get("updated_hard_reject_freq"),
            }
        )
    _write_folds_csv(out_root / "walk_forward_hard_rejects.csv", hr_rows)

    bucket_notes = [
        "Per-fold score buckets: `walk_forward_score_buckets.csv`.",
        "Per-fold tier and leverage bands: `walk_forward_tier_metrics.csv`, `walk_forward_leverage_metrics.csv`.",
        "Hard reject frequencies per OOS week: `walk_forward_hard_rejects.csv`.",
    ]
    report_md = _build_recommendation_md(
        blocks_meta=blocks_meta,
        fold_rows=fold_output_rows,
        stability_notes=stability_notes,
        bucket_notes=bucket_notes,
    )
    (out_root / "walk_forward_report.md").write_text(report_md, encoding="utf-8")

    return {"output_dir": str(out_root), "summary": summary}


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    p = argparse.ArgumentParser(description="NXFH01 weekly walk-forward ranker validation (research-only)")
    p.add_argument("--archive-dir", required=True)
    p.add_argument("--output-dir", required=True)
    p.add_argument("--candles-dir", default=None)
    p.add_argument("--outcomes-dir", default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--end-date", default=None, help="ISO end of evaluation window (UTC). Default: now")
    p.add_argument("--days", type=int, default=30)
    p.add_argument("--block-days", type=int, default=7)
    p.add_argument("--min-candidates-per-fold", type=int, default=5)
    p.add_argument(
        "--allow-api-backfill",
        action="store_true",
        help="If local candles missing, fetch candleSnapshot; if archive empty, fetch one metaAndAssetCtxs (degraded)",
    )
    p.add_argument("--hl-info-url", default="https://api.hyperliquid.xyz/info")
    args = p.parse_args()
    end = _parse_dt(args.end_date) if args.end_date else None
    out = run_walk_forward_validation(
        archive_dir=args.archive_dir,
        candles_dir=args.candles_dir,
        outcomes_dir=args.outcomes_dir,
        output_dir=args.output_dir,
        config_path=args.config,
        end_date=end,
        total_days=int(args.days),
        block_days=int(args.block_days),
        min_candidates_per_fold=int(args.min_candidates_per_fold),
        allow_api_backfill=bool(args.allow_api_backfill),
        hl_info_url=str(args.hl_info_url),
    )
    print(json.dumps(out["summary"], indent=2, sort_keys=True, default=str))


if __name__ == "__main__":
    main()
