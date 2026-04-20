"""Build historical ranker feature datasets from archived Hyperliquid context."""

from __future__ import annotations

import gzip
import json
import logging
from bisect import bisect_right
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Protocol

from src.market_context.hl_meta_snapshot import parse_meta_and_asset_ctxs

logger = logging.getLogger(__name__)


class SnapshotAdapter(Protocol):
    """Adapter contract for vendor replay sources (Tardis, etc.)."""

    def iter_snapshots(self) -> list[tuple[datetime | None, Any]]:
        ...


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, (int, float)):
        # Accept either sec or ms.
        if value > 10_000_000_000:
            return datetime.fromtimestamp(float(value) / 1000.0, tz=timezone.utc)
        return datetime.fromtimestamp(float(value), tz=timezone.utc)
    if isinstance(value, str) and value.strip():
        raw = value.strip().replace("Z", "+00:00")
        try:
            ts = datetime.fromisoformat(raw)
        except ValueError:
            return None
        return ts if ts.tzinfo else ts.replace(tzinfo=timezone.utc)
    return None


def _extract_snapshot(payload: Any) -> tuple[datetime | None, Any]:
    if isinstance(payload, dict):
        for key in ("metaAndAssetCtxs", "asset_ctxs", "payload", "data"):
            if key in payload:
                ts = _parse_ts(
                    payload.get("timestamp")
                    or payload.get("ts")
                    or payload.get("time")
                    or payload.get("collected_at")
                )
                return ts, payload.get(key)
    return None, payload


def _iter_lines(path: Path) -> list[Any]:
    opener = gzip.open if path.suffix == ".gz" else open
    out: list[Any] = []
    with opener(path, "rt", encoding="utf-8") as f:
        for line in f:
            s = line.strip()
            if not s:
                continue
            out.append(json.loads(s))
    return out


def _load_snapshots(path: Path) -> list[tuple[datetime | None, Any]]:
    if path.suffixes[-2:] == [".jsonl", ".gz"] or path.suffix == ".jsonl":
        rows = _iter_lines(path)
        out = [_extract_snapshot(row) for row in rows]
        return out
    opener = gzip.open if path.suffix == ".gz" else open
    with opener(path, "rt", encoding="utf-8") as f:
        payload = json.load(f)
    if isinstance(payload, list) and payload and isinstance(payload[0], dict):
        return [_extract_snapshot(row) for row in payload]
    return [_extract_snapshot(payload)]


def _build_regime_index(
    candle_rows: list[dict[str, Any]],
) -> dict[str, tuple[list[datetime], list[dict[str, Any]]]]:
    by_sym: dict[str, list[dict[str, Any]]] = {}
    for row in candle_rows:
        sym = str(row.get("symbol") or row.get("coin") or "").strip().upper()
        ts = _parse_ts(row.get("timestamp") or row.get("ts") or row.get("time"))
        if not sym or ts is None:
            continue
        z = dict(row)
        z["_ts"] = ts
        by_sym.setdefault(sym, []).append(z)
    index: dict[str, tuple[list[datetime], list[dict[str, Any]]]] = {}
    for sym, rows in by_sym.items():
        rows.sort(key=lambda r: r["_ts"])
        index[sym] = ([r["_ts"] for r in rows], rows)
    return index


def _closest_regime(
    sym: str,
    ts: datetime | None,
    regime_index: dict[str, tuple[list[datetime], list[dict[str, Any]]]],
) -> dict[str, Any]:
    if ts is None:
        return {}
    slot = regime_index.get(sym)
    if slot is None:
        return {}
    tss, rows = slot
    i = bisect_right(tss, ts) - 1
    if i < 0:
        return {}
    row = rows[i]
    return {
        "regime_value": row.get("regime_value") or row.get("regime") or "unknown",
        "regime_source_ts": row["_ts"].isoformat(),
    }


def build_historical_ranker_dataset(
    archive_dir: str | Path,
    *,
    candle_rows: list[dict[str, Any]] | None = None,
    snapshot_adapter: SnapshotAdapter | None = None,
) -> list[dict[str, Any]]:
    """Normalize archived ``asset_ctxs`` into canonical ranker feature rows."""
    regime_index = _build_regime_index(candle_rows or [])
    out: list[dict[str, Any]] = []
    parse_errors = 0
    invalid_snapshots = 0
    if snapshot_adapter is not None:
        snapshots_with_source = [("adapter", ts, snap) for ts, snap in snapshot_adapter.iter_snapshots()]
        files_count = 0
    else:
        root = Path(archive_dir)
        if not root.is_dir():
            raise ValueError(f"archive_dir not found: {root}")
        files = sorted(
            [
                *root.rglob("*.json"),
                *root.rglob("*.jsonl"),
                *root.rglob("*.json.gz"),
                *root.rglob("*.jsonl.gz"),
            ]
        )
        if not files:
            raise ValueError(f"no archive files found under {root}")
        snapshots_with_source: list[tuple[str, datetime | None, Any]] = []
        for fp in files:
            try:
                snapshots = _load_snapshots(fp)
            except Exception as e:
                parse_errors += 1
                logger.warning("RISK_RESEARCH_ARCHIVE_READ_FAIL file=%s error=%s", fp, e)
                continue
            for ts, snap in snapshots:
                snapshots_with_source.append((str(fp), ts, snap))
        files_count = len(files)

    for src, ts, snap in snapshots_with_source:
        ok, by_upper, err = parse_meta_and_asset_ctxs(snap)
        if not ok:
            invalid_snapshots += 1
            logger.debug(
                "RISK_RESEARCH_ARCHIVE_SNAPSHOT_INVALID source=%s reason=%s",
                src,
                err or "unknown",
            )
            continue
        if ts is None:
            use_ts = datetime.now(timezone.utc)
        else:
            use_ts = ts
        for sym, row in by_upper.items():
            regime_blob = _closest_regime(sym, use_ts, regime_index)
            out.append(
                {
                    "timestamp": use_ts.isoformat(),
                    "symbol": sym,
                    "asset_max_leverage": int(row.max_leverage),
                    "only_isolated": bool(row.only_isolated),
                    "day_ntl_vlm": float(row.day_ntl_vlm),
                    "open_interest": float(row.open_interest),
                    "mid_px": row.mid_px,
                    "mark_px": row.mark_px,
                    "oracle_px": row.oracle_px,
                    "impact_pxs": list(row.impact_pxs),
                    "funding": float(row.funding),
                    "premium": row.premium,
                    "prev_day_px": row.prev_day_px,
                    "raw_asset_ctx": dict(row.raw_asset_ctx),
                    "raw_universe_row": dict(row.raw_universe_row),
                    **regime_blob,
                }
            )

    if not out:
        raise ValueError(
            "no usable historical rows parsed from archives; "
            f"files={files_count} parse_errors={parse_errors} invalid_snapshots={invalid_snapshots}"
        )
    logger.info(
        "RISK_RESEARCH_DATASET_BUILT rows=%d files=%d parse_errors=%d invalid_snapshots=%d",
        len(out),
        files_count,
        parse_errors,
        invalid_snapshots,
    )
    return out


def rows_to_dataframe(rows: list[dict[str, Any]]) -> Any:
    """Return pandas DataFrame if pandas is available."""
    try:
        import pandas as pd
    except Exception as e:
        raise RuntimeError("pandas unavailable for dataframe conversion") from e
    return pd.DataFrame(rows)

