from __future__ import annotations

import logging
import re

import httpx

from src.engines.acevault.models import AceSignal
from src.regime.models import RegimeState

logger = logging.getLogger(__name__)


class FathomAdvisor:
    def __init__(self, config: dict, ollama_base_url: str) -> None:
        self._config = config
        self._ollama_base_url = ollama_base_url.rstrip("/")
        self._fathom_cfg = config["fathom"]
        self._timeout = self._fathom_cfg.get("timeout_seconds", 15)
        self._max_mult = self._fathom_cfg.get("acevault_max_mult", 1.5)

    async def advise_acevault(
        self,
        signal: AceSignal,
        regime_state: RegimeState,
        prior_context: str,
    ) -> dict:
        prompt = self._build_acevault_prompt(signal, regime_state, prior_context)
        raw = await self._call_fathom(prompt)
        if raw is None:
            advice = self._deterministic_default(signal)
        else:
            advice = self._parse_response(raw, signal)

        logger.info(
            "FATHOM_ACEVAULT_ADVICE coin=%s mult=%s source=%s reasoning=%s",
            signal.coin,
            advice["size_mult"],
            advice["source"],
            advice["reasoning"],
        )
        return advice

    def _build_acevault_prompt(
        self,
        signal: AceSignal,
        regime_state: RegimeState,
        prior_context: str,
    ) -> str:
        return (
            "You are an advisory layer for a Hyperliquid perpetuals trading agent.\n"
            "You may ONLY suggest a size multiplier between 1.0 and 1.5.\n"
            "You cannot block trades, change direction, or widen stop-losses.\n"
            "If unavailable or uncertain, respond with: MULTIPLIER: 1.0\n"
            f"\nCURRENT REGIME: {regime_state.regime.value} "
            f"(confidence: {regime_state.confidence:.2f})\n"
            f"SIGNAL: {signal.coin} SHORT at {signal.entry_price} "
            f"| weakness_score={signal.weakness_score:.3f}\n"
            f"STOP: {signal.stop_loss_price} (immutable) "
            f"| TP: {signal.take_profit_price}\n"
            f"BASE SIZE: ${signal.position_size_usd}\n"
            f"\nPRIOR DECISIONS IN THIS REGIME:\n{prior_context}\n"
            "\nShould size be increased? Reply: MULTIPLIER: [1.0 to 1.5]\n"
            "Then one sentence of reasoning."
        )

    async def _call_fathom(self, prompt: str) -> str | None:
        url = f"{self._ollama_base_url}/api/generate"
        payload = {
            "model": self._fathom_cfg["model"],
            "prompt": prompt,
            "stream": False,
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                return resp.json()["response"]
        except (httpx.TimeoutException, httpx.HTTPError, KeyError, Exception) as exc:
            logger.warning("FATHOM_TIMEOUT reason=%s", exc)
            return None

    def _parse_response(self, response: str, signal: AceSignal) -> dict:
        match = re.search(r"MULTIPLIER:\s*([\d.]+)", response)
        if match is None:
            return self._deterministic_default(signal)

        try:
            mult = float(match.group(1))
        except ValueError:
            return self._deterministic_default(signal)

        mult = max(1.0, min(mult, self._max_mult))

        reasoning_part = response[match.end():].strip()
        reasoning = reasoning_part if reasoning_part else "no_reasoning_provided"

        return {"size_mult": mult, "reasoning": reasoning, "source": "fathom"}

    def _deterministic_default(self, signal: AceSignal) -> dict:
        return {
            "size_mult": 1.0,
            "reasoning": "fathom_unavailable",
            "source": "deterministic",
        }
