import logging
import time
from datetime import datetime, timezone
from typing import Any

import pandas as pd

from src.engines.acevault.models import AltCandidate
from src.regime.indicators import compute_alt_btc_ratio, compute_volatility

logger = logging.getLogger(__name__)

EXCLUDED_COINS = frozenset({"BTC", "ETH", "SOL"})


class AltScanner:
    def __init__(self, config: dict, hl_client: Any) -> None:
        self.config = config
        self.hl_client = hl_client
        self._btc_df: pd.DataFrame | None = None

    def scan(self) -> list[AltCandidate]:
        try:
            markets = self._fetch_all_markets()
        except Exception as e:
            logger.error(
                "ACEVAULT_SCAN_FAILED reason=api_error error=%s", str(e)
            )
            return []

        self._btc_df = self._fetch_candles("BTC")

        skipped = sum(1 for c in markets if "/" in c or c.startswith("@"))
        scannable = len(markets) - len(EXCLUDED_COINS & set(markets)) - skipped
        logger.info("ACEVAULT_SCAN_FILTERING skipped_spot=%d skipped_index=%d scannable=%d",
                     sum(1 for c in markets if "/" in c),
                     sum(1 for c in markets if c.startswith("@")),
                     scannable)

        candidates: list[AltCandidate] = []
        for coin in markets:
            if coin in EXCLUDED_COINS:
                continue
            if "/" in coin or coin.startswith("@"):
                continue
            try:
                result = self._compute_weakness_score(coin, markets)
            except Exception as e:
                logger.error(
                    "ACEVAULT_SCAN_FAILED reason=api_error error=%s", str(e)
                )
                continue
            if result is not None:
                candidates.append(result)

        if not candidates:
            logger.warning("ACEVAULT_NO_CANDIDATES")
            return []

        candidates.sort(key=lambda c: c.weakness_score, reverse=True)
        max_candidates = self.config["acevault"]["max_candidates"]
        top = candidates[:max_candidates]

        logger.info(
            "ACEVAULT_SCAN_COMPLETE candidates=%d top_coin=%s top_score=%.3f",
            len(top),
            top[0].coin,
            top[0].weakness_score,
        )
        return top

    def _fetch_all_markets(self) -> dict[str, float]:
        raw = self.hl_client.all_mids()
        result = {coin: float(price) for coin, price in raw.items()}
        excluded_count = sum(1 for c in result if c in EXCLUDED_COINS)
        logger.info(
            "ACEVAULT_SCAN_MARKETS total=%d excluded=%d",
            len(result),
            excluded_count,
        )
        return result

    def _fetch_candles(self, coin: str) -> pd.DataFrame | None:
        try:
            now_ms = int(time.time() * 1000)
            two_hours_ago_ms = now_ms - (2 * 60 * 60 * 1000)
            raw = self.hl_client.candles_snapshot(
                coin, "5m", two_hours_ago_ms, now_ms
            )
            if len(raw) < 24:
                return None
            df = pd.DataFrame(raw)
            col_map = {"o": "open", "h": "high", "l": "low", "c": "close", "v": "volume"}
            df.rename(columns=col_map, inplace=True)
            for col in ("open", "high", "low", "close", "volume"):
                if col in df.columns:
                    df[col] = pd.to_numeric(df[col], errors="coerce")
            return df
        except Exception as exc:
            logger.debug("ACEVAULT_CANDLE_FETCH_FAIL coin=%s error=%s", coin, exc)
            return None

    def _compute_weakness_score(
        self, coin: str, price_data: dict
    ) -> AltCandidate | None:
        alt_df = self._fetch_candles(coin)
        if alt_df is None or len(alt_df) < 24:
            logger.debug("ACEVAULT_SCANNER_DATA_MISSING coin=%s", coin)
            return None

        if self._btc_df is None or len(self._btc_df) < 24:
            logger.debug("ACEVAULT_SCANNER_DATA_MISSING coin=%s reason=btc_data", coin)
            return None

        try:
            relative_strength_1h = compute_alt_btc_ratio(
                alt_df, self._btc_df, window_bars=12
            )
        except (ValueError, ZeroDivisionError):
            logger.debug("ACEVAULT_SCANNER_DATA_MISSING coin=%s reason=ratio_calc", coin)
            return None

        try:
            vol_1h = compute_volatility(alt_df, window_bars=12)
        except ValueError:
            logger.debug("ACEVAULT_SCANNER_DATA_MISSING coin=%s reason=vol_calc", coin)
            return None

        if vol_1h == 0:
            return None

        pct_change_15m = float(alt_df["close"].pct_change(3).iloc[-1])
        momentum_score = pct_change_15m / vol_1h

        vol_mean = float(alt_df["volume"].rolling(20).mean().iloc[-1])
        if vol_mean == 0:
            volume_ratio = 1.0
        else:
            volume_ratio = float(alt_df["volume"].iloc[-1]) / vol_mean

        weakness_score = (-relative_strength_1h) + (-momentum_score)

        return AltCandidate(
            coin=coin,
            weakness_score=weakness_score,
            relative_strength_1h=relative_strength_1h,
            momentum_score=momentum_score,
            volume_ratio=volume_ratio,
            current_price=price_data.get(coin, 0.0),
            timestamp=datetime.now(timezone.utc),
        )
