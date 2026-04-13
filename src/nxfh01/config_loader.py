from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml


def load_config(path: Path | None = None) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    cfg_path = path or (root / "config.yaml")
    with cfg_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("ACEVAULT_CONFIG_INVALID root must be a mapping")
    return data
