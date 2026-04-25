"""SHA-256 fingerprint of canonical config."""

from __future__ import annotations

import hashlib
import json
from typing import Any

from src.config_intelligence.normalize import canonicalize_for_hash, strip_sensitive


def fingerprint_sha256_from_canonical(canonical: dict[str, Any]) -> str:
    payload = json.dumps(canonical, separators=(",", ":"), sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def fingerprint_merged_config(merged_root: dict[str, Any]) -> tuple[dict[str, Any], str]:
    """Return (canonical_dict, sha256_hex) for a merged runtime config root."""
    redacted = strip_sensitive(merged_root)
    canonical = canonicalize_for_hash(redacted)
    if not isinstance(canonical, dict):
        canonical = {"_root": canonical}
    assert isinstance(canonical, dict)
    h = fingerprint_sha256_from_canonical(canonical)
    return canonical, h
