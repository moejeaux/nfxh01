"""Normalized L2 snapshot row schema for Parquet / DuckDB."""

from __future__ import annotations

from typing import TypedDict


class L2BookRow(TypedDict, total=False):
    """One price level on one side at one snapshot time."""

    event_time_ms: int
    wall_time_iso: str | None
    token: str
    side: str
    level: int
    price: float
    size: float
    order_count: int
    block_number: int | None
    s3_key: str
    source_provider: str
