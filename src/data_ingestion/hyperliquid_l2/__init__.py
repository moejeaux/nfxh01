"""Hyperliquid perp L2 archive ingestion from ``s3://hyperliquid-archive``."""

from __future__ import annotations

__all__ = ["ingest_token_day"]


def __getattr__(name: str):
    if name == "ingest_token_day":
        from src.data_ingestion.hyperliquid_l2.ingest_day import ingest_token_day

        return ingest_token_day
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
