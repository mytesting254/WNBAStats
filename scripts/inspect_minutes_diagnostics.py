from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from backend.app.main import minutes_diagnostics


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect minutes diagnostics with regression breakdowns.")
    parser.add_argument("--db-path", type=Path, help="Override WNBA_DB_PATH for this run.")
    parser.add_argument("--start-date", default="", help="Evaluation start date (YYYY-MM-DD).")
    parser.add_argument(
        "--top-loss-role",
        default="",
        help="If set, print only the top-loss rows for this role bucket (for example: rotation). Use 'overall' for all rows.",
    )
    args = parser.parse_args()

    if args.db_path:
        os.environ["WNBA_DB_PATH"] = str(args.db_path.resolve())

    result = minutes_diagnostics(start_date=args.start_date or None)
    if args.top_loss_role:
        key = args.top_loss_role.strip()
        if key == "overall":
            payload = result["breakdown"]["top_loss_rows"]["overall"]
        else:
            payload = result["breakdown"]["top_loss_rows"].get(key, [])
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
