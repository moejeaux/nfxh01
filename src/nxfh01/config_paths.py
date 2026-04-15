"""Resolve repo-root config.yaml from any package location."""

from __future__ import annotations

from pathlib import Path


def find_config_yaml(start: Path | None = None) -> Path:
    """Walk upward from ``start`` (default: this file) until ``config.yaml`` exists."""
    here = (start or Path(__file__)).resolve()
    for d in [here.parent, *here.parents]:
        candidate = d / "config.yaml"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("config.yaml not found in parents of %s" % here)
