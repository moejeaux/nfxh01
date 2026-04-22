import src.regime.regime_metrics as rm


def setup_function() -> None:
    rm.reset()


def test_record_cycle_strict_counters_and_reasons() -> None:
    rm.reset()
    rm.record_cycle(
        legacy_ranging=True,
        final_ranging=True,
        strict_passed=True,
        strict_evaluated=True,
        strict_ranging_pass=True,
        fail_reasons=[],
    )
    s = rm.snapshot()
    assert s["cycles_legacy_ranging_true"] == 1
    assert s["cycles_strict_ranging_evaluated"] == 1
    assert s["cycles_strict_ranging_passed"] == 1
    assert s["cycles_legacy_true_strict_false"] == 0
    assert s["strict_fail_reason_counts"] == {}
    assert s["cycles_final_ranging_and_strict_passed"] == 1


def test_record_cycle_legacy_strict_mismatch_increments_reasons() -> None:
    rm.reset()
    rm.record_cycle(
        legacy_ranging=True,
        final_ranging=True,
        strict_passed=False,
        strict_evaluated=True,
        strict_ranging_pass=False,
        fail_reasons=["htf_slope_too_high", "insufficient_edge_bounces"],
    )
    s = rm.snapshot()
    assert s["cycles_legacy_true_strict_false"] == 1
    assert s["cycles_strict_ranging_passed"] == 0
    assert s["strict_fail_reason_counts"]["htf_slope_too_high"] == 1
    assert s["strict_fail_reason_counts"]["insufficient_edge_bounces"] == 1


def test_final_ranging_strict_passed_independent_of_strict_eval() -> None:
    rm.reset()
    rm.record_cycle(
        legacy_ranging=False,
        final_ranging=True,
        strict_passed=True,
        strict_evaluated=False,
        strict_ranging_pass=True,
        fail_reasons=[],
    )
    s = rm.snapshot()
    assert s["cycles_final_ranging_and_strict_passed"] == 1
