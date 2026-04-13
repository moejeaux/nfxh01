"""NXFH01 main entry point — async loop driving AceVault engine cycles."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.acp.degen_claw import DegenClawAcp
from src.engines.acevault.degen_adapter import DegenExecutorAdapter
from src.engines.acevault.engine import AceVaultEngine
from src.regime.detector import RegimeDetector
from src.risk.engine_killswitch import KillSwitch
from src.risk.portfolio_state import PortfolioState
from src.risk.unified_risk import UnifiedRiskLayer

logger = logging.getLogger(__name__)

VERSION = "1.0.0"


def build_context() -> dict:
    load_dotenv()

    config_path = Path(__file__).resolve().parents[1] / "config.yaml"
    with config_path.open(encoding="utf-8") as f:
        config = yaml.safe_load(f)

    kill_switch = KillSwitch(config)
    portfolio_state = PortfolioState()
    risk_layer = UnifiedRiskLayer(config, portfolio_state, kill_switch)

    wallet_address = os.environ.get("HL_WALLET_ADDRESS", "")
    if not wallet_address:
        raise RuntimeError("NXFH01_FATAL HL_WALLET_ADDRESS not set in environment")

    from hyperliquid.info import Info

    hl_client = Info(base_url="https://api.hyperliquid.xyz", skip_ws=True)

    regime_detector = RegimeDetector(config, data_fetcher=None)

    acp = DegenClawAcp()
    degen_executor = DegenExecutorAdapter(acp)

    acevault_engine = AceVaultEngine(
        config, hl_client, regime_detector, risk_layer, degen_executor, kill_switch
    )

    return {
        "config": config,
        "kill_switch": kill_switch,
        "portfolio_state": portfolio_state,
        "risk_layer": risk_layer,
        "hl_client": hl_client,
        "regime_detector": regime_detector,
        "acevault_engine": acevault_engine,
    }


def _log_startup_sequence(ctx: dict) -> None:
    config = ctx["config"]
    regime_detector = ctx["regime_detector"]
    acevault_engine = ctx["acevault_engine"]

    risk_cfg = config.get("risk", {})
    max_dd = risk_cfg.get("max_portfolio_drawdown_24h", 0.05)
    max_exposure = risk_cfg.get("max_gross_multiplier", 3.0)

    logger.info("NXFH01_STARTING version=%s", VERSION)

    logger.info(
        "RISK_LAYER_INITIALIZED max_dd=%.0f%% max_exposure=%.0f%%",
        max_dd * 100,
        max_exposure * 100,
    )

    initial_market_data = {
        "btc_1h_return": 0.0,
        "btc_4h_return": 0.0,
        "btc_vol_1h": 0.004,
    }
    regime_state = regime_detector.detect(market_data=initial_market_data)
    logger.info(
        "REGIME_DETECTED regime=%s confidence=%.2f",
        regime_state.regime.value,
        regime_state.confidence,
    )

    weight = acevault_engine._get_regime_weight(regime_state.regime)
    logger.info(
        "ACEVAULT_ENGINE_INITIALIZED regime=%s weight=%.2f",
        regime_state.regime.value,
        weight,
    )

    cycle_interval = config.get("acevault", {}).get("cycle_interval_seconds", 30)
    cycles_per_minute = 60.0 / cycle_interval if cycle_interval > 0 else 0.0
    logger.info("NXFH01_READY cycles_per_minute=%.1f", cycles_per_minute)


async def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    ctx = build_context()
    _log_startup_sequence(ctx)

    config = ctx["config"]
    acevault_engine = ctx["acevault_engine"]
    cycle_interval = config.get("acevault", {}).get("cycle_interval_seconds", 30)

    shutdown_event = asyncio.Event()

    def _handle_sigterm() -> None:
        logger.info("NXFH01_SHUTDOWN_INITIATED")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            loop.add_signal_handler(sig, _handle_sigterm)
    except NotImplementedError:
        signal.signal(signal.SIGINT, lambda *_: _handle_sigterm())

    while not shutdown_event.is_set():
        try:
            results = await acevault_engine.run_cycle()
            logger.info("NXFH01_CYCLE_COMPLETE results=%s", results)
        except Exception:
            logger.exception("NXFH01_CYCLE_ERROR")

        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=cycle_interval)
        except asyncio.TimeoutError:
            pass

    logger.info("NXFH01_SHUTDOWN_COMPLETE")


if __name__ == "__main__":
    asyncio.run(main())
