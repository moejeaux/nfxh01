"""Portfolio-level BTC market context model (read-only for consumers)."""

from __future__ import annotations

from datetime import datetime
from enum import Enum

from pydantic import BaseModel, ConfigDict, Field


class BTCRegime(str, Enum):
    TRENDING_UP = "trending_up"
    TRENDING_DOWN = "trending_down"
    RANGE = "range"
    HIGH_VOL = "high_volatility"
    POST_IMPULSE = "post_impulse"


class BTCDominanceState(str, Enum):
    RISING = "rising"
    FALLING = "falling"
    NEUTRAL = "neutral"


class BTCRiskMode(str, Enum):
    GREEN = "green"
    YELLOW = "yellow"
    RED = "red"


class BTCAlignment(str, Enum):
    ALIGNED = "aligned"
    NEUTRAL = "neutral"
    CONFLICT = "conflict"


class BtcDenyReason(str, Enum):
    NONE = "none"
    BTC_TREND_CONFLICT = "btc_trend_conflict"
    BTC_SHOCK = "btc_shock"
    BTC_VOL_TOO_HIGH = "btc_vol_too_high"
    BTC_POST_IMPULSE_EXTENSION = "btc_post_impulse_extension"
    PORTFOLIO_BTC_BETA_CAP = "portfolio_btc_beta_cap"
    OTHER = "other"


def compute_btc_alignment(side: str, regime: BTCRegime, risk_mode: BTCRiskMode) -> BTCAlignment:
    """Direction vs BTC regime; NEUTRAL in range/high-vol or when risk is ambiguous."""
    s = (side or "").lower()
    if regime in (BTCRegime.RANGE, BTCRegime.HIGH_VOL):
        return BTCAlignment.NEUTRAL
    if regime == BTCRegime.POST_IMPULSE:
        return BTCAlignment.NEUTRAL
    benign = risk_mode in (BTCRiskMode.GREEN, BTCRiskMode.YELLOW)
    if not benign:
        return BTCAlignment.NEUTRAL
    if regime == BTCRegime.TRENDING_UP:
        if s == "long":
            return BTCAlignment.ALIGNED
        if s == "short":
            return BTCAlignment.CONFLICT
    if regime == BTCRegime.TRENDING_DOWN:
        if s == "short":
            return BTCAlignment.ALIGNED
        if s == "long":
            return BTCAlignment.CONFLICT
    return BTCAlignment.NEUTRAL


class BTCMarketContext(BaseModel):
    model_config = ConfigDict(frozen=True)

    regime: BTCRegime
    trend_score: float = Field(ge=-1.0, le=1.0)
    volatility_score: float = Field(ge=0.0, le=1.0)
    impulse_score: float = Field(ge=0.0, le=1.0)
    extension_score: float = Field(ge=0.0, le=1.0)
    dominance_state: BTCDominanceState
    risk_mode: BTCRiskMode
    shock_state: bool
    updated_at: datetime
    bundle_error: str | None = None
    primary_regime_lane: str | None = Field(
        default=None,
        description="BTCPrimaryRegime value from lane detector for analytics",
    )
