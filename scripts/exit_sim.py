#!/usr/bin/env python3
"""Replay a comma-separated price series through LiveExitEngine (config-driven policies)."""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from dotenv import load_dotenv

from src.engines.acevault.models import AcePosition
from src.exits.manager import LiveExitEngine
from src.nxfh01.models import AceSignal
from src.nxfh01.runtime import load_config


def _parse_prices(s: str) -> list[float]:
    out: list[float] = []
    for part in s.split(","):
        part = part.strip()
        if part:
            out.append(float(part))
    return out


def main() -> int:
    p = argparse.ArgumentParser(description="Exit policy simulation over a price ladder.")
    p.add_argument("--coin", default="ETH", help="Perp coin symbol")
    p.add_argument("--side", default="short", choices=("long", "short"))
    p.add_argument("--entry", type=float, required=True)
    p.add_argument("--stop", type=float, required=True)
    p.add_argument("--take-profit", type=float, required=True)
    p.add_argument("--size-usd", type=float, default=100.0)
    p.add_argument(
        "--prices",
        required=True,
        help="Comma-separated mid prices in chronological order",
    )
    p.add_argument("--regime-exit-all", action="store_true")
    args = p.parse_args()

    load_dotenv()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    cfg = load_config()
    prices = _parse_prices(args.prices)
    if not prices:
        logging.getLogger(__name__).error("EXIT_SIM_ABORT reason=no_prices")
        return 1

    now = datetime.now(timezone.utc)
    sig = AceSignal(
        coin=args.coin,
        side=args.side,
        entry_price=args.entry,
        stop_loss_price=args.stop,
        take_profit_price=args.take_profit,
        position_size_usd=args.size_usd,
        weakness_score=0.5,
        regime_at_entry="SIDEWAYS_WEAK",
        timestamp=now,
    )
    pos = AcePosition(
        position_id="exit_sim_1",
        signal=sig,
        opened_at=now,
        current_price=prices[0],
        unrealized_pnl_usd=0.0,
        status="open",
    )
    eng = LiveExitEngine(cfg)
    log = logging.getLogger(__name__)
    for i, px in enumerate(prices):
        pos.current_price = px
        exits = eng.evaluate_portfolio_positions(
            engine_id="acevault",
            positions=[pos],
            current_prices={args.coin: px},
            regime_exit_all=bool(args.regime_exit_all),
        )
        log.info("EXIT_SIM_TICK i=%d price=%.6f exits=%d", i, px, len(exits))
        if exits:
            e = exits[0]
            log.info(
                "EXIT_SIM_RESULT reason=%s pnl_usd=%.4f pnl_pct=%.5f hold_s=%d",
                e.exit_reason,
                e.pnl_usd,
                e.pnl_pct,
                e.hold_duration_seconds,
            )
            return 0
    log.info("EXIT_SIM_NO_EXIT ticks=%d", len(prices))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
