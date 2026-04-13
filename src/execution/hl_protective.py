"""Hyperliquid venue TP/SL (reduce-only trigger orders) after a successful perp open.

Uses the official Python SDK pattern (see hyperliquid-python-sdk examples/basic_tpsl.py):
trigger orders with ``tpsl`` of ``tp`` / ``sl``, ``isMarket: True``, and a worst-case
``limit_px`` per Hyperliquid docs (mark-based trigger; limit controls slippage tolerance).

DegenClaw path: signs with ``ACP_WALLET_*`` (same wallet as ACP fills).
Senpi path: signs with ``SENPI_STRATEGY_WALLET_PRIVATE_KEY`` when set (same address as
``SENPI_STRATEGY_WALLET_ADDRESS``). Senpi MCP ``edit_position`` is **not** wired here —
no stable public schema in-repo; venue orders require the strategy wallet private key.

See: https://hyperliquid.gitbook.io/hyperliquid-docs/trading/take-profit-and-stop-loss-orders-tp-sl
"""

from __future__ import annotations

import logging
import time
from typing import Any, Literal

from eth_account import Account
from hyperliquid.exchange import Exchange
from hyperliquid.utils.signing import OrderRequest

from src.acp.degen_claw import AcpTradeRequest
from src.config import (
    ACP_WALLET_ADDRESS,
    ACP_WALLET_PRIVATE_KEY,
    HL_API_URL,
    HL_VENUE_TPSL_SLIPPAGE,
    HL_WALLET_ADDRESS,
    SENPI_STRATEGY_WALLET_ADDRESS,
    SENPI_STRATEGY_WALLET_PRIVATE_KEY,
)

logger = logging.getLogger(__name__)

FilledVia = Literal["senpi", "degen"]


def closing_order_is_buy(position_side: Literal["long", "short"]) -> bool:
    """Close long → sell (is_buy False); close short → buy (is_buy True)."""
    return position_side == "short"


def trigger_worst_limit_px(
    trigger_px: float,
    *,
    closing_order_is_buy: bool,
    slippage: float,
) -> float:
    """Worst acceptable limit once the trigger fires (Privy / HL TP-SL pattern)."""
    if closing_order_is_buy:
        return trigger_px * (1.0 + slippage)
    return trigger_px * (1.0 - slippage)


def round_perp_px(px: float, sz_decimals: int) -> float:
    """Match SDK price rounding: 5 significant figures, max decimals 6 - sz_decimals."""
    max_decimals = max(0, 6 - sz_decimals)
    return round(float(f"{px:.5g}"), max_decimals)


def build_protective_order_requests(
    coin: str,
    position_side: Literal["long", "short"],
    abs_size_coin: float,
    stop_loss: float | None,
    take_profit: float | None,
    *,
    slippage: float = 0.10,
    sz_decimals: int = 4,
) -> list[OrderRequest]:
    """Build 1–2 reduce-only HL trigger ``OrderRequest`` dicts (SDK-typed)."""
    orders: list[OrderRequest] = []
    close_buy = closing_order_is_buy(position_side)
    sz = round(abs_size_coin, sz_decimals)
    if sz <= 0:
        return orders

    # Stop-loss first (matches basic_tpsl.py ordering for SL before TP)
    if stop_loss is not None:
        lim = round_perp_px(
            trigger_worst_limit_px(stop_loss, closing_order_is_buy=close_buy, slippage=slippage),
            sz_decimals,
        )
        trig = round_perp_px(stop_loss, sz_decimals)
        orders.append(
            {
                "coin": coin,
                "is_buy": close_buy,
                "sz": sz,
                "limit_px": lim,
                "order_type": {
                    "trigger": {"triggerPx": trig, "isMarket": True, "tpsl": "sl"},
                },
                "reduce_only": True,
            }
        )
    if take_profit is not None:
        lim = round_perp_px(
            trigger_worst_limit_px(take_profit, closing_order_is_buy=close_buy, slippage=slippage),
            sz_decimals,
        )
        trig = round_perp_px(take_profit, sz_decimals)
        orders.append(
            {
                "coin": coin,
                "is_buy": close_buy,
                "sz": sz,
                "limit_px": lim,
                "order_type": {
                    "trigger": {"triggerPx": trig, "isMarket": True, "tpsl": "tp"},
                },
                "reduce_only": True,
            }
        )
    return orders


