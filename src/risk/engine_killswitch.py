from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_DEFAULT_ENGINE_CONFIGS: dict[str, dict] = {
    "acevault": {"loss_pct": 0.03, "cooldown_hours": 4},
    "growi": {"loss_pct": 0.04, "cooldown_hours": 6},
    "mc": {"loss_pct": 0.02, "cooldown_hours": 2},
    "anticze": {"loss_pct": 0.02, "cooldown_hours": 8},
    "eternal": {"loss_pct": 0.02, "cooldown_hours": 12},
}


class KillSwitch:

    def __init__(self, config: dict) -> None:
        self._config = config
        self._loss_window: dict[str, list[tuple[datetime, float]]] = {}
        self._tripped_at: dict[str, datetime | None] = {}

    def _engine_cfg(self, engine_id: str) -> dict:
        engines_cfg = self._config.get("engines", {})
        if engine_id in engines_cfg:
            return engines_cfg[engine_id]
        return _DEFAULT_ENGINE_CONFIGS.get(engine_id, {"loss_pct": 0.03, "cooldown_hours": 4})

    def record_loss(self, engine_id: str, loss_pct: float) -> None:
        now = datetime.now(timezone.utc)
        ecfg = self._engine_cfg(engine_id)
        cooldown_hours = ecfg["cooldown_hours"]

        if engine_id not in self._loss_window:
            self._loss_window[engine_id] = []

        self._loss_window[engine_id].append((now, loss_pct))

        cutoff = now - timedelta(hours=cooldown_hours)
        self._loss_window[engine_id] = [
            (ts, pct) for ts, pct in self._loss_window[engine_id] if ts >= cutoff
        ]

        cumulative = sum(pct for _, pct in self._loss_window[engine_id])

        logger.info(
            "KILLSWITCH_LOSS_RECORDED engine=%s loss_pct=%.4f cumulative=%.4f threshold=%.4f",
            engine_id, loss_pct, cumulative, ecfg["loss_pct"],
        )

        if cumulative >= ecfg["loss_pct"] and self._tripped_at.get(engine_id) is None:
            self._trip(engine_id, cumulative)

    def is_active(self, engine_id: str) -> bool:
        self._check_auto_reset(engine_id)
        return self._tripped_at.get(engine_id) is not None

    def get_resume_time(self, engine_id: str) -> datetime | None:
        tripped = self._tripped_at.get(engine_id)
        if tripped is None:
            return None
        ecfg = self._engine_cfg(engine_id)
        return tripped + timedelta(hours=ecfg["cooldown_hours"])

    def reset(self, engine_id: str, reason: str = "manual") -> None:
        self._tripped_at[engine_id] = None
        self._loss_window[engine_id] = []
        logger.warning(
            "KILLSWITCH_RESET engine=%s reason=%s",
            engine_id, reason,
        )

    def _trip(self, engine_id: str, cumulative_loss: float) -> None:
        self._tripped_at[engine_id] = datetime.now(timezone.utc)
        logger.warning(
            "KILLSWITCH_TRIPPED engine=%s cumulative_loss=%.4f",
            engine_id, cumulative_loss,
        )

    def _check_auto_reset(self, engine_id: str) -> None:
        tripped = self._tripped_at.get(engine_id)
        if tripped is None:
            return
        ecfg = self._engine_cfg(engine_id)
        elapsed = datetime.now(timezone.utc) - tripped
        if elapsed >= timedelta(hours=ecfg["cooldown_hours"]):
            self.reset(engine_id, "auto")
