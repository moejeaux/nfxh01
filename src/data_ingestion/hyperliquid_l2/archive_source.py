"""``MarketDataIngestSource`` adapter for ``hyperliquid-archive`` L2 keys."""

from __future__ import annotations

from datetime import date
from typing import Any

from src.data_ingestion.hyperliquid_l2.constants import HL_ARCHIVE_BUCKET
from src.data_ingestion.hyperliquid_l2.s3_io import list_l2_keys_for_day, make_s3_client
from src.data_ingestion.sources.base import ObjectDescriptor


def _hour_from_key(key: str) -> int:
    parts = key.split("/")
    if len(parts) >= 4 and parts[0] == "market_data":
        try:
            return int(parts[2])
        except ValueError:
            return -1
    return -1


class HyperliquidArchiveL2Source:
    """List S3 objects for L2 snapshots (Phase 1); swap or extend for SonarX later."""

    provider_id = "hyperliquid_archive"

    def __init__(
        self,
        *,
        bucket: str = HL_ARCHIVE_BUCKET,
        s3_client: Any | None = None,
        request_payer: str = "requester",
    ) -> None:
        self._bucket = bucket
        self._client = s3_client or make_s3_client()
        self._request_payer = request_payer

    def iter_objects(self, token: str, day: date) -> list[ObjectDescriptor]:
        ymd = day.strftime("%Y%m%d")
        keys = list_l2_keys_for_day(
            bucket=self._bucket,
            token=token,
            ymd=ymd,
            s3_client=self._client,
            request_payer=self._request_payer,
        )
        out: list[ObjectDescriptor] = []
        for k in keys:
            out.append(
                ObjectDescriptor(
                    uri=f"s3://{self._bucket}/{k}",
                    token=token,
                    day=day,
                    hour=_hour_from_key(k),
                    provider=self.provider_id,
                )
            )
        return out
