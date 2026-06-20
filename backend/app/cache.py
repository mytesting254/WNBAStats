from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from uuid import uuid4

from .paths import get_cache_dir


def _cache_path(name: str):
    return get_cache_dir() / name


def write_json_cache(name: str, payload: Any) -> Path:
    cache_dir = get_cache_dir()
    cache_dir.mkdir(parents=True, exist_ok=True)
    path = cache_dir / name
    tmp_path = path.with_name(f"{path.name}.{uuid4().hex}.tmp")
    tmp_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    tmp_path.replace(path)
    return path


def read_json_cache(name: str) -> Any | None:
    path = _cache_path(name)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return None


def delete_json_cache(name: str) -> bool:
    path = _cache_path(name)
    if not path.exists():
        return False
    path.unlink()
    return True
