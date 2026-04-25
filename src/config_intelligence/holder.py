"""Process-wide active config version metadata for trade stamping (main + hot-reload)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass
class ActiveConfigVersionHolder:
    """Updated after each successful ``register_active_version`` call."""

    version_id: str | None = None
    config_hash: str | None = None
    environment: str | None = None
    venue: str | None = None
    applied_at: datetime | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def update(
        self,
        *,
        version_id: str | None,
        config_hash: str | None,
        environment: str | None,
        venue: str | None,
        applied_at: datetime | None,
        **kwargs: Any,
    ) -> None:
        self.version_id = version_id
        self.config_hash = config_hash
        self.environment = environment
        self.venue = venue
        self.applied_at = applied_at
        if kwargs:
            self.extra.update(kwargs)


_holder = ActiveConfigVersionHolder()


def get_active_holder() -> ActiveConfigVersionHolder:
    return _holder


def reset_holder_for_tests() -> None:
    global _holder
    _holder = ActiveConfigVersionHolder()
