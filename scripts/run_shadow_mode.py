"""Shadow-mode runner: exercises the full signal pipeline without submitting real trades."""
from __future__ import annotations

import asyncio
import logging
import os
import sys
from pathlib import Path

import yaml
from dotenv import load_dotenv

from src.verification.shadow_report import ShadowReport

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)


def _load_config() -> dict:
    candidates = [
        Path(__file__).resolve().parent.parent / "config.yaml",
        Path.cwd() / "config.yaml",
    ]
    for p in candidates:
        if p.is_file():
            with open(p, encoding="utf-8") as f:
                return yaml.safe_load(f) or {}
    logger.error("SHADOW_MODE config.yaml not found")
    sys.exit(1)


async def main() -> int:
    load_dotenv()
    config = _load_config()

    verification = config.get("verification") or {}
    if not verification.get("shadow_mode_enabled", False):
        logger.error("SHADOW_MODE_DISABLED verification.shadow_mode_enabled is not true")
        return 1

    shadow_cycles = int(verification.get("shadow_cycles", 1))

    from src.risk.engine_killswitch import KillSwitch
    from src.risk.portfolio_state import PortfolioState
    from src.risk.unified_risk import UnifiedRiskLayer
    from src.regime.detector import RegimeDetector
    from src.market.btc_context_holder import BTCMarketContextHolder
    from src.engines.acevault.scanner import AltScanner

    kill_switch = KillSwitch(config)
    portfolio_state = PortfolioState()
    btc_context_holder = BTCMarketContextHolder()

    from src.market_data.hl_rate_limited_info import RateLimitedInfo
    hl_api = config["hyperliquid_api"]
    hl_client = RateLimitedInfo(
        base_url=hl_api["api_base_url"],
        skip_ws=True,
        rate_config=hl_api,
    )

    risk_layer = UnifiedRiskLayer(
        config,
        portfolio_state,
        kill_switch,
        btc_context_holder=btc_context_holder,
    )
    regime_detector = RegimeDetector(config, data_fetcher=None)
    scanner = AltScanner(config, hl_client)

    report = ShadowReport()

    initial_market_data = {
        "btc_1h_return": 0.0,
        "btc_4h_return": 0.0,
        "btc_vol_1h": 0.004,
    }

    for cycle in range(shadow_cycles):
        logger.info("SHADOW_CYCLE_START cycle=%d/%d", cycle + 1, shadow_cycles)

        regime_state = regime_detector.detect(market_data=initial_market_data)
        regime_label = regime_state.regime.value

        candidates = scanner.scan()
        if not candidates:
            logger.info("SHADOW_CYCLE_NO_CANDIDATES cycle=%d", cycle + 1)
            continue

        from dataclasses import dataclass

        @dataclass
        class _ShadowSignal:
            coin: str
            side: str
            position_size_usd: float
            weakness_score: float
            metadata: dict | None = None

        for cand in candidates:
            signal = _ShadowSignal(
                coin=cand.coin,
                side="short",
                position_size_usd=float(
                    config.get("acevault", {}).get("default_position_size_usd", 25)
                ),
                weakness_score=cand.weakness_score,
            )

            decision = risk_layer.validate(signal, "acevault")
            estimated_cost_bps = 0.0

            report.record({
                "coin": cand.coin,
                "regime": regime_label,
                "expected_entry_price": cand.current_price,
                "estimated_cost_bps": estimated_cost_bps,
                "approved": decision.approved,
                "reject_reason": decision.reason if not decision.approved else "",
            })

        logger.info("SHADOW_CYCLE_COMPLETE cycle=%d candidates=%d", cycle + 1, len(candidates))

    summary = report.summarize()
    logger.info("SHADOW_MODE_SUMMARY %s", summary)
    print("\n=== Shadow Mode Summary ===")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print("=== End Shadow Mode ===\n")

    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
