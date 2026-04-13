from __future__ import annotations

from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal


@dataclass(frozen=True)
class AceVaultStop:
    entry_px: Decimal
    stop_px: Decimal
    distance_pct: Decimal

    @classmethod
    def from_entry(
        cls,
        entry_px: Decimal,
        is_long: bool,
        stop_loss_distance_pct: Decimal,
    ) -> AceVaultStop:
        d = stop_loss_distance_pct / Decimal("100")
        q = Decimal("0.0000001")
        if is_long:
            stop_px = (entry_px * (Decimal("1") - d)).quantize(q, rounding=ROUND_HALF_UP)
        else:
            stop_px = (entry_px * (Decimal("1") + d)).quantize(q, rounding=ROUND_HALF_UP)
        return cls(entry_px=entry_px, stop_px=stop_px, distance_pct=stop_loss_distance_pct)
