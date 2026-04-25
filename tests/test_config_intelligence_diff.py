"""Unit tests: semantic diff and bundles."""

from __future__ import annotations

from src.config_intelligence.diff import classify_path, diff_versions


def test_diff_emits_leaf_paths() -> None:
    old = {"risk": {"max": 1.0}}
    new = {"risk": {"max": 1.1}}
    ev = diff_versions(old, new)
    paths = {e["path"] for e in ev if e.get("change_kind") == "leaf"}
    assert "risk.max" in paths


def test_classify_path_prefix_rules() -> None:
    rules = [{"prefix": "acevault.cycle", "category": "policy"}]
    assert classify_path("acevault.cycle_interval_seconds", rules) == "policy"


def test_semantic_bundle_when_all_prefixes_match() -> None:
    old = {"a": {"x": 1}, "b": {"y": 2}}
    new = {"a": {"x": 2}, "b": {"y": 3}}
    bundles = [
        {
            "bundle_id": "test_bundle",
            "path_prefixes": ["a.x", "b.y"],
            "category": "policy",
            "tags": ["t1"],
        }
    ]
    ev = diff_versions(old, new, bundle_rules=bundles)
    kinds = [e["change_kind"] for e in ev]
    assert "semantic_bundle" in kinds
