"""Weekly research/calibration review runner (recommend-only)."""

from __future__ import annotations

import argparse
import csv
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

import yaml

from src.calibration.opportunity_outcomes import OpportunityOutcomeStore
from src.calibration.score_calibrator import CalibrationResult, calibrate_score_components
from src.intelligence.weekly_summary import run_weekly_llm_summary
from src.nxfh01.runtime import load_config
from src.research.historical_ranker_dataset import build_historical_ranker_dataset
from src.research.ranker_backtest import RankerBacktestResult, run_ranker_backtest


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


def _load_config(config_path: str | None) -> dict[str, Any]:
    if config_path is None:
        return load_config()
    p = Path(config_path)
    if not p.is_file():
        raise ValueError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("config root must be a mapping")
    return data


def resolve_weekly_review_dirs(
    cfg: dict[str, Any],
    archive_dir: str | None,
    output_dir: str | None,
) -> tuple[str, str]:
    research = cfg.get("research")
    if not isinstance(research, dict):
        research = {}
    arch = (archive_dir or "").strip()
    if not arch:
        arch = str(research.get("weekly_review_archive_dir") or "").strip()
    out = (output_dir or "").strip()
    if not out:
        out = str(research.get("weekly_review_output_dir") or "").strip()
    if not arch:
        raise ValueError(
            "weekly review archive_dir missing: pass --archive-dir or set "
            "research.weekly_review_archive_dir in config.yaml"
        )
    if not out:
        raise ValueError(
            "weekly review output_dir missing: pass --output-dir or set "
            "research.weekly_review_output_dir in config.yaml"
        )
    return arch, out


def _load_candle_rows(candles_dir: str | None) -> tuple[list[dict[str, Any]], list[str]]:
    if not candles_dir:
        return [], ["candles_dir_not_provided"]
    root = Path(candles_dir)
    if not root.is_dir():
        return [], [f"candles_dir_missing:{root}"]
    files = sorted([*root.rglob("*.json"), *root.rglob("*.jsonl")])
    if not files:
        return [], [f"candles_files_missing:{root}"]
    rows: list[dict[str, Any]] = []
    gaps: list[str] = []
    for fp in files:
        if fp.suffix == ".jsonl":
            with fp.open("r", encoding="utf-8") as f:
                for line in f:
                    s = line.strip()
                    if not s:
                        continue
                    try:
                        obj = json.loads(s)
                    except json.JSONDecodeError:
                        gaps.append(f"invalid_candle_jsonl_line:{fp.name}")
                        continue
                    if isinstance(obj, dict):
                        rows.append(obj)
        else:
            with fp.open("r", encoding="utf-8") as f:
                try:
                    payload = json.load(f)
                except json.JSONDecodeError:
                    gaps.append(f"invalid_candle_json:{fp.name}")
                    continue
            if isinstance(payload, list):
                rows.extend([x for x in payload if isinstance(x, dict)])
            elif isinstance(payload, dict):
                rows.append(payload)
    return rows, gaps


def _filter_rows_by_range(
    rows: list[dict[str, Any]], start_dt: datetime | None, end_dt: datetime | None
) -> list[dict[str, Any]]:
    if start_dt is None and end_dt is None:
        return rows
    out: list[dict[str, Any]] = []
    for r in rows:
        ts = _parse_dt(str(r.get("timestamp") or ""))
        if ts is None:
            continue
        if start_dt is not None and ts < start_dt:
            continue
        if end_dt is not None and ts > end_dt:
            continue
        out.append(r)
    return out


def _json_dump(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=True, default=str)
        f.write("\n")


def _bucket_csv(path: Path, bucket_metrics: dict[str, Any]) -> None:
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["bucket", "count", "mean_pnl", "win_rate"])
        w.writeheader()
        for bucket in sorted(bucket_metrics.keys()):
            row = bucket_metrics[bucket] or {}
            w.writerow(
                {
                    "bucket": bucket,
                    "count": row.get("count"),
                    "mean_pnl": row.get("mean_pnl"),
                    "win_rate": row.get("win_rate"),
                }
            )


def _hard_reject_by_reason(rows: list[dict[str, Any]]) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        if not bool(r.get("hard_reject")):
            continue
        reason = str(r.get("hard_reject_reason") or "unknown")
        out[reason] = out.get(reason, 0) + 1
    return dict(sorted(out.items()))


def _engine_skew(rows: list[dict[str, Any]]) -> dict[str, Any]:
    by_engine: dict[str, list[float]] = {}
    for r in rows:
        engine = str(r.get("engine_id") or "unknown")
        pnl = r.get("realized_net_pnl")
        if pnl is None:
            pnl = r.get("realized_pnl")
        try:
            p = float(pnl)
        except (TypeError, ValueError):
            continue
        by_engine.setdefault(engine, []).append(p)
    out: dict[str, Any] = {}
    for engine in sorted(by_engine.keys()):
        vals = by_engine[engine]
        wins = sum(1 for x in vals if x > 0)
        out[engine] = {
            "samples": len(vals),
            "mean_pnl": sum(vals) / len(vals),
            "win_rate": wins / len(vals),
        }
    return out


