import asyncio
import logging
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from datetime import datetime, timezone

from src.main import VERSION, _log_startup_sequence, build_context
from src.nxfh01.orchestration.strategy_registry import StrategyRegistry
from src.nxfh01.orchestration.strategy_orchestrator import StrategyOrchestrator
from src.nxfh01.orchestration.types import OrchestratorTickSummary, StrategyTickResult


@pytest.fixture
def base_config():
    return {
        "hyperliquid_api": {
            "api_base_url": "https://api.hyperliquid.xyz",
            "min_interval_ms": 85,
            "max_retries_on_429": 8,
            "backoff_base_seconds": 1.25,
            "backoff_max_seconds": 120.0,
            "backoff_jitter_ratio": 0.35,
        },
        "database": {"pool_min_size": 1, "pool_max_size": 4},
        "engines": {
            "planned_count": 5,
            "acevault": {"loss_pct": 0.03, "cooldown_hours": 4},
            "growi": {"loss_pct": 0.04, "cooldown_hours": 6},
            "mc": {"loss_pct": 0.02, "cooldown_hours": 2},
        },
        "acevault": {
            "stop_loss_distance_pct": 0.3,
            "take_profit_distance_pct": 2.7,
            "ranging_min_weakness_score": 0.45,
            "min_volume_ratio": 0.8,
            "max_candidates": 5,
            "min_weakness_score": 0.3,
            "max_concurrent_positions": 5,
            "max_hold_minutes": 240,
            "cycle_interval_seconds": 30,
            "verification_size_usd": 50,
            "regime_weights": {
                "trending_up": 0.4,
                "trending_down": 0.9,
                "ranging": 0.6,
                "risk_off": 1.0,
            },
        },
        "regime": {
            "btc_1h_risk_off_threshold": -0.02,
            "btc_vol_risk_off_threshold": 0.008,
            "btc_4h_trend_threshold": 0.015,
            "btc_vol_trend_threshold": 0.006,
            "min_transition_interval_minutes": 15,
        },
        "risk": {
            "total_capital_usd": 10000,
            "max_portfolio_drawdown_24h": 0.05,
            "max_gross_multiplier": 3.0,
            "max_correlated_longs": 3,
            "min_available_capital_usd": 10.50,
        },
        "orchestration": {
            "tick_interval_seconds": 30,
            "execution_order": ["acevault", "growi_hf", "mc_recovery"],
            "conflict": {
                "mode": "skip_opposing",
                "priority": ["acevault", "growi_hf", "mc_recovery"],
            },
        },
        "strategies": {
            "acevault": {"enabled": True, "engine_id": "acevault"},
            "growi_hf": {"enabled": False, "engine_id": "growi"},
            "mc_recovery": {"enabled": False, "engine_id": "mc"},
        },
    }


@pytest.fixture
def config_yaml_path(tmp_path, base_config):
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(yaml.dump(base_config), encoding="utf-8")
    return cfg_path


@pytest.fixture
def ctx(base_config):
    """Build a full context dict using real components, mocked externals."""
    from src.engines.acevault.engine import AceVaultEngine
    from src.regime.detector import RegimeDetector
    from src.risk.engine_killswitch import KillSwitch
    from src.risk.portfolio_state import PortfolioState
    from src.risk.unified_risk import UnifiedRiskLayer

    kill_switch = KillSwitch(base_config)
    portfolio_state = PortfolioState()
    risk_layer = UnifiedRiskLayer(base_config, portfolio_state, kill_switch)
    hl_client = MagicMock()
    regime_detector = RegimeDetector(base_config, data_fetcher=None)

    with (
        patch("src.engines.acevault.engine.AltScanner"),
        patch("src.engines.acevault.engine.EntryManager"),
        patch("src.engines.acevault.engine.ExitManager"),
    ):
        acevault_engine = AceVaultEngine(
            base_config, hl_client, regime_detector, risk_layer, None
        )

    tick_iv = float(
        (base_config.get("orchestration") or {}).get("tick_interval_seconds")
        or base_config["acevault"]["cycle_interval_seconds"]
    )
    reg = StrategyRegistry(base_config)
    orch = StrategyOrchestrator(
        base_config,
        reg,
        {"acevault": acevault_engine.run_cycle, "growi_hf": AsyncMock(return_value=[]), "mc_recovery": AsyncMock(return_value=[])},
    )
    return {
        "config": base_config,
        "kill_switch": kill_switch,
        "portfolio_state": portfolio_state,
        "risk_layer": risk_layer,
        "hl_client": hl_client,
        "regime_detector": regime_detector,
        "acevault_engine": acevault_engine,
        "orchestrator": orch,
        "tick_interval_seconds": tick_iv,
    }


