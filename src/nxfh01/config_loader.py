from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from src.nxfh01.config_merge import deep_merge


def load_config(path: Path | None = None) -> dict[str, Any]:
    root = Path(__file__).resolve().parents[2]
    cfg_path = path or (root / "config.yaml")
    with cfg_path.open(encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError("ACEVAULT_CONFIG_INVALID root must be a mapping")
    btc_path = cfg_path.parent / "config" / "btc_strategy.yaml"
    if btc_path.is_file():
        with btc_path.open(encoding="utf-8") as f:
            btc_raw = yaml.safe_load(f)
        if isinstance(btc_raw, dict):
            existing = data.get("btc_strategy")
            base = existing if isinstance(existing, dict) else {}
            data["btc_strategy"] = deep_merge(base, btc_raw)
    return data