def _format_bucket_lines(bucket_metrics: dict[str, Any]) -> list[str]:
    lines: list[str] = []
    for bucket in sorted(bucket_metrics.keys()):
        m = bucket_metrics[bucket] or {}
        lines.append(
            f"- `{bucket}`: count={m.get('count')} mean_pnl={m.get('mean_pnl')} win_rate={m.get('win_rate')}"
        )
    return lines or ["- none"]


def _load_baseline_metrics_for_merge(out_root: Path) -> tuple[dict[str, Any] | None, str | None]:
    """If output_dir contains baseline_metrics.json (JSON object), return it for the deterministic MD.

    Operators may drop this file next to other weekly_review outputs before a run; the same filename
    is optional input for ``run_weekly_llm_summary`` (see src/intelligence/weekly_summary.py).
    """
    path = out_root / "baseline_metrics.json"
    if not path.is_file():
        return None, None
    try:
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
    except (OSError, json.JSONDecodeError) as e:
        logger.warning("weekly_review baseline_metrics.json unreadable: %s", e)
        return None, f"baseline_metrics.json present but not readable: {e}"
    if not isinstance(obj, dict):
        logger.warning("weekly_review baseline_metrics.json must be a JSON object")
        return None, "baseline_metrics.json must be a JSON object (top-level mapping)"
    return obj, None


def _format_baseline_section(
    baseline: dict[str, Any] | None, baseline_error: str | None
) -> list[str]:
    lines: list[str] = [
        "## Baseline metrics (baseline_metrics.json)",
        "",
        (
            "- Operator-supplied reference JSON merged into this deterministic report when the file "
            "exists under the same `--output-dir` before the run (optional input for weekly LLM summary)."
        ),
        "",
    ]
    if baseline_error:
        lines.append(f"- {baseline_error}")
        lines.append("")
        return lines
    if baseline is None:
        return []
    body = json.dumps(baseline, indent=2, sort_keys=True, ensure_ascii=True, default=str)
    lines.append("```json")
    lines.extend(body.splitlines())
    lines.append("```")
    lines.append("")
    return lines


def _build_weekly_markdown(
    *,
    start_date: str,
    end_date: str,
    completeness_gaps: list[str],
    counts: dict[str, Any],
    backtest: RankerBacktestResult,
    hard_rejects: dict[str, int],
    engine_skew: dict[str, Any],
    calibration: CalibrationResult,
    baseline: dict[str, Any] | None = None,
    baseline_error: str | None = None,
) -> str:
    lines: list[str] = []
    lines.append("# NXFH01 Weekly Ranker Review")
    lines.append("")
    lines.append("## Date Range Analyzed")
    lines.append(f"- start: {start_date}")
    lines.append(f"- end: {end_date}")
    lines.append("")
    lines.append("## Data Completeness / Gaps")
    if completeness_gaps:
        lines.extend([f"- {g}" for g in completeness_gaps])
    else:
        lines.append("- no critical gaps detected")
    lines.append("")
    lines.append("## Volume Summary")
    lines.append(f"- symbols: {counts.get('symbols')}")
    lines.append(f"- timestamps: {counts.get('timestamps')}")
    lines.append(f"- candidates_analyzed: {counts.get('candidates_analyzed')}")
    lines.append("")
    lines.extend(_format_baseline_section(baseline, baseline_error))
    lines.append("## Hard Rejects + Drops")
    lines.append(f"- hard_reject_frequency: {backtest.metrics.get('hard_reject_frequency')}")
    lines.append(f"- candidate_drop_rate: {backtest.metrics.get('candidate_drop_rate')}")
    lines.append("- hard_reject_by_reason:")
    if hard_rejects:
        lines.extend([f"  - {k}: {v}" for k, v in hard_rejects.items()])
    else:
        lines.append("  - none")
    lines.append("")
    lines.append("## Performance by Score Bucket")
    lines.extend(_format_bucket_lines(backtest.metrics.get("score_bucket_performance") or {}))
    lines.append("")
    lines.append("## Performance by Tier")
    lines.extend(_format_bucket_lines(backtest.metrics.get("tier_performance") or {}))
    lines.append("")
    lines.append("## Performance by Leverage Band")
    lines.extend(_format_bucket_lines(backtest.metrics.get("leverage_band_performance") or {}))
    lines.append("")
    lines.append("## Notable Engine Skew")
    if engine_skew:
        for e in sorted(engine_skew.keys()):
            s = engine_skew[e]
            lines.append(
                f"- `{e}`: samples={s.get('samples')} mean_pnl={s.get('mean_pnl')} win_rate={s.get('win_rate')}"
            )
    else:
        lines.append("- insufficient realized-pnl rows to assess engine skew")
    lines.append("")
    lines.append("## Calibration Recommendations")
    lines.append(f"- status: {calibration.recommendations.get('status')}")
    deltas = calibration.recommendations.get("deltas") or {}
    if deltas:
        for k in sorted(deltas.keys()):
            lines.append(f"- {k}: {json.dumps(deltas[k], sort_keys=True)}")
    else:
        lines.append("- no deltas recommended this run")
    lines.append("")
    lines.append("## Confidence / Caution Notes")
    paired = calibration.summary_metrics.get("paired_trade_count", 0)
    if calibration.recommendations.get("status") == "insufficient_data" or paired == 0:
        lines.append("- low confidence: insufficient matched candidate/outcome pairs")
    else:
        lines.append("- medium confidence: recommendations based on observed live outcomes")
    lines.append("- this workflow is recommend-only and does not auto-apply config changes")
    lines.append("")
    return "\n".join(lines)


