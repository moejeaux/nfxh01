"""High-level ingest: list S3 keys for one token-day, download, parse, write Parquet."""

from __future__ import annotations

import logging
from collections.abc import Iterator
from datetime import date
from pathlib import Path
from typing import Any

import pyarrow as pa

from src.data_ingestion.hyperliquid_l2.constants import HL_ARCHIVE_BUCKET
from src.data_ingestion.hyperliquid_l2.decompress import iter_json_lines_from_lz4_stream
from src.data_ingestion.hyperliquid_l2.parse_l2 import parse_l2_archive_line
from src.data_ingestion.hyperliquid_l2.parquet_writer import parquet_writer_append_schema
from src.data_ingestion.hyperliquid_l2.s3_io import get_object_stream, list_l2_keys_for_day, make_s3_client
from src.data_ingestion.hyperliquid_l2.schema import L2BookRow

logger = logging.getLogger(__name__)


def _rows_from_s3_key(
    *,
    bucket: str,
    key: str,
    token: str,
    s3_client: Any,
    request_payer: str,
    max_attempts: int,
) -> list[L2BookRow]:
    stream: Iterator[bytes] = get_object_stream(
        bucket=bucket,
        key=key,
        s3_client=s3_client,
        request_payer=request_payer,
        max_attempts=max_attempts,
    )
    rows: list[L2BookRow] = []
    for line in iter_json_lines_from_lz4_stream(stream):
        rows.extend(parse_l2_archive_line(line, token=token, s3_key=key))
    return rows


def ingest_token_day(
    *,
    token: str,
    day: date,
    output_base: Path,
    bucket: str = HL_ARCHIVE_BUCKET,
    s3_client: Any | None = None,
    request_payer: str = "requester",
    max_attempts: int = 5,
    compression: str | None = "zstd",
) -> Path | None:
    """Download all available ``l2Book`` hours for ``token`` on ``day``, normalize, write one Parquet.

    Returns output path, or ``None`` if no objects exist for that day (common archive gap).
    """
    ymd = day.strftime("%Y%m%d")
    client = s3_client or make_s3_client()
    keys = list_l2_keys_for_day(
        bucket=bucket, token=token, ymd=ymd, s3_client=client, request_payer=request_payer
    )
    if not keys:
        logger.warning("HL_L2_DAY_SKIP token=%s day=%s (no S3 keys)", token, day)
        return None

    out_dir = output_base / token
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"{day.isoformat()}.parquet"

    writer, schema = parquet_writer_append_schema(out_path, compression=compression)
    batch: list[L2BookRow] = []
    batch_size = 200_000
    total = 0
    wrote_any = False
    try:
        for key in keys:
            logger.info("HL_L2_HOUR_INGEST token=%s key=%s", token, key)
            hour_rows = _rows_from_s3_key(
                bucket=bucket,
                key=key,
                token=token,
                s3_client=client,
                request_payer=request_payer,
                max_attempts=max_attempts,
            )
            batch.extend(hour_rows)
            total += len(hour_rows)
            while len(batch) >= batch_size:
                chunk = batch[:batch_size]
                del batch[:batch_size]
                table = pa.Table.from_pylist(chunk, schema=schema)
                writer.write_table(table)
                wrote_any = True
        if batch:
            table = pa.Table.from_pylist(batch, schema=schema)
            writer.write_table(table)
            wrote_any = True
        if not wrote_any:
            writer.write_table(pa.Table.from_pylist([], schema=schema))
    finally:
        writer.close()

    logger.info("HL_L2_DAY_DONE token=%s day=%s rows=%d path=%s", token, day, total, out_path)
    return out_path