class TestBuildContext:
    @pytest.mark.asyncio
    async def test_returns_all_required_keys(self, config_yaml_path):
        cfg = yaml.safe_load(config_yaml_path.read_text(encoding="utf-8"))
        with (
            patch("src.nxfh01.runtime.init_hl_client", new_callable=AsyncMock) as mock_hl,
            patch("src.engines.acevault.engine.AltScanner"),
            patch("src.engines.acevault.engine.EntryManager"),
            patch("src.engines.acevault.engine.ExitManager"),
        ):
            mock_hl.return_value = MagicMock()
            ctx = await build_context(cfg)

            expected_keys = {
            "config",
            "kill_switch",
            "portfolio_state",
            "risk_layer",
            "hl_client",
            "regime_detector",
            "journal",
            "fathom_advisor",
            "degen_executor",
            "acevault_engine",
            "growi_hf_engine",
            "mc_recovery_engine",
            "strategy_registry",
            "orchestrator",
            "track_a_executor",
            "tick_interval_seconds",
        }
        assert set(ctx.keys()) == expected_keys


class TestBuildContextInitOrder:
    def test_kill_switch_receives_config(self, ctx):
        assert ctx["kill_switch"]._config == ctx["config"]

    def test_risk_layer_receives_portfolio_and_killswitch(self, ctx):
        assert ctx["risk_layer"]._portfolio_state is ctx["portfolio_state"]
        assert ctx["risk_layer"]._kill_switch is ctx["kill_switch"]

    def test_risk_layer_portfolio_state_property(self, ctx):
        assert ctx["risk_layer"].portfolio_state is ctx["portfolio_state"]

    def test_acevault_engine_receives_dependencies(self, ctx):
        engine = ctx["acevault_engine"]
        assert engine.regime_detector is ctx["regime_detector"]
        assert engine.risk_layer is ctx["risk_layer"]

    def test_acevault_engine_has_config(self, ctx):
        assert ctx["acevault_engine"]._config is ctx["config"]


class TestStartupLogSequence:
    def test_logs_appear_in_order(self, ctx, caplog):
        with caplog.at_level(logging.INFO):
            _log_startup_sequence(ctx)

        messages = [
            r.message
            for r in caplog.records
            if r.name == "src.nxfh01.runtime"
        ]
        expected_prefixes = [
            "NXFH01_STARTING",
            "RISK_LAYER_INITIALIZED",
            "REGIME_DETECTED",
            "ACEVAULT_ENGINE_INITIALIZED",
            "NXFH01_READY",
        ]

        found_indices = []
        for prefix in expected_prefixes:
            for i, msg in enumerate(messages):
                if msg.startswith(prefix) and i not in found_indices:
                    found_indices.append(i)
                    break

        assert len(found_indices) == len(expected_prefixes)
        assert found_indices == sorted(found_indices)

    def test_version_in_starting_log(self, ctx, caplog):
        with caplog.at_level(logging.INFO):
            _log_startup_sequence(ctx)

        starting = [r.message for r in caplog.records if "NXFH01_STARTING" in r.message]
        assert len(starting) >= 1
        assert VERSION in starting[0]

    def test_risk_layer_log_contains_max_dd(self, ctx, caplog):
        with caplog.at_level(logging.INFO):
            _log_startup_sequence(ctx)

        risk_msgs = [r.message for r in caplog.records if "RISK_LAYER_INITIALIZED" in r.message]
        assert len(risk_msgs) >= 1
        assert "max_dd=5%" in risk_msgs[0]

    def test_risk_layer_log_contains_max_exposure(self, ctx, caplog):
        with caplog.at_level(logging.INFO):
            _log_startup_sequence(ctx)

        risk_msgs = [r.message for r in caplog.records if "RISK_LAYER_INITIALIZED" in r.message]
        assert "max_exposure=300%" in risk_msgs[0]

    def test_regime_detected_logged(self, ctx, caplog):
        with caplog.at_level(logging.INFO):
            _log_startup_sequence(ctx)

        regime_msgs = [r.message for r in caplog.records if "REGIME_DETECTED" in r.message]
        assert len(regime_msgs) >= 1

    def test_acevault_engine_initialized_contains_weight(self, ctx, caplog):
        with caplog.at_level(logging.INFO):
            _log_startup_sequence(ctx)

        engine_msgs = [
            r.message for r in caplog.records if "ACEVAULT_ENGINE_INITIALIZED" in r.message
        ]
        assert len(engine_msgs) >= 1
        assert "weight=" in engine_msgs[0]

    def test_cycles_per_minute_calculated(self, ctx, caplog):
        with caplog.at_level(logging.INFO):
            _log_startup_sequence(ctx)

        ready_msgs = [r.message for r in caplog.records if "NXFH01_READY" in r.message]
        assert len(ready_msgs) >= 1
        assert "cycles_per_minute=2.0" in ready_msgs[0]

    def test_cycles_per_minute_with_different_interval(self, ctx, caplog):
        ctx["config"]["acevault"]["cycle_interval_seconds"] = 60
        ctx["tick_interval_seconds"] = 60.0
        with caplog.at_level(logging.INFO):
            _log_startup_sequence(ctx)

        ready_msgs = [r.message for r in caplog.records if "NXFH01_READY" in r.message]
        assert "cycles_per_minute=1.0" in ready_msgs[0]


