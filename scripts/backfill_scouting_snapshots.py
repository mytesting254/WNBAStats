"""Rebuild WNBA team scouting snapshots from local game data and ESPN cache."""

from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

from backend.app.scouting_snapshots import rebuild_snapshots


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", required=True, type=Path, help="SQLite database to update")
    parser.add_argument("--cache-dir", type=Path, default=Path("data/cache"))
    parser.add_argument("--season", type=int, help="Rebuild one season; default: all seasons")
    args = parser.parse_args()
    if not args.db_path.is_file():
        parser.error(f"database does not exist: {args.db_path}")
    if not args.cache_dir.is_dir():
        parser.error(f"cache directory does not exist: {args.cache_dir}")
    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = rebuild_snapshots(conn, args.cache_dir, season=args.season)
    finally:
        conn.close()
    print(result)


if __name__ == "__main__":
    main()
