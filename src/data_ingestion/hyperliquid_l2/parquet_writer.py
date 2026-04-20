"""Append normalized rows to partitioned Parquet files."""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path
from typing import Any

from src.data_ingestion.hyperliquid_l2.schema import L2BookRow

logger = logging.getLogger(__name__)


def write_token_day_parquet(
    rows: list[L2BookRow],
    *,
    token: str,
    day: date,
    output_base: Path,
    compression: str = "zstd",
) -> Path:
    """Write ``data/.../{token}/{YYYY-MM-DD}.parquet`` (creates parent dirs)."""
    import pandas as pd

    out_dir = output_base / token
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{day.isoformat()}.parquet"
    if not rows:
        logger.warning("HL_L2_PARQUET_EMPTY token=%s day=%s", token, day)
        df = pd.DataFrame(
            columns=[
                "event_time_ms",
                "wall_time_iso",
                "token",
                "side",
                "level",
                "price",
                "size",
                "order_count",
                "block_number",
                "s3_key",
                "source_provider",
            ]
        )
    else:
        df = pd.DataFrame(rows)
    df.to_parquet(out_path, index=False, compression=compression, engine="pyarrow")
    logger.info("HL_L2_PARQUET_WROTE path=%s rows=%d", out_path, len(rows))
    return out_path


def parquet_writer_append_schema(
    path: Path,
    *,
    compression: str | None = "zstd",
) -> tuple[Any, Any]:
    """Return ``(ParquetWriter, schema)`` for incremental writes; caller must ``close()``."""
    import pyarrow as pa
    import pyarrow.parquet as pq

    schema = pa.schema(
        [
            pa.field("event_time_ms", pa.int64(), nullable=False),
            pa.field("wall_time_iso", pa.string(), nullable=True),
            pa.field("token", pa.string(), nullable=False),
            pa.field("side", pa.string(), nullable=False),
            pa.field("level", pa.int32(), nullable=False),
            pa.field("price", pa.float64(), nullable=False),
            pa.field("size", pa.float64(), nullable=False),
            pa.field("order_count", pa.int32(), nullable=False),
            pa.field("block_number", pa.int64(), nullable=True),
            pa.field("s3_key", pa.string(), nullable=False),
            pa.field("source_provider", pa.string(), nullable=False),
        ]
    )
    path.parent.mkdir(parents=True, exist_ok=True)
    if compression:
        writer = pq.ParquetWriter(str(path), schema, compression=compression)
    else:
        writer = pq.ParquetWriter(str(path), schema)
    return writer, schema
