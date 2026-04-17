"""Bounded reduction of ``fathom.acevault_max_mult`` for retrospective auto-apply."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_KEY = "acevault_max_mult"


def compute_fathom_acevault_max_mult_patch(
    first: dict[str, Any],
    fathom_sec: dict[str, Any],
    learn: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """
    Plan a single reduction of ``fathom.acevault_max_mult``.

    ``value`` is a multiplicative factor in (0, 1); omitted uses
    ``learning.fathom_acevault_max_mult_reduce_factor_default``. Result is floored by
    ``learning.fathom_acevault_max_mult_floor``. Never increases the cap; returns
    None if no effective reduction.
    """
    try:
        old_v = float(fathom_sec.get(_KEY, 1.0))
    except (TypeError, ValueError):
        return None

    floor_v = float(learn.get("fathom_acevault_max_mult_floor", 1.0))
    default_factor = float(learn.get("fathom_acevault_max_mult_reduce_factor_default", 0.9))

    if first.get("value") is None:
        factor = default_factor
    else:
        try:
            factor = float(first["value"])
        except (TypeError, ValueError):
            return None

    if not 0.0 < factor < 1.0:
        logger.info(
            "RETRO_APPLIER_SKIP reason=fathom_mult_factor_out_of_range factor=%s",
            factor,
        )
        return None

    proposed = old_v * factor
    new_v = max(floor_v, proposed)
    new_v = round(new_v, 4)

    if new_v >= old_v - 1e-9:
        logger.info(
            "RETRO_APPLIER_SKIP reason=fathom_mult_no_effective_reduction old=%.6f new=%.6f",
            old_v,
            new_v,
        )
        return None

    old_round = round(old_v, 4)
    return ({"fathom": {_KEY: old_round}}, {"fathom": {_KEY: new_v}})
