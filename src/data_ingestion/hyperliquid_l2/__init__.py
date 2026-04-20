"""Hyperliquid perp L2 archive ingestion from ``s3://hyperliquid-archive``."""

from src.data_ingestion.hyperliquid_l2.ingest_day import ingest_token_day

__all__ = ["ingest_token_day"]
