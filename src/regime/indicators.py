from typing import Any

import pandas as pd


def compute_btc_return(df: pd.DataFrame, window_bars: int) -> float:
    if len(df) < window_bars:
        raise ValueError(f"DataFrame has {len(df)} rows, need at least {window_bars}")
    return float(df["close"].pct_change(window_bars).iloc[-1])


def compute_volatility(df: pd.DataFrame, window_bars: int) -> float:
    if len(df) < window_bars:
        raise ValueError(f"DataFrame has {len(df)} rows, need at least {window_bars}")
    return float(df["close"].pct_change().rolling(window_bars).std().iloc[-1])


def compute_funding_rate(coin: str, hl_client: Any) -> float:
    result = hl_client.info.funding_history(coin=coin, startTime=None, limit=1)
    return float(result[-1]["fundingRate"])


def compute_alt_btc_ratio(
    alt_df: pd.DataFrame, btc_df: pd.DataFrame, window_bars: int
) -> float:
    if len(alt_df) < window_bars:
        raise ValueError(f"DataFrame has {len(alt_df)} rows, need at least {window_bars}")
    if len(btc_df) < window_bars:
        raise ValueError(f"DataFrame has {len(btc_df)} rows, need at least {window_bars}")
    alt_return = float(alt_df["close"].pct_change(window_bars).iloc[-1])
    btc_return = float(btc_df["close"].pct_change(window_bars).iloc[-1])
    return alt_return - btc_return
