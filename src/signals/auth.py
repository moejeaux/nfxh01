"""Swappable webhook authentication — HMAC-SHA256(body) or constant-time header secret."""

from __future__ import annotations

import hashlib
import hmac
import logging
from typing import Protocol, runtime_checkable

logger = logging.getLogger("nxfh02.signal")


@runtime_checkable
class WebhookAuthVerifier(Protocol):
    def verify(self, body: bytes, headers: dict[str, str]) -> bool:
        ...


class HmacSha256BodyVerifier:
    """Signature = hex(HMAC-SHA256(key, body)). Header default: X-NXFH02-Signature."""

    def __init__(self, secret: bytes, header_name: str = "x-nxfh02-signature") -> None:
        self._secret = secret
        self._header = header_name.lower()

    def verify(self, body: bytes, headers: dict[str, str]) -> bool:
        expected_hex = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        got = ""
        for k, v in headers.items():
            if k.lower() == self._header:
                got = v.strip()
                break
        if not got:
            logger.warning("SIGNAL_AUTH_FAIL: missing signature header %s", self._header)
            return False
        # Allow optional "sha256=" prefix
        if got.lower().startswith("sha256="):
            got = got.split("=", 1)[1].strip()
        try:
            expected = bytes.fromhex(expected_hex)
            candidate = bytes.fromhex(got)
        except ValueError:
            logger.warning("SIGNAL_AUTH_FAIL: signature not valid hex")
            return False
        return hmac.compare_digest(expected, candidate)


class HeaderSecretVerifier:
    """Constant-time compare: X-NXFH02-Webhook-Secret must equal secret (plaintext)."""

    def __init__(self, secret: str, header_name: str = "x-nxfh02-webhook-secret") -> None:
        self._secret = secret.encode("utf-8")
        self._header = header_name.lower()

    def verify(self, body: bytes, headers: dict[str, str]) -> bool:
        got = ""
        for k, v in headers.items():
            if k.lower() == self._header:
                got = v
                break
        if not got:
            logger.warning("SIGNAL_AUTH_FAIL: missing header %s", self._header)
            return False
        return hmac.compare_digest(self._secret, got.strip().encode("utf-8"))


class NoopVerifier:
    """Accept all requests. Activated ONLY by NXFH02_SIGNAL_AUTH_DISABLED=1 for local dev."""

    def verify(self, body: bytes, headers: dict[str, str]) -> bool:
        return True


def build_webhook_verifier_from_env() -> WebhookAuthVerifier | None:
    """Returns None if senpi signal mode not configured (caller handles).

    When NXFH02_SIGNAL_AUTH_DISABLED=1 is set (local dev only), returns a NoopVerifier
    that accepts all requests without requiring a webhook secret.
    """
    import os

    auth_disabled = (os.getenv("NXFH02_SIGNAL_AUTH_DISABLED") or "").strip().lower()
    if auth_disabled in ("1", "true", "yes"):
        logger.warning(
            "SIGNAL_AUTH_DISABLED — webhook auth bypassed (local dev). "
            "DO NOT use in production."
        )
        return NoopVerifier()

    secret = (os.getenv("NXFH02_SIGNAL_WEBHOOK_SECRET") or "").strip()
    if not secret:
        return None
    method = (os.getenv("NXFH02_SIGNAL_AUTH_METHOD") or "hmac_sha256").strip().lower()
    if method in ("hmac", "hmac_sha256", "sha256"):
        return HmacSha256BodyVerifier(secret.encode("utf-8"))
    if method in ("header", "header_secret", "plaintext"):
        return HeaderSecretVerifier(secret)
    logger.error("Unknown NXFH02_SIGNAL_AUTH_METHOD=%r — use hmac_sha256 or header", method)
    return None
