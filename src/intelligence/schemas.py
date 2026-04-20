"""Structured schema + validation for weekly LLM summary artifacts."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

from src.retro.analysis_parse import try_parse_analysis_json

NON_AUTO_APPLY_NOTE = "Recommendations are advisory only and are not auto-applied."

_VERDICT_LABELS = {"improving", "stable", "degraded", "inconclusive"}
_CONFIDENCE = {"low", "medium", "high"}
_ENGINES = {"acevault", "growi_hf", "mc_recovery", "mixed"}
_CHANGE_TYPES = {"threshold", "weight", "leverage", "reject_rule", "watch_only"}
_DQ_SEVERITIES = {"info", "caution", "critical"}
_REVIEW_STATUS = {"ok", "inconclusive", "insufficient_data"}
_FLAG_TYPES = {"regime_mismatch", "execution_risk", "leverage_caution", "score_too_close", "data_quality"}
_ACTIONABILITY = {"clear", "cautious", "weak_signal"}


@dataclass(frozen=True)
class WeeklyVerdict:
    label: str
    reason: str


@dataclass(frozen=True)
class EvidenceItem:
    title: str
    evidence: str
    confidence: str


@dataclass(frozen=True)
class EngineObservation:
    engine: str
    observation: str
    evidence: str
    confidence: str


@dataclass(frozen=True)
class RecommendedChange:
    priority: int
    change_type: str
    target: str
    recommendation: str
    expected_benefit: str
    risk: str
    confidence: str


@dataclass(frozen=True)
class HoldSteadyItem:
    item: str
    reason: str


@dataclass(frozen=True)
class DataQualityNote:
    severity: str
    note: str


@dataclass(frozen=True)
class OperatorMemo:
    summary: str
    next_week_focus: str
    non_auto_apply_note: str


@dataclass(frozen=True)
class WeeklySummaryResult:
    weekly_verdict: WeeklyVerdict
    top_positive_changes: list[EvidenceItem]
    top_risks: list[EvidenceItem]
    engine_observations: list[EngineObservation]
    recommended_changes: list[RecommendedChange]
    things_not_to_change: list[HoldSteadyItem]
    data_quality_notes: list[DataQualityNote]
    operator_memo: OperatorMemo

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class TopChoice:
    symbol: str
    reason: str
    confidence: str


@dataclass(frozen=True)
class RankingAdjustment:
    symbol: str
    suggested_rank: int
    current_rank: int
    reason: str
    confidence: str


@dataclass(frozen=True)
class CautionFlag:
    symbol: str
    flag_type: str
    message: str
    confidence: str


@dataclass(frozen=True)
class OverallAssessment:
    summary: str
    actionability: str
    confidence: str


@dataclass(frozen=True)
class TopKReviewResult:
    review_status: str
    top_choice: TopChoice
    ranking_adjustments: list[RankingAdjustment]
    caution_flags: list[CautionFlag]
    overall_assessment: OverallAssessment

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_weekly_summary_response(raw: str, *, max_recommendations: int) -> WeeklySummaryResult:
    obj = try_parse_analysis_json(raw)
    if not isinstance(obj, dict):
        raise ValueError("LLM weekly summary is not valid JSON object")
    return parse_weekly_summary_object(obj, max_recommendations=max_recommendations)


def parse_weekly_summary_object(
    obj: dict[str, Any], *, max_recommendations: int
) -> WeeklySummaryResult:
    if max_recommendations < 1:
        raise ValueError("max_recommendations must be >= 1")
    weekly_verdict = _parse_weekly_verdict(_require_mapping(obj, "weekly_verdict"))
    top_positive = _parse_evidence_list(obj, "top_positive_changes")
    top_risks = _parse_evidence_list(obj, "top_risks")
    engine_observations = _parse_engine_observations(obj, "engine_observations")
    recommendations = _parse_recommended_changes(obj, "recommended_changes")
    if len(recommendations) > max_recommendations:
        raise ValueError(
            f"recommended_changes exceeds cap: {len(recommendations)} > {max_recommendations}"
        )
    hold_steady = _parse_hold_steady(obj, "things_not_to_change")
    if not hold_steady:
        raise ValueError("things_not_to_change must include at least one item")
    data_quality_notes = _parse_data_quality_notes(obj, "data_quality_notes")
    operator_memo = _parse_operator_memo(_require_mapping(obj, "operator_memo"))
    return WeeklySummaryResult(
        weekly_verdict=weekly_verdict,
        top_positive_changes=top_positive,
        top_risks=top_risks,
        engine_observations=engine_observations,
        recommended_changes=recommendations,
        things_not_to_change=hold_steady,
        data_quality_notes=data_quality_notes,
        operator_memo=operator_memo,
    )


def build_fallback_weekly_summary(reason: str) -> WeeklySummaryResult:
    note = reason.strip() or "Model output unavailable."
    return WeeklySummaryResult(
        weekly_verdict=WeeklyVerdict(label="inconclusive", reason=note),
        top_positive_changes=[],
        top_risks=[
            EvidenceItem(
                title="LLM summary unavailable",
                evidence=note,
                confidence="low",
            )
        ],
        engine_observations=[],
        recommended_changes=[],
        things_not_to_change=[
            HoldSteadyItem(
                item="Core risk controls",
                reason="Insufficient validated LLM output to support changes.",
            )
        ],
        data_quality_notes=[DataQualityNote(severity="caution", note=note)],
        operator_memo=OperatorMemo(
            summary="LLM summary unavailable; rely on deterministic weekly artifacts.",
            next_week_focus="Restore LLM availability and review baseline artifact quality.",
            non_auto_apply_note=NON_AUTO_APPLY_NOTE,
        ),
    )


def parse_topk_review_response(
    raw: str,
    *,
    allowed_symbols: set[str],
    top_k: int,
) -> TopKReviewResult:
    obj = try_parse_analysis_json(raw)
    if not isinstance(obj, dict):
        raise ValueError("LLM top-K review is not valid JSON object")
    return parse_topk_review_object(obj, allowed_symbols=allowed_symbols, top_k=top_k)


def parse_topk_review_object(
    obj: dict[str, Any],
    *,
    allowed_symbols: set[str],
    top_k: int,
) -> TopKReviewResult:
    if top_k < 1:
        raise ValueError("top_k must be >= 1")
    if not allowed_symbols:
        raise ValueError("allowed_symbols must not be empty")
    review_status = _require_str(obj, "review_status")
    _ensure_in("review_status", review_status, _REVIEW_STATUS)
    top_choice = _parse_top_choice(_require_mapping(obj, "top_choice"), allowed_symbols)
    ranking_adjustments = _parse_ranking_adjustments(
        _require_list(obj, "ranking_adjustments"),
        allowed_symbols,
        top_k,
    )
    caution_flags = _parse_caution_flags(_require_list(obj, "caution_flags"), allowed_symbols)
    overall_assessment = _parse_overall_assessment(_require_mapping(obj, "overall_assessment"))
    return TopKReviewResult(
        review_status=review_status,
        top_choice=top_choice,
        ranking_adjustments=ranking_adjustments,
        caution_flags=caution_flags,
        overall_assessment=overall_assessment,
    )


def build_fallback_topk_review(reason: str, *, symbol: str) -> TopKReviewResult:
    note = reason.strip() or "topk_review_unavailable"
    return TopKReviewResult(
        review_status="inconclusive",
        top_choice=TopChoice(
            symbol=symbol,
            reason=f"Fallback advisory only: {note}",
            confidence="low",
        ),
        ranking_adjustments=[],
        caution_flags=[
            CautionFlag(
                symbol=symbol,
                flag_type="data_quality",
                message=f"Model unavailable or invalid response: {note}",
                confidence="low",
            )
        ],
        overall_assessment=OverallAssessment(
            summary="Deterministic ranking remains source of truth. LLM review unavailable.",
            actionability="weak_signal",
            confidence="low",
        ),
    )


def _parse_weekly_verdict(obj: dict[str, Any]) -> WeeklyVerdict:
    label = _require_str(obj, "label")
    _ensure_in("weekly_verdict.label", label, _VERDICT_LABELS)
    return WeeklyVerdict(label=label, reason=_require_str(obj, "reason"))


def _parse_evidence_list(parent: dict[str, Any], key: str) -> list[EvidenceItem]:
    out: list[EvidenceItem] = []
    for idx, item in enumerate(_require_list(parent, key)):
        m = _as_mapping(item, f"{key}[{idx}]")
        conf = _require_str(m, "confidence")
        _ensure_in(f"{key}[{idx}].confidence", conf, _CONFIDENCE)
        out.append(
            EvidenceItem(
                title=_require_str(m, "title"),
                evidence=_require_str(m, "evidence"),
                confidence=conf,
            )
        )
    return out


def _parse_engine_observations(parent: dict[str, Any], key: str) -> list[EngineObservation]:
    out: list[EngineObservation] = []
    for idx, item in enumerate(_require_list(parent, key)):
        m = _as_mapping(item, f"{key}[{idx}]")
        engine = _require_str(m, "engine")
        _ensure_in(f"{key}[{idx}].engine", engine, _ENGINES)
        conf = _require_str(m, "confidence")
        _ensure_in(f"{key}[{idx}].confidence", conf, _CONFIDENCE)
        out.append(
            EngineObservation(
                engine=engine,
                observation=_require_str(m, "observation"),
                evidence=_require_str(m, "evidence"),
                confidence=conf,
            )
        )
    return out


def _parse_recommended_changes(parent: dict[str, Any], key: str) -> list[RecommendedChange]:
    out: list[RecommendedChange] = []
    for idx, item in enumerate(_require_list(parent, key)):
        m = _as_mapping(item, f"{key}[{idx}]")
        conf = _require_str(m, "confidence")
        _ensure_in(f"{key}[{idx}].confidence", conf, _CONFIDENCE)
        ctype = _require_str(m, "change_type")
        _ensure_in(f"{key}[{idx}].change_type", ctype, _CHANGE_TYPES)
        out.append(
            RecommendedChange(
                priority=_require_int(m, "priority"),
                change_type=ctype,
                target=_require_str(m, "target"),
                recommendation=_require_str(m, "recommendation"),
                expected_benefit=_require_str(m, "expected_benefit"),
                risk=_require_str(m, "risk"),
                confidence=conf,
            )
        )
    return out


def _parse_hold_steady(parent: dict[str, Any], key: str) -> list[HoldSteadyItem]:
    out: list[HoldSteadyItem] = []
    for idx, item in enumerate(_require_list(parent, key)):
        m = _as_mapping(item, f"{key}[{idx}]")
        out.append(HoldSteadyItem(item=_require_str(m, "item"), reason=_require_str(m, "reason")))
    return out


def _parse_data_quality_notes(parent: dict[str, Any], key: str) -> list[DataQualityNote]:
    out: list[DataQualityNote] = []
    for idx, item in enumerate(_require_list(parent, key)):
        m = _as_mapping(item, f"{key}[{idx}]")
        severity = _require_str(m, "severity")
        _ensure_in(f"{key}[{idx}].severity", severity, _DQ_SEVERITIES)
        out.append(DataQualityNote(severity=severity, note=_require_str(m, "note")))
    return out


def _parse_operator_memo(obj: dict[str, Any]) -> OperatorMemo:
    note = _require_str(obj, "non_auto_apply_note")
    if note != NON_AUTO_APPLY_NOTE:
        raise ValueError("operator_memo.non_auto_apply_note must match advisory-only policy text")
    return OperatorMemo(
        summary=_require_str(obj, "summary"),
        next_week_focus=_require_str(obj, "next_week_focus"),
        non_auto_apply_note=note,
    )


def _parse_top_choice(obj: dict[str, Any], allowed_symbols: set[str]) -> TopChoice:
    symbol = _require_str(obj, "symbol")
    _ensure_symbol_allowed("top_choice.symbol", symbol, allowed_symbols)
    conf = _require_str(obj, "confidence")
    _ensure_in("top_choice.confidence", conf, _CONFIDENCE)
    return TopChoice(
        symbol=symbol,
        reason=_require_str(obj, "reason"),
        confidence=conf,
    )


def _parse_ranking_adjustments(
    items: list[Any],
    allowed_symbols: set[str],
    top_k: int,
) -> list[RankingAdjustment]:
    out: list[RankingAdjustment] = []
    for idx, item in enumerate(items):
        m = _as_mapping(item, f"ranking_adjustments[{idx}]")
        symbol = _require_str(m, "symbol")
        _ensure_symbol_allowed(f"ranking_adjustments[{idx}].symbol", symbol, allowed_symbols)
        sug = _require_int(m, "suggested_rank")
        cur = _require_int(m, "current_rank")
        if sug < 1 or sug > top_k:
            raise ValueError(f"ranking_adjustments[{idx}].suggested_rank must be in [1,{top_k}]")
        if cur < 1 or cur > top_k:
            raise ValueError(f"ranking_adjustments[{idx}].current_rank must be in [1,{top_k}]")
        conf = _require_str(m, "confidence")
        _ensure_in(f"ranking_adjustments[{idx}].confidence", conf, _CONFIDENCE)
        out.append(
            RankingAdjustment(
                symbol=symbol,
                suggested_rank=sug,
                current_rank=cur,
                reason=_require_str(m, "reason"),
                confidence=conf,
            )
        )
    return out


def _parse_caution_flags(items: list[Any], allowed_symbols: set[str]) -> list[CautionFlag]:
    out: list[CautionFlag] = []
    for idx, item in enumerate(items):
        m = _as_mapping(item, f"caution_flags[{idx}]")
        symbol = _require_str(m, "symbol")
        _ensure_symbol_allowed(f"caution_flags[{idx}].symbol", symbol, allowed_symbols)
        ftype = _require_str(m, "flag_type")
        _ensure_in(f"caution_flags[{idx}].flag_type", ftype, _FLAG_TYPES)
        conf = _require_str(m, "confidence")
        _ensure_in(f"caution_flags[{idx}].confidence", conf, _CONFIDENCE)
        out.append(
            CautionFlag(
                symbol=symbol,
                flag_type=ftype,
                message=_require_str(m, "message"),
                confidence=conf,
            )
        )
    return out


def _parse_overall_assessment(obj: dict[str, Any]) -> OverallAssessment:
    conf = _require_str(obj, "confidence")
    _ensure_in("overall_assessment.confidence", conf, _CONFIDENCE)
    actionability = _require_str(obj, "actionability")
    _ensure_in("overall_assessment.actionability", actionability, _ACTIONABILITY)
    return OverallAssessment(
        summary=_require_str(obj, "summary"),
        actionability=actionability,
        confidence=conf,
    )


def _as_mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{path} must be an object")
    return value


def _require_mapping(parent: dict[str, Any], key: str) -> dict[str, Any]:
    if key not in parent:
        raise ValueError(f"missing required key: {key}")
    return _as_mapping(parent[key], key)


def _require_list(parent: dict[str, Any], key: str) -> list[Any]:
    if key not in parent:
        raise ValueError(f"missing required key: {key}")
    value = parent[key]
    if not isinstance(value, list):
        raise ValueError(f"{key} must be a list")
    return value


def _require_str(parent: dict[str, Any], key: str) -> str:
    if key not in parent:
        raise ValueError(f"missing required key: {key}")
    value = parent[key]
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{key} must be a non-empty string")
    return value.strip()


def _require_int(parent: dict[str, Any], key: str) -> int:
    if key not in parent:
        raise ValueError(f"missing required key: {key}")
    value = parent[key]
    if not isinstance(value, int):
        raise ValueError(f"{key} must be an integer")
    return value


def _ensure_in(path: str, value: str, allowed: set[str]) -> None:
    if value not in allowed:
        choices = ", ".join(sorted(allowed))
        raise ValueError(f"{path} invalid value '{value}'; allowed: {choices}")


def _ensure_symbol_allowed(path: str, symbol: str, allowed_symbols: set[str]) -> None:
    if symbol not in allowed_symbols:
        raise ValueError(f"{path} symbol '{symbol}' is not in deterministic top-K candidates")