def _read_abs_position_sz(info: Any, address: str, coin: str) -> float | None:
    dex = coin.split(":")[0] if ":" in coin else ""
    try:
        st = info.user_state(address, dex)
    except Exception as e:
        logger.debug("user_state failed: %s", e)
        return None
    for ap in st.get("assetPositions") or []:
        pos = ap.get("position") or {}
        if pos.get("coin") != coin:
            continue
        try:
            return abs(float(pos.get("szi", 0)))
        except (TypeError, ValueError):
            return None
    return None


def _resolve_sz_coin(
    exchange: Exchange,
    address: str,
    coin: str,
    fallback_sz: float,
) -> float:
    for attempt in range(4):
        sz = _read_abs_position_sz(exchange.info, address, coin)
        if sz and sz > 0:
            return sz
        if attempt < 3:
            time.sleep(0.4)
    if fallback_sz > 0:
        logger.warning(
            "HL protective: position size not found for %s — using notional/price fallback %.8f",
            coin,
            fallback_sz,
        )
        return fallback_sz
    return 0.0


def _resolve_wallet_for_tpsl(filled_via: FilledVia | None) -> tuple[str, str]:
    """Return (address, private_key_hex) for signing venue TP/SL."""
    if filled_via == "senpi":
        addr = (SENPI_STRATEGY_WALLET_ADDRESS or "").strip()
        pk = (SENPI_STRATEGY_WALLET_PRIVATE_KEY or "").strip()
        return addr, pk
    addr = (ACP_WALLET_ADDRESS or HL_WALLET_ADDRESS or "").strip()
    pk = (ACP_WALLET_PRIVATE_KEY or "").strip()
    return addr, pk


def maybe_place_venue_tpsl_after_open(
    *,
    filled_via: FilledVia | None,
    request: AcpTradeRequest,
    entry_price: float,
    acp_live: bool,
    slippage: float | None = None,
) -> None:
    """Place HL reduce-only TP/SL triggers if keys + position wallet are configured. Never raises."""
    if request.stop_loss is None and request.take_profit is None:
        return
    fv = filled_via or "degen"
    if fv == "degen" and not acp_live:
        logger.debug("Venue TP/SL skipped: ACP dry-run (no on-chain position expected).")
        return

    slip = slippage if slippage is not None else HL_VENUE_TPSL_SLIPPAGE

    addr, pk = _resolve_wallet_for_tpsl(fv if fv in ("senpi", "degen") else "degen")
    if not addr or not pk:
        if fv == "senpi":
            logger.warning(
                "Venue TP/SL skipped for Senpi fill: set SENPI_STRATEGY_WALLET_PRIVATE_KEY "
                "(same wallet as SENPI_STRATEGY_WALLET_ADDRESS) to sign HL orders. "
                "Senpi MCP edit_position is not wired (no stable schema in-repo).",
            )
        else:
            logger.warning(
                "Venue TP/SL skipped: ACP_WALLET_PRIVATE_KEY / ACP_WALLET_ADDRESS not configured.",
            )
        return

    coin = request.coin
    side = request.side
    notional = max(float(request.size_usd), 1.0)
    ref_px = max(float(entry_price), 1e-8)
    fallback_sz = notional / ref_px

    try:
        wallet = Account.from_key(pk)
        if wallet.address.lower() != addr.lower():
            logger.warning(
                "Venue TP/SL skipped: private key does not match wallet address (%s vs %s).",
                wallet.address[:10],
                addr[:10],
            )
            return
        exchange = Exchange(wallet, base_url=HL_API_URL, account_address=addr)
        asset = exchange.info.name_to_asset(coin)
        sz_decimals = exchange.info.asset_to_sz_decimals[asset]
    except Exception as e:
        logger.warning("Venue TP/SL: could not init HL Exchange: %s", e)
        return

    abs_sz = _resolve_sz_coin(exchange, addr, coin, fallback_sz)
    if abs_sz <= 0:
        logger.warning("Venue TP/SL skipped: zero size for %s", coin)
        return

    orders = build_protective_order_requests(
        coin,
        side,
        abs_sz,
        request.stop_loss,
        request.take_profit,
        slippage=slip,
        sz_decimals=sz_decimals,
    )
    if not orders:
        return

    try:
        resp = exchange.bulk_orders(orders, grouping="na")
        logger.info(
            "Venue TP/SL placed for %s %s (%d orders) filled_via=%s resp_status=%s",
            side,
            coin,
            len(orders),
            fv,
            resp.get("status") if isinstance(resp, dict) else "ok",
        )
        if isinstance(resp, dict) and resp.get("status") != "ok":
            logger.warning("Venue TP/SL exchange response: %s", resp)
    except Exception as e:
        logger.warning(
            "Venue TP/SL placement failed (position still open; internal SL/TP unchanged): %s",
            e,
        )

