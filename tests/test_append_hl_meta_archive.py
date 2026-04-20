from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

from src.research.hl_meta_archive_append import append_hl_meta_snapshot


def _minimal_cfg(archive: Path) -> dict:
    return {
        "hyperliquid_api": {"api_base_url": "https://api.hyperliquid.test"},
        "research": {
            "weekly_review_archive_dir": str(archive),
            "weekly_review_snap_filename": "snap.jsonl",
            "weekly_review_info_timeout_seconds": 5,
        },
    }


def test_append_hl_meta_snapshot_writes_line(tmp_path: Path) -> None:
    arch = tmp_path / "hl_meta_archive"
    arch.mkdir()
    cfg_path = tmp_path / "cfg.json"
    cfg_path.write_text(json.dumps(_minimal_cfg(arch)), encoding="utf-8")
    fake = {"timestamp": "2026-04-10T12:00:00+00:00", "metaAndAssetCtxs": [[], []], "_test": True}
    with patch(
        "src.research.hl_meta_archive_append.fetch_meta_snapshot_archive_object",
        return_value=fake,
    ):
        dest = append_hl_meta_snapshot(config_path=str(cfg_path), archive_dir=None)
    assert dest == arch / "snap.jsonl"
    lines = dest.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 1
    assert json.loads(lines[0]) == fake


def test_append_hl_meta_archive_dir_cli_override(tmp_path: Path) -> None:
    arch_cfg = tmp_path / "from_config"
    arch_cli = tmp_path / "from_cli"
    arch_cli.mkdir()
    cfg_path = tmp_path / "cfg.json"
    base = _minimal_cfg(arch_cfg)
    cfg_path.write_text(json.dumps(base), encoding="utf-8")
    fake = {"timestamp": "2026-04-10T12:00:00+00:00", "metaAndAssetCtxs": [[], []]}
    with patch(
        "src.research.hl_meta_archive_append.fetch_meta_snapshot_archive_object",
        return_value=fake,
    ):
        dest = append_hl_meta_snapshot(config_path=str(cfg_path), archive_dir=str(arch_cli))
    assert dest.parent == arch_cli
