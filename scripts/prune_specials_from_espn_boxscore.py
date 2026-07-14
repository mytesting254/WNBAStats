#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect, init_db
from backend.app.stocks_tracking import delete_unavailable_special_snapshots_from_espn


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Delete unsettled Specials snapshots when ESPN box score shows the player missing or did not play."
    )
    parser.add_argument("--game-id", type=int, action="append", help="Limit to one or more game ids.")
    parser.add_argument("--date", action="append", help="Limit to one or more canonical game dates (YYYY-MM-DD).")
    parser.add_argument("--force-refresh", action="store_true", help="Refresh ESPN summary payloads instead of using cache.")
    parser.add_argument("--dry-run", action="store_true", help="Report rows that would be deleted without deleting them.")
    parser.add_argument(
        "--show-decisions",
        action="store_true",
        help="Include per-player keep/delete decisions in the JSON output.",
    )
    args = parser.parse_args()

    init_db()
    with connect() as conn:
        result = delete_unavailable_special_snapshots_from_espn(
            conn,
            game_ids=args.game_id,
            target_dates=args.date,
            force_refresh=bool(args.force_refresh),
            dry_run=bool(args.dry_run),
        )
    if not args.show_decisions:
        result = {key: value for key, value in result.items() if key != "decisions"}
    print(json.dumps(result, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
