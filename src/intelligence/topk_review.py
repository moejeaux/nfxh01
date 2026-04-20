"""Top-K advisory review (non-authoritative, bounded, optional)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from src.intelligence.fathom_client import FathomClient
from src.intelligence.schemas import (
    TopKReviewResult,
    build_fallback_topk_review,
    parse_topk_review_response,
)

logger = logging.getLogger(__name__)
PROMPT_TEMPLATE_ID = "topk_review_prompt_v1"

_SYSTEM_PROMPT = (
    "You are a top-K advisory reviewer for a deterministic Hyperliquid system. "
    "You provide commentary only, never execution or control."
)


def build_topk_candidate_payload(
    candidates: list[Mapping[str, Any]],
    *,
    top_k: int,
) -> tuple[list[dict[str, Any]], dict[str, int]]:
    trimmed = list(candidates)[: max(1, int(top_k))]
    payload: list[dict[str, Any]] = []
    rank_by_symbol: dict[str, int] = {}
    for idx, row in enumerate(trimmed, start=1):
        symbol = str(row.get("symbol") or "").strip().upper()
        if not symbol:
            continue
        rank_by_symbol[symbol] = idx
        payload.append(
            {
                "symbol": symbol,
                "engine": str(row.get("engine") or "acevault"),
                "raw_score": _f(row.get("raw_score")),
                "signal_alpha": _f(row.get("signal_alpha")),
                "liq_mult": _f(row.get("liq_mult")),
                "regime_mult": _f(row.get("regime_mult")),
                "cost_mult": _f(row.get("cost_mult")),
                "final_score": _f(row.get("final_score")),
                "market_tier": _i(row.get("market_tier"), 3),
                "leverage_proposal": _i(row.get("leverage_proposal"), 1),
                "funding": _f(row.get("funding")),
                "premium": _f(row.get("premium")),
                "impact_proxy": _f(row.get("impact_proxy")),
                "volume_band": str(row.get("volume_band") or "unknown"),
                "open_interest_band": str(row.get("open_interest_band") or "unknown"),
                "hard_reject_reason": row.get("hard_reject_reason"),
                "regime_summary": str(row.get("regime_summary") or ""),
                "recent_performance_tag": str(row.get("recent_performance_tag") or ""),
                "current_rank": idx,
            }
        )
    return payload, rank_by_symbol


def run_topk_advisory_review(
    *,
    candidates: list[Mapping[str, Any]],
    config: dict[str, Any],
    artifact_dir: str | None = None,
    client: FathomClient | None = None,
    engine: str = "acevault",
    trace_id: str | None = None,
    cycle_id: str | None = None,
    event_timestamp: str | None = None,
) -> dict[str, Any]:
    intelligence = config.get("intelligence") or {}
    topk_cfg = (intelligence.get("topk_review") or {}) if isinstance(intelligence, dict) else {}
    if not bool(intelligence.get("enabled", False)):
        return {"enabled": False, "reason": "intelligence.disabled"}
    if not bool(topk_cfg.get("enabled", False)):
        return {"enabled": False, "reason": "intelligence.topk_review.disabled"}
    if not bool(topk_cfg.get("advisory_only", True)):
        return {"enabled": False, "reason": "intelligence.topk_review.advisory_only_required"}

    top_k = max(1, int(topk_cfg.get("top_k", 5)))
    payload, rank_by_symbol = build_topk_candidate_payload(candidates, top_k=top_k)
    if not payload:
        return {"enabled": True, "status": "insufficient_data", "reason": "no_candidates"}

    allowed_symbols = set(rank_by_symbol.keys())
    deterministic_candidate_ranks = [
        {"symbol": item["symbol"], "rank": int(item["current_rank"])}
        for item in payload
    ]
    input_payload_hash = _sha(
        json.dumps(
            {"topk_candidates": payload},
            sort_keys=True,
            ensure_ascii=True,
        )
    )
    prompt = _build_prompt(payload)
    prompt_hash = _sha(prompt)
    warnings: list[str] = []
    status = "ok"
    raw = ""
    llm_review: TopKReviewResult

    cc = client or FathomClient.from_config(config)
    try:
        raw = cc.complete_json(system_prompt=_SYSTEM_PROMPT, user_prompt=prompt)
        llm_review = parse_topk_review_response(raw, allowed_symbols=allowed_symbols, top_k=len(payload))
    except Exception as err:
        status = "fallback"
        note = f"topk_review_failed:{cc.sanitize_error(err)}"
        warnings.append(note)
        logger.warning("RISK_TOPK_REVIEW_FAILED detail=%s", note)
        first_symbol = payload[0]["symbol"]
        llm_review = build_fallback_topk_review(note, symbol=first_symbol)

    out: dict[str, Any] = {
        "enabled": True,
        "engine": engine,
        "timestamp": event_timestamp or datetime.now(timezone.utc).isoformat(),
        "cycle_id": cycle_id,
        "trace_id": trace_id,
        "prompt_template_id": PROMPT_TEMPLATE_ID,
        "input_payload_hash": input_payload_hash,
        "deterministic_candidate_ranks": deterministic_candidate_ranks,
        "status": status,
        "top_k": len(payload),
        "llm_review_status": llm_review.review_status,
        "llm_top_choice": llm_review.top_choice.symbol,
        "llm_confidence": llm_review.overall_assessment.confidence,
        "llm_caution_flags": [f"{x.symbol}:{x.flag_type}" for x in llm_review.caution_flags],
        "review": llm_review.to_dict(),
        "trace": {
            "prompt_hash_sha256": prompt_hash,
            "symbols": sorted(allowed_symbols),
            "prompt_template_id": PROMPT_TEMPLATE_ID,
        },
        "warnings": warnings,
    }
    if raw:
        out["trace"]["raw_response_hash_sha256"] = _sha(raw)
    if bool(topk_cfg.get("write_artifacts", True)):
        out["artifacts"] = _write_artifacts(
            out,
            review=llm_review,
            config=config,
            artifact_dir=artifact_dir,
        )
    return out


def render_topk_review_markdown(result: TopKReviewResult) -> str:
    lines = [
        "# Top-K Advisory Review",
        "",
        "## Review status",
        f"- {result.review_status}",
        "",
        "## Top choice",
        f"- {result.top_choice.symbol} ({result.top_choice.confidence}): {result.top_choice.reason}",
        "",
        "## Ranking adjustments",
    ]
    if result.ranking_adjustments:
        for r in result.ranking_adjustments:
            lines.append(
                f"- {r.symbol}: current #{r.current_rank} -> suggested #{r.suggested_rank} "
                f"({r.confidence}) {r.reason}"
            )
    else:
        lines.append("- No meaningful re-rank suggested.")
    lines.extend(["", "## Caution flags"])
    if result.caution_flags:
        for f in result.caution_flags:
            lines.append(f"- {f.symbol} [{f.flag_type}] ({f.confidence}): {f.message}")
    else:
        lines.append("- None.")
    lines.extend(
        [
            "",
            "## Overall assessment",
            f"- {result.overall_assessment.summary}",
            f"- actionability: {result.overall_assessment.actionability}",
            f"- confidence: {result.overall_assessment.confidence}",
            "",
            "_Advisory only. Deterministic ranking remains source of truth._",
            "",
        ]
    )
    return "\n".join(lines)


def _build_prompt(payload: list[dict[str, Any]]) -> str:
    template = _load_prompt_template()
    body = json.dumps({"topk_candidates": payload}, sort_keys=True, indent=2, ensure_ascii=True)
    return template.replace("{{ candidates_json }}", body)


def _load_prompt_template() -> str:
    path = Path(__file__).resolve().parent / "prompt_templates" / "topk_review_prompt.md"
    return path.read_text(encoding="utf-8")


def _write_artifacts(
    full_payload: dict[str, Any],
    *,
    review: TopKReviewResult,
    config: dict[str, Any],
    artifact_dir: str | None,
) -> dict[str, str]:
    root = Path(
        artifact_dir
        or str(
            (((config.get("research") or {}).get("data_dir") or "data/research"))
            + "/topk_reviews"
        )
    )
    ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = root / ts
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "topk_review.json"
    md_path = out_dir / "topk_review.md"
    with json_path.open("w", encoding="utf-8") as f:
        json.dump(full_payload, f, indent=2, sort_keys=True, ensure_ascii=True)
        f.write("\n")
    md_path.write_text(render_topk_review_markdown(review), encoding="utf-8")
    return {"topk_review_json": str(json_path), "topk_review_md": str(md_path)}


def _sha(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _f(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _i(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
