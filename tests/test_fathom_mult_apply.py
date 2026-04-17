"""Tests for bounded fathom.acevault_max_mult reduction planning."""

from __future__ import annotations

import pytest

from src.retro.fathom_mult_apply import compute_fathom_acevault_max_mult_patch


def test_reduce_success_default_factor():
    learn = {
        "fathom_acevault_max_mult_floor": 1.0,
        "fathom_acevault_max_mult_reduce_factor_default": 0.9,
    }
    first = {"action": "reduce_fathom_acevault_max_mult", "target": "acevault", "value": None}
    fh = {"acevault_max_mult": 1.28}
    out = compute_fathom_acevault_max_mult_patch(first, fh, learn)
    assert out is not None
    old_s, new_s = out
    assert old_s == {"fathom": {"acevault_max_mult": 1.28}}
    assert new_s["fathom"]["acevault_max_mult"] == pytest.approx(1.152, rel=1e-6)


def test_reduce_explicit_factor():
    learn = {
        "fathom_acevault_max_mult_floor": 1.0,
        "fathom_acevault_max_mult_reduce_factor_default": 0.9,
    }
    first = {
        "action": "reduce_fathom_acevault_max_mult",
        "target": "acevault",
        "value": 0.8,
    }
    fh = {"acevault_max_mult": 1.25}
    out = compute_fathom_acevault_max_mult_patch(first, fh, learn)
    assert out is not None
    assert out[1]["fathom"]["acevault_max_mult"] == pytest.approx(1.0, rel=1e-6)


def test_never_increase_rejects_factor_one():
    learn = {
        "fathom_acevault_max_mult_floor": 1.0,
        "fathom_acevault_max_mult_reduce_factor_default": 0.9,
    }
    first = {"action": "reduce_fathom_acevault_max_mult", "value": 1.0}
    fh = {"acevault_max_mult": 1.2}
    assert compute_fathom_acevault_max_mult_patch(first, fh, learn) is None


def test_never_increase_rejects_factor_above_one():
    learn = {
        "fathom_acevault_max_mult_floor": 1.0,
        "fathom_acevault_max_mult_reduce_factor_default": 0.9,
    }
    first = {"action": "reduce_fathom_acevault_max_mult", "value": 1.1}
    fh = {"acevault_max_mult": 1.2}
    assert compute_fathom_acevault_max_mult_patch(first, fh, learn) is None


def test_floor_prevents_below_configured_minimum():
    learn = {
        "fathom_acevault_max_mult_floor": 1.1,
        "fathom_acevault_max_mult_reduce_factor_default": 0.5,
    }
    first = {"action": "reduce_fathom_acevault_max_mult", "value": 0.5}
    fh = {"acevault_max_mult": 1.2}
    out = compute_fathom_acevault_max_mult_patch(first, fh, learn)
    assert out is not None
    assert out[1]["fathom"]["acevault_max_mult"] == 1.1


def test_no_effective_reduction_when_already_at_floor():
    learn = {
        "fathom_acevault_max_mult_floor": 1.0,
        "fathom_acevault_max_mult_reduce_factor_default": 0.9,
    }
    first = {"action": "reduce_fathom_acevault_max_mult", "value": 0.95}
    fh = {"acevault_max_mult": 1.0}
    assert compute_fathom_acevault_max_mult_patch(first, fh, learn) is None


def test_invalid_factor_zero():
    learn = {"fathom_acevault_max_mult_floor": 1.0, "fathom_acevault_max_mult_reduce_factor_default": 0.9}
    first = {"action": "reduce_fathom_acevault_max_mult", "value": 0.0}
    fh = {"acevault_max_mult": 1.2}
    assert compute_fathom_acevault_max_mult_patch(first, fh, learn) is None
