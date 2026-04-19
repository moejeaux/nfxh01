"""Hyperliquid perp universe: top-N tradable subset for new entries (config-driven)."""

from __future__ import annotations

import hashlib
import logging
import threading
import time
from typing import Any, Callable, Mapping

logger = logging.getLogger(__name__)


def _coin_name(row: Any) -> str | None:
    if isinstance(row, str):
        s = row.strip()
        return s or None
    if isinstance(row, dict):
        n = row.get("name")
        if isinstance(n, str) and n.strip():
            return n.strip()
    return None


def _safe_float(x: Any) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


class DefaultMarketRanker:
    """Liquidity-oriented score; pluggable for richer signals later."""

    def rank_market(self, symbol: str, context: Mapping[str, Any]) -> float:
        _ = symbol
        per = context.get("asset_ctx") if isinstance(context.get("asset_ctx"), dict) else {}
        vlm = _safe_float(per.get("dayNtlVlm"))
        oi = _safe_float(per.get("openInterest"))
        return vlm if vlm != 0.0 else oi


RankFn = Callable[[str, Mapping[str, Any]], float]


class Top25UniverseManager:
    def __init__(
        self,
        hl_client: Any,
        config: Mapping[str, Any],
        *,
        rank_fn: RankFn | None = None,
    ) -> None:
        self._hl = hl_client
        self._config = dict(config) if isinstance(config, dict) else {}
        self._ucfg = self._config.get("universe") or {}
        dm = DefaultMarketRanker()
        self._rank: RankFn = rank_fn if rank_fn is not None else dm.rank_market
        self._lock = threading.Lock()
        self._allowed_upper: frozenset[str] = frozenset()
        self._asset_index_by_upper: dict[str, int] = {}
        self._canonical_by_upper: dict[str, str] = {}
        self._had_successful_refresh = False
        self._last_error: str | None = None
        self._last_attempt_mono: float = 0.0

    def rank_market(self, symbol: str, context: Mapping[str, Any]) -> float:
        """Pluggable ranking hook; forwards to ``rank_fn`` (inject for custom scores)."""
        return float(self._rank(symbol, context))

    def _top_n(self) -> int:
        try:
            return int(self._ucfg.get("top_n", 25))
        except (TypeError, ValueError):
            return 25

    def _refresh_seconds(self) -> float:
        try:
            return float(self._ucfg.get("refresh_seconds", 600))
        except (TypeError, ValueError):
            return 600.0

    def _fetch_meta_and_ctxs(self) -> tuple[list[Any], list[dict[str, Any]]]:
        raw = self._hl.post("/info", {"type": "metaAndAssetCtxs"})
        if not isinstance(raw, (list, tuple)) or len(raw) < 1:
            raise ValueError("metaAndAssetCtxs_invalid_shape")
        meta = raw[0]
        if not isinstance(meta, dict):
            raise ValueError("meta_invalid_type")
        uni = meta.get("universe")
        if not isinstance(uni, list) or not uni:
            raise ValueError("meta_missing_universe")
        ctxs: list[dict[str, Any]] = []
        if len(raw) >= 2 and isinstance(raw[1], list):
            for row in raw[1]:
                ctxs.append(row if isinstance(row, dict) else {})
        while len(ctxs) < len(uni):
            ctxs.append({})
        return uni, ctxs[: len(uni)]

    def _fetch_meta_only(self) -> list[Any]:
        meta = self._hl.post("/info", {"type": "meta"})
        if not isinstance(meta, dict):
            raise ValueError("meta_invalid_type")
        uni = meta.get("universe")
        if not isinstance(uni, list) or not uni:
            raise ValueError("meta_missing_universe")
        return uni

    def refresh(self) -> None:
        try:
            try:
                universe_rows, asset_ctxs = self._fetch_meta_and_ctxs()
                used_mac = True
            except Exception as e1:
                logger.info(
                    "RISK_TOP25_INFO_PRIMARY_FAILED trying_meta_only error=%s",
                    e1,
                )
                universe_rows = self._fetch_meta_only()
                asset_ctxs = [{} for _ in universe_rows]
                used_mac = False

            indexed: list[tuple[int, str, dict[str, Any]]] = []
            full_index: dict[str, int] = {}
            canon: dict[str, str] = {}
            for i, row in enumerate(universe_rows):
                name = _coin_name(row)
                if name is None:
                    continue
                upper = name.upper()
                full_index[upper] = i
                canon[upper] = name
                ctx = asset_ctxs[i] if i < len(asset_ctxs) else {}
                indexed.append((i, upper, ctx))

            scores: list[tuple[float, int, str]] = []
            fallback = False
            for i, upper, ctx in indexed:
                ctx_map: Mapping[str, Any] = {"asset_ctx": ctx}
                sc = self.rank_market(upper, ctx_map)
                scores.append((sc, i, upper))
            if not used_mac or all(s[0] == 0.0 for s in scores):
                fallback = True
                scores = [(0.0, i, upper) for i, upper, _ in indexed]

            scores.sort(key=lambda t: (-t[0], t[1]))
            n = max(0, self._top_n())
            top = scores[:n] if n else []
            allowed = frozenset(u for _, _, u in top)

            with self._lock:
                self._allowed_upper = allowed
                self._asset_index_by_upper = full_index
                self._canonical_by_upper = canon
                self._had_successful_refresh = True
                self._last_error = None

            sym_list = [canon[u] for u in sorted(allowed)]
            blob = ",".join(sym_list)
            digest = hashlib.sha256(blob.encode()).hexdigest()[:12] if blob else "empty"
            logger.info(
                "RISK_TOP25_WHITELIST_REFRESHED top_n=%d count=%d digest=%s fallback_rank=%s",
                n,
                len(allowed),
                digest,
                fallback,
            )
            if fallback:
                logger.warning(
                    "RISK_TOP25_FALLBACK_RANKING reason=%s",
                    "no_metaAndAssetCtxs" if not used_mac else "all_scores_zero",
                )
        except Exception as e:
            self._last_error = f"{type(e).__name__}:{e}"
            logger.warning(
                "RISK_TOP25_REFRESH_FAILED_PREVIOUS_WHITELIST_RETAINED error=%s had_prior=%s",
                self._last_error,
                self._had_successful_refresh,
            )
        finally:
            self._last_attempt_mono = time.monotonic()

    def refresh_if_needed(self) -> None:
        if not (self._ucfg.get("enabled", False)):
            return
        gap = time.monotonic() - self._last_attempt_mono
        if gap >= self._refresh_seconds():
            self.refresh()

    def can_open(self, symbol: str) -> bool:
        u = self._ucfg
        if not bool(u.get("enabled", False)):
            return True
        if not bool(u.get("block_new_entries_outside_universe", True)):
            return True
        if not self._had_successful_refresh:
            return False
        key = (symbol or "").strip().upper()
        if not key:
            return False
        with self._lock:
            return key in self._allowed_upper

    def get_allowed_symbols(self) -> list[str]:
        with self._lock:
            return [self._canonical_by_upper[u] for u in sorted(self._allowed_upper)]

    def get_asset_index(self, symbol: str) -> int | None:
        key = (symbol or "").strip().upper()
        if not key:
            return None
        with self._lock:
            return self._asset_index_by_upper.get(key)
