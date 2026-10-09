"""Fetch and store RotoWire WNBA scouting tables in the active runtime DB."""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from backend.app.rotowire_scouting import pull_scouting_reports


ROOT = Path(__file__).resolve().parents[1]
FIRST_PULL_DATE = "2026-09-24"


def _live_db_path() -> Path:
    result = subprocess.run(
        [sys.executable, str(ROOT / "scripts" / "live_backend.py"), "host-runtime-info"],
        cwd=ROOT, text=True, capture_output=True, check=False,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or "Could not locate active runtime database")
    info = json.loads(result.stdout)
    return Path(info["db_path"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, help="Explicit SQLite DB for local use; defaults to active runtime DB")
    parser.add_argument("--run-now", action="store_true", help="Run an explicitly requested pull before the scheduled start date")
    args = parser.parse_args()
    today = datetime.now(ZoneInfo("America/New_York")).date().isoformat()
    if today < FIRST_PULL_DATE and not args.run_now:
        print(json.dumps({"status": "not_started", "first_pull_date": FIRST_PULL_DATE, "today": today}))
        return
    db_path = args.db_path or (Path(os.environ["WNBA_SCOUTING_DB_PATH"]) if os.getenv("WNBA_SCOUTING_DB_PATH") else _live_db_path())
    if not db_path.is_file():
        parser.error(f"database does not exist: {db_path}")
    conn = sqlite3.connect(db_path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout = 30000")
    try:
        result = pull_scouting_reports(conn)
    finally:
        conn.close()
    print(json.dumps({"db_path": str(db_path), **result}, sort_keys=True))
    if not result["complete"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
