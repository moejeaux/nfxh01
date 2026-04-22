from typing import Any
import logging

logger = logging.getLogger(__name__)


class CostGuard:
    def __init__(self, config: dict, hl_client: Any) -> None:
        self.config = config
        self.hl_client = hl_client

    def should_allow_entry(self, coin: str, position_size_usd: float, side: str = "long") -> tuple[bool, dict]:
        snapshot = self._fetch_l2(coin)
        spread_bps = self._estimate_spread_bps_from_snapshot(snapshot)
        slippage_bps = self._estimate_slippage_bps_from_snapshot(snapshot, position_size_usd, side)
        ex = self.config["execution"]
        total_cost_bps = (
            spread_bps
            + slippage_bps
            + float(ex["entry_fee_bps"])
            + float(ex["exit_fee_bps"])
        )
        detail = {
            "spread_bps": float(spread_bps),
            "slippage_bps": float(slippage_bps),
            "total_cost_bps": float(total_cost_bps),
            "reason": "approved",
        }
        if spread_bps > float(ex["max_spread_bps"]):
            detail["reason"] = "spread_limit"
            logger.info(
                "RISK_COST_GUARD_REJECTED coin=%s reason=%s spread_bps=%.2f slippage_bps=%.2f total_cost_bps=%.2f",
                coin,
                detail["reason"],
                spread_bps,
                slippage_bps,
                total_cost_bps,
            )
            return False, detail
        if slippage_bps > float(ex["max_slippage_bps"]):
            detail["reason"] = "slippage_limit"
            logger.info(
                "RISK_COST_GUARD_REJECTED coin=%s reason=%s spread_bps=%.2f slippage_bps=%.2f total_cost_bps=%.2f",
                coin,
                detail["reason"],
                spread_bps,
                slippage_bps,
                total_cost_bps,
            )
            return False, detail
        if total_cost_bps > float(ex["max_total_round_trip_cost_bps"]):
            detail["reason"] = "total_cost_limit"
            logger.info(
                "RISK_COST_GUARD_REJECTED coin=%s reason=%s spread_bps=%.2f slippage_bps=%.2f total_cost_bps=%.2f",
                coin,
                detail["reason"],
                spread_bps,
                slippage_bps,
                total_cost_bps,
            )
            return False, detail
        logger.info(
            "RISK_COST_GUARD_APPROVED coin=%s spread_bps=%.2f slippage_bps=%.2f total_cost_bps=%.2f",
            coin,
            spread_bps,
            slippage_bps,
            total_cost_bps,
        )
        return True, detail

    def _fetch_l2(self, coin: str) -> dict | None:
        try:
            raw = self.hl_client.info.l2_snapshot(coin=coin)
        except Exception:
            return None
        if not isinstance(raw, dict):
            return None
        return raw

    def _estimate_spread_bps(self, coin: str) -> float:
        return self._estimate_spread_bps_from_snapshot(self._fetch_l2(coin))

    def _estimate_spread_bps_from_snapshot(self, snapshot: dict | None) -> float:
        ex = self.config["execution"]
        fallback = float(ex["fallback_spread_bps"])
        if snapshot is None:
            return fallback
        levels = snapshot.get("levels")
        if not isinstance(levels, list) or len(levels) < 2:
            return fallback
        bids, asks = levels[0], levels[1]
        if not isinstance(bids, list) or not isinstance(asks, list):
            return fallback
        if not bids or not asks:
            return fallback
        top_bid = bids[0]
        top_ask = asks[0]
        if not isinstance(top_bid, dict) or not isinstance(top_ask, dict):
            return fallback
        try:
            bid = float(top_bid.get("px", 0) or 0)
            ask = float(top_ask.get("px", 0) or 0)
        except (TypeError, ValueError):
            return fallback
        if bid <= 0.0 or ask <= 0.0 or ask < bid:
            return fallback
        mid = (bid + ask) / 2.0
        if mid <= 0.0:
            return fallback
        return ((ask - bid) / mid) * 10000.0

    def _estimate_slippage_bps(self, coin: str, position_size_usd: float, side: str = "long") -> float:
        return self._estimate_slippage_bps_from_snapshot(self._fetch_l2(coin), position_size_usd, side)

    def _estimate_slippage_bps_from_snapshot(
        self, snapshot: dict | None, position_size_usd: float, side: str = "long"
    ) -> float:
        ex = self.config["execution"]
        fallback = float(ex["fallback_slippage_bps"])
        if position_size_usd <= 0.0:
            return fallback
        if snapshot is None:
            return fallback
        levels = snapshot.get("levels")
        if not isinstance(levels, list) or len(levels) < 2:
            return fallback

        if side == "short":
            book_side = levels[0]  # bids — selling into bids
        else:
            book_side = levels[1]  # asks — buying into asks

        if not isinstance(book_side, list) or not book_side:
            return fallback
        top_level = book_side[0]
        if not isinstance(top_level, dict):
            return fallback
        try:
            best_px = float(top_level.get("px", 0) or 0)
        except (TypeError, ValueError):
            return fallback
        if best_px <= 0.0:
            return fallback
        total_usd = 0.0
        total_sz = 0.0
        remaining = float(position_size_usd)
        for level in book_side:
            if not isinstance(level, dict):
                return fallback
            try:
                px = float(level.get("px", 0) or 0)
                sz = float(level.get("sz", 0) or 0)
            except (TypeError, ValueError):
                return fallback
            if px <= 0.0 or sz <= 0.0:
                continue
            level_usd = px * sz
            if level_usd <= 0.0:
                continue
            if remaining <= 0.0:
                break
            if level_usd <= remaining:
                total_usd += level_usd
                total_sz += sz
                remaining -= level_usd
            else:
                frac = remaining / level_usd
                total_usd += remaining
                total_sz += sz * frac
                remaining = 0.0
                break
        if remaining > 0.0 or total_sz <= 0.0 or total_usd <= 0.0:
            return fallback
        vwap = total_usd / total_sz
        return abs((vwap - best_px) / best_px) * 10000.0

    def _estimate_total_round_trip_cost_bps(self, coin: str, position_size_usd: float, side: str = "long") -> float:
        snapshot = self._fetch_l2(coin)
        ex = self.config["execution"]
        return (
            self._estimate_spread_bps_from_snapshot(snapshot)
            + self._estimate_slippage_bps_from_snapshot(snapshot, position_size_usd, side)
            + float(ex["entry_fee_bps"])
            + float(ex["exit_fee_bps"])
        )
