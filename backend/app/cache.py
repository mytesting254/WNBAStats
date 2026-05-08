from __future__ import annotations

import json
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT_DIR / "data" / "cache"


def write_json_cache(name: str, payload: Any) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / name
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    return path


def read_json_cache(name: str) -> Any | None:
    path = CACHE_DIR / name
    if not path.exists():
        return None
    return json.loads(path.read_text(encoding="utf-8"))
