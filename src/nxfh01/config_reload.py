"""Merge on-disk config snapshots into the live process root dict (hot-reload without restart)."""

from __future__ import annotations

import copy
import logging
from typing import Any

from src.nxfh01.orchestration.config_validation import validate_multi_strategy_config

logger = logging.getLogger(__name__)


def merge_into_live_config(live_root: dict[str, Any], snapshot: dict[str, Any]) -> None:
    """
    Recursively merge ``snapshot`` into ``live_root`` in place.

    Preserves the identity of ``live_root`` so components that hold a reference
    to the same dict (engines, risk, kill switch) see updates. Nested dicts are
    merged; scalars and lists are replaced. Keys present only in ``live_root`` are
    kept (not removed).
    """
    for k, v in snapshot.items():
        if (
            k in live_root
            and isinstance(live_root[k], dict)
            and isinstance(v, dict)
        ):
            merge_into_live_config(live_root[k], v)
        else:
            live_root[k] = v


def validate_merge_preview(
    live_root: dict[str, Any], snapshot: dict[str, Any]
) -> dict[str, Any]:
    """Return a deep-copied merge of live + snapshot and run orchestration validation."""
    preview = copy.deepcopy(live_root)
    merge_into_live_config(preview, snapshot)
    validate_multi_strategy_config(preview)
    return preview
