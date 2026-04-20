"""Parse Hyperliquid L2 archive JSON lines into normalized rows."""

from __future__ import annotations

import json
import logging
from datetime import datetime
from typing import Any

from src.data_ingestion.hyperliquid_l2.schema import L2BookRow

logger = logging.getLogger(__name__)


def _f_px_sz(order: dict[str, Any]) -> tuple[float, float, int]:
    px = float(order.get("px", "0") or 0)
    sz = float(order.get("sz", "0") or 0)
    n = int(order.get("n", 0) or 0)
    return px, sz, n


def _rows_for_side(
    *,
    orders: list[dict[str, Any]] | None,
    side: str,
    token: str,
    event_time_ms: int,
    wall_time_iso: str | None,
    block_number: int | None,
    s3_key: str,
    source_provider: str,
) -> list[L2BookRow]:
    out: list[L2BookRow] = []
    if not orders:
        return out
    for level_idx, order in enumerate(orders, start=1):
        if not isinstance(order, dict):
            continue
        px, sz, n = _f_px_sz(order)
        out.append(
            {
                "event_time_ms": event_time_ms,
                "wall_time_iso": wall_time_iso,
                "token": token,
                "side": side,
                "level": level_idx,
                "price": px,
                "size": sz,
                "order_count": n,
                "block_number": block_number,
                "s3_key": s3_key,
                "source_provider": source_provider,
            }
        )
    return out


def _extract_inner(obj: dict[str, Any]) -> tuple[dict[str, Any] | None, str | None]:
    """Return ``(inner_book_dict, wall_time_iso)`` if recognizable."""
    wall: str | None = None
    t = obj.get("time")
    if isinstance(t, str):
        wall = t
    raw = obj.get("raw")
    if isinstance(raw, dict):
        data = raw.get("data")
        if isinstance(data, dict):
            return data, wall
    return None, wall


def parse_l2_archive_line(
    line: str,
    *,
    token: str,
    s3_key: str,
    source_provider: str = "hyperliquid_archive",
) -> list[L2BookRow]:
    """Parse one decompressed line into zero or more ``L2BookRow`` dicts."""
    try:
        obj: dict[str, Any] = json.loads(line)
    except json.JSONDecodeError as e:
        logger.warning("HL_L2_PARSE_JSON_SKIP key=%s err=%s", s3_key, e)
        return []

    inner, wall = _extract_inner(obj)
    candidates: list[dict[str, Any]] = []
    if inner is not None:
        candidates.append(inner)
    candidates.append(obj)

    book: dict[str, Any] | None = None
    for c in candidates:
        if isinstance(c, dict) and "bids" in c and "asks" in c:
            book = c
            break

    if book is not None:
        t_raw = book.get("time")
        if isinstance(t_raw, (int, float)):
            event_time_ms = int(t_raw)
        elif isinstance(obj.get("time"), (int, float)):
            event_time_ms = int(obj["time"])
        elif isinstance(wall, str) and wall:
            try:
                dt = datetime.fromisoformat(wall.replace("Z", "+00:00"))
                event_time_ms = int(dt.timestamp() * 1000)
            except ValueError:
                event_time_ms = 0
        else:
            event_time_ms = 0
        coin = str(book.get("coin") or token)
        block = book.get("block_number")
        bn = int(block) if isinstance(block, (int, float)) else None
        rows: list[L2BookRow] = []
        rows.extend(
            _rows_for_side(
                orders=book.get("bids") if isinstance(book.get("bids"), list) else None,
                side="bid",
                token=coin,
                event_time_ms=event_time_ms,
                wall_time_iso=wall,
                block_number=bn,
                s3_key=s3_key,
                source_provider=source_provider,
            )
        )
        rows.extend(
            _rows_for_side(
                orders=book.get("asks") if isinstance(book.get("asks"), list) else None,
                side="ask",
                token=coin,
                event_time_ms=event_time_ms,
                wall_time_iso=wall,
                block_number=bn,
                s3_key=s3_key,
                source_provider=source_provider,
            )
        )
        return rows

    data = inner if inner is not None else obj
    levels = data.get("levels") if isinstance(data, dict) else None
    if isinstance(levels, list) and levels:
        t_raw = data.get("time") if isinstance(data, dict) else None
        if not isinstance(t_raw, (int, float)):
            t_raw = obj.get("time") if isinstance(obj.get("time"), (int, float)) else 0
        event_time_ms = int(t_raw) if isinstance(t_raw, (int, float)) else 0
        if len(levels) == 2 and all(isinstance(x, list) for x in levels):
            bids_side, asks_side = levels[0], levels[1]
            rows = []
            rows.extend(
                _rows_for_side(
                    orders=bids_side,
                    side="bid",
                    token=token,
                    event_time_ms=event_time_ms,
                    wall_time_iso=wall,
                    block_number=None,
                    s3_key=s3_key,
                    source_provider=source_provider,
                )
            )
            rows.extend(
                _rows_for_side(
                    orders=asks_side,
                    side="ask",
                    token=token,
                    event_time_ms=event_time_ms,
                    wall_time_iso=wall,
                    block_number=None,
                    s3_key=s3_key,
                    source_provider=source_provider,
                )
            )
            return rows

        out_legacy: list[L2BookRow] = []
        for i, order_level in enumerate(levels):
            if not isinstance(order_level, list):
                continue
            for order in order_level:
                if not isinstance(order, dict) or "px" not in order:
                    continue
                px, sz, n = _f_px_sz(order)
                out_legacy.append(
                    {
                        "event_time_ms": event_time_ms,
                        "wall_time_iso": wall,
                        "token": token,
                        "side": "unknown",
                        "level": i + 1,
                        "price": px,
                        "size": sz,
                        "order_count": n,
                        "block_number": None,
                        "s3_key": s3_key,
                        "source_provider": source_provider,
                    }
                )
        if out_legacy:
            logger.debug(
                "HL_L2_PARSE_LEGACY_LEVELS key=%s rows=%d (side=unknown)",
                s3_key,
                len(out_legacy),
            )
        return out_legacy

    logger.debug("HL_L2_PARSE_EMPTY key=%s", s3_key)
    return []
