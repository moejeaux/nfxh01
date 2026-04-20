#!/usr/bin/env python3
"""CLI wrapper: append one HL ``metaAndAssetCtxs`` line — see ``src.research.hl_meta_archive_append``."""

from __future__ import annotations

import sys
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.research.hl_meta_archive_append import main  # noqa: E402

if __name__ == "__main__":
    main()
