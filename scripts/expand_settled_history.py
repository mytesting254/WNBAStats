from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect, init_db
from backend.app.history_expansion import expand_settled_prop_history


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit and expand missing settled WNBA prop history.")
    parser.add_argument("--start-date", help="First game date to audit in YYYY-MM-DD format.")
    parser.add_argument("--end-date", help="Last game date to audit in YYYY-MM-DD format.")
    parser.add_argument("--max-dates", type=int, default=None, help="Optional cap on missing dates to backfill this run.")
    parser.add_argument("--dry-run", action="store_true", help="Only audit; do not import or settle anything.")
    parser.add_argument("--no-force-refresh", action="store_true", help="Reuse cached ESPN payloads when available.")
    args = parser.parse_args()

    init_db()
    with connect() as conn:
        result = expand_settled_prop_history(
            conn,
            start_date=args.start_date,
            end_date=args.end_date,
            force_refresh=not args.no_force_refresh,
            max_dates=args.max_dates,
            dry_run=args.dry_run,
        )
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
