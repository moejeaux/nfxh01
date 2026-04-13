"""Normalized trade intent from external ingress (HTTP now; MCP later)."""

from __future__ import annotations

import logging
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger("nxfh02.signal")

EntryType = Literal["market", "limit"]


class SignalIntent(BaseModel):
    """Canonical payload after JSON validation (pre-executor)."""

    signal_id: str = Field(min_length=1, description="Idempotency key from sender")
    symbol: str = Field(min_length=1, description="Perp symbol e.g. BTC")
    side: Literal["long", "short"]
    thesis: str = Field(default="", description="Human-readable reason")
    confidence: float = Field(ge=0.0, le=1.0)
    entry_type: EntryType = "market"
    risk_pct: float | None = Field(
        default=None,
        ge=0.0,
        le=1.0,
        description="Fraction of equity; exclusive with size_hint_usd",
    )
    size_hint_usd: float | None = Field(
        default=None,
        ge=0.0,
        description="Absolute USD notional hint; exclusive with risk_pct",
    )
    stop_loss_pct: float = Field(ge=0.0001, le=0.5)
    take_profit_pct: float = Field(ge=0.0001, le=2.0)
    time_in_force: str | None = Field(
        default=None,
        description="ioc|gtc|fok (case-insensitive); validated in resolve_execution_constraints",
    )
    origin_metadata: dict[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _risk_xor_size(self) -> SignalIntent:
        has_r = self.risk_pct is not None
        has_s = self.size_hint_usd is not None
        if has_r == has_s:
            raise ValueError("Exactly one of risk_pct or size_hint_usd must be set")
        return self

    def resolve_execution_constraints(self) -> tuple[SignalIntent, list[str]]:
        """Apply TIF/entry rules. Returns (possibly adjusted intent, log lines)."""
        notes: list[str] = []
        tif_raw = (self.time_in_force or "").strip().lower()
        et = self.entry_type

        if et == "limit":
            raise ValueError("limit entry_type is not supported in v1 — use market only")

        if et == "market":
            if not tif_raw or tif_raw in ("ioc", "immediate_or_cancel"):
                return self, notes
            if tif_raw == "gtc":
                logger.warning(
                    "SIGNAL_TIF_DOWGRADED signal_id=%s: GTC with market is unsupported on HL perps "
                    "for this stack — treating as IOC-style market",
                    self.signal_id[:24],
                )
                notes.append("tif_downgraded_gtc_to_market_ioc")
                return self.model_copy(update={"time_in_force": "ioc"}), notes
            if tif_raw == "fok":
                logger.warning(
                    "SIGNAL_TIF_DOWGRADED signal_id=%s: FOK not distinct in v1 — treating as IOC",
                    self.signal_id[:24],
                )
                notes.append("tif_downgraded_fok_to_ioc")
                return self.model_copy(update={"time_in_force": "ioc"}), notes
            raise ValueError(
                f"unsupported time_in_force={self.time_in_force!r} for entry_type=market"
            )

        raise ValueError(f"unsupported entry_type={et!r}")
