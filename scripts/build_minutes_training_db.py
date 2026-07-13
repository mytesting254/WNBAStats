from __future__ import annotations

import argparse
import os
from pathlib import Path

from backend.app.db import connect, init_db
from backend.app.minutes_training_db import ensure_minutes_training_db
from backend.app.paths import ROOT_DIR, get_training_db_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Build or refresh the curated minutes training SQLite database.")
    parser.add_argument("--db-path", type=Path, help="Source app SQLite DB path. Defaults to data/wnba.sqlite.")
    parser.add_argument("--training-db-path", type=Path, help="Target minutes training DB path.")
    parser.add_argument("--force", action="store_true", help="Force a full rebuild even if metadata matches.")
    args = parser.parse_args()

    if args.db_path:
        os.environ["WNBA_DB_PATH"] = str(args.db_path.resolve())
    else:
        os.environ.setdefault("WNBA_DB_PATH", str(ROOT_DIR / "data" / "wnba.sqlite"))
    if args.training_db_path:
        os.environ["WNBA_TRAINING_DB_PATH"] = str(args.training_db_path.resolve())

    init_db()
    with connect() as conn:
        info = ensure_minutes_training_db(conn, force=bool(args.force))

    print(
        {
            "training_db_path": str(get_training_db_path()),
            "rebuilt": bool(info["rebuilt"]),
            "candidate_rows": int(info["candidate_rows"]),
            "included_rows": int(info["included_rows"]),
            "excluded_rows": int(info["excluded_rows"]),
            "built_at": info["built_at"],
        }
    )


if __name__ == "__main__":
    main()
