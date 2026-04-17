"""Tests for retrospective JSON extraction from model output."""

from __future__ import annotations

from src.retro.analysis_parse import try_parse_analysis_json


def test_plain_object():
    assert try_parse_analysis_json('{"a": 1}') == {"a": 1}


def test_markdown_json_fence():
    raw = """Here is the result:
```json
{"schema_version": 1, "diagnosis": "x"}
```
"""
    out = try_parse_analysis_json(raw)
    assert out == {"schema_version": 1, "diagnosis": "x"}


def test_prefix_before_object():
    raw = 'Here:\n{"schema_version": 1, "diagnosis": "d"}'
    assert try_parse_analysis_json(raw) == {"schema_version": 1, "diagnosis": "d"}


def test_nested_braces_in_string():
    raw = r'{"diagnosis": "use {brace} here", "x": 1}'
    out = try_parse_analysis_json(raw)
    assert out["diagnosis"] == "use {brace} here"
    assert out["x"] == 1
