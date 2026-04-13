"""Nansen Pro API client for Hyperliquid smart money tracking.

Verified working endpoints:
  POST /api/v1/tgm/perp-pnl-leaderboard  (5 credits) — top traders per coin with positions
  POST /api/v1/profiler/perp-positions     (1 credit)  — all positions for a wallet
  POST /api/v1/profiler/perp-trades        (1 credit)  — recent trades for a wallet

Auth: apiKey header.
Credit budget: ~40 credits per full refresh (8 coins × 5 credits).
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_BASE = "https://api.nansen.ai/api/v1"

# Cache duration — don't re-query within this window
LEADERBOARD_CACHE_MINUTES = 60  # refresh top traders hourly
POSITION_CACHE_MINUTES = 30


class NansenTraderSignal:
    """Processed signal from a single top trader's position."""

    def __init__(
        self,
        address: str,
        coin: str,
        direction: str,  # "long", "short", "flat"
        position_value_usd: float,
        pnl_usd: float,
        roi_pct: float,
        num_trades: int,
    ):
        self.address = address
        self.coin = coin
        self.direction = direction
        self.position_value_usd = position_value_usd
        self.pnl_usd = pnl_usd
        self.roi_pct = roi_pct
        self.num_trades = num_trades

    def __repr__(self) -> str:
        return (
            f"Trader({self.address[:8]}.. {self.direction} {self.coin} "
            f"${self.position_value_usd:,.0f} PnL=${self.pnl_usd:,.0f})"
        )


class CoinConsensus:
    """Aggregated smart money consensus for a single coin."""

    def __init__(self, coin: str):
        self.coin = coin
        self.long_count = 0
        self.short_count = 0
        self.flat_count = 0
        self.long_value = 0.0
        self.short_value = 0.0
        self.top_traders: list[NansenTraderSignal] = []

    @property
    def total_traders(self) -> int:
        return self.long_count + self.short_count + self.flat_count

    @property
    def net_direction(self) -> str:
        if self.long_count > self.short_count:
            return "long"
        elif self.short_count > self.long_count:
            return "short"
        return "neutral"

    @property
    def consensus_strength(self) -> float:
        """0.0 to 1.0 — how strong the directional agreement is."""
        total = self.long_count + self.short_count
        if total == 0:
            return 0.0
        majority = max(self.long_count, self.short_count)
        return majority / total

    @property
    def confidence_modifier(self) -> float:
        """Confidence multiplier for signal enrichment.

        Strong consensus in same direction → boost (1.05-1.15)
        Strong consensus in opposite direction → reduce (0.85-0.95)
        Weak/no consensus → neutral (1.0)
        """
        strength = self.consensus_strength
        if strength >= 0.7:
            return 1.05 + (strength - 0.7) * 0.33  # 1.05 to 1.15
        elif strength <= 0.3:
            return 0.85 + strength * 0.33  # 0.85 to 0.95
        return 1.0

    def to_dict(self) -> dict:
        return {
            "coin": self.coin,
            "long_count": self.long_count,
            "short_count": self.short_count,
            "flat_count": self.flat_count,
            "net_direction": self.net_direction,
            "consensus_strength": round(self.consensus_strength, 2),
            "confidence_modifier": round(self.confidence_modifier, 3),
            "long_value_usd": round(self.long_value),
            "short_value_usd": round(self.short_value),
            "total_traders": self.total_traders,
        }


