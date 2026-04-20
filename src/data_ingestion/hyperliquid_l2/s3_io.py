"""S3 listing and download with requester-pays and retries."""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator
from typing import Any

logger = logging.getLogger(__name__)


def _require_boto3():
    try:
        import boto3  # noqa: WPS433
        from botocore.exceptions import ClientError
    except ImportError as e:
        raise ImportError(
            "hyperliquid L2 ingestion requires optional deps: pip install nxfh01[ingest] "
            "(boto3, lz4, pyarrow)"
        ) from e
    return boto3, ClientError


def make_s3_client(region_name: str | None = None) -> Any:
    """Return a boto3 S3 client (uses default credential chain / env)."""
    boto3, _ = _require_boto3()
    return boto3.client("s3", region_name=region_name or None)


def list_l2_keys_for_day(
    *,
    bucket: str,
    token: str,
    ymd: str,
    s3_client: Any | None = None,
    request_payer: str = "requester",
) -> list[str]:
    """Return existing object keys for ``market_data/{ymd}/{hour}/l2Book/{token}.lz4`` (hour 0–23).

    Uses ``list_objects_v2`` under the day prefix and filters by suffix. Hours without data
    are omitted (archive gaps are normal per Hyperliquid docs).
    """
    boto3, _ = _require_boto3()
    client = s3_client or make_s3_client()
    prefix = f"market_data/{ymd}/"
    suffix = f"/l2Book/{token}.lz4"
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    kwargs: dict[str, Any] = {"Bucket": bucket, "Prefix": prefix, "RequestPayer": request_payer}
    for page in paginator.paginate(**kwargs):
        for obj in page.get("Contents") or []:
            k = obj.get("Key") or ""
            if k.endswith(suffix):
                keys.append(k)
    keys.sort()
    logger.info("HL_L2_LIST day=%s token=%s keys=%d", ymd, token, len(keys))
    return keys


def get_object_stream(
    *,
    bucket: str,
    key: str,
    s3_client: Any,
    request_payer: str = "requester",
    max_attempts: int = 5,
    base_sleep_s: float = 0.5,
) -> Iterator[bytes]:
    """Yield raw object bytes from S3 in chunks, with retries on transient errors."""
    _, ClientError = _require_boto3()
    attempt = 0
    while True:
        attempt += 1
        try:
            resp = s3_client.get_object(Bucket=bucket, Key=key, RequestPayer=request_payer)
            body = resp["Body"]
            for chunk in body.iter_chunks(chunk_size=1024 * 1024):
                if chunk:
                    yield chunk
            return
        except ClientError as e:
            err = e.response.get("Error", {}) or {}
            code = err.get("Code", "")
            if attempt >= max_attempts or code in ("404", "NoSuchKey", "NotFound"):
                logger.error("HL_L2_S3_FAIL key=%s code=%s attempt=%s", key, code, attempt)
                raise
            sleep = base_sleep_s * (2 ** (attempt - 1))
            logger.warning(
                "HL_L2_S3_RETRY key=%s code=%s attempt=%s sleep_s=%.2f",
                key,
                code,
                attempt,
                sleep,
            )
            time.sleep(sleep)