def _fake_summary(raw_events: int = 0) -> OrchestratorTickSummary:
    return OrchestratorTickSummary(
        tick_at=datetime.now(timezone.utc),
        strategy_results=[
            StrategyTickResult(
                "acevault",
                "acevault",
                True,
                None,
                raw_events,
                None,
            ),
        ],
        normalized_intents_produced=0,
        intents_after_conflict=0,
        tick_duration_ms=1.0,
    )


class TestMainLoop:
    @pytest.mark.asyncio
    async def test_runs_cycle_and_logs_complete(self, ctx, caplog):
        call_count = 0
        shutdown = asyncio.Event()

        async def counting_tick():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                shutdown.set()
            return _fake_summary(0)

        ctx["orchestrator"].run_tick = counting_tick

        with caplog.at_level(logging.INFO):
            await _run_main_loop(ctx, shutdown, cycle_interval=0.01)

        cycle_msgs = [r.message for r in caplog.records if "NXFH01_CYCLE_COMPLETE" in r.message]
        assert len(cycle_msgs) >= 1

    @pytest.mark.asyncio
    async def test_logs_shutdown_complete(self, ctx, caplog):
        ctx["orchestrator"].run_tick = AsyncMock(return_value=_fake_summary(0))

        shutdown = asyncio.Event()
        shutdown.set()

        with caplog.at_level(logging.INFO):
            await _run_main_loop(ctx, shutdown, cycle_interval=0.01)

        shutdown_msgs = [
            r.message for r in caplog.records if "NXFH01_SHUTDOWN_COMPLETE" in r.message
        ]
        assert len(shutdown_msgs) == 1

    @pytest.mark.asyncio
    async def test_cycle_error_does_not_crash_loop(self, ctx, caplog):
        call_count = 0
        shutdown = asyncio.Event()

        async def failing_then_ok():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("test explosion")
            shutdown.set()
            return _fake_summary(0)

        ctx["orchestrator"].run_tick = failing_then_ok

        with caplog.at_level(logging.INFO):
            await _run_main_loop(ctx, shutdown, cycle_interval=0.01)

        error_msgs = [r.message for r in caplog.records if "NXFH01_CYCLE_ERROR" in r.message]
        assert len(error_msgs) >= 1
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_cycle_results_logged(self, ctx, caplog):
        shutdown = asyncio.Event()
        call_count = 0

        async def one_then_stop():
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                shutdown.set()
            return _fake_summary(2)

        ctx["orchestrator"].run_tick = one_then_stop

        with caplog.at_level(logging.INFO):
            await _run_main_loop(ctx, shutdown, cycle_interval=0.01)

        cycle_msgs = [r.message for r in caplog.records if "NXFH01_CYCLE_COMPLETE" in r.message]
        assert len(cycle_msgs) >= 1
        assert "raw_events=2" in cycle_msgs[0]


class TestConfigLoading:
    def test_loads_real_config_yaml(self):
        real_config_path = Path(__file__).resolve().parents[1] / "config.yaml"
        if not real_config_path.exists():
            pytest.skip("config.yaml not present")
        with real_config_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f)
        assert "acevault" in config
        assert "risk" in config
        assert "regime" in config
        assert "cycle_interval_seconds" in config["acevault"]

    def test_config_has_required_risk_keys(self):
        real_config_path = Path(__file__).resolve().parents[1] / "config.yaml"
        if not real_config_path.exists():
            pytest.skip("config.yaml not present")
        with real_config_path.open(encoding="utf-8") as f:
            config = yaml.safe_load(f)
        risk = config["risk"]
        assert "max_portfolio_drawdown_24h" in risk
        assert "max_gross_multiplier" in risk
        assert "total_capital_usd" in risk


# --- test helper: extracted main loop for testing without signal handlers ---

async def _run_main_loop(
    ctx: dict,
    shutdown_event: asyncio.Event,
    cycle_interval: float = 0.01,
) -> None:
    orchestrator = ctx["orchestrator"]
    log = logging.getLogger("src.main")
    while not shutdown_event.is_set():
        try:
            summary = await orchestrator.run_tick()
            ran = sum(1 for r in summary.strategy_results if r.ran)
            log.info(
                "NXFH01_CYCLE_COMPLETE strategies_ran=%d raw_events=%d tick_ms=%.2f "
                "track_a_submitted=%d track_a_registered=%d",
                ran,
                sum(r.raw_result_count for r in summary.strategy_results),
                summary.tick_duration_ms,
                summary.track_a_submitted,
                summary.track_a_registered,
            )
        except Exception:
            log.exception("NXFH01_CYCLE_ERROR")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=cycle_interval)
        except asyncio.TimeoutError:
            pass
    log.info("NXFH01_SHUTDOWN_COMPLETE")
