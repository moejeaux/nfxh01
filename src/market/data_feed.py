"""Read-only Hyperliquid market data feed.

Uses HL Info class ONLY — no Exchange class. All trade execution
goes through the ACP plugin (acp/degen_claw.py), not directly.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from hyperliquid.info import Info

from src.config import HL_API_URL, HL_WALLET_ADDRESS
from src.market.freshness import FreshnessTracker
from src.market.types import (
    AccountState,
    BookLevel,
    Candle,
    FundingRate,
    OrderBook,
    Position,
)

logger = logging.getLogger(__name__)


class MarketDataFeed:
    """Read-only wrapper around Hyperliquid Info API.

    Provides market data + account state. Does NOT place orders.
    """

    def __init__(
        self,
        freshness: FreshnessTracker,
        wallet_address: str | None = None,
        api_url: str | None = None,
    ):
        self._api_url = api_url or HL_API_URL
        self._wallet_address = (wallet_address or HL_WALLET_ADDRESS).lower()
        self._freshness = freshness

        self.info = Info(self._api_url, skip_ws=True)

        # Caches
        self._mids: dict[str, float] = {}
        self._candles: dict[str, list[Candle]] = {}
        self._funding: list[FundingRate] = []
        self._asset_ctxs: dict[str, dict] = {}
        self._oi_snapshots: dict[str, float] = {}
        self._oi_previous: dict[str, float] = {}
        self._mark_prices: dict[str, float] = {}
        self._oracle_prices: dict[str, float] = {}
        self._premium: dict[str, float] = {}
        self._predicted_funding: dict[str, float] = {}

        logger.info("MarketDataFeed initialized (read-only) for %s", self._wallet_address)

    # ── account state ────────────────────────────────────────────────────

    def get_account_state(self) -> AccountState:
        """Fetch current account balances and positions."""
        raw = self.info.user_state(self._wallet_address)

        positions = []
        for p in raw.get("assetPositions", []):
            pos = p.get("position", {})
            szi = float(pos.get("szi", 0))
            if szi == 0:
                continue
            side = "long" if szi > 0 else "short"
            abs_size = abs(szi)
            positions.append(Position(
                coin=pos.get("coin", ""),
                side=side,
                size=abs_size,
                entry_price=float(pos.get("entryPx", 0)),
                mark_price=float(pos.get("positionValue", 0)) / max(abs_size, 1e-9),
                unrealized_pnl=float(pos.get("unrealizedPnl", 0)),
                leverage=float(pos.get("leverage", {}).get("value", 1)),
                liquidation_price=float(lp) if (lp := pos.get("liquidationPx")) else None,
                margin_used=float(pos.get("marginUsed", 0)),
            ))

        cross_margin = raw.get("crossMarginSummary", {})
        perp_equity = float(cross_margin.get("accountValue", 0))
        perp_margin_used = float(cross_margin.get("totalMarginUsed", 0))
        perp_available = float(cross_margin.get("totalRawUsd", 0))

        # Also fetch spot USDC balance — it counts toward total deployable capital
        spot_usdc = 0.0
        try:
            spot_raw = self.info.spot_user_state(self._wallet_address)
            for bal in spot_raw.get("balances", []):
                if bal.get("coin", "").upper() in ("USDC", "USDC.E"):
                    spot_usdc = float(bal.get("total", 0))
                    break
        except Exception:
            pass  # spot endpoint may not be available for all accounts

        # Total equity = perp account value + spot USDC
        total_equity = perp_equity + spot_usdc
        total_available = perp_available + spot_usdc

        if spot_usdc > 0:
            logger.info(
                "Account: perp_equity=$%.2f, spot_usdc=$%.2f, total=$%.2f",
                perp_equity, spot_usdc, total_equity,
            )

        state = AccountState(
            equity=total_equity,
            available_margin=total_available,
            total_margin_used=perp_margin_used,
            positions=positions,
            timestamp=datetime.now(timezone.utc),
        )
        self._freshness.record("account_state")
        return state

    # ── prices ───────────────────────────────────────────────────────────

    def refresh_prices(self) -> dict[str, float]:
        """Fetch latest mid prices for all markets."""
        self._mids = {k: float(v) for k, v in self.info.all_mids().items()}
        self._freshness.record("prices")
        return self._mids

    def get_mid(self, coin: str) -> float | None:
        return self._mids.get(coin)

    def get_all_mids(self) -> dict[str, float]:
        return dict(self._mids)

    # ── candles ──────────────────────────────────────────────────────────

    def refresh_candles(self, coin: str, interval: str = "4h", limit: int = 100) -> list[Candle]:
        """Fetch OHLCV candles."""
        # candles_snapshot expects (coin, interval, startTime, endTime) in ms
        interval_ms = {
            "1m": 60_000, "5m": 300_000, "15m": 900_000,
            "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000,
        }
        period = interval_ms.get(interval, 14_400_000)
        end_time = int(datetime.now(timezone.utc).timestamp() * 1000)
        start_time = end_time - (period * limit)
        raw = self.info.candles_snapshot(coin, interval, start_time, end_time)
        candles = []
        for c in raw:
            candles.append(Candle(
                timestamp=datetime.fromtimestamp(c["t"] / 1000, tz=timezone.utc),
                open=float(c["o"]),
                high=float(c["h"]),
                low=float(c["l"]),
                close=float(c["c"]),
                volume=float(c["v"]),
            ))

        key = f"{coin}_{interval}"
        self._candles[key] = candles
        self._freshness.record(f"candles_{key}")
        if coin == "BTC":
            self._freshness.record("btc_candles")
        return candles

    def get_candles(self, coin: str, interval: str = "4h") -> list[Candle]:
        return self._candles.get(f"{coin}_{interval}", [])

    # ── funding rates ────────────────────────────────────────────────────

    def refresh_funding(self) -> list[FundingRate]:
        """Fetch current funding rates, OI, mark/oracle prices for all perp markets."""
        rates = []
        try:
            ctx_data = self.info.meta_and_asset_ctxs()
            if ctx_data and len(ctx_data) > 1:
                universe = ctx_data[0].get("universe", [])
                for i, asset_ctx in enumerate(ctx_data[1]):
                    if not isinstance(asset_ctx, dict):
                        continue
                    coin = universe[i]["name"] if i < len(universe) else ""
                    funding = float(asset_ctx.get("funding", 0))
                    rates.append(FundingRate(
                        coin=coin,
                        rate=funding,
                        predicted_rate=None,
                        timestamp=datetime.now(timezone.utc),
                    ))

                    # Cache full asset context for OI/exhaustion queries
                    self._asset_ctxs[coin] = asset_ctx

                    # Capture mark/oracle/premium
                    mark_px = float(asset_ctx.get("markPx", 0) or 0)
                    oracle_px = float(asset_ctx.get("oraclePx", 0) or 0)
                    if coin and mark_px > 0:
                        self._mark_prices[coin] = mark_px
                    if coin and oracle_px > 0:
                        self._oracle_prices[coin] = oracle_px
                        if mark_px > 0:
                            self._premium[coin] = ((mark_px - oracle_px) / oracle_px) * 100

                    oi = float(asset_ctx.get("openInterest", 0))
                    if oi > 0:
                        if coin in self._oi_snapshots:
                            self._oi_previous[coin] = self._oi_snapshots[coin]
                        self._oi_snapshots[coin] = oi

        except Exception as e:
            logger.error("Failed to fetch funding rates: %s", e)

        self._funding = rates
        self._freshness.record("funding")
        return rates

    def get_funding(self) -> list[FundingRate]:
        return list(self._funding)

    def get_funding_rate(self, coin: str) -> FundingRate | None:
        return next((r for r in self._funding if r.coin == coin), None)

    def get_extreme_funding(
        self, min_hourly_rate: float, allowed_coins: set[str] | None = None
    ) -> list[FundingRate]:
        """Return funding rates above threshold, sorted by magnitude."""
        candidates = []
        for r in self._funding:
            if allowed_coins and r.coin not in allowed_coins:
                continue
            if abs(r.hourly) >= min_hourly_rate:
                candidates.append(r)
        candidates.sort(key=lambda r: abs(r.hourly), reverse=True)
        return candidates

    # ── predicted funding ──────────────────────────────────────────────

    def refresh_predicted_funding(self) -> dict[str, float]:
        """Fetch predicted funding rates from Hyperliquid.

        Uses the predictedFundings endpoint. 8h rate — divide by 8 for hourly.
        Falls back to SDK method if available, otherwise uses requests directly.
        """
        try:
            predicted = self.info.predicted_fundings()
            if predicted:
                predicted_map: dict[str, float] = {}
                for entry in predicted:
                    if isinstance(entry, dict):
                        coin = entry.get("coin", "")
                        rate = float(entry.get("predictedFunding", 0))
                        if coin:
                            predicted_map[coin] = rate
                for fr in self._funding:
                    if fr.coin in predicted_map:
                        fr.predicted_rate = predicted_map[fr.coin]
                self._predicted_funding.update(predicted_map)
                self._freshness.record("predicted_funding")
                return dict(self._predicted_funding)
        except AttributeError:
            pass
        except Exception as e:
            logger.debug("SDK predicted funding failed: %s", e)

        try:
            import requests
            resp = requests.post(
                self._api_url,
                json={"type": "predictedFundings"},
                timeout=10,
            )
            if resp.status_code == 200:
                data = resp.json()
                for item in data:
                    if isinstance(item, list) and len(item) >= 2:
                        coin = item[0]
                        rates = item[1]
                        if isinstance(rates, list) and len(rates) > 0:
                            rate_8h = float(rates[0][1] if isinstance(rates[0], list) else rates[0])
                            self._predicted_funding[coin] = rate_8h / 8
                self._freshness.record("predicted_funding")
        except Exception as e:
            logger.debug("Predicted funding fetch failed: %s", e)
        return dict(self._predicted_funding)

    # ── OI + exhaustion context ───────────────────────────────────────

    _OI_STRONG_CONVICTION = 0.05
    _OI_WEAK_SIGNAL = -0.03

    def get_premium_pct(self, coin: str) -> float:
        """Return mark vs oracle premium as % for a coin."""
        return self._premium.get(coin, 0.0)

    def get_mark_price(self, coin: str) -> float | None:
        return self._mark_prices.get(coin)

    def get_oracle_price(self, coin: str) -> float | None:
        return self._oracle_prices.get(coin)

    def get_all_oracle_prices(self) -> dict[str, float]:
        return dict(self._oracle_prices)

    def get_predicted_funding_hourly(self, coin: str) -> float:
        return self._predicted_funding.get(coin, 0.0)

    def get_exhaustion_context(self, coin: str) -> dict:
        """Return funding + OI + premium context for exhaustion analysis."""
        fr = self.get_funding_rate(coin)
        ctx = self._asset_ctxs.get(coin, {})
        return {
            "funding_hourly": fr.hourly if fr else 0.0,
            "predicted_funding_hourly": (
                (fr.predicted_rate / 8) if fr and fr.predicted_rate else 0.0
            ),
            "mark_price": float(ctx.get("markPx", 0)),
            "oracle_price": float(ctx.get("oraclePx", 0)),
            "oi_current": self._oi_snapshots.get(coin, 0.0),
            "oi_previous": self._oi_previous.get(coin, 0.0),
        }

    def get_oi_change_pct(self, coin: str) -> float | None:
        """Return OI percent change from previous snapshot."""
        current = self._oi_snapshots.get(coin)
        previous = self._oi_previous.get(coin)
        if current is None or previous is None or previous <= 0:
            return None
        return (current - previous) / previous

    def get_oi_signal(self, coin: str) -> str:
        """Return OI conviction signal: strong, weak, neutral, or unknown."""
        change = self.get_oi_change_pct(coin)
        if change is None:
            return "unknown"
        if change >= self._OI_STRONG_CONVICTION:
            return "strong"
        if change <= self._OI_WEAK_SIGNAL:
            return "weak"
        return "neutral"

    def get_oi_summary(self, coins: list[str]) -> dict:
        """Return OI signal and change for each coin."""
        result = {}
        for coin in coins:
            chg = self.get_oi_change_pct(coin)
            result[coin] = {
                "oi_signal": self.get_oi_signal(coin),
                "oi_change_pct": round(chg * 100, 2) if chg is not None else None,
            }
        return result

    # ── order book ───────────────────────────────────────────────────────

    def get_l2_book(self, coin: str) -> OrderBook:
        """Fetch L2 order book snapshot."""
        raw = self.info.l2_snapshot(coin)
        levels = raw.get("levels", [[], []])
        book = OrderBook(
            coin=coin,
            bids=[BookLevel(price=float(b["px"]), size=float(b["sz"])) for b in levels[0]],
            asks=[BookLevel(price=float(a["px"]), size=float(a["sz"])) for a in levels[1]] if len(levels) > 1 else [],
            timestamp=datetime.now(timezone.utc),
        )
        self._freshness.record(f"orderbook_{coin}")
        return book

    # ── metadata ─────────────────────────────────────────────────────────

    def get_meta(self) -> dict:
        """Fetch exchange metadata (universe, asset info)."""
        return self.info.meta()

    # ── bulk refresh ─────────────────────────────────────────────────────

    def refresh_all(self, btc_candle_interval: str = "4h") -> None:
        """Refresh prices, funding rates, and BTC candles."""
        self.refresh_prices()
        self.refresh_funding()
        self.refresh_candles("BTC", btc_candle_interval)
