"""Optional weekly research summarization with Fathom (advisory-only)."""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

import yaml

from src.intelligence.fathom_client import FathomClient
from src.intelligence.schemas import (
    NON_AUTO_APPLY_NOTE,
    WeeklySummaryResult,
    build_fallback_weekly_summary,
    parse_weekly_summary_response,
)

logger = logging.getLogger(__name__)

_SYSTEM_PROMPT = (
    "You are an analytical review assistant for a deterministic Hyperliquid trading system. "
    "Your output is advisory-only and must never imply auto-apply behavior."
)


def _json_dump(path: Path, payload: dict[str, Any]) -> None:
    with path.open("w", encoding="utf-8") as f:
        json.dump(payload, f, indent=2, sort_keys=True, ensure_ascii=True)
        f.write("\n")


def _load_config(config_path: str | None) -> dict[str, Any]:
    if not config_path:
        from src.nxfh01.runtime import load_config

        return load_config()
    p = Path(config_path)
    if not p.is_file():
        raise ValueError(f"config file not found: {p}")
    with p.open("r", encoding="utf-8") as f:
        raw = yaml.safe_load(f)
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")
    return raw


def _read_json_file(path: Path, *, required: bool) -> dict[str, Any] | None:
    if not path.is_file():
        if required:
            raise ValueError(f"missing required artifact: {path.name}")
        return None
    with path.open("r", encoding="utf-8") as f:
        obj = json.load(f)
    if not isinstance(obj, dict):
        raise ValueError(f"artifact must be JSON object: {path.name}")
    return obj


def _read_text_file(path: Path, *, required: bool) -> str | None:
    if not path.is_file():
        if required:
            raise ValueError(f"missing required artifact: {path.name}")
        return None
    return path.read_text(encoding="utf-8")


def _load_prompt_template() -> str:
    p = Path(__file__).resolve().parent / "prompt_templates" / "weekly_summary_prompt.md"
    return p.read_text(encoding="utf-8")


def _build_prompt(*, artifacts: dict[str, Any], max_recommendations: int) -> str:
    template = _load_prompt_template()
    rendered = template.replace("{{ max_recommendations }}", str(max_recommendations))
    artifacts_json = json.dumps(artifacts, indent=2, sort_keys=True, ensure_ascii=True, default=str)
    return rendered.replace("{{ artifacts_json }}", artifacts_json)


def _build_artifact_payload(input_dir: Path) -> dict[str, Any]:
    payload = {
        "historical_summary": _read_json_file(input_dir / "historical_summary.json", required=True),
        "calibration_recommendations": _read_json_file(
            input_dir / "calibration_recommendations.json",
            required=True,
        ),
        "weekly_review_markdown": _read_text_file(input_dir / "weekly_review.md", required=True),
        "baseline_metrics": _read_json_file(input_dir / "baseline_metrics.json", required=False),
        "data_quality": _read_json_file(input_dir / "data_quality.json", required=False),
    }
    payload["available_files"] = sorted(
        [
            name
            for name in (
                "historical_summary.json",
                "calibration_recommendations.json",
                "weekly_review.md",
                "baseline_metrics.json",
                "data_quality.json",
            )
            if (input_dir / name).is_file()
        ]
    )
    return payload


def _hash_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _to_markdown(summary: WeeklySummaryResult) -> str:
    lines: list[str] = [
        "# Weekly LLM Summary",
        "",
        "## Verdict",
        f"- {summary.weekly_verdict.label}: {summary.weekly_verdict.reason}",
        "",
        "## What improved",
    ]
    if summary.top_positive_changes:
        for item in summary.top_positive_changes:
            lines.append(f"- {item.title} ({item.confidence}): {item.evidence}")
    else:
        lines.append("- No high-confidence improvement identified from provided artifacts.")
    lines.extend(["", "## What degraded"])
    if summary.top_risks:
        for item in summary.top_risks:
            lines.append(f"- {item.title} ({item.confidence}): {item.evidence}")
    else:
        lines.append("- No material degradation signal identified from provided artifacts.")
    lines.extend(["", "## Engine observations"])
    if summary.engine_observations:
        for item in summary.engine_observations:
            lines.append(f"- {item.engine} ({item.confidence}): {item.observation} | {item.evidence}")
    else:
        lines.append("- Insufficient engine-level evidence for segmented observations.")
    lines.extend(["", "## Recommended changes"])
    if summary.recommended_changes:
        for item in summary.recommended_changes:
            lines.append(
                "- "
                + f"[P{item.priority}] {item.change_type} {item.target}: {item.recommendation} "
                + f"(benefit: {item.expected_benefit}; risk: {item.risk}; confidence: {item.confidence})"
            )
    else:
        lines.append("- No config changes recommended this week.")
    lines.extend(["", "## Do not change yet"])
    for item in summary.things_not_to_change:
        lines.append(f"- {item.item}: {item.reason}")
    lines.extend(["", "## Data quality notes"])
    if summary.data_quality_notes:
        for item in summary.data_quality_notes:
            lines.append(f"- [{item.severity}] {item.note}")
    else:
        lines.append("- No major data-quality notes were surfaced.")
    lines.extend(
        [
            "",
            "## Operator focus next week",
            f"- Summary: {summary.operator_memo.summary}",
            f"- Focus: {summary.operator_memo.next_week_focus}",
            f"- {summary.operator_memo.non_auto_apply_note}",
            "",
        ]
    )
    return "\n".join(lines)


