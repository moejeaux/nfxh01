from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from concurrent.futures import TimeoutError as FuturesTimeout
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable, Mapping

from src.nxfh01.contracts.engine import EngineId
from src.nxfh01.logging.structured import log_acevault_event


@dataclass(frozen=True)
class FathomSizeAdvice:
    size_multiplier: Decimal


class FathomAdvisory:
    def __init__(self, config: Mapping[str, Any]) -> None:
        f = config["fathom"]
        self._timeout = float(f["timeout_seconds"])
        self._max_ace = Decimal(str(f["acevault_max_mult"]))
        self._max_maj = Decimal(str(f["majors_max_mult"]))

    def clamp_size_multiplier(
        self, requested: Decimal, engine: EngineId, is_major_asset: bool
    ) -> FathomSizeAdvice:
        cap = self._max_maj if is_major_asset else self._max_ace
        m = max(Decimal("1"), min(requested, cap))
        return FathomSizeAdvice(size_multiplier=m)

    def call_with_timeout(self, fn: Callable[[], Any], default: Any) -> Any:
        ex = ThreadPoolExecutor(max_workers=1)
        fut = ex.submit(fn)
        try:
            return fut.result(timeout=self._timeout)
        except FuturesTimeout:
            fut.cancel()
            log_acevault_event("FATHOM_TIMEOUT", detail="deterministic_default")
            return default
        finally:
            ex.shutdown(wait=False, cancel_futures=True)


def apply_fathom_payload(
    payload: Mapping[str, Any],
    advisory: FathomAdvisory,
    engine: EngineId,
    is_major_asset: bool,
    default_mult: Decimal = Decimal("1"),
) -> FathomSizeAdvice:
    if payload.get("block_trade") is True or payload.get("block") is True:
        log_acevault_event("FATHOM_IGNORED_FIELD", field="block_trade")
    raw = payload.get("size_multiplier", default_mult)
    try:
        requested = Decimal(str(raw))
    except Exception:
        log_acevault_event("FATHOM_BAD_MULTIPLIER", detail="deterministic_default")
        requested = default_mult
    return advisory.clamp_size_multiplier(requested, engine, is_major_asset)
