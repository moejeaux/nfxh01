"""Append live Hyperliquid ``metaAndAssetCtxs`` lines to the Phase 1 weekly-review archive."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from src.research.walk_forward_validation import fetch_meta_snapshot_archive_object
from src.research.weekly_review import _load_config


def _hl_info_url(cfg: dict[str, Any]) -> str:
    hl = cfg.get("hyperliquid_api")
    if not isinstance(hl, dict):
        raise ValueError("hyperliquid_api block missing in config")
    base = str(hl.get("api_base_url") or "").strip().rstrip("/")
    if not base:
        raise ValueError("hyperliquid_api.api_base_url missing")
    return f"{base}/info"


def _archive_root(cfg: dict[str, Any], archive_dir_cli: str | None) -> Path:
    research = cfg.get("research")
    if not isinstance(research, dict):
        research = {}
    raw = (archive_dir_cli or "").strip() or str(research.get("weekly_review_archive_dir") or "").strip()
    if not raw:
        raise ValueError("set research.weekly_review_archive_dir or pass --archive-dir")
    return Path(raw)


def _timeout_seconds(cfg: dict[str, Any]) -> float:
    research = cfg.get("research")
    if not isinstance(research, dict):
        research = {}
    return float(research.get("weekly_review_info_timeout_seconds", 30))


def _snap_filename(cfg: dict[str, Any]) -> str:
    research = cfg.get("research")
    if not isinstance(research, dict):
        research = {}
    name = str(research.get("weekly_review_snap_filename") or "snap.jsonl").strip()
    return name or "snap.jsonl"


def append_hl_meta_snapshot(
    *,
    config_path: str | None,
    archive_dir: str | None,
) -> Path:
    cfg = _load_config(config_path)
    root = _archive_root(cfg, archive_dir)
    root.mkdir(parents=True, exist_ok=True)
    dest = root / _snap_filename(cfg)
    url = _hl_info_url(cfg)
    timeout = _timeout_seconds(cfg)
    line = fetch_meta_snapshot_archive_object(api_url=url, timeout=timeout)
    with dest.open("a", encoding="utf-8") as f:
        f.write(json.dumps(line, separators=(",", ":"), default=str) + "\n")
    return dest


def main() -> None:
    ap = argparse.ArgumentParser(description="Append one HL metaAndAssetCtxs JSON line to the Phase 1 archive.")
    ap.add_argument("--config", default=None, help="Path to config.yaml (default: repo discovery)")
    ap.add_argument("--archive-dir", default=None, help="Override research.weekly_review_archive_dir")
    args = ap.parse_args()
    dest = append_hl_meta_snapshot(config_path=args.config, archive_dir=args.archive_dir)
    print(f"RESEARCH_HL_META_SNAPSHOT_APPENDED path={dest}", flush=True)


if __name__ == "__main__":
    main()
