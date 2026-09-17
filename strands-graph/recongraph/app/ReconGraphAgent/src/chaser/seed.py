"""``python -m chaser.seed``: (re)create .data/chaser.db from data/*.json."""

from __future__ import annotations

import json

from .service import seed

if __name__ == "__main__":
    print(json.dumps({"ok": True, "counts": seed()}, indent=2))
