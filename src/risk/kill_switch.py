import logging

logger = logging.getLogger(__name__)


class KillSwitch:
    def __init__(self) -> None:
        self._active: dict[str, bool] = {}

    def activate(self, engine_id: str) -> None:
        self._active[engine_id] = True
        logger.warning("RISK_KILLSWITCH engine=%s activated", engine_id)

    def deactivate(self, engine_id: str) -> None:
        self._active[engine_id] = False
        logger.info("RISK_KILLSWITCH engine=%s deactivated", engine_id)

    def is_active(self, engine_id: str) -> bool:
        return self._active.get(engine_id, False)
