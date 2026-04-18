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
        self._ollama_base_url = ollama_base_url.rstrip('/')
        self._fathom_cfg = config['fathom']
        self._timeout = float(
            self._fathom_cfg.get(
                'entry_timeout_seconds',
                self._fathom_cfg.get('timeout_seconds', 30),
            )
        )
        self._min_mult = float(self._fathom_cfg.get('acevault_min_mult', 0.90))
        self._max_mult = float(self._fathom_cfg.get('acevault_max_mult', 1.15))
        self._fast_model = self._fathom_cfg.get('fast_model', 'llama3.2:3b')

    def _clamp_acevault_mult(self, raw: float, signal: AceSignal) -> tuple[float, float]:
        """Return (applied, raw) with ``applied`` in [min, max]. Logs when clamped."""
        try:
            x = float(raw)
        except (TypeError, ValueError):
            return self._min_mult, raw
        applied = max(self._min_mult, min(x, self._max_mult))
        if applied != x:
            logger.info(
                'FATHOM_MULT_CLAMPED coin=%s raw=%.4f applied=%.4f min=%.4f max=%.4f',
                signal.coin, x, applied, self._min_mult, self._max_mult,
            )
        return applied, x

    async def advise_acevault(self, signal, regime_state, prior_context):
        prompt = self._build_acevault_prompt(signal, regime_state, prior_context)
        raw = await self._call_fathom(prompt)
        advice = self._parse_response(raw, signal) if raw is not None else self._deterministic_default(signal)
        logger.info(
            'FATHOM_ACEVAULT_ADVICE coin=%s mult_applied=%s mult_raw=%s source=%s reasoning=%s',
            signal.coin,
            advice['size_mult'],
            advice.get('size_mult_raw', advice['size_mult']),
            advice['source'],
            advice['reasoning'][:80],
        )
        return advice

    def _build_acevault_prompt(self, signal, regime_state, prior_context):
        prior_str = str(prior_context)[:300] if prior_context else 'none'
        return (
            f'TRADE DATA:\n'
            f'regime={regime_state.regime.value} confidence={regime_state.confidence:.2f}\n'
            f'coin={signal.coin} side=SHORT entry={signal.entry_price}\n'
            f'stop={signal.stop_loss_price} tp={signal.take_profit_price}\n'
            f'size_usd={signal.position_size_usd} weakness={signal.weakness_score:.3f}\n'
            f'PRIOR_DECISIONS: {prior_str}\n\n'
            'Based on the above, output your size recommendation.\n'
            f'End with: MULTIPLIER: X.X (advisory size factor between {self._min_mult:.2f} and {self._max_mult:.2f})'
        )

    async def _call_fathom(self, prompt):
        url = f'{self._ollama_base_url}/api/chat'
        lo, hi = self._min_mult, self._max_mult
        payload = {
            'model': self._fast_model,
            'stream': False,
            'options': {'num_predict': 60, 'temperature': 0.0},
            'messages': [
                {'role': 'system', 'content': (
                    'Reply in this exact format only:\n'
                    'MULTIPLIER: X.X\n'
                    'REASON: one sentence.\n'
                    f'X.X must be between {lo:.2f} and {hi:.2f}. No other text.'
                )},
                {'role': 'user', 'content': prompt},
            ],
        }
        try:
            async with httpx.AsyncClient(timeout=self._timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                text = resp.json()['message']['content']
                logger.info('FATHOM_RAW tail=%s', text[-200:])
                return text
        except httpx.TimeoutException as exc:
            logger.warning('FATHOM_TIMEOUT after=%ss reason=%s', self._timeout, exc)
        except httpx.HTTPError as exc:
            logger.warning('FATHOM_HTTP_ERROR reason=%s', exc)
        except Exception as exc:
            logger.warning('FATHOM_UNEXPECTED_ERROR reason=%s', exc)
        return None

    def _extract_reason_text(self, response: str) -> str | None:
        m = re.search(r'REASON:\s*(.+?)(?:\n|$)', response, flags=re.IGNORECASE | re.DOTALL)
        if m:
            return m.group(1).strip()[:200]
        return None

    def _parse_response(self, response, signal):
        clean = re.sub(r'<redacted_thinking>.*?</redacted_thinking>', '', response, flags=re.DOTALL).strip()
        search_in = clean if clean else response
        match = re.search(r'MULTIPLIER:\s*([\d.]+)', search_in, re.IGNORECASE)
        if not match:
            match = re.search(r'MULTIPLIER:\s*([\d.]+)', response, re.IGNORECASE)
        if not match:
            tail_matches = re.findall(
                r'\b(\d+\.\d+|\d+)\b',
                response[-400:],
            )
            if tail_matches:
                try:
                    raw_val = float(tail_matches[-1])
                    mult, raw_tracked = self._clamp_acevault_mult(raw_val, signal)
                    logger.info('FATHOM_FALLBACK_MULT coin=%s mult_raw=%s mult_applied=%s', signal.coin, raw_tracked, mult)
                    reason = self._extract_reason_text(response) or 'fathom_fallback_parse'
                    return {
                        'size_mult': mult,
                        'size_mult_raw': raw_tracked,
                        'reasoning': reason,
                        'source': 'fathom',
                    }
                except ValueError:
                    pass
            logger.warning('FATHOM_PARSE_FAILED tail=%s', response[-150:])
            return self._deterministic_default(signal)
        try:
            raw_val = float(match.group(1))
        except ValueError:
            return self._deterministic_default(signal)
        mult, raw_tracked = self._clamp_acevault_mult(raw_val, signal)
        reason = self._extract_reason_text(response)
        if not reason:
            pos = response.rfind(match.group(0))
            after = response[pos + len(match.group(0)):].strip()
            reason = (after[:200] if after else 'no_reasoning_provided')
        return {
            'size_mult': mult,
            'size_mult_raw': raw_tracked,
            'reasoning': reason,
            'source': 'fathom',
        }

    async def analyse_trade(self, decision: dict, journal) -> None:
        """Post-trade deep analysis using fathom-r1-14b. Runs async, never blocks entries."""
        decision_id = str(decision.get("id", ""))
        coin = decision.get("coin", "?")
        try:
            prompt = (
                f"You are reviewing a completed trade for learning purposes.\n"
                f"Coin: {coin}\n"
                f"Entry: {decision.get('entry_price')} | Exit: {decision.get('exit_price')}\n"
                f"Stop: {decision.get('stop_loss_price')} | TP: {decision.get('take_profit_price')}\n"
                f"PnL: {decision.get('pnl_pct', 0):.3%} ({decision.get('pnl_usd', 0):.2f} USD)\n"
                f"Exit reason: {decision.get('exit_reason')}\n"
                f"Regime at entry: {decision.get('regime')} | at close: {decision.get('regime_at_close')}\n"
                f"Hold duration: {decision.get('hold_duration_seconds', 0):.0f}s\n"
                f"Fathom multiplier used: {decision.get('fathom_size_mult', 1.0)}\n"
                f"\nIn 2-3 sentences: what went right or wrong, and what should change next time?"
            )
            url = f"{self._ollama_base_url}/api/chat"
            payload = {
                "model": self._fathom_cfg["model"],
                "stream": False,
                "options": {"num_predict": 300, "temperature": 0.3},
                "messages": [
                    {"role": "system", "content": "You are a trading coach reviewing a completed trade. Be concise and specific. 2-3 sentences only."},
                    {"role": "user", "content": prompt},
                ],
            }
            deep_timeout = self._fathom_cfg.get("post_analysis_timeout_seconds", 180)
            async with httpx.AsyncClient(timeout=deep_timeout) as client:
                resp = await client.post(url, json=payload)
                resp.raise_for_status()
                analysis = resp.json()["message"]["content"].strip()

            logger.info("FATHOM_POST_ANALYSIS coin=%s decision_id=%s analysis=%s",
                        coin, decision_id, analysis[:120])

            if journal is not None:
                await journal.log_post_analysis(decision_id, analysis)

        except Exception as exc:
            logger.warning("FATHOM_POST_ANALYSIS_FAILED coin=%s decision_id=%s error=%s",
                           coin, decision_id, exc)

    def _deterministic_default(self, signal):
        return {
            'size_mult': 1.0,
            'size_mult_raw': 1.0,
            'reasoning': 'fathom_unavailable',
            'source': 'deterministic',
        }
