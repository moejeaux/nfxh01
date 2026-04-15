import asyncio
import logging
import os
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import yaml

from src.main import VERSION, _log_startup_sequence, build_context


@pytest.fixture
def base_config():
    return {
        "engines": {
            "planned_count": 5,
            "acevault": {"loss_pct": 0.03, "cooldown_hours": 4},
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

    return {
        "config": base_config,
        "kill_switch": kill_switch,
        "portfolio_state": portfolio_state,
        "risk_layer": risk_layer,
        "hl_client": hl_client,
        "regime_detector": regime_detector,
        "acevault_engine": acevault_engine,
    }


class TestBuildContext:
    def test_returns_all_required_keys(self, config_yaml_path):
        with (
            patch.dict(os.environ, {"HL_WALLET_ADDRESS": "0xabc123"}),
            patch("src.main.load_dotenv"),
            patch("src.main.Path") as mock_path_cls,
            patch("src.engines.acevault.engine.AltScanner"),
            patch("src.engines.acevault.engine.EntryManager"),
            patch("src.engines.acevault.engine.ExitManager"),
        ):
            mock_path_cls.return_value.resolve.return_value.parents.__getitem__ = (
                lambda s, i: config_yaml_path.parent
            )

            with patch("src.main.yaml.safe_load") as mock_yaml:
                mock_yaml.return_value = yaml.safe_load(
                    config_yaml_path.read_text(encoding="utf-8")
                )
                with patch("hyperliquid.info.Info"):
                    ctx = build_context()

            expected_keys = {
                "config",
                "kill_switch",
                "portfolio_state",
                "risk_layer",
                "hl_client",
                "regime_detector",
                "acevault_engine",
            }
            assert set(ctx.keys()) == expected_keys

    def test_raises_without_wallet_address(self, config_yaml_path):
        with (
            patch.dict(os.environ, {"HL_WALLET_ADDRESS": ""}, clear=False),
            patch("src.main.load_dotenv"),
            patch("src.main.yaml.safe_load") as mock_yaml,
        ):
            mock_yaml.return_value = yaml.safe_load(
                config_yaml_path.read_text(encoding="utf-8")
            )
            with pytest.raises(RuntimeError, match="HL_WALLET_ADDRESS"):
                build_context()

    def test_raises_when_wallet_address_env_absent(self, config_yaml_path, monkeypatch):
        monkeypatch.delenv("HL_WALLET_ADDRESS", raising=False)
        with (
            patch("src.main.load_dotenv"),
            patch("src.main.yaml.safe_load") as mock_yaml,
        ):
            mock_yaml.return_value = yaml.safe_load(
                config_yaml_path.read_text(encoding="utf-8")
            )
            with pytest.raises(RuntimeError, match="HL_WALLET_ADDRESS"):
                build_context()


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

        messages = [r.message for r in caplog.records]
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
        with caplog.at_level(logging.INFO):
            _log_startup_sequence(ctx)

        ready_msgs = [r.message for r in caplog.records if "NXFH01_READY" in r.message]
        assert "cycles_per_minute=1.0" in ready_msgs[0]


class TestMainLoop:
    @pytest.mark.asyncio
    async def test_runs_cycle_and_logs_complete(self, ctx, caplog):
        engine = ctx["acevault_engine"]
        engine.run_cycle = AsyncMock(return_value=[])

        call_count = 0
        shutdown = asyncio.Event()

        async def counting_run_cycle():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                shutdown.set()
            return []

        engine.run_cycle = counting_run_cycle

        with caplog.at_level(logging.INFO):
            await _run_main_loop(ctx, shutdown, cycle_interval=0.01)

        cycle_msgs = [r.message for r in caplog.records if "NXFH01_CYCLE_COMPLETE" in r.message]
        assert len(cycle_msgs) >= 1

    @pytest.mark.asyncio
    async def test_logs_shutdown_complete(self, ctx, caplog):
        engine = ctx["acevault_engine"]
        engine.run_cycle = AsyncMock(return_value=[])

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
        engine = ctx["acevault_engine"]
        call_count = 0
        shutdown = asyncio.Event()

        async def failing_then_ok():
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise ValueError("test explosion")
            shutdown.set()
            return []

        engine.run_cycle = failing_then_ok

        with caplog.at_level(logging.INFO):
            await _run_main_loop(ctx, shutdown, cycle_interval=0.01)

        error_msgs = [r.message for r in caplog.records if "NXFH01_CYCLE_ERROR" in r.message]
        assert len(error_msgs) >= 1
        assert call_count >= 2

    @pytest.mark.asyncio
    async def test_cycle_results_logged(self, ctx, caplog):
        engine = ctx["acevault_engine"]
        engine.run_cycle = AsyncMock(return_value=["signal_a", "exit_b"])

        shutdown = asyncio.Event()
        call_count = 0

        original = engine.run_cycle

        async def one_then_stop():
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                shutdown.set()
            return await original()

        engine.run_cycle = one_then_stop

        with caplog.at_level(logging.INFO):
            await _run_main_loop(ctx, shutdown, cycle_interval=0.01)

        cycle_msgs = [r.message for r in caplog.records if "NXFH01_CYCLE_COMPLETE" in r.message]
        assert len(cycle_msgs) >= 1
        assert "signal_a" in cycle_msgs[0]


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
    engine = ctx["acevault_engine"]
    log = logging.getLogger("src.main")
    while not shutdown_event.is_set():
        try:
            results = await engine.run_cycle()
            log.info("NXFH01_CYCLE_COMPLETE results=%s", results)
        except Exception:
            log.exception("NXFH01_CYCLE_ERROR")
        try:
            await asyncio.wait_for(shutdown_event.wait(), timeout=cycle_interval)
        except asyncio.TimeoutError:
            pass
    log.info("NXFH01_SHUTDOWN_COMPLETE")
