import pytest
from regime.indicators import (
    compute_btc_return,
    compute_volatility,
    compute_funding_rate,
    compute_alt_btc_ratio,
)


def test_compute_btc_return_positive(mock_ohlcv_df):
    df = mock_ohlcv_df(10)
    result = compute_btc_return(df, 5)
    assert isinstance(result, float)
    assert result > 0


def test_compute_btc_return_negative(mock_ohlcv_df):
    df = mock_ohlcv_df(10)
    # Reverse prices to create downtrend
    df["close"] = df["close"].iloc[::-1].values
    result = compute_btc_return(df, 5)
    assert isinstance(result, float)
    assert result < 0


def test_compute_btc_return_insufficient_data(mock_ohlcv_df):
    df = mock_ohlcv_df(3)
    with pytest.raises(ValueError, match="DataFrame has 3 rows, need at least 5"):
        compute_btc_return(df, 5)


def test_compute_volatility_returns_float(mock_ohlcv_df):
    df = mock_ohlcv_df(20)
    result = compute_volatility(df, 10)
    assert isinstance(result, float)
    assert result >= 0


def test_compute_volatility_insufficient_data(mock_ohlcv_df):
    df = mock_ohlcv_df(3)
    with pytest.raises(ValueError, match="DataFrame has 3 rows, need at least 5"):
        compute_volatility(df, 5)


def test_compute_funding_rate_parses_correctly(mock_hl_client):
    result = compute_funding_rate("BTC", mock_hl_client)
    assert result == 0.0001
    mock_hl_client.info.funding_history.assert_called_once_with(coin="BTC", startTime=None, limit=1)


def test_compute_alt_btc_ratio_outperforming(mock_ohlcv_df):
    alt_df = mock_ohlcv_df(10)
    btc_df = mock_ohlcv_df(10)
    alt_df["close"] = [100 + i * 200 for i in range(10)]
    btc_df["close"] = [100 + i * 50 for i in range(10)]
    result = compute_alt_btc_ratio(alt_df, btc_df, 5)
    assert isinstance(result, float)
    assert result > 0


def test_compute_alt_btc_ratio_underperforming(mock_ohlcv_df):
    alt_df = mock_ohlcv_df(10)
    btc_df = mock_ohlcv_df(10)
    alt_df["close"] = [100 + i * 50 for i in range(10)]
    btc_df["close"] = [100 + i * 200 for i in range(10)]
    result = compute_alt_btc_ratio(alt_df, btc_df, 5)
    assert isinstance(result, float)
    assert result < 0


def test_compute_alt_btc_ratio_insufficient_alt_data(mock_ohlcv_df):
    alt_df = mock_ohlcv_df(3)
    btc_df = mock_ohlcv_df(10)
    with pytest.raises(ValueError, match="DataFrame has 3 rows, need at least 5"):
        compute_alt_btc_ratio(alt_df, btc_df, 5)


def test_compute_alt_btc_ratio_insufficient_btc_data(mock_ohlcv_df):
    alt_df = mock_ohlcv_df(10)
    btc_df = mock_ohlcv_df(3)
    with pytest.raises(ValueError, match="DataFrame has 3 rows, need at least 5"):
        compute_alt_btc_ratio(alt_df, btc_df, 5)