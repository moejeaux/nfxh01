from __future__ import annotations

import sys
from pathlib import Path

# Ensure `from src....` imports work in every environment (pytest.ini pythonpath can be ignored or
# behave differently across pytest / Python versions).
_root = Path(__file__).resolve().parent
_rp = str(_root)
if _rp not in sys.path:
    sys.path.insert(0, _rp)

import pytest
import pandas as pd
from unittest.mock import Mock


@pytest.fixture
def mock_ohlcv_df():
    def _create_df(n_bars: int, base_price: float = 80000, step: float = 100) -> pd.DataFrame:
        prices = [base_price + i * step for i in range(n_bars)]
        return pd.DataFrame({
            "open": prices,
            "high": [p + 50 for p in prices],
            "low": [p - 50 for p in prices],
            "close": prices,
            "volume": [1000] * n_bars,
        })
    return _create_df


@pytest.fixture
def mock_hl_client():
    client = Mock()
    client.info.funding_history.return_value = [
        {"fundingRate": "0.0001", "time": 123456789}
    ]
    return client
