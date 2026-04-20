"""Small bounded client for Fathom-style weekly summary generation."""

from __future__ import annotations

import json
import os
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class FathomClientConfig:
    endpoint: str
    model: str
    timeout_seconds: float
    api_key: str | None = None


class FathomClient:
    def __init__(self, cfg: FathomClientConfig) -> None:
        self._cfg = cfg

    @classmethod
    def from_config(cls, config: dict[str, Any]) -> "FathomClient":
        intelligence = config.get("intelligence") or {}
        model = str(intelligence.get("model", "fathom-14b"))
        timeout_seconds = float(intelligence.get("timeout_seconds", 60))
        endpoint = (
            os.getenv("FATHOM_API_ENDPOINT")
            or os.getenv("OLLAMA_BASE_URL")
            or "http://127.0.0.1:11434"
        )
        endpoint = endpoint.rstrip("/") + "/api/chat"
        api_key = os.getenv("FATHOM_API_KEY") or os.getenv("OLLAMA_API_KEY")
        return cls(
            FathomClientConfig(
                endpoint=endpoint,
                model=model,
                timeout_seconds=timeout_seconds,
                api_key=api_key,
            )
        )

    def complete_json(self, *, system_prompt: str, user_prompt: str) -> str:
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._cfg.api_key:
            headers["Authorization"] = f"Bearer {self._cfg.api_key}"
        payload: dict[str, Any] = {
            "model": self._cfg.model,
            "stream": False,
            "format": "json",
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
        }
        with httpx.Client(timeout=self._cfg.timeout_seconds) as client:
            resp = client.post(self._cfg.endpoint, headers=headers, json=payload)
            resp.raise_for_status()
            content = (resp.json().get("message") or {}).get("content")
            if not isinstance(content, str) or not content.strip():
                raise ValueError("Fathom response missing message.content")
            return content.strip()

    def trace_config(self) -> dict[str, Any]:
        return {
            "endpoint": self._cfg.endpoint,
            "model": self._cfg.model,
            "timeout_seconds": self._cfg.timeout_seconds,
            "auth_configured": bool(self._cfg.api_key),
        }

    @staticmethod
    def sanitize_error(err: Exception) -> str:
        msg = str(err).strip()
        if len(msg) > 240:
            msg = msg[:240] + "..."
        return msg or err.__class__.__name__

    @staticmethod
    def stable_json(obj: dict[str, Any]) -> str:
        return json.dumps(obj, sort_keys=True, ensure_ascii=True, separators=(",", ":"))
