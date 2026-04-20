"""LZ4 frame decompression for archive objects (S3 byte stream in, text lines out)."""

from __future__ import annotations

import logging
from collections.abc import Iterator

logger = logging.getLogger(__name__)


def _require_lz4():
    try:
        import lz4.frame  # noqa: WPS433
    except ImportError as e:
        raise ImportError("install lz4: pip install nxfh01[ingest]") from e
    return lz4.frame


def decompress_lz4_to_bytes(chunks: Iterator[bytes]) -> bytes:
    """Concatenate S3 chunks and decompress as a single LZ4 frame (one archive object)."""
    lz4 = _require_lz4()
    raw = b"".join(chunks)
    if not raw:
        return b""
    out = lz4.frame.decompress(raw)
    logger.debug("HL_L2_LZ4_DECOMP compressed=%d decompressed=%d", len(raw), len(out))
    return out


def iter_json_lines_from_lz4_stream(chunks: Iterator[bytes]) -> Iterator[str]:
    """Decompress one ``.lz4`` object and yield non-empty text lines (typically JSON per line)."""
    text = decompress_lz4_to_bytes(chunks).decode("utf-8", errors="replace")
    for line in text.splitlines():
        s = line.strip()
        if s:
            yield s
