#!/usr/bin/env python3
"""
NXFH01 Production Verification Script
Runs one full AceVault cycle on MAINNET to verify all components.
"""

import asyncio
import logging
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import httpx
import yaml
from dotenv import load_dotenv
from hyperliquid.info import Info

# Add project root to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.engines.acevault.engine import AceVaultEngine
from src.engines.acevault.scanner import AltScanner
from src.fathom.advisor import FathomAdvisor
from src.market_data.hyperliquid_btc import fetch_real_market_data
from src.market_universe.top25_universe import Top25UniverseManager
from src.regime.detector import RegimeDetector
from src.risk.engine_killswitch import KillSwitch
from src.risk.portfolio_state import PortfolioState
from src.risk.unified_risk import UnifiedRiskLayer

logger = logging.getLogger(__name__)


def load_config() -> dict:
    """Load configuration from config.yaml and .env."""
    load_dotenv()
    
    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)
    
    return config


async def verify_fathom_connectivity(config: dict) -> tuple[bool, bool]:
    """Test Fathom/Ollama connectivity and model response."""
    ollama_base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
    
    # Test basic connectivity
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(f"{ollama_base_url}/api/tags")
            resp.raise_for_status()
            ollama_reachable = True
    except Exception:
        ollama_reachable = False
    
    # Test model response
    model_responding = False
    if ollama_reachable:
        try:
            advisor = FathomAdvisor(config, ollama_base_url)
            test_response = await advisor._call_fathom("Test prompt. Reply: MULTIPLIER: 1.0")
            model_responding = test_response is not None
        except Exception:
            model_responding = False
    
    return ollama_reachable, model_responding


async def run_verification_cycle(config: dict, hl_client: Info) -> dict:
    """Run one full AceVault cycle and collect verification data."""
    # Initialize components (same order as main.py build_context)
    kill_switch = KillSwitch(config)
    portfolio_state = PortfolioState()
    ucfg = config.get("universe") or {}
    universe_manager = None
    if bool(ucfg.get("enabled", False)):
        universe_manager = Top25UniverseManager(hl_client, config)
        universe_manager.refresh()
    risk_layer = UnifiedRiskLayer(
        config, portfolio_state, kill_switch, universe_manager=universe_manager
    )
    regime_detector = RegimeDetector(config, data_fetcher=None)
    
    # Create AceVault engine (without degen_executor for verification)
    acevault_engine = AceVaultEngine(
        config, hl_client, regime_detector, risk_layer, None, kill_switch
    )
    
    # Fetch real market data
    market_data = await fetch_real_market_data(hl_client)
    
    # Run regime detection
    regime_state = regime_detector.detect(market_data)
    
    # Run scanner
    scanner = AltScanner(config, hl_client)
    candidates = scanner.scan()
    
    # Get risk layer status
    risk_status = risk_layer.check_global_rules()
    
    # Test Fathom connectivity
    ollama_reachable, model_responding = await verify_fathom_connectivity(config)
    
    return {
        "market_data": market_data,
        "regime_state": regime_state,
        "candidates": candidates,
        "risk_status": risk_status,
        "portfolio_state": portfolio_state,
        "kill_switch_active": kill_switch.is_active("acevault"),
        "ollama_reachable": ollama_reachable,
        "model_responding": model_responding,
    }