def _summary_to_output_payload(
    summary: WeeklySummaryResult,
    *,
    status: str,
    trace: dict[str, Any],
    warnings: list[str],
) -> dict[str, Any]:
    payload = summary.to_dict()
    payload["status"] = status
    payload["trace"] = trace
    payload["warnings"] = warnings
    return payload


def run_weekly_llm_summary(
    *,
    input_dir: str,
    output_dir: str,
    config_path: str | None,
) -> dict[str, Any]:
    cfg = _load_config(config_path)
    intelligence = cfg.get("intelligence") or {}
    weekly_cfg = (intelligence.get("weekly_summary") or {}) if isinstance(intelligence, dict) else {}
    if not bool(intelligence.get("enabled", False)):
        return {"enabled": False, "reason": "intelligence.disabled"}
    if not bool(weekly_cfg.get("enabled", True)):
        return {"enabled": False, "reason": "intelligence.weekly_summary.disabled"}

    max_recommendations = int(weekly_cfg.get("max_recommendations", 2))
    write_markdown = bool(weekly_cfg.get("write_markdown", True))
    require_structured = bool(weekly_cfg.get("require_structured_output", True))

    in_dir = Path(input_dir)
    out_dir = Path(output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    artifacts = _build_artifact_payload(in_dir)
    prompt = _build_prompt(artifacts=artifacts, max_recommendations=max_recommendations)
    prompt_hash = _hash_text(prompt)
    artifacts_hash = _hash_text(json.dumps(artifacts, sort_keys=True, ensure_ascii=True, default=str))
    trace = {
        "prompt_hash_sha256": prompt_hash,
        "artifacts_hash_sha256": artifacts_hash,
        "input_dir": str(in_dir),
        "artifact_files": artifacts.get("available_files") or [],
    }

    summary: WeeklySummaryResult
    warnings: list[str] = []
    status = "ok"
    raw_response = ""
    client = FathomClient.from_config(cfg)
    try:
        raw_response = client.complete_json(system_prompt=_SYSTEM_PROMPT, user_prompt=prompt)
        summary = parse_weekly_summary_response(raw_response, max_recommendations=max_recommendations)
    except Exception as err:
        warning = f"llm_summary_failed:{client.sanitize_error(err)}"
        warnings.append(warning)
        logger.warning("INTELLIGENCE_WEEKLY_SUMMARY_FAILED detail=%s", warning)
        if require_structured:
            summary = build_fallback_weekly_summary(warning)
            status = "fallback"
        else:
            summary = build_fallback_weekly_summary(warning)
            status = "best_effort_fallback"

    if summary.operator_memo.non_auto_apply_note != NON_AUTO_APPLY_NOTE:
        raise ValueError("weekly summary output violated non-auto-apply policy")

    out_json = out_dir / "llm_weekly_summary.json"
    out_md = out_dir / "llm_weekly_summary.md"
    payload = _summary_to_output_payload(summary, status=status, trace=trace, warnings=warnings)
    payload["model_trace"] = client.trace_config()
    if raw_response:
        payload["raw_response_hash_sha256"] = _hash_text(raw_response)
    _json_dump(out_json, payload)

    if write_markdown:
        out_md.write_text(_to_markdown(summary), encoding="utf-8")

    return {
        "enabled": True,
        "status": status,
        "llm_weekly_summary_json": str(out_json),
        "llm_weekly_summary_md": str(out_md) if write_markdown else None,
        "warnings": warnings,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate advisory weekly LLM summary artifacts")
    parser.add_argument("--input-dir", required=True)
    parser.add_argument("--output-dir", required=True)
    parser.add_argument("--config", default=None)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    out = run_weekly_llm_summary(
        input_dir=args.input_dir,
        output_dir=args.output_dir,
        config_path=args.config,
    )
    print(json.dumps(out, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
