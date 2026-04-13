"""Normalized onchain feature model for perps and wallet watchlist entries."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class OnchainFeatures:
    """Normalized onchain intelligence snapshot for a single perp symbol.

    Produced by PerpsOnchainEnricher, consumed by strategies and risk layer.
    All numeric fields default to neutral values so strategies degrade gracefully
    when a provider is unavailable.
    """

    symbol: str
    asset: str
    as_of: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    # Smart money flow (GoldRush ERC20 transfers for tracked wallets)
    smart_money_netflow_usd: float = 0.0
    smart_money_buy_pressure: float = 0.0   # 0.0-1.0
    smart_money_sell_pressure: float = 0.0  # 0.0-1.0

    # Whale activity (GoldRush transaction counts)
    whale_inflow_count: int = 0
    whale_outflow_count: int = 0

    # Holder dynamics (GoldRush token holders snapshots)
    top_wallet_balance_delta_pct: float = 0.0
    accumulation_score: float = 0.0  # 0.0-1.0

    # Spot-perp context (GoldRush spot prices vs HL perp prices)
    spot_perp_basis_pct: float = 0.0    # (perp - spot) / spot as %
    spot_lead_lag_score: float = 0.0    # -1 to +1: +1 = spot leading up

    # Transfer activity
    transfer_count: int = 0
    large_tx_count: int = 0  # transfers > $50k USD

    # Anomaly / risk signals
    anomaly_score: float = 0.0       # 0.0-1.0
    bridge_flow_score: float = 0.0   # -1 to +1: +1 = net inflow to HyperEVM

    # Provider metadata
    goldrush_healthy: bool = True
    nansen_healthy: bool = True
    stale: bool = False

    def to_dict(self) -> dict:
        return {
            "symbol": self.symbol,
            "asset": self.asset,
            "as_of": self.as_of.isoformat(),
            "smart_money_netflow_usd": self.smart_money_netflow_usd,
            "smart_money_buy_pressure": round(self.smart_money_buy_pressure, 4),
            "smart_money_sell_pressure": round(self.smart_money_sell_pressure, 4),
            "whale_inflow_count": self.whale_inflow_count,
            "whale_outflow_count": self.whale_outflow_count,
            "top_wallet_balance_delta_pct": round(self.top_wallet_balance_delta_pct, 4),
            "accumulation_score": round(self.accumulation_score, 4),
            "spot_perp_basis_pct": round(self.spot_perp_basis_pct, 4),
            "spot_lead_lag_score": round(self.spot_lead_lag_score, 4),
            "transfer_count": self.transfer_count,
            "large_tx_count": self.large_tx_count,
            "anomaly_score": round(self.anomaly_score, 4),
            "bridge_flow_score": round(self.bridge_flow_score, 4),
            "goldrush_healthy": self.goldrush_healthy,
            "nansen_healthy": self.nansen_healthy,
            "stale": self.stale,
        }


@dataclass
class WalletWatchlistEntry:
    """A wallet tracked for onchain activity relevant to perp signals."""

    address: str
    source: str  # "nansen" | "manual" | "discovered"
    label: str = ""
    tags: list[str] = field(default_factory=list)
    is_smart_money: bool = False
    last_seen: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    track_chains: list[str] = field(default_factory=lambda: ["eth-mainnet"])


# Perp symbol → {chain: contract_address} for GoldRush queries.
# Assets without entries get OnchainFeatures with stale=True.
PERP_ASSET_MAP: dict[str, dict[str, str]] = {
    "BTC": {
        "eth-mainnet": "0x2260FAC5E5542a773Aa44fBCfeDf7C193bc2C599",  # WBTC
    },
    "ETH": {
        "eth-mainnet": "0xC02aaA39b223FE8D0A0e5C4F27eAD9083C756Cc2",  # WETH
        "arbitrum-mainnet": "0x82aF49447D8a07e3bd95BD0d56f35241523fBab1",
    },
    # SOL: GoldRush does not index wSOL (0xD31a59c8...) on eth-mainnet for price or holders.
    # Falls back to HL mark/oracle basis via _fallback_hl_basis. No entry here saves 2 requests/cycle.
    # "SOL": {},
    "HYPE": {
        "hyperevm-mainnet": "0x5555555555555555555555555555555555555555",  # WHYPE — price works, holders 501
    },
    "AVAX": {
        "eth-mainnet": "0x85f138bfee4ef8e540890cfb48f620571d67eda3",  # WAVAX (Wormhole) on ETH
    },
    "ARB": {
        "eth-mainnet": "0xB50721BCf8d664c30412Cfbc6cf7a15145234ad1",  # ARB governance token on ETH
    },
    "LINK": {
        "eth-mainnet": "0x514910771AF9Ca656af840dff83E8264EcF986CA",  # Chainlink ERC-20
    },
    # OP is native to OP Mainnet only — no ETH mainnet ERC-20, falls back to HL oracle basis
    # DOGE and POL have no meaningful ERC-20 representation on eth-mainnet — HL oracle basis only
}