class NansenClient:
    """Credit-efficient Nansen Pro API client.

    Primary strategy: use perp-pnl-leaderboard per coin to get
    top traders AND their current positions in one call.
    This avoids expensive per-wallet position lookups.

    Credit cost per full refresh: 8 coins × 5 credits = 40 credits
    At hourly refresh: 960 credits/day
    At 2-hour refresh: 480 credits/day
    """

    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = httpx.Client(timeout=30)

        # Cache
        self._consensus: dict[str, CoinConsensus] = {}
        self._last_refresh: datetime | None = None
        self._tracked_wallets: set[str] = set()
        self._credits_used = 0

        logger.info("NansenClient initialized (Pro endpoints)")

    def _post(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        """Make authenticated POST request."""
        resp = self._client.post(
            f"{_BASE}{path}",
            headers={
                "apiKey": self._api_key,
                "Content-Type": "application/json",
            },
            json=body,
        )
        if resp.status_code == 200:
            self._credits_used += 5 if "leaderboard" in path else 1
            return resp.json()

        logger.warning(
            "Nansen %s failed (%d): %s",
            path, resp.status_code, resp.text[:200],
        )
        return {}

    def _is_cache_fresh(self) -> bool:
        if self._last_refresh is None:
            return False
        age = (datetime.now(timezone.utc) - self._last_refresh).total_seconds()
        return age < LEADERBOARD_CACHE_MINUTES * 60

    # ── Core: Leaderboard-based discovery ────────────────────────────────

    def refresh_coin_leaders(
        self,
        coin: str,
        days: int = 7,
        limit: int = 10,
    ) -> CoinConsensus:
        """Fetch top traders for a specific coin and build consensus.

        Uses perp-pnl-leaderboard which returns position data inline.
        5 credits per call.
        """
        now = datetime.now(timezone.utc)
        from_date = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        to_date = now.strftime("%Y-%m-%d")

        data = self._post("/tgm/perp-pnl-leaderboard", {
            "token_symbol": coin,
            "date": {"from": from_date, "to": to_date},
            "pagination": {"page": 1, "per_page": limit},
        })

        consensus = CoinConsensus(coin)
        traders = data.get("data", [])

        for t in traders:
            address = t.get("trader_address", "")
            holding = float(t.get("holding_amount", 0))
            value = float(t.get("position_value_usd", 0))
            pnl = float(t.get("pnl_usd_total", 0) or t.get("pnl_usd_realised", 0))
            roi = float(t.get("roi_percent_total", 0) or 0)
            trades = int(t.get("nof_trades", 0))

            if holding > 0:
                direction = "long"
                consensus.long_count += 1
                consensus.long_value += abs(value)
            elif holding < 0:
                direction = "short"
                consensus.short_count += 1
                consensus.short_value += abs(value)
            else:
                direction = "flat"
                consensus.flat_count += 1

            signal = NansenTraderSignal(
                address=address,
                coin=coin,
                direction=direction,
                position_value_usd=abs(value),
                pnl_usd=pnl,
                roi_pct=roi,
                num_trades=trades,
            )
            consensus.top_traders.append(signal)

            # Track profitable addresses for future cross-coin analysis
            if pnl > 0:
                self._tracked_wallets.add(address)

        logger.info(
            "Nansen %s: %d traders | %dL/%dS/%dF | consensus=%s (%.0f%% strength)",
            coin, len(traders),
            consensus.long_count, consensus.short_count, consensus.flat_count,
            consensus.net_direction, consensus.consensus_strength * 100,
        )

        return consensus

    def refresh_all_coins(
        self,
        coins: list[str],
        days: int = 7,
        limit: int = 10,
    ) -> dict[str, CoinConsensus]:
        """Refresh leaderboard for all coins. ~5 credits per coin.

        Args:
            coins: list of coin symbols to query
            days: lookback period for PnL ranking
            limit: number of top traders per coin
        """
        if self._is_cache_fresh():
            logger.debug("Nansen cache fresh — skipping refresh")
            return self._consensus

        logger.info(
            "Nansen: refreshing %d coins (est. %d credits)",
            len(coins), len(coins) * 5,
        )

        for coin in coins:
            try:
                consensus = self.refresh_coin_leaders(coin, days, limit)
                self._consensus[coin] = consensus
            except Exception as e:
                logger.warning("Nansen: failed to refresh %s: %s", coin, e)

        self._last_refresh = datetime.now(timezone.utc)
        logger.info(
            "Nansen refresh complete: %d coins, %d total credits used",
            len(self._consensus), self._credits_used,
        )
        return self._consensus

    # ── Wallet-level detail (1 credit each) ──────────────────────────────

    def get_positions(self, address: str) -> list[dict[str, Any]]:
        """Fetch current perp positions for a wallet. 1 credit."""
        data = self._post("/profiler/perp-positions", {"address": address})
        return data.get("data", {}).get("asset_positions", [])

    def get_recent_trades(
        self, address: str, hours: int = 24,
    ) -> list[dict[str, Any]]:
        """Fetch recent perp trades for a wallet. 1 credit."""
        now = datetime.now(timezone.utc)
        data = self._post("/profiler/perp-trades", {
            "address": address,
            "date": {
                "from": (now - timedelta(hours=hours)).strftime("%Y-%m-%d"),
                "to": now.strftime("%Y-%m-%d"),
            },
        })
        return data.get("data", [])

    # ── Public query methods ─────────────────────────────────────────────

    def get_consensus(self, coin: str) -> CoinConsensus | None:
        """Get cached consensus for a coin."""
        return self._consensus.get(coin)

    def get_all_consensus(self) -> dict[str, CoinConsensus]:
        """Get all cached consensus data."""
        return dict(self._consensus)

    def get_tracked_wallets(self) -> list[str]:
        """Return addresses of profitable traders discovered so far."""
        return list(self._tracked_wallets)

    def discover_top_traders(
        self, days: int = 7, min_pnl: int = 0, limit: int = 10,
    ) -> list[str]:
        """Legacy compatibility — discovers via BTC leaderboard."""
        consensus = self.refresh_coin_leaders("BTC", days, limit)
        return [
            t.address for t in consensus.top_traders
            if t.pnl_usd >= min_pnl
        ]

    # ── Aggregated signals for SmartMoneyConfirmation ────────────────────

    def get_smart_money_signals(self) -> dict[str, Any]:
        """Build signals from cached consensus data.

        Compatible with existing SmartMoneyConfirmation interface.
        """
        consensus_data = {}
        new_trades: list[dict[str, Any]] = []

        for coin, cons in self._consensus.items():
            consensus_data[coin] = {
                "long_count": cons.long_count,
                "short_count": cons.short_count,
                "net_direction": cons.net_direction,
                "total_value": cons.long_value + cons.short_value,
                "consensus_strength": cons.consensus_strength,
                "confidence_modifier": cons.confidence_modifier,
            }

            # Include top trader details as "new trades"
            for t in cons.top_traders[:3]:
                if t.direction != "flat":
                    new_trades.append({
                        "trader": t.address[:10],
                        "coin": t.coin,
                        "action": "holding",
                        "side": t.direction,
                        "value_usd": t.position_value_usd,
                    })

        return {
            "consensus": consensus_data,
            "new_trades": new_trades,
            "tracked_count": len(self._tracked_wallets),
            "credits_used": self._credits_used,
            "last_refresh": (
                self._last_refresh.isoformat() if self._last_refresh else None
            ),
        }

    def status(self) -> dict:
        """Health and usage stats."""
        return {
            "connected": bool(self._api_key),
            "coins_tracked": list(self._consensus.keys()),
            "wallets_discovered": len(self._tracked_wallets),
            "credits_used": self._credits_used,
            "cache_fresh": self._is_cache_fresh(),
            "last_refresh": (
                self._last_refresh.strftime("%H:%M UTC")
                if self._last_refresh else "never"
            ),
        }