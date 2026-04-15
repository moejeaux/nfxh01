"""Shared process bootstrap: config, context, startup logs, orchestrator wiring."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from hyperliquid.info import Info

from src.acp.degen_claw import DegenClawAcp as DegenClawExecutor
from src.db.decision_journal import DecisionJournal
from src.engines.acevault.engine import AceVaultEngine
from src.engines.growi_hf.engine import GrowiHFEngine
from src.engines.mc_recovery.engine import MCRecoveryEngine
from src.fathom.advisor import FathomAdvisor
from src.nxfh01.config_paths import find_config_yaml
from src.nxfh01.orchestration.config_validation import validate_multi_strategy_config
from src.nxfh01.orchestration.strategy_orchestrator import StrategyOrchestrator
from src.nxfh01.orchestration.strategy_registry import StrategyRegistry
from src.nxfh01.orchestration.track_a_executor import TrackAExecutor
from src.regime.detector import RegimeDetector
from src.risk.engine_killswitch import KillSwitch
from src.risk.portfolio_state import PortfolioState
from src.risk.unified_risk import UnifiedRiskLayer

load_dotenv()

logger = logging.getLogger(__name__)

VERSION = "1.0.0"


def load_config() -> dict:
    config_path = find_config_yaml(Path(__file__))
    with open(config_path, encoding="utf-8") as f:
        return yaml.safe_load(f)


async def init_hl_client() -> Info:
    for attempt in range(5):
        try:
            client = Info(base_url="https://api.hyperliquid.xyz", skip_ws=True)
            logger.info("HL_CLIENT_INITIALIZED attempt=%d", attempt + 1)
            return client
        except Exception as e:
            if "429" in str(e):
                wait = (attempt + 1) * 15
                logger.warning(
                    "HL_CLIENT_RATE_LIMITED attempt=%d waiting=%ds",
                    attempt + 1,
                    wait,
                )
                await asyncio.sleep(wait)
            else:
                logger.error("HL_CLIENT_INIT_ERROR error=%s", e)
                raise
    logger.error("HL_CLIENT_INIT_FAILED after 5 attempts")
    sys.exit(1)


def _log_startup_sequence(ctx: dict) -> None:
    """Emit the same startup logs as ``main`` for tests and operators."""
    config = ctx["config"]
    regime_detector = ctx["regime_detector"]
    initial_market_data = {
        "btc_1h_return": 0.0,
        "btc_4h_return": 0.0,
        "btc_vol_1h": 0.004,
    }
    initial_regime = regime_detector.detect(market_data=initial_market_data)
    weight = config["acevault"]["regime_weights"][
        initial_regime.regime.value.lower()
    ]
    tick_iv = ctx.get("tick_interval_seconds", config["acevault"]["cycle_interval_seconds"])

    logger.info("NXFH01_STARTING version=%s", VERSION)
    logger.info(
        "RISK_LAYER_INITIALIZED max_dd=%s%% max_exposure=%s%%",
        int(config["risk"]["max_portfolio_drawdown_24h"] * 100),
        int(config["risk"]["max_gross_multiplier"] * 100),
    )
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
        "NXFH01_READY cycles_per_minute=%.1f orchestrator_tick_s=%.1f",
        60 / tick_iv,
        tick_iv,
    )


async def build_context(config: dict) -> dict:
    validate_multi_strategy_config(config)

    kill_switch = KillSwitch(config)
    portfolio_state = PortfolioState()
    risk_layer = UnifiedRiskLayer(config, portfolio_state, kill_switch)

    hl_client = await init_hl_client()
    regime_detector = RegimeDetector(config, data_fetcher=None)

    journal = None
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        try:
            journal = DecisionJournal(database_url)
            await journal.connect()
            logger.info("DECISION_JOURNAL_CONNECTED")
        except Exception as e:
            logger.warning(
                "DECISION_JOURNAL_CONNECT_FAILED error=%s — continuing without journal",
                e,
            )
            journal = None
    else:
        logger.warning("DECISION_JOURNAL_DISABLED DATABASE_URL not set")

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
            logger.warning(
                "FATHOM_ADVISOR_INIT_FAILED error=%s — continuing without Fathom",
                e,
            )
            fathom_advisor = None
    else:
        logger.info("FATHOM_ADVISOR_DISABLED enabled=false in config")

    degen_executor = DegenClawExecutor(config=config)

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

    growi_engine = GrowiHFEngine(
        config,
        hl_client,
        regime_detector,
        kill_switch,
        portfolio_state,
    )
    mc_engine = MCRecoveryEngine(
        config,
        hl_client,
        regime_detector,
        kill_switch,
        portfolio_state,
    )

    registry = StrategyRegistry(config)
    runners = {
        "acevault": acevault_engine.run_cycle,
        "growi_hf": growi_engine.run_cycle,
        "mc_recovery": mc_engine.run_cycle,
    }
    track_a_executor = TrackAExecutor(
        config,
        risk_layer,
        portfolio_state,
        degen_executor,
        hl_client,
        journal=journal,
    )

    orch_cfg = config.get("orchestration") or {}
    hl_addr = os.getenv("HL_WALLET_ADDRESS", "").strip()
    if hl_addr:
        if orch_cfg.get("hl_sync_on_startup"):
            portfolio_state.sync_from_hl(hl_client, hl_addr)
        if orch_cfg.get("hl_reconcile_on_startup"):
            portfolio_state.reconcile_open_positions_vs_hl(hl_client, hl_addr)
    orchestrator = StrategyOrchestrator(
        config,
        registry,
        runners,
        track_a_executor=track_a_executor,
    )

    tick_interval = float(
        orch_cfg.get("tick_interval_seconds")
        or config["acevault"]["cycle_interval_seconds"]
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
        "growi_hf_engine": growi_engine,
        "mc_recovery_engine": mc_engine,
        "strategy_registry": registry,
        "orchestrator": orchestrator,
        "track_a_executor": track_a_executor,
        "tick_interval_seconds": tick_interval,
    }
