"""Liquidation cascade risk model and holder (read-only for consumers)."""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

logger = logging.getLogger(__name__)


class CascadeLevel(str, Enum):
    """Advisory cascade risk tier (logged, never blocks trades directly)."""
    NONE = "none"
    LOW = "low"
    ELEVATED = "elevated"
    HIGH = "high"
    CRITICAL = "critical"


class CascadeRisk(BaseModel):
    """Immutable snapshot of liquidation-cascade risk for a single tick."""
    model_config = ConfigDict(frozen=True)

    risk_score: float = Field(ge=0.0, le=1.0)
    level: CascadeLevel
    oi_delta_pct: float = Field(
        description="OI % change over lookback; negative = liquidation unwind",
    )
    funding_abs: float = Field(
        ge=0.0,
        description="Absolute funding rate; extreme values signal crowded positioning",
    )
    premium_abs: float = Field(
        ge=0.0,
        description="Absolute mark-oracle premium; divergence = stress",
    )
    oi_at_cap_count: int = Field(
        ge=0,
        description="Number of assets at their OI ceiling",
    )
    book_thinning_score: float = Field(
        ge=0.0, le=1.0,
        description="Order-book depth drop vs recent avg; 1.0 = vacuum",
    )
    updated_at: datetime
    error: str | None = None


SAFE_DEFAULT = CascadeRisk(
    risk_score=0.0,
    level=CascadeLevel.NONE,
    oi_delta_pct=0.0,
    funding_abs=0.0,
    premium_abs=0.0,
    oi_at_cap_count=0,
    book_thinning_score=0.0,
    updated_at=datetime.now(timezone.utc),
    error=None,
)


@dataclass
class CascadeRiskHolder:
    """Updated once per orchestrator tick; readers treat snapshot as read-only."""

    _snapshot: CascadeRisk | None = None
    _tick_at: datetime | None = None
    _seq: int = field(default=0, repr=False)

    @property
    def snapshot(self) -> CascadeRisk | None:
        return self._snapshot

    @property
    def tick_at(self) -> datetime | None:
        return self._tick_at

    @property
    def seq(self) -> int:
        return self._seq

    def set_risk(self, risk: CascadeRisk | None, *, tick_at: datetime | None = None) -> None:
        self._snapshot = risk
        self._tick_at = tick_at or datetime.now(timezone.utc)
        self._seq += 1
