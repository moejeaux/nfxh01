"""Deterministic conflict resolution for normalized entry intents (Track A)."""

from __future__ import annotations

import logging
from typing import Literal

from src.nxfh01.orchestration.types import NormalizedEntryIntent

logger = logging.getLogger(__name__)

ConflictMode = Literal["skip_opposing", "priority"]


def apply_conflict_policy(
    intents: list[NormalizedEntryIntent],
    *,
    mode: ConflictMode,
    priority_order: list[str],
) -> tuple[list[NormalizedEntryIntent], list[str]]:
    """
    Same coin + opposing sides → drop all intents for that coin when mode is skip_opposing.

    ``priority_order`` lists ``strategy_key`` values (first wins per coin for same-side duplicates
    when mode is priority; opposing sides still skip in v1).
    """
    if not intents:
        return [], []

    by_coin: dict[str, list[NormalizedEntryIntent]] = {}
    for it in intents:
        k = it.coin.strip().upper()
        by_coin.setdefault(k, []).append(it)

    kept: list[NormalizedEntryIntent] = []
    notes: list[str] = []

    for coin, group in sorted(by_coin.items()):
        sides = {g.side for g in group}
        if len(sides) > 1:
            msg = (
                "ORCH_CONFLICT_SKIP coin=%s mode=%s opposing_sides=%s strategies=%s"
                % (
                    coin,
                    mode,
                    sorted(sides),
                    [g.strategy_key for g in group],
                )
            )
            logger.info(msg)
            notes.append(msg)
            continue

        if len(group) == 1:
            kept.append(group[0])
            continue

        # Same side, multiple strategies
        if mode == "skip_opposing":
            # Safe default: first in priority_order wins; if not listed, preserve input order
            ranked = sorted(
                group,
                key=lambda x: (
                    priority_order.index(x.strategy_key)
                    if x.strategy_key in priority_order
                    else len(priority_order)
                ),
            )
            winner = ranked[0]
            kept.append(winner)
            dropped = [x.strategy_key for x in ranked[1:]]
            msg = "ORCH_CONFLICT_SAME_SIDE coin=%s winner=%s dropped=%s" % (
                coin,
                winner.strategy_key,
                dropped,
            )
            logger.info(msg)
            notes.append(msg)
        else:
            ranked = sorted(
                group,
                key=lambda x: (
                    priority_order.index(x.strategy_key)
                    if x.strategy_key in priority_order
                    else len(priority_order)
                ),
            )
            kept.append(ranked[0])
            notes.append(
                "ORCH_PRIORITY_WIN coin=%s winner=%s"
                % (coin, ranked[0].strategy_key)
            )

    return kept, notes