def run_weekly_review(
    *,
    archive_dir: str,
    candles_dir: str | None,
    outcomes_dir: str | None,
    output_dir: str,
    start_date: str | None,
    end_date: str | None,
    lookback_days: int | None,
    config_path: str | None,
    with_llm_summary: bool = False,
) -> dict[str, str]:
    cfg = _load_config(config_path)
    if lookback_days is not None:
        cfg.setdefault("calibration", {})
        cfg["calibration"]["lookback_days"] = int(lookback_days)

    archive_root = Path(archive_dir)
    if not archive_root.is_dir():
        raise ValueError(f"archive-dir not found: {archive_root}")

    out_root = Path(output_dir)
    out_root.mkdir(parents=True, exist_ok=True)
    baseline_metrics, baseline_metrics_error = _load_baseline_metrics_for_merge(out_root)

    candle_rows, candle_gaps = _load_candle_rows(candles_dir)
    hist_rows = build_historical_ranker_dataset(archive_root, candle_rows=candle_rows)
    start_dt = _parse_dt(start_date)
    end_dt = _parse_dt(end_date)
    filt_rows = _filter_rows_by_range(hist_rows, start_dt, end_dt)
    if not filt_rows:
        raise ValueError("no rows after date-range filter")

    backtest = run_ranker_backtest(filt_rows, cfg=cfg, default_engine_id="acevault", default_side="short")

    data_dir = outcomes_dir or str((cfg.get("research") or {}).get("data_dir", "data/research"))
    store_cfg = {
        "research": {
            "enabled": True,
            "data_dir": data_dir,
            "save_candidate_logs": True,
            "save_trade_outcomes": True,
        },
        "calibration": cfg.get("calibration") or {"enabled": True},
    }
    store = OpportunityOutcomeStore(store_cfg)
    candidates = store.load_candidates()
    outcomes = store.load_trade_outcomes()
    calibration = calibrate_score_components(cfg=cfg, candidates=candidates, outcomes=outcomes)

    timestamps = sorted({str(r.get("timestamp")) for r in filt_rows})
    symbols = sorted({str(r.get("symbol")) for r in filt_rows})
    regime_covered = sum(1 for r in filt_rows if str(r.get("regime_value") or "").strip())
    regime_coverage_ratio = (regime_covered / len(filt_rows)) if filt_rows else 0.0
    completeness_gaps = list(candle_gaps)
    if not candle_rows:
        completeness_gaps.append("reduced_regime_coverage:no_candle_inputs")
    if regime_coverage_ratio < 0.5:
        completeness_gaps.append(f"low_regime_coverage_ratio:{regime_coverage_ratio:.3f}")
    if len(outcomes) < int((cfg.get("calibration") or {}).get("min_trades_for_update", 50)):
        completeness_gaps.append("insufficient_outcomes_for_high_confidence_calibration")
    if baseline_metrics_error:
        completeness_gaps.append(f"baseline_metrics_merge:{baseline_metrics_error}")

    hard_rejects = _hard_reject_by_reason(backtest.rows)
    engine_skew = _engine_skew(backtest.rows)
    period_start = start_dt.isoformat() if start_dt else timestamps[0]
    period_end = end_dt.isoformat() if end_dt else timestamps[-1]
    counts = {
        "symbols": len(symbols),
        "timestamps": len(timestamps),
        "candidates_analyzed": len(backtest.rows),
        "historical_rows": len(filt_rows),
        "candidate_logs_loaded": len(candidates),
        "outcome_logs_loaded": len(outcomes),
    }

    historical_summary = {
        "period": {"start": period_start, "end": period_end},
        "input_paths": {
            "archive_dir": str(archive_root),
            "candles_dir": candles_dir,
            "outcomes_dir": data_dir,
        },
        "data_completeness": {
            "regime_coverage_ratio": regime_coverage_ratio,
            "gaps": completeness_gaps,
        },
        "counts": counts,
        "hard_reject_by_reason": hard_rejects,
        "engine_skew": engine_skew,
        "backtest_metrics": backtest.metrics,
    }
    calibration_json = {
        "recommend_only": True,
        "status": calibration.recommendations.get("status"),
        "summary_metrics": calibration.summary_metrics,
        "recommendations": calibration.recommendations,
        "confidence_notes": [
            "insufficient outcomes - low confidence"
            if calibration.recommendations.get("status") == "insufficient_data"
            else "outcomes available - review manually before any config update"
        ],
    }
    weekly_md = _build_weekly_markdown(
        start_date=period_start,
        end_date=period_end,
        completeness_gaps=completeness_gaps,
        counts=counts,
        backtest=backtest,
        hard_rejects=hard_rejects,
        engine_skew=engine_skew,
        calibration=calibration,
        baseline=baseline_metrics,
        baseline_error=baseline_metrics_error,
    )

    historical_path = out_root / "historical_summary.json"
    calibration_path = out_root / "calibration_recommendations.json"
    weekly_md_path = out_root / "weekly_review.md"
    _json_dump(historical_path, historical_summary)
    _json_dump(calibration_path, calibration_json)
    weekly_md_path.write_text(weekly_md, encoding="utf-8")

    _bucket_csv(out_root / "score_bucket_metrics.csv", backtest.metrics.get("score_bucket_performance") or {})
    _bucket_csv(out_root / "rank_bucket_metrics.csv", backtest.metrics.get("rank_bucket_performance") or {})
    _bucket_csv(out_root / "tier_metrics.csv", backtest.metrics.get("tier_performance") or {})
    _bucket_csv(
        out_root / "leverage_band_metrics.csv",
        backtest.metrics.get("leverage_band_performance") or {},
    )

    outputs = {
        "historical_summary": str(historical_path),
        "calibration_recommendations": str(calibration_path),
        "weekly_review": str(weekly_md_path),
        "score_bucket_metrics": str(out_root / "score_bucket_metrics.csv"),
        "rank_bucket_metrics": str(out_root / "rank_bucket_metrics.csv"),
        "tier_metrics": str(out_root / "tier_metrics.csv"),
        "leverage_band_metrics": str(out_root / "leverage_band_metrics.csv"),
    }
    if with_llm_summary:
        try:
            llm_out = run_weekly_llm_summary(
                input_dir=str(out_root),
                output_dir=str(out_root),
                config_path=config_path,
            )
            if llm_out.get("enabled"):
                llm_json = llm_out.get("llm_weekly_summary_json")
                llm_md = llm_out.get("llm_weekly_summary_md")
                if isinstance(llm_json, str):
                    outputs["llm_weekly_summary_json"] = llm_json
                if isinstance(llm_md, str):
                    outputs["llm_weekly_summary_md"] = llm_md
        except Exception as e:
            print(f"WEEKLY_REVIEW_LLM_SUMMARY_SKIPPED error={e}")
    return outputs


