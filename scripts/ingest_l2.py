#!/usr/bin/env python3
"""CLI: ingest Hyperliquid perp L2 archives from S3 into partitioned Parquet.

Example::

    pip install nxfh01[ingest]
    python scripts/ingest_l2.py --start 2024-09-01 --end 2024-10-01 --tokens BTC,ETH,HYPE,SOL

Requires AWS credentials with S3 read + **requester pays** acceptance.
"""

from __future__ import annotations

import argparse
import logging
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

from src.data_ingestion.hyperliquid_l2.constants import DEFAULT_TOKENS
from src.data_ingestion.hyperliquid_l2.ingest_day import ingest_token_day


def _parse_date(s: str) -> date:
    return datetime.strptime(s.strip(), "%Y-%m-%d").date()


def _iter_days(start: date, end_exclusive: date):
    d = start
    while d < end_exclusive:
        yield d
        d += timedelta(days=1)


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(
        level=logging.INFO,
        format="%(levelname)s %(name)s %(message)s",
    )
    p = argparse.ArgumentParser(description="Ingest Hyperliquid L2 lz4 archives to Parquet")
    p.add_argument("--start", required=True, help="Start date inclusive (YYYY-MM-DD)")
    p.add_argument("--end", required=True, help="End date exclusive (YYYY-MM-DD)")
    p.add_argument(
        "--tokens",
        default=",".join(DEFAULT_TOKENS),
        help=f"Comma-separated perp names (default: {','.join(DEFAULT_TOKENS)})",
    )
    p.add_argument(
        "--output-base",
        type=Path,
        default=Path("data/hyperliquid/l2"),
        help="Root directory for Parquet output (token/date partitions)",
    )
    p.add_argument("--bucket", default="hyperliquid-archive")
    p.add_argument("--max-attempts", type=int, default=5)
    p.add_argument("--compression", default="zstd", choices=("zstd", "snappy", "gzip", "none"))
    args = p.parse_args(argv)

    try:
        import boto3  # noqa: F401, WPS433
        import lz4.frame  # noqa: F401, WPS433
        import pyarrow  # noqa: F401, WPS433
    except ImportError:
        logging.error(
            "Missing optional dependencies. Install with: pip install nxfh01[ingest] "
            "(boto3, lz4, pyarrow)"
        )
        return 2

    start = _parse_date(args.start)
    end_excl = _parse_date(args.end)
    if end_excl <= start:
        logging.error("--end must be after --start")
        return 2

    tokens = [t.strip().upper() for t in args.tokens.split(",") if t.strip()]
    comp: str | None = None if args.compression == "none" else args.compression

    for d in _iter_days(start, end_excl):
        for token in tokens:
            path = ingest_token_day(
                token=token,
                day=d,
                output_base=args.output_base,
                bucket=args.bucket,
                request_payer="requester",
                max_attempts=int(args.max_attempts),
                compression=comp,
            )
            if path:
                logging.info("Wrote %s", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
