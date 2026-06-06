from __future__ import annotations

import os
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DATA_DIR = ROOT_DIR / "data"


def get_data_dir() -> Path:
    return Path(os.getenv("WNBA_DATA_DIR", DEFAULT_DATA_DIR))


def get_db_path() -> Path:
    override = os.getenv("WNBA_DB_PATH")
    if override:
        return Path(override)
    return get_data_dir() / "wnba.sqlite"


def get_cache_dir() -> Path:
    override = os.getenv("WNBA_CACHE_DIR")
    if override:
        return Path(override)
    return get_data_dir() / "cache"


def get_snapshot_dir() -> Path:
    override = os.getenv("WNBA_SNAPSHOT_DIR")
    if override:
        return Path(override)
    return get_data_dir() / "snapshots"