def main() -> None:
    p = argparse.ArgumentParser(description="NXFH01 weekly research/calibration review runner")
    p.add_argument(
        "--archive-dir",
        default=None,
        help="Directory with *.json / *.jsonl HL meta archives (default: research.weekly_review_archive_dir)",
    )
    p.add_argument("--candles-dir", default=None)
    p.add_argument("--outcomes-dir", default=None)
    p.add_argument(
        "--output-dir",
        default=None,
        help="Directory for weekly_review.md and JSON outputs (default: research.weekly_review_output_dir)",
    )
    p.add_argument("--start-date", default=None)
    p.add_argument("--end-date", default=None)
    p.add_argument("--lookback-days", type=int, default=None)
    p.add_argument("--config", default=None)
    p.add_argument("--with-llm-summary", action="store_true")
    args = p.parse_args()
    cfg = _load_config(args.config)
    try:
        archive_dir, output_dir = resolve_weekly_review_dirs(cfg, args.archive_dir, args.output_dir)
    except ValueError as e:
        p.error(str(e))
    paths = run_weekly_review(
        archive_dir=archive_dir,
        candles_dir=args.candles_dir,
        outcomes_dir=args.outcomes_dir,
        output_dir=output_dir,
        start_date=args.start_date,
        end_date=args.end_date,
        lookback_days=args.lookback_days,
        config_path=args.config,
        with_llm_summary=bool(args.with_llm_summary),
    )
    print(json.dumps(paths, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