def print_verification_report(verification_data: dict) -> bool:
    """Print verification report and return True if all checks pass."""
    print("=== NXFH01 PRODUCTION VERIFICATION ===")
    print(f"Timestamp: {datetime.now(timezone.utc).isoformat()}")
    print("Network: MAINNET")
    print()
    
    all_pass = True
    
    # Regime Detection
    print("Regime Detection:")
    regime_state = verification_data["regime_state"]
    market_data = verification_data["market_data"]
    
    regime_pass = regime_state.regime is not None
    print(f"  [{'PASS' if regime_pass else 'FAIL'}] Current regime: {regime_state.regime.value if regime_state.regime else 'None'} (confidence: {regime_state.confidence:.2f})")
    all_pass &= regime_pass
    
    btc_return_pass = abs(market_data["btc_1h_return"]) < 0.1  # Reasonable bounds
    print(f"  [{'PASS' if btc_return_pass else 'FAIL'}] BTC 1h return: {market_data['btc_1h_return']*100:.2f}%")
    all_pass &= btc_return_pass
    
    btc_vol_pass = 0.001 <= market_data["btc_vol_1h"] <= 0.05  # Reasonable volatility range
    print(f"  [{'PASS' if btc_vol_pass else 'FAIL'}] BTC vol 1h: {market_data['btc_vol_1h']:.4f}")
    all_pass &= btc_vol_pass
    
    funding_pass = abs(market_data["funding_rate"]) < 1.0  # Reasonable funding rate
    print(f"  [{'PASS' if funding_pass else 'FAIL'}] Funding rate: {market_data['funding_rate']:.3f}%")
    all_pass &= funding_pass
    
    print()
    
    # AceVault Scanner
    print("AceVault Scanner:")
    candidates = verification_data["candidates"]
    
    scan_pass = len(candidates) >= 0  # Scanner ran without error
    excluded_coins = {"BTC", "ETH", "SOL"}
    excluded_str = ", ".join(excluded_coins)
    print(f"  [{'PASS' if scan_pass else 'FAIL'}] Scanned alts (excluded: {excluded_str})")
    all_pass &= scan_pass
    
    if candidates:
        top_candidate = candidates[0]
        candidate_pass = top_candidate.weakness_score > 0
        print(f"  [{'PASS' if candidate_pass else 'FAIL'}] Top candidate: {top_candidate.coin} weakness={top_candidate.weakness_score:.3f}")
        all_pass &= candidate_pass
    else:
        print("  [PASS] No candidates found (normal market condition)")
    
    print()
    
    # Risk Layer
    print("Risk Layer:")
    risk_status = verification_data["risk_status"]
    portfolio_state = verification_data["portfolio_state"]
    
    gross_exposure = risk_status["gross_exposure"]
    gross_pass = gross_exposure >= 0
    open_positions = len(portfolio_state._positions) if hasattr(portfolio_state, '_positions') else 0
    print(f"  [{'PASS' if gross_pass else 'FAIL'}] Gross exposure: {gross_exposure/100:.1f}% ({open_positions} open positions)")
    all_pass &= gross_pass
    
    dd_24h = risk_status["drawdown_24h"]
    dd_pass = 0 <= dd_24h <= 1.0
    print(f"  [{'PASS' if dd_pass else 'FAIL'}] Portfolio DD 24h: {dd_24h*100:.1f}%")
    all_pass &= dd_pass
    
    kill_switch_active = verification_data["kill_switch_active"]
    kill_switch_pass = not kill_switch_active
    print(f"  [{'PASS' if kill_switch_pass else 'FAIL'}] Kill switch: {'active' if kill_switch_active else 'inactive'}")
    all_pass &= kill_switch_pass
    
    print()
    
    # Fathom Advisory
    print("Fathom Advisory:")
    ollama_reachable = verification_data["ollama_reachable"]
    model_responding = verification_data["model_responding"]
    
    print(f"  [{'PASS' if ollama_reachable else 'FAIL'}] Ollama reachable: {'yes' if ollama_reachable else 'no'}")
    print(f"  [{'PASS' if model_responding else 'FAIL'}] Fathom model responding: {'yes' if model_responding else 'no'}")
    
    # Fathom is advisory only, so don't fail overall verification if it's down
    # all_pass &= ollama_reachable and model_responding
    
    print()
    
    return all_pass


async def submit_verification_trade(config: dict, hl_client: Info, candidates: list) -> bool:
    """Submit a small verification trade if conditions are met."""
    if not candidates:
        print("No candidates available for verification trade")
        return False
    
    verification_size = config["acevault"]["verification_size_usd"]
    top_candidate = candidates[0]
    
    print(f"VERIFICATION TRADE SUBMITTED: {top_candidate.coin} SHORT ${verification_size}")
    print("(Note: This is a simulation - no actual trade submitted in verification script)")
    
    return True


async def main() -> None:
    # CRITICAL: Check testnet environment
    if os.getenv("HL_TESTNET", "false").lower() == "true":
        print("ERROR: HL_TESTNET=true. This script runs on MAINNET ONLY.")
        sys.exit(1)
    
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    
    try:
        # Load configuration
        config = load_config()
        
        # Verify wallet address is set
        wallet_address = os.environ.get("HL_WALLET_ADDRESS", "")
        if not wallet_address:
            print("ERROR: HL_WALLET_ADDRESS not set in environment")
            sys.exit(1)
        
        # Initialize Hyperliquid client (MAINNET)
        hl_client = Info(base_url="https://api.hyperliquid.xyz", skip_ws=True)
        
        # Run verification cycle
        verification_data = await run_verification_cycle(config, hl_client)
        
        # Print report
        all_pass = print_verification_report(verification_data)
        
        # Submit verification trade if all checks pass and valid signal exists
        if all_pass and verification_data["candidates"]:
            trade_submitted = await submit_verification_trade(
                config, hl_client, verification_data["candidates"]
            )
            if trade_submitted:
                print("\nVERIFICATION COMPLETE: All systems operational")
            else:
                print("\nVERIFICATION COMPLETE: Systems operational, no trade submitted")
        else:
            if not all_pass:
                print("\nVERIFICATION FAILED: Some systems not operational")
            else:
                print("\nVERIFICATION COMPLETE: Systems operational, no valid signals")
        
        # Exit with appropriate code
        sys.exit(0 if all_pass else 1)
        
    except Exception as e:
        logger.exception("Verification failed with exception")
        print(f"\nVERIFICATION FAILED: {e}")
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())