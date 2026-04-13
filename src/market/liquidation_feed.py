"""Real-time liquidation and OI monitoring via Hyperliquid WebSocket.

Subscribes to all allowed perp coins for:
  1. Liquidation cascade detection — protects shorts from squeeze events
  2. OI change tracking — confirms or weakens momentum signal conviction
"""

from __future__ import annotations

import json
import logging
import threading
import time
from collections import defaultdict, deque
from datetime import datetime, timedelta, timezone
from typing import Any

import websocket

logger = logging.getLogger(__name__)

# Fallback coin list — always pass config.allowed_markets.perps at startup
DEFAULT_COINS = ["BTC", "ETH", "SOL", "DOGE", "ARB", "OP", "AVAX", "POL", "HYPE", "LINK"]

# Liquidation cascade thresholds
SQUEEZE_WINDOW_SECONDS = 120
SQUEEZE_USD_THRESHOLD = 50_000
SQUEEZE_BLOCK_MINUTES = 10

# OI change thresholds
OI_STRONG_CONVICTION = 0.05
OI_WEAK_SIGNAL = -0.03


class LiquidationEvent:
    def __init__(self, coin: str, side: str, size_usd: float, timestamp: datetime):
        self.coin = coin
        self.side = side
        self.size_usd = size_usd
        self.timestamp = timestamp

    def __repr__(self) -> str:
        return (
            f"LiqEvent({self.coin} {self.side} "
            f"${self.size_usd:,.0f} @ {self.timestamp.strftime('%H:%M:%S')})"
        )


