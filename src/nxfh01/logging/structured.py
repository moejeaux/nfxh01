from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger("nxfh01.directives")


def _kv(**fields: Any) -> str:
    return " ".join(f"{k}={v}" for k, v in fields.items())


def log_risk_rejected(reason: str, **extra: Any) -> None:
    tail = _kv(reason=reason, **extra)
    _logger.info("RISK_REJECTED %s", tail)


def log_risk_event(event: str, **fields: Any) -> None:
    _logger.info("RISK_%s %s", event, _kv(**fields) if fields else "")


def log_regime_event(event: str, **fields: Any) -> None:
    _logger.info("REGIME_%s %s", event, _kv(**fields) if fields else "")


def log_acevault_event(event: str, **fields: Any) -> None:
    _logger.info("ACEVAULT_%s %s", event, _kv(**fields) if fields else "")


def log_acevault_no_candidates(**fields: Any) -> None:
    _logger.info("ACEVAULT_NO_CANDIDATES %s", _kv(**fields))


def log_killswitch_active(engine: int | str, **extra: Any) -> None:
    _logger.warning("KILLSWITCH_ACTIVE engine=%s %s", engine, _kv(**extra).strip())
