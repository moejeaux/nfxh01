import logging

import pytest
from unittest.mock import Mock, patch
import pandas as pd

from src.engines.acevault.scanner import AltScanner, EXCLUDED_COINS
from src.engines.acevault.models import AltCandidate


@pytest.fixture
def mock_config():
    return {
        "acevault": {
            "max_candidates": 3
        }
    }


@pytest.fixture
def mock_hl_client():
    client = Mock()
    client.all_mids.return_value = {
        "BTC": "80000",
        "ETH": "3000", 
        "SOL": "150",
        "DOGE": "0.15",
        "PEPE": "0.000008",
        "WIF": "2.50"
    }
    
    def candles_side_effect(coin, interval, startTime, endTime):
        base_prices = {
            "BTC": 80000,
            "DOGE": 0.15,
            "PEPE": 0.000008,
            "WIF": 2.50,
            "ETH": 3000,
            "SOL": 150
        }
        base = base_prices.get(coin, 1.0)
        
        candles = []
        for i in range(24):
            price = base * (1 + (i - 12) * 0.001)
            volume = 1000 + i * 10
            candles.append({
                "o": str(price * 0.999),
                "h": str(price * 1.002),
                "l": str(price * 0.998),
                "c": str(price),
                "v": str(volume)
            })
        return candles
    
    client.candles_snapshot.side_effect = candles_side_effect
    return client


def test_scan_excludes_btc_eth_sol(mock_config, mock_hl_client):
    scanner = AltScanner(mock_config, mock_hl_client)
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', return_value=-0.05), \
         patch('src.engines.acevault.scanner.compute_volatility', return_value=0.02):
        
        results = scanner.scan()
        
        result_coins = {candidate.coin for candidate in results}
        assert not (result_coins & EXCLUDED_COINS), f"Found excluded coins: {result_coins & EXCLUDED_COINS}"


def test_scan_returns_altcandidates(mock_config, mock_hl_client):
    scanner = AltScanner(mock_config, mock_hl_client)
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', return_value=-0.05), \
         patch('src.engines.acevault.scanner.compute_volatility', return_value=0.02):
        
        results = scanner.scan()
        
        assert all(isinstance(candidate, AltCandidate) for candidate in results)
        assert len(results) > 0


def test_scan_sorted_by_weakness(mock_config, mock_hl_client):
    scanner = AltScanner(mock_config, mock_hl_client)
    
    # Mock different weakness scores for different coins
    def mock_alt_btc_ratio(alt_df, btc_df, window_bars):
        coin_scores = {"DOGE": -0.01, "PEPE": -0.08, "WIF": -0.05}
        # Infer coin from DataFrame (crude but works for test)
        close_price = float(alt_df["close"].iloc[-1])
        if 0.14 < close_price < 0.16:
            return coin_scores["DOGE"]
        elif close_price < 0.00001:
            return coin_scores["PEPE"] 
        else:
            return coin_scores["WIF"]
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', side_effect=mock_alt_btc_ratio), \
         patch('src.engines.acevault.scanner.compute_volatility', return_value=0.02):
        
        results = scanner.scan()
        
        # Should be sorted by weakness_score descending
        weakness_scores = [candidate.weakness_score for candidate in results]
        assert weakness_scores == sorted(weakness_scores, reverse=True)


def test_scan_respects_max_candidates(mock_config, mock_hl_client):
    scanner = AltScanner(mock_config, mock_hl_client)
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', return_value=-0.05), \
         patch('src.engines.acevault.scanner.compute_volatility', return_value=0.02):
        
        results = scanner.scan()
        
        assert len(results) <= mock_config["acevault"]["max_candidates"]


def test_scan_handles_api_failure(mock_config, mock_hl_client):
    scanner = AltScanner(mock_config, mock_hl_client)
    
    # Make candles_snapshot fail for DOGE but work for others
    def failing_candles(coin, interval, startTime, endTime):
        if coin == "DOGE":
            raise Exception("API failure")
        base = 1.0
        candles = []
        for i in range(24):
            price = base * (1 + i * 0.001)
            candles.append({
                "o": str(price), "h": str(price), 
                "l": str(price), "c": str(price),
                "v": "1000"
            })
        return candles
    
    mock_hl_client.candles_snapshot.side_effect = failing_candles
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', return_value=-0.05), \
         patch('src.engines.acevault.scanner.compute_volatility', return_value=0.02):
        
        results = scanner.scan()
        
        # Should not crash and DOGE should be skipped
        result_coins = {candidate.coin for candidate in results}
        assert "DOGE" not in result_coins


