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
    raise RuntimeError(
        "WNBA_DB_PATH is required. Refusing to guess a runtime database path because repo-local SQLite "
        "defaults caused split-brain data between the app runtime and the checkout."
    )


def is_repo_local_runtime_db_path(path: Path | str) -> bool:
    try:
        resolved = Path(path).resolve()
        repo_data = (ROOT_DIR / "data").resolve()
        resolved.relative_to(repo_data)
        return True
    except (OSError, ValueError):
        return False


def get_cache_dir() -> Path:
    override = os.getenv("WNBA_CACHE_DIR")
    if override:
        return Path(override)
    db_override = os.getenv("WNBA_DB_PATH")
    if db_override:
        return Path(db_override).resolve().parent / "cache"
    return get_data_dir() / "cache"


def get_snapshot_dir() -> Path:
    override = os.getenv("WNBA_SNAPSHOT_DIR")
    if override:
        return Path(override)
    db_override = os.getenv("WNBA_DB_PATH")
    if db_override:
        return Path(db_override).resolve().parent / "snapshots"
    return get_data_dir() / "snapshots"


def get_training_db_path() -> Path:
    override = os.getenv("WNBA_TRAINING_DB_PATH")
    if override:
        return Path(override)
    db_override = os.getenv("WNBA_DB_PATH")
    if db_override:
        base_dir = Path(db_override).resolve().parent
        return base_dir / "wnba-training.sqlite"
    return get_data_dir() / "wnba-training.sqlite"


def get_segment_training_db_path() -> Path:
    override = os.getenv("WNBA_SEGMENT_TRAINING_DB_PATH")
    if override:
        return Path(override)
    db_override = os.getenv("WNBA_DB_PATH")
    if db_override:
        base_dir = Path(db_override).resolve().parent
        return base_dir / "wnba-segment-training.sqlite"
    return get_data_dir() / "wnba-segment-training.sqlite"


def get_player_half_training_db_path() -> Path:
    override = os.getenv("WNBA_PLAYER_HALF_TRAINING_DB_PATH")
    if override:
        return Path(override)
    db_override = os.getenv("WNBA_DB_PATH")
    if db_override:
        base_dir = Path(db_override).resolve().parent
        return base_dir / "wnba-player-half-training.sqlite"
    return get_data_dir() / "wnba-player-half-training.sqlite"
