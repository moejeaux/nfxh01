"""Perps Onchain Enricher — produces normalized OnchainFeatures per perp symbol.

Sits between raw providers (GoldRush, Nansen) and the perps pipeline.
Strategies consume features through MarketSnapshot.onchain, never calling
providers directly. Runs synchronously from the background data refresh thread
using asyncio.run() for GoldRush async calls.
"""

from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timezone
from typing import TYPE_CHECKING, Any

from src.enrichment.models import OnchainFeatures, PERP_ASSET_MAP
from src.feature_flags import (
    PERPS_BRIDGE_MONITOR_ENABLED,
    PERPS_ONCHAIN_HOLDER_TRACKING_ENABLED,
    PERPS_ONCHAIN_SPOT_CONTEXT_ENABLED,
    PERPS_ONCHAIN_TRANSFER_TRACKING_ENABLED,
    PERPS_ONCHAIN_TX_ANALYSIS_ENABLED,
    PERPS_ONCHAIN_WALLET_TRACKING_ENABLED,
)

if TYPE_CHECKING:
    from src.adapters.goldrush.client import GoldRushClient
    from src.config import PerpsOnchainConfig
    from src.enrichment.bridge_monitor import BridgeMonitor
    from src.enrichment.store import PerpsEnrichmentStore
    from src.enrichment.wallet_watchlist import SmartMoneyWatchlist

logger = logging.getLogger(__name__)


