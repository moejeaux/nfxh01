"""Unit tests: config canonicalization and redaction."""

from __future__ import annotations

from src.config_intelligence.hashing import fingerprint_merged_config
from src.config_intelligence.normalize import canonicalize_for_hash, strip_sensitive


def test_canonicalize_sorts_dict_keys() -> None:
    c = canonicalize_for_hash({"z": 1, "a": {"m": 2, "b": 3}})
    assert list(c.keys()) == ["a", "z"]
    assert list(c["a"].keys()) == ["b", "m"]


def test_strip_sensitive_redacts_secret_like_keys() -> None:
    raw = {"api_secret": "x", "nested": {"password": "p"}, "ok": 1}
    s = strip_sensitive(raw)
    assert s["api_secret"] == "<redacted>"
    assert s["nested"]["password"] == "<redacted>"
    assert s["ok"] == 1


def test_fingerprint_stable_under_key_reorder() -> None:
    a, h1 = fingerprint_merged_config({"b": 1, "a": 2})
    _, h2 = fingerprint_merged_config({"a": 2, "b": 1})
    assert h1 == h2
    assert isinstance(a, dict)
