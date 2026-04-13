from __future__ import annotations

import pathlib


def test_src_excludes_mock_degen_executor_name():
    root = pathlib.Path(__file__).resolve().parents[1] / "src"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "MockDegenExecutor" not in text, path