class PerpsOnchainEnricher:
    """Produces OnchainFeatures snapshots for all tracked perp symbols."""

    def __init__(
        self,
        goldrush: GoldRushClient,
        watchlist: SmartMoneyWatchlist,
        bridge_monitor: BridgeMonitor,
        store: PerpsEnrichmentStore,
        config: PerpsOnchainConfig,
        nansen_client: Any = None,
    ):
        self._goldrush = goldrush
        self._watchlist = watchlist
        self._bridge = bridge_monitor
        self._store = store
        self._config = config
        self._nansen = nansen_client
        self._features: dict[str, OnchainFeatures] = {}
        self._request_count: int = 0
        self._loop: asyncio.AbstractEventLoop | None = None

    # ── Public interface (called from sync background thread) ────────────

    def refresh_sync(
        self, symbols: list[str], current_mids: dict[str, float],
        oracle_prices: dict[str, float] | None = None,
    ) -> None:
        """Synchronous entry point — runs the async enrichment loop."""
        try:
            if self._loop is None or self._loop.is_closed():
                self._loop = asyncio.new_event_loop()
            self._loop.run_until_complete(
                self._refresh(symbols, current_mids, oracle_prices or {}),
            )
        except Exception as e:
            logger.error("Perps enrichment cycle failed: %s", e)

    def get_all_features(self) -> dict[str, OnchainFeatures]:
        return dict(self._features)

    def get_features(self, symbol: str) -> OnchainFeatures | None:
        return self._features.get(symbol)

    # ── Core async enrichment loop ───────────────────────────────────────

    async def _refresh(
        self, symbols: list[str], current_mids: dict[str, float],
        oracle_prices: dict[str, float] | None = None,
    ) -> None:
        self._request_count = 0
        budget = self._config.max_goldrush_requests_per_cycle

        if self._watchlist.needs_refresh():
            self._watchlist.refresh_from_nansen(self._nansen)

        logger.info("Perps enrichment cycle starting for %d symbols (budget=%d)", len(symbols), budget)

        bridge_score = 0.0
        if PERPS_BRIDGE_MONITOR_ENABLED:
            bridge_score = await self._bridge.refresh()
            self._request_count += 1

        for symbol in symbols:
            if self._request_count >= budget:
                logger.debug("GoldRush budget exhausted (%d/%d), skipping remaining", self._request_count, budget)
                break

            asset_map = PERP_ASSET_MAP.get(symbol, {})
            if not asset_map:
                feat = OnchainFeatures(
                    symbol=symbol, asset="", stale=True,
                    goldrush_healthy=self._goldrush.status()["healthy"],
                    nansen_healthy=self._nansen is not None,
                )
                if oracle_prices:
                    self._fallback_hl_basis(feat, current_mids, oracle_prices)
                self._features[symbol] = feat
                logger.info(
                    "Enriched %s: STALE (no asset map) basis=%.3f%%",
                    symbol, feat.spot_perp_basis_pct,
                )
                continue

            features = OnchainFeatures(symbol=symbol, asset=symbol)

            if PERPS_ONCHAIN_TRANSFER_TRACKING_ENABLED:
                await self._enrich_transfers(features, asset_map)

            if PERPS_ONCHAIN_SPOT_CONTEXT_ENABLED:
                await self._enrich_spot_context(features, asset_map, current_mids)
                if features.spot_perp_basis_pct == 0.0 and oracle_prices:
                    self._fallback_hl_basis(features, current_mids, oracle_prices)

            if PERPS_ONCHAIN_TX_ANALYSIS_ENABLED:
                await self._enrich_whale_activity(features)

            if PERPS_ONCHAIN_HOLDER_TRACKING_ENABLED:
                await self._enrich_holders(features, asset_map)

            if PERPS_ONCHAIN_WALLET_TRACKING_ENABLED:
                await self._enrich_wallet_balances(features, asset_map)

            features.bridge_flow_score = bridge_score
            features.anomaly_score = self._compute_anomaly(features)
            features.goldrush_healthy = self._goldrush.status()["healthy"]
            features.nansen_healthy = self._nansen is not None

            self._features[symbol] = features
            self._store.save_snapshot(features)

            logger.info(
                "Enriched %s: flow=$%.0f acc=%.2f basis=%.3f%% whale=%d/%d txs=%d large=%d anom=%.2f (reqs=%d)",
                symbol, features.smart_money_netflow_usd,
                features.accumulation_score, features.spot_perp_basis_pct,
                features.whale_inflow_count, features.whale_outflow_count,
                features.transfer_count, features.large_tx_count,
                features.anomaly_score, self._request_count,
            )

            if features.anomaly_score >= self._config.anomaly_block_threshold:
                self._store.save_anomaly(
                    symbol, features.anomaly_score,
                    anomaly_type="composite_high",
                    details=features.to_dict(),
                )

    # ── Per-capability enrichment methods ────────────────────────────────

    async def _enrich_transfers(
        self, features: OnchainFeatures, asset_map: dict[str, str],
    ) -> None:
        total_in = 0.0
        total_out = 0.0
        tx_count = 0
        large_count = 0

        tracked = self._watchlist.get_tracked()
        for wallet in tracked[:10]:
            for chain_name, token_addr in asset_map.items():
                if self._request_count >= self._config.max_goldrush_requests_per_cycle:
                    break
                try:
                    data = await self._goldrush.get_erc20_transfers(
                        wallet.address, page_size=20, chain=chain_name,
                        contract_address=token_addr,
                    )
                    self._request_count += 1
                    for item in data.get("items", []):
                        transfers = item.get("transfers", [])
                        for tx in transfers:
                            contract = (tx.get("contract_address") or "").lower()
                            if contract != token_addr.lower():
                                continue
                            delta = float(tx.get("delta", 0))
                            usd_val = abs(delta) * float(tx.get("quote_rate", 0) or 0)
                            tx_count += 1
                            if usd_val > 50_000:
                                large_count += 1
                            if tx.get("transfer_type") == "IN":
                                total_in += usd_val
                            else:
                                total_out += usd_val
                except Exception as e:
                    logger.warning("Transfer fetch failed for %s on %s: %s", wallet.address[:8], chain_name, e)

        total = total_in + total_out
        features.smart_money_netflow_usd = total_in - total_out
        features.smart_money_buy_pressure = total_in / total if total > 0 else 0.0
        features.smart_money_sell_pressure = total_out / total if total > 0 else 0.0
        features.transfer_count = tx_count
        features.large_tx_count = large_count

    async def _enrich_spot_context(
        self, features: OnchainFeatures, asset_map: dict[str, str],
        current_mids: dict[str, float],
    ) -> None:
        for chain_name, token_addr in asset_map.items():
            if self._request_count >= self._config.max_goldrush_requests_per_cycle:
                break
            try:
                prices = await self._goldrush.get_token_prices(
                    token_addr, chain=chain_name,
                )
                self._request_count += 1
                if not prices.items:
                    logger.info("GoldRush price: %s/%s (%s) → no items", features.symbol, chain_name, token_addr[:10])
                    continue
                logger.info("GoldRush price: %s/%s → %d items, latest=$%s", features.symbol, chain_name, len(prices.items), prices.items[-1].price)
                spot = prices.items[-1].price
                if spot and spot > 0:
                    perp = current_mids.get(features.symbol, spot)
                    features.spot_perp_basis_pct = ((perp - spot) / spot) * 100

                    if len(prices.items) >= 2:
                        prev_spot = prices.items[-2].price
                        if prev_spot and prev_spot > 0:
                            spot_return = (spot - prev_spot) / prev_spot
                            perp_return = 0.0
                            features.spot_lead_lag_score = max(-1.0, min(1.0, spot_return * 10))
                    break
            except Exception as e:
                logger.warning("Spot price fetch failed for %s on %s: %s", features.symbol, chain_name, e)

    @staticmethod
    def _fallback_hl_basis(
        features: OnchainFeatures,
        current_mids: dict[str, float],
        oracle_prices: dict[str, float],
    ) -> None:
        """Use HL mark vs oracle as a proxy for spot–perp basis when GoldRush has no price data."""
        mark = current_mids.get(features.symbol)
        oracle = oracle_prices.get(features.symbol)
        if mark and oracle and oracle > 0:
            features.spot_perp_basis_pct = ((mark - oracle) / oracle) * 100

    async def _enrich_whale_activity(self, features: OnchainFeatures) -> None:
        whales = self._watchlist.get_whales()
        inflow = 0
        outflow = 0
        for wallet in whales[:5]:
            if self._request_count >= self._config.max_goldrush_requests_per_cycle:
                break
            chain = wallet.track_chains[0] if wallet.track_chains else "eth-mainnet"
            try:
                txs = await self._goldrush.get_transactions(
                    wallet.address, page_size=10, chain=chain,
                )
                self._request_count += 1
                for tx in txs.items:
                    if hasattr(tx, "to_address") and tx.to_address:
                        to_lower = tx.to_address.lower()
                        from_lower = (tx.from_address or "").lower() if hasattr(tx, "from_address") else ""
                        if from_lower == wallet.address.lower():
                            outflow += 1
                        elif to_lower == wallet.address.lower():
                            inflow += 1
            except Exception as e:
                logger.warning("Whale tx fetch failed for %s on %s: %s", wallet.address[:8], chain, e)

        features.whale_inflow_count = inflow
        features.whale_outflow_count = outflow

    _HOLDER_SUPPORTED_CHAINS = {"eth-mainnet"}  # hyperevm-mainnet returns 501 for token_holders_v2

    async def _enrich_holders(
        self, features: OnchainFeatures, asset_map: dict[str, str],
    ) -> None:
        for chain_name, token_addr in asset_map.items():
            if chain_name not in self._HOLDER_SUPPORTED_CHAINS:
                continue
            if self._request_count >= self._config.max_goldrush_requests_per_cycle:
                break
            try:
                holders = await self._goldrush.get_token_holders_at_block(
                    token_addr, chain=chain_name,
                )
                self._request_count += 1
                if not holders.items:
                    continue

                top_balance = sum(
                    float(h.balance or 0) for h in holders.items[:10]
                )
                total_balance = sum(
                    float(h.balance or 0) for h in holders.items
                )

                if total_balance > 0:
                    concentration = top_balance / total_balance
                    features.accumulation_score = max(0.0, min(1.0, 1.0 - concentration))
                    logger.info(
                        "GoldRush holders: %s/%s → %d holders, top10=%.1f%%, acc=%.2f",
                        features.symbol, chain_name, len(holders.items),
                        concentration * 100, features.accumulation_score,
                    )
                break
            except Exception as e:
                logger.warning("Holder fetch failed for %s on %s: %s", features.symbol, chain_name, e)

    async def _enrich_wallet_balances(
        self, features: OnchainFeatures, asset_map: dict[str, str],
    ) -> None:
        tracked = self._watchlist.get_tracked()
        total_delta = 0.0
        total_value = 0.0

        for wallet in tracked[:5]:
            for chain_name, token_addr in asset_map.items():
                if self._request_count >= self._config.max_goldrush_requests_per_cycle:
                    break
                try:
                    balances = await self._goldrush.get_token_balances(
                        wallet.address, chain=chain_name,
                    )
                    self._request_count += 1
                    for b in balances.items:
                        addr = getattr(b, "contract_address", "") or ""
                        if addr.lower() == token_addr.lower():
                            usd = getattr(b, "usd_value", 0) or 0
                            total_value += usd
                except Exception as e:
                    logger.warning("Balance fetch failed for %s on %s: %s", wallet.address[:8], chain_name, e)

        if total_value > 0:
            features.top_wallet_balance_delta_pct = total_delta

    # ── Anomaly detection ────────────────────────────────────────────────

    def _compute_anomaly(self, f: OnchainFeatures) -> float:
        score = 0.0
        if abs(f.smart_money_netflow_usd) > 1_000_000:
            score += 0.3
        if f.large_tx_count > 10:
            score += 0.2
        if abs(f.bridge_flow_score) > 0.7:
            score += 0.2
        if abs(f.spot_perp_basis_pct) > 2.0:
            score += 0.15
        if f.whale_outflow_count > 5:
            score += 0.15
        return min(1.0, score)

    # ── Status ───────────────────────────────────────────────────────────

    def status(self) -> dict:
        stale_count = sum(1 for f in self._features.values() if f.stale)
        return {
            "symbols_tracked": len(self._features),
            "stale_count": stale_count,
            "request_count_last_cycle": self._request_count,
            "watchlist": self._watchlist.status(),
            "bridge": self._bridge.status(),
            "goldrush_healthy": self._goldrush.status()["healthy"],
        }
