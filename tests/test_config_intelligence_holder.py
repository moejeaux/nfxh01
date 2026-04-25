"""Unit tests: active version holder."""

from __future__ import annotations

from datetime import datetime, timezone

from src.config_intelligence.holder import get_active_holder, reset_holder_for_tests


def test_holder_update_roundtrip() -> None:
    reset_holder_for_tests()
    h = get_active_holder()
    now = datetime.now(timezone.utc)
    h.update(
        version_id="abc",
        config_hash="deadbeef",
        environment="live",
        venue="hyperliquid",
        applied_at=now,
    )
    assert h.version_id == "abc"
    assert h.config_hash == "deadbeef"