def test_scan_empty_market(mock_config, mock_hl_client, caplog):
    # Mock all_mids to return only excluded coins
    mock_hl_client.all_mids.return_value = {
        "BTC": "80000",
        "ETH": "3000", 
        "SOL": "150"
    }
    
    scanner = AltScanner(mock_config, mock_hl_client)
    
    results = scanner.scan()
    
    assert results == []
    assert "ACEVAULT_NO_CANDIDATES" in caplog.text


def test_weakness_score_computed(mock_config, mock_hl_client):
    scanner = AltScanner(mock_config, mock_hl_client)
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', return_value=-0.05), \
         patch('src.engines.acevault.scanner.compute_volatility', return_value=0.02):
        
        results = scanner.scan()
        
        for candidate in results:
            assert candidate.weakness_score is not None
            assert isinstance(candidate.weakness_score, float)


def test_volume_ratio_zero_mean_handled(mock_config, mock_hl_client):
    scanner = AltScanner(mock_config, mock_hl_client)
    
    # Mock candles with zero volume to create zero mean
    def zero_volume_candles(coin, interval, startTime, endTime):
        candles = []
        for i in range(24):
            candles.append({
                "o": "1.0", "h": "1.0", 
                "l": "1.0", "c": "1.0",
                "v": "0"
            })
        return candles
    
    mock_hl_client.candles_snapshot.side_effect = zero_volume_candles
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', return_value=-0.05), \
         patch('src.engines.acevault.scanner.compute_volatility', return_value=0.02):
        
        results = scanner.scan()
        
        # Should handle zero volume mean gracefully
        for candidate in results:
            assert candidate.volume_ratio == 1.0


def test_insufficient_candle_data_skipped(mock_config, mock_hl_client, caplog):
    caplog.set_level(logging.DEBUG)
    scanner = AltScanner(mock_config, mock_hl_client)
    
    # Mock candles_snapshot to return insufficient data for DOGE
    def insufficient_candles(coin, interval, startTime, endTime):
        if coin == "DOGE":
            return [{"o": "1", "h": "1", "l": "1", "c": "1", "v": "1000"}]
        candles = []
        for i in range(24):
            candles.append({
                "o": "1.0", "h": "1.0", 
                "l": "1.0", "c": "1.0",
                "v": "1000"
            })
        return candles
    
    mock_hl_client.candles_snapshot.side_effect = insufficient_candles
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', return_value=-0.05), \
         patch('src.engines.acevault.scanner.compute_volatility', return_value=0.02):
        
        results = scanner.scan()
        
        result_coins = {candidate.coin for candidate in results}
        assert "DOGE" not in result_coins
        assert "ACEVAULT_SCANNER_DATA_MISSING coin=DOGE" in caplog.text


def test_zero_volatility_skipped(mock_config, mock_hl_client):
    scanner = AltScanner(mock_config, mock_hl_client)
    
    # Mock compute_volatility to return 0 for some coins
    def mock_volatility(df, window_bars):
        close_price = float(df["close"].iloc[-1])
        if 0.14 < close_price < 0.16:  # DOGE
            return 0.0  # Zero volatility
        return 0.02
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', return_value=-0.05), \
         patch('src.engines.acevault.scanner.compute_volatility', side_effect=mock_volatility):
        
        results = scanner.scan()
        
        result_coins = {candidate.coin for candidate in results}
        assert "DOGE" not in result_coins


def test_indicators_exception_handling(mock_config, mock_hl_client, caplog):
    caplog.set_level(logging.DEBUG)
    scanner = AltScanner(mock_config, mock_hl_client)
    
    # Mock compute_alt_btc_ratio to raise ValueError for DOGE
    def failing_ratio(alt_df, btc_df, window_bars):
        close_price = float(alt_df["close"].iloc[-1])
        if 0.14 < close_price < 0.16:  # DOGE
            raise ValueError("Insufficient data")
        return -0.05
    
    with patch('src.engines.acevault.scanner.compute_alt_btc_ratio', side_effect=failing_ratio), \
         patch('src.engines.acevault.scanner.compute_volatility', return_value=0.02):
        
        results = scanner.scan()
        
        result_coins = {candidate.coin for candidate in results}
        assert "DOGE" not in result_coins
        assert "ACEVAULT_SCANNER_DATA_MISSING coin=DOGE" in caplog.text