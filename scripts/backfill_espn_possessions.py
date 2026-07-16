from __future__ import annotations

import argparse
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect, init_db
from backend.app.espn_history import backfill_espn_team_possessions


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill team possessions from ESPN summary boxscores.")
    parser.add_argument("--season", type=int, required=True, help="Season year, for example 2026.")
    parser.add_argument("--date", default=None, help="Optional YYYY-MM-DD date filter.")
    parser.add_argument("--force-refresh", action="store_true", help="Refetch ESPN summaries instead of using cache when present.")
    parser.add_argument("--all-games", action="store_true", help="Backfill all final games in scope, not only placeholder 78.0 rows.")
    parser.add_argument("--max-games", type=int, default=None, help="Optional cap on games processed in this run.")
    args = parser.parse_args()

    init_db()
    with connect() as conn:
        result = backfill_espn_team_possessions(
            conn,
            season=args.season,
            force_refresh=args.force_refresh,
            selected_date=args.date,
            only_placeholder=not args.all_games,
            max_games=args.max_games,
        )

    for key, value in result.items():
        print(f"{key}={value}")


if __name__ == "__main__":
    main()
