"""Async client for local LLMs via Ollama HTTP API (/api/generate)."""

from __future__ import annotations

import logging
import os
from typing import Any
from urllib.parse import urljoin

import httpx

logger = logging.getLogger("nxfh02.local_llm")


def _normalize_base(url: str) -> str:
    u = url.rstrip("/")
    return u if u else "http://127.0.0.1:11434"


class LocalLLMClient:
    """Ollama-backed reasoning. Use ``is_ready`` before awaiting ``generate_reasoning``."""

    def __init__(
        self,
        *,
        base_url: str,
        model: str,
        num_ctx: int,
        default_max_tokens: int,
        timeout_s: float = 120.0,
    ) -> None:
        self._base = _normalize_base(base_url)
        self._model = model
        self._num_ctx = max(256, num_ctx)
        self._default_max_tokens = max(1, default_max_tokens)
        self._timeout = timeout_s
        self._ready = False
        self._health_error: str | None = None

    @property
    def is_ready(self) -> bool:
        return self._ready

    @property
    def model(self) -> str:
        return self._model

    @property
    def base_url(self) -> str:
        return self._base

    def disable(self, reason: str) -> None:
        self._ready = False
        self._health_error = reason

    async def health_check(self) -> bool:
        """Ping Ollama with a minimal generate; sets ``is_ready`` and logs outcome."""
        url = urljoin(self._base + "/", "api/generate")
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": "ping",
            "stream": False,
            "options": {
                "num_ctx": self._num_ctx,
                "num_predict": min(8, self._default_max_tokens),
            },
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                r = await client.post(url, json=payload)
                r.raise_for_status()
                data = r.json()
                if not data.get("done", True):
                    raise ValueError("incomplete Ollama health response")
        except Exception as e:
            self._ready = False
            self._health_error = str(e)
            logger.warning(
                "LOCAL_LLM unavailable — local reasoning disabled (%s: %s). "
                "Check LOCAL_LLM_BASE_URL / model / Ollama process.",
                type(e).__name__,
                e,
            )
            return False

        self._ready = True
        self._health_error = None
        logger.info("LOCAL_LLM_READY model=%s base=%s", self._model, self._base)
        return True

    async def generate_reasoning(
        self,
        prompt: str,
        *,
        system: str | None = None,
        max_tokens: int | None = None,
    ) -> str:
        """POST /api/generate (non-streaming). Raises if local LLM is not ready."""
        if not self._ready:
            raise RuntimeError(
                self._health_error
                or "local LLM not ready — health check failed or not configured"
            )

        predict = max_tokens if max_tokens is not None else self._default_max_tokens
        predict = max(1, int(predict))

        url = urljoin(self._base + "/", "api/generate")
        payload: dict[str, Any] = {
            "model": self._model,
            "prompt": prompt,
            "stream": False,
            "options": {
                "num_ctx": self._num_ctx,
                "num_predict": predict,
            },
        }
        if system:
            payload["system"] = system

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            r = await client.post(url, json=payload)
            r.raise_for_status()
            data = r.json()
        return (data.get("response") or "").strip()

    def run_health_sync(self) -> bool:
        """Run :meth:`health_check` from sync code (e.g. startup)."""
        import asyncio

        return asyncio.run(self.health_check())


def build_local_llm_client() -> LocalLLMClient | None:
    """Instantiate client when ``LOCAL_LLM_PROVIDER=ollama`` and model is set.

    Reads env at call time (not import time) so tests and late ``load_dotenv`` see updates.
    """
    provider = (os.getenv("LOCAL_LLM_PROVIDER") or "").strip().lower()
    if not provider:
        return None
    if provider != "ollama":
        logger.warning(
            "LOCAL_LLM_PROVIDER=%s is not supported — only 'ollama' is wired; local LLM disabled",
            os.getenv("LOCAL_LLM_PROVIDER"),
        )
        return None
    model = (os.getenv("LOCAL_LLM_MODEL") or "").strip()
    if not model:
        logger.warning(
            "LOCAL_LLM_MODEL is empty — local LLM client not created "
            "(set LOCAL_LLM_MODEL e.g. fathom-r1-14b)",
        )
        return None

    base = (os.getenv("LOCAL_LLM_BASE_URL") or "http://127.0.0.1:11434").strip()
    try:
        ctx_n = int(os.getenv("LOCAL_LLM_CONTEXT") or "4096")
    except ValueError:
        ctx_n = 4096
    try:
        mt = int(os.getenv("LOCAL_LLM_MAX_TOKENS") or "512")
    except ValueError:
        mt = 512
    ctx_n = ctx_n if ctx_n > 0 else 4096
    mt = mt if mt > 0 else 512

    return LocalLLMClient(
        base_url=base,
        model=model,
        num_ctx=ctx_n,
        default_max_tokens=mt,
    )
