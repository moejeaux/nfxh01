from src.regime.btc.detector import BTCRegimeDetector
from src.regime.btc.models import BTCPrimaryRegime


def _candle(c: float, h: float | None = None, l: float | None = None) -> dict:
    hh = h if h is not None else c * 1.001
    ll = l if l is not None else c * 0.999
    return {"o": c, "h": hh, "l": ll, "c": c, "v": 1.0}


def _bundle(n: int, drift: float) -> dict:
    base = 100.0
    c5 = []
    for i in range(n):
        px = base + i * drift
        c5.append(_candle(px))
    c15 = c5[::3][: max(20, n // 3)]
    c1h = c5[::12][: max(20, n // 12)]
    if len(c15) < 20:
        c15 = [_candle(base + j * drift) for j in range(48)]
    if len(c1h) < 60:
        c1h = [_candle(base + j * drift * 0.1) for j in range(60)]
    return {"candles": {"5m": c5, "15m": c15, "1h": c1h}}


def test_detector_returns_state():
    cfg = {
        "btc_strategy": {
            "thresholds": {
                "hysteresis_ticks": 2,
                "swing_bars": 3,
                "structure_bars_5m": 10,
            }
        }
    }
    det = BTCRegimeDetector(cfg)
    md = _bundle(80, 0.15)
    st = det.detect(md)
    assert st.primary_regime in BTCPrimaryRegime
    assert 0.0 <= st.confidence <= 1.0
    assert isinstance(st.trend_session_id, int)


def test_insufficient_candles_fallback():
    cfg = {"btc_strategy": {}}
    det = BTCRegimeDetector(cfg)
    st = det.detect({"candles": {}})
    assert st.confidence == 0.0
