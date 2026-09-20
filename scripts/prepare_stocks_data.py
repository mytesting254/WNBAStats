#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import connect
from backend.app.odds_import import discover_odds_api_game_ids
from backend.app.stocks_tracking import default_prep_dates, get_tracking_db_path, prepare_stocks_data
from backend.app.timezone_utils import APP_TIMEZONE


def _selected_dates(args: argparse.Namespace) -> list[str]:
    explicit_dates = sorted({str(value).strip() for value in (args.date or []) if str(value).strip()})
    if explicit_dates:
        return explicit_dates
    today = datetime.now(APP_TIMEZONE).date()
    if args.today_only:
        return [today.isoformat()]
    if args.tomorrow_only:
        return [(today + timedelta(days=1)).isoformat()]
    return default_prep_dates(include_tomorrow=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Prepare Specials stocks candidate pools and snapshots.")
    parser.add_argument("--date", action="append", help="Target game date in YYYY-MM-DD. Repeatable.")
    parser.add_argument(
        "--today-only",
        action="store_true",
        help="Prepare only today's slate in America/New_York.",
    )
    parser.add_argument(
        "--tomorrow-only",
        action="store_true",
        help="Prepare only tomorrow's slate in America/New_York.",
    )
    parser.add_argument(
        "--odds-events",
        action="store_true",
        help="Discover the selected slate from Odds API events before preparing stocks.",
    )
    args = parser.parse_args()

    selected_dates = _selected_dates(args)
    with connect() as conn:
        if args.odds_events:
            discovered_game_ids = discover_odds_api_game_ids(conn, target_dates=selected_dates)
            if not discovered_game_ids:
                result = {
                    "selected_dates": selected_dates,
                    "game_ids": [],
                    "prepared_games": 0,
                    "candidate_players": 0,
                    "team_prep_context": 0,
                    "player_prep_features": 0,
                    "game_board_summaries": 0,
                    "snapshots_written": 0,
                    "status": "no_upcoming_odds_events",
                }
            else:
                result = prepare_stocks_data(conn, game_ids=discovered_game_ids)
        else:
            result = prepare_stocks_data(conn, target_dates=selected_dates)
    result["tracking_db_path"] = str(get_tracking_db_path())
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
