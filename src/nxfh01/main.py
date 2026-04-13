from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv
from hyperliquid.info import Info
from hyperliquid.utils import constants

from src.acp.degen_claw import DegenClawExecutor
from src.db.decision_journal import DecisionJournal
from src.engines.acevault.engine import AceVaultEngine
from src.fathom.advisor import FathomAdvisor
from src.regime.detector import RegimeDetector
from src.risk.engine_killswitch import KillSwitch
from src.risk.portfolio_state import PortfolioState
from src.risk.unified_risk import UnifiedRiskLayer

load_dotenv()

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def load_config() -> dict:
    config_path = Path(__file__).parent.parent / "config.yaml"
    with open(config_path) as f:
        return yaml.safe_load(f)


async def build_context(config: dict) -> dict:
    # 1. Kill switch
    kill_switch = KillSwitch(config)

    # 2. Portfolio state
    portfolio_state = PortfolioState()

    # 3. Unified risk layer
    risk_layer = UnifiedRiskLayer(config, portfolio_state, kill_switch)
    logger.info(
        "RISK_LAYER_INITIALIZED max_dd=%s%% max_exposure=%s%%",
        int(config["risk"]["max_portfolio_drawdown_24h"] * 100),
        int(config["risk"]["max_gross_exposure_multiplier"] * 100),
    )

    # 4. Hyperliquid client
    hl_client = Info(
        constants.MAINNET_API_URL,
        skip_ws=True,
    )

    # 5. Regime detector
    regime_detector = RegimeDetector(config, data_fetcher=None)

    # 6. Decision journal
    journal = None
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            journal = DecisionJournal(database_url)
            await journal.connect()
            logger.info("DECISION_JOURNAL_CONNECTED")
        except Exception as e:
            logger.warning("DECISION_JOURNAL_CONNECT_FAILED error=%s — continuing without journal", e)
            journal = None
    else:
        logger.warning("DECISION_JOURNAL_DISABLED DATABASE_URL not set")

    # 7. Fathom advisor
    fathom_advisor = None
    if config.get("fathom", {}).get("enabled", False):
        try:
            ollama_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434")
            fathom_advisor = FathomAdvisor(config, ollama_url)
            logger.info(
                "FATHOM_ADVISOR_INITIALIZED model=%s url=%s",
                config["fathom"]["model"],
                ollama_url,
            )
        except Exception as e:
            logger.warning("FATHOM_ADVISOR_INIT_FAILED error=%s — continuing without Fathom", e)
            fathom_advisor = None
    else:
        logger.info("FATHOM_ADVISOR_DISABLED enabled=false in config")

    # 8. DegenClaw executor
    degen_executor = DegenClawExecutor(config)

    # 9. AceVault engine
    acevault_engine = AceVaultEngine(
        config=config,
        hl_client=hl_client,
        regime_detector=regime_detector,
        risk_layer=risk_layer,
        degen_executor=degen_executor,
        kill_switch=kill_switch,
        journal=journal,
        fathom_advisor=fathom_advisor,
    )

    return {
        "config": config,
        "kill_switch": kill_switch,
        "portfolio_state": portfolio_state,
        "risk_layer": risk_layer,
        "hl_client": hl_client,
        "regime_detector": regime_detector,
        "journal": journal,
        "fathom_advisor": fathom_advisor,
        "degen_executor": degen_executor,
        "acevault_engine": acevault_engine,
    }


async def main() -> None:
    logger.info("NXFH01_STARTING version=1.0.0")

    config = load_config()
    ctx = await build_context(config)

    acevault_engine: AceVaultEngine = ctx["acevault_engine"]
    cycle_interval = config["acevault"]["cycle_interval_seconds"]

    # Detect initial regime for startup log
    from src.regime.models import RegimeType
    initial_market_data = {
        "btc_1h_return": 0.0,
        "btc_4h_return": 0.0,
        "btc_vol_1h": 0.004,
    }
    initial_regime = ctx["regime_detector"].detect(market_data=initial_market_data)
    weight = config["acevault"]["regime_weights"][initial_regime.regime.value.lower()]

    logger.info(
        "REGIME_DETECTED regime=%s confidence=%.2f",
        initial_regime.regime.value,
        initial_regime.confidence,
    )
    logger.info(
        "ACEVAULT_ENGINE_INITIALIZED regime=%s weight=%.2f",
        initial_regime.regime.value,
        weight,
    )
    logger.info(
        "NXFH01_READY cycles_per_minute=%.1f",
        60 / cycle_interval,
    )

    # Graceful shutdown
    shutdown_event = asyncio.Event()

    def handle_shutdown(sig, frame):
        logger.info("NXFH01_SHUTDOWN_INITIATED signal=%s", sig)
        shutdown_event.set()

    signal.signal(signal.SIGTERM, handle_shutdown)
    signal.signal(signal.SIGINT, handle_shutdown)

    # Main loop
    while not shutdown_event.is_set():
        try:
            results = await acevault_engine.run_cycle()
            logger.info("NXFH01_CYCLE_COMPLETE results=%s", results)
        except Exception as e:
            logger.error("NXFH01_CYCLE_ERROR error=%s", e, exc_info=True)

        try:
            await asyncio.wait_for(
                shutdown_event.wait(),
                timeout=cycle_interval,
            )
        except asyncio.TimeoutError:
            pass

    logger.info("NXFH01_SHUTDOWN_COMPLETE")


if __name__ == "__main__":
    asyncio.run(main())
