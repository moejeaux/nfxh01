"""Persistence for opportunity candidate and trade outcome calibration records."""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

from src.calibration.schema import CandidateRankRecord, TradeOutcomeRecord

logger = logging.getLogger(__name__)

_STORE_LOCK = threading.Lock()
_STORE_BY_ROOT: dict[str, "OpportunityOutcomeStore"] = {}


def _config_bool(section: dict[str, Any], key: str, default: bool) -> bool:
    return bool(section.get(key, default))


class OpportunityOutcomeStore:
    def __init__(self, config: dict[str, Any]) -> None:
        self._config = config
        self._research = config.get("research") or {}
        self._calibration = config.get("calibration") or {}
        data_dir = str(self._research.get("data_dir", "data/research")).strip()
        self._base_dir = Path(data_dir)
        self._candidate_path = self._base_dir / "opportunity_candidates.jsonl"
        self._trade_path = self._base_dir / "opportunity_trade_outcomes.jsonl"
        self._candidate_enabled = _config_bool(self._research, "save_candidate_logs", True)
        self._trade_enabled = _config_bool(self._research, "save_trade_outcomes", True)
        self._global_enabled = _config_bool(self._research, "enabled", False) or _config_bool(
            self._calibration, "enabled", False
        )
        self._io_lock = threading.Lock()

    @property
    def enabled(self) -> bool:
        return self._global_enabled

    @property
    def candidate_path(self) -> Path:
        return self._candidate_path

    @property
    def trade_path(self) -> Path:
        return self._trade_path

    def _ensure_parent_dir(self) -> None:
        self._base_dir.mkdir(parents=True, exist_ok=True)

    def _append_jsonl(self, path: Path, payload: dict[str, Any]) -> None:
        self._ensure_parent_dir()
        with self._io_lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(payload, ensure_ascii=True, default=str) + "\n")

    def record_candidate(self, record: CandidateRankRecord) -> None:
        if not self.enabled or not self._candidate_enabled:
            return
        try:
            self._append_jsonl(self._candidate_path, record.to_dict())
        except Exception as e:
            logger.warning("RISK_CALIBRATION_CANDIDATE_WRITE_FAIL error=%s", e)

    def record_trade_outcome(self, record: TradeOutcomeRecord) -> None:
        if not self.enabled or not self._trade_enabled:
            return
        try:
            self._append_jsonl(self._trade_path, record.to_dict())
        except Exception as e:
            logger.warning("RISK_CALIBRATION_OUTCOME_WRITE_FAIL error=%s", e)

    def _load_jsonl(self, path: Path) -> list[dict[str, Any]]:
        if not path.is_file():
            return []
        out: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(row, dict):
                    out.append(row)
        return out

    def load_candidates(self) -> list[dict[str, Any]]:
        return self._load_jsonl(self._candidate_path)

    def load_trade_outcomes(self) -> list[dict[str, Any]]:
        return self._load_jsonl(self._trade_path)


def get_outcome_store(config: dict[str, Any]) -> OpportunityOutcomeStore | None:
    research = config.get("research") or {}
    calibration = config.get("calibration") or {}
    enabled = bool(research.get("enabled", False) or calibration.get("enabled", False))
    if not enabled:
        return None
    root = str(research.get("data_dir", "data/research")).strip()
    with _STORE_LOCK:
        if root not in _STORE_BY_ROOT:
            _STORE_BY_ROOT[root] = OpportunityOutcomeStore(config)
        return _STORE_BY_ROOT[root]

