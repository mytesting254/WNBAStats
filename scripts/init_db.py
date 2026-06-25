from pathlib import Path
import os
import sqlite3
import sys
import time


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.bootstrap import ensure_teams
from backend.app.db import connect, init_db


def _is_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "database is locked" in str(exc).lower()


def _retry_attempts() -> int:
    return max(1, int(os.getenv("WNBA_INIT_DB_LOCK_ATTEMPTS", "8")))


def _retry_base_sleep() -> float:
    return max(0.1, float(os.getenv("WNBA_INIT_DB_LOCK_BASE_SLEEP", "2.0")))


def initialize_with_retry() -> None:
    attempts = _retry_attempts()
    base_sleep = _retry_base_sleep()
    for attempt in range(attempts):
        try:
            init_db()
            with connect() as conn:
                ensure_teams(conn)
            return
        except sqlite3.OperationalError as exc:
            if not _is_locked_error(exc) or attempt == attempts - 1:
                raise
            sleep_seconds = base_sleep * (attempt + 1)
            print(
                f"SQLite database is locked during startup init; retrying in {sleep_seconds:.1f}s "
                f"({attempt + 1}/{attempts})"
            )
            time.sleep(sleep_seconds)


if __name__ == "__main__":
    initialize_with_retry()
    print("Initialized WNBA database schema and teams.")
