"""Mutable holder for the latest BTC market context (injected, not a global singleton)."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from src.market.btc_context import BTCMarketContext


@dataclass
class BTCMarketContextHolder:
    """Updated once per orchestrator tick; readers must treat snapshot as read-only."""

    _snapshot: object | None = None
    _tick_at: datetime | None = None
    _seq: int = field(default=0, repr=False)
    _last_log_digest: str | None = field(default=None, repr=False)

    @property
    def snapshot(self) -> BTCMarketContext | None:
        return self._snapshot  # type: ignore[return-value]

    @property
    def tick_at(self) -> datetime | None:
        return self._tick_at

    @property
    def seq(self) -> int:
        return self._seq

    @property
    def last_log_digest(self) -> str | None:
        return self._last_log_digest

    def set_log_digest(self, digest: str | None) -> None:
        self._last_log_digest = digest

    def set_context(self, ctx: BTCMarketContext | None, *, tick_at: datetime | None = None) -> None:
        self._snapshot = ctx
        self._tick_at = tick_at or datetime.now(timezone.utc)
        self._seq += 1