class LiquidationFeed:
    """Monitors liquidations and OI for all allowed perp coins via WebSocket."""

    def __init__(
        self,
        ws_url: str = "wss://api.hyperliquid.xyz/ws",
        coins: list[str] | None = None,
    ):
        self._ws_url = ws_url
        self._coins = coins or DEFAULT_COINS
        self._ws: websocket.WebSocketApp | None = None
        self._thread: threading.Thread | None = None
        self._running = False

        self._liq_events: dict[str, deque] = defaultdict(
            lambda: deque(maxlen=500)
        )
        self._squeeze_blocks: dict[str, datetime] = {}
        self._oi_snapshots: dict[str, float] = {}
        self._oi_previous: dict[str, float] = {}
        self._oi_last_update: datetime | None = None

        self._connected = False
        self._reconnect_delay = 5
        self._message_count = 0

        self._trade_callbacks: list = []

        logger.info(
            "LiquidationFeed initialized for %d coins: %s",
            len(self._coins), self._coins,
        )

    # ── Trade callback fan-out (non-breaking extension for CVDTracker) ──────

    def register_trade_callback(self, fn) -> None:
        """Register a callback to receive raw trade data from the WS.

        The callback receives a list[dict] of trade events.
        Errors in callbacks are caught and logged — they never affect
        liquidation or OI processing.
        """
        self._trade_callbacks.append(fn)
        logger.info("LiqFeed: registered trade callback (%d total)", len(self._trade_callbacks))

    def _fan_out_trades(self, trades) -> None:
        if not self._trade_callbacks:
            return
        items = trades if isinstance(trades, list) else [trades]
        for cb in self._trade_callbacks:
            try:
                cb(items)
            except Exception as e:
                logger.debug("Trade callback error: %s", e)

    # ── WebSocket lifecycle ───────────────────────────────────────────────────

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(
            target=self._run_loop,
            daemon=True,
            name="liq-feed",
        )
        self._thread.start()
        logger.info("Liquidation feed thread started")

    def stop(self) -> None:
        self._running = False
        if self._ws:
            self._ws.close()
        logger.info("Liquidation feed stopped")

    def _run_loop(self) -> None:
        while self._running:
            try:
                self._connect()
            except Exception as e:
                logger.warning(
                    "LiqFeed WS error: %s — reconnecting in %ds",
                    e, self._reconnect_delay,
                )
            if self._running:
                time.sleep(self._reconnect_delay)
                self._reconnect_delay = min(60, self._reconnect_delay * 2)

    def _connect(self) -> None:
        self._ws = websocket.WebSocketApp(
            self._ws_url,
            on_open=self._on_open,
            on_message=self._on_message,
            on_error=self._on_error,
            on_close=self._on_close,
        )
        self._ws.run_forever(ping_interval=30, ping_timeout=10)

    def _on_open(self, ws) -> None:
        self._connected = True
        self._reconnect_delay = 5
        logger.info("LiqFeed WS connected — subscribing to %d coins", len(self._coins))

        # Subscribe to liquidations feed (covers ALL coins globally)
        ws.send(json.dumps({
            "method": "subscribe",
            "subscription": {"type": "liquidations"},
        }))
        logger.info("Subscribed: liquidations (all coins)")

        # Subscribe to activeAssetCtx for each coin — provides OI + funding updates
        # Small delay between sends to avoid burst rejection
        import time as _time
        for coin in self._coins:
            ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "activeAssetCtx", "coin": coin},
            }))
            _time.sleep(0.05)
        logger.info("Subscribed: activeAssetCtx for %s", self._coins)

        # Subscribe to trades for each coin — used for volume/flow context
        for coin in self._coins:
            ws.send(json.dumps({
                "method": "subscribe",
                "subscription": {"type": "trades", "coin": coin},
            }))
            _time.sleep(0.05)
        logger.info("Subscribed: trades for %s", self._coins)

    def _on_message(self, ws, message: str) -> None:
        try:
            data = json.loads(message)
            self._message_count += 1
            channel = data.get("channel", "")

            if channel == "liquidations":
                self._handle_liquidation(data.get("data", {}))
            elif channel == "activeAssetCtx":
                self._handle_asset_ctx(data.get("data", {}))
            elif channel == "trades":
                self._fan_out_trades(data.get("data", []))

        except Exception as e:
            logger.debug("LiqFeed parse error: %s", e)

    def _on_error(self, ws, error) -> None:
        logger.warning("LiqFeed WS error: %s", error)
        self._connected = False

    def _on_close(self, ws, close_status_code, close_msg) -> None:
        logger.info("LiqFeed WS closed (code=%s)", close_status_code)
        self._connected = False

    # ── event handlers ────────────────────────────────────────────────────────

    def _handle_liquidation(self, data: dict | list) -> None:
        """Process liquidation events — handles both single and batch formats."""
        if isinstance(data, list):
            for item in data:
                self._process_single_liquidation(item)
        elif isinstance(data, dict):
            self._process_single_liquidation(data)

    def _process_single_liquidation(self, data: dict) -> None:
        try:
            coin = data.get("coin", "")
            if not coin:
                return

            side = data.get("side", "")
            size = float(data.get("sz", 0))
            px = float(data.get("px", 0))
            size_usd = size * px

            if size_usd < 100:
                return

            # HL side convention: "A" = ask side = short was liquidated
            liquidated_side = "short" if side == "A" else "long"

            event = LiquidationEvent(
                coin=coin,
                side=liquidated_side,
                size_usd=size_usd,
                timestamp=datetime.now(timezone.utc),
            )
            self._liq_events[coin].append(event)

            if size_usd > 10_000:
                logger.info(
                    "LIQUIDATION: %s %s $%.0f",
                    coin, liquidated_side, size_usd,
                )

            self._check_squeeze(coin)

        except Exception as e:
            logger.debug("Liquidation process error: %s", e)

    def _handle_asset_ctx(self, data: dict) -> None:
        """Process OI update from asset context subscription."""
        try:
            # HL sends: {"coin": "BTC", "ctx": {"funding": ..., "openInterest": ...}}
            coin = data.get("coin", "")
            ctx = data.get("ctx", {})
            if not coin or not ctx:
                # Try flat format
                coin = data.get("coin", "")
                oi = float(data.get("openInterest", 0))
            else:
                oi = float(ctx.get("openInterest", 0))

            if coin and oi > 0:
                if coin in self._oi_snapshots:
                    self._oi_previous[coin] = self._oi_snapshots[coin]
                self._oi_snapshots[coin] = oi
                self._oi_last_update = datetime.now(timezone.utc)

        except Exception as e:
            logger.debug("OI parse error: %s", e)

    def _check_squeeze(self, coin: str) -> None:
        """Detect short squeeze from accumulated liquidation events."""
        now = datetime.now(timezone.utc)
        events = self._liq_events.get(coin, deque())

        short_liq_usd = sum(
            e.size_usd for e in events
            if e.side == "short"
            and (now - e.timestamp).total_seconds() <= SQUEEZE_WINDOW_SECONDS
        )

        if short_liq_usd >= SQUEEZE_USD_THRESHOLD:
            unblock_time = now + timedelta(minutes=SQUEEZE_BLOCK_MINUTES)
            current_block = self._squeeze_blocks.get(coin)
            if not current_block or current_block < unblock_time:
                self._squeeze_blocks[coin] = unblock_time
                logger.warning(
                    "SQUEEZE DETECTED: %s — $%.0f short liqs in %ds. "
                    "Blocking new shorts until %s",
                    coin, short_liq_usd, SQUEEZE_WINDOW_SECONDS,
                    unblock_time.strftime("%H:%M UTC"),
                )

    # ── public query methods ──────────────────────────────────────────────────

    def is_squeeze_risk(self, coin: str) -> bool:
        """True if coin has active squeeze block."""
        block_until = self._squeeze_blocks.get(coin)
        if not block_until:
            return False
        now = datetime.now(timezone.utc)
        if now >= block_until:
            self._squeeze_blocks.pop(coin, None)
            logger.info("Squeeze block expired for %s", coin)
            return False
        return True

    def get_squeeze_remaining_minutes(self, coin: str) -> float:
        block_until = self._squeeze_blocks.get(coin)
        if not block_until:
            return 0.0
        remaining = (block_until - datetime.now(timezone.utc)).total_seconds()
        return max(0.0, remaining / 60)

    def get_oi_change_pct(self, coin: str) -> float | None:
        current = self._oi_snapshots.get(coin)
        previous = self._oi_previous.get(coin)
        if current is None or previous is None or previous <= 0:
            return None
        return (current - previous) / previous

    def get_oi_signal(self, coin: str) -> str:
        change = self.get_oi_change_pct(coin)
        if change is None:
            return "unknown"
        if change >= OI_STRONG_CONVICTION:
            return "strong"
        if change <= OI_WEAK_SIGNAL:
            return "weak"
        return "neutral"

    def get_recent_liquidations(self, coin: str, seconds: int = 300) -> dict:
        now = datetime.now(timezone.utc)
        events = [
            e for e in self._liq_events.get(coin, deque())
            if (now - e.timestamp).total_seconds() <= seconds
        ]
        long_liqs = sum(e.size_usd for e in events if e.side == "long")
        short_liqs = sum(e.size_usd for e in events if e.side == "short")
        return {
            "coin": coin,
            "window_seconds": seconds,
            "long_liquidated_usd": round(long_liqs, 2),
            "short_liquidated_usd": round(short_liqs, 2),
            "squeeze_risk": self.is_squeeze_risk(coin),
            "squeeze_remaining_min": round(self.get_squeeze_remaining_minutes(coin), 1),
        }

    def status(self) -> dict:
        return {
            "connected": self._connected,
            "subscribed_coins": self._coins,
            "messages_received": self._message_count,
            "active_squeeze_blocks": {
                coin: t.strftime("%H:%M UTC")
                for coin, t in self._squeeze_blocks.items()
            },
            "oi_tracked_coins": sorted(self._oi_snapshots.keys()),
            "oi_signals": {
                coin: self.get_oi_signal(coin)
                for coin in self._coins
            },
        }