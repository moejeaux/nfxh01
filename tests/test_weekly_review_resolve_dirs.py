from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.research.weekly_review import resolve_weekly_review_dirs


def test_resolve_weekly_review_dirs_from_config(tmp_path: Path) -> None:
    cfg = {
        "research": {
            "weekly_review_archive_dir": str(tmp_path / "a"),
            "weekly_review_output_dir": str(tmp_path / "o"),
        }
    }
    arch, out = resolve_weekly_review_dirs(cfg, None, None)
    assert arch == str(tmp_path / "a")
    assert out == str(tmp_path / "o")


def test_resolve_weekly_review_dirs_cli_overrides(tmp_path: Path) -> None:
    cfg = {
        "research": {
            "weekly_review_archive_dir": str(tmp_path / "ignored_a"),
            "weekly_review_output_dir": str(tmp_path / "ignored_o"),
        }
    }
    arch, out = resolve_weekly_review_dirs(cfg, str(tmp_path / "cli_a"), str(tmp_path / "cli_o"))
    assert arch == str(tmp_path / "cli_a")
    assert out == str(tmp_path / "cli_o")


def test_resolve_weekly_review_dirs_missing_raises(tmp_path: Path) -> None:
    cfg: dict = {"research": {}}
    with pytest.raises(ValueError, match="archive_dir missing"):
        resolve_weekly_review_dirs(cfg, None, str(tmp_path / "o"))
    with pytest.raises(ValueError, match="output_dir missing"):
        resolve_weekly_review_dirs(cfg, str(tmp_path / "a"), None)


def test_resolve_weekly_review_dirs_non_mapping_research(tmp_path: Path) -> None:
    cfg = {"research": None, "x": 1}
    with pytest.raises(ValueError, match="archive_dir missing"):
        resolve_weekly_review_dirs(cfg, None, None)


def test_resolve_weekly_review_dirs_strips_whitespace(tmp_path: Path) -> None:
    cfg = {
        "research": {
            "weekly_review_archive_dir": f"  {tmp_path / 'a'}  ",
            "weekly_review_output_dir": f" {tmp_path / 'o'} ",
        }
    }
    arch, out = resolve_weekly_review_dirs(cfg, "  ", "  ")
    assert arch == str(tmp_path / "a")
    assert out == str(tmp_path / "o")
