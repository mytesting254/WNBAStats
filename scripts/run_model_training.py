#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]


def _set_default_env(args: argparse.Namespace) -> None:
    os.environ.setdefault("USE_TURSO", "0")
    os.environ.setdefault("USE_LOCAL_DB", "true")
    os.environ.setdefault("WNBA_DB_PATH", str(args.db_path or ROOT / "data" / "wnba.sqlite"))
    os.environ.setdefault("WNBA_CACHE_DIR", str(args.cache_dir or Path(os.environ["WNBA_DB_PATH"]).resolve().parent / "cache"))
    if args.training_start_date:
        os.environ["WNBA_TRAINING_START_DATE"] = args.training_start_date
    if args.max_workers is not None:
        os.environ["WNBA_TRAINING_MAX_WORKERS"] = str(args.max_workers)


def _json_default(value: Any) -> str:
    return str(value)


def main() -> int:
    parser = argparse.ArgumentParser(description="Run WNBA model training against a local SQLite database.")
    parser.add_argument("--db-path", type=Path, help="SQLite DB path. Defaults to data/wnba.sqlite.")
    parser.add_argument("--cache-dir", type=Path, help="Model/cache output directory. Defaults beside the DB.")
    parser.add_argument("--training-start-date", help="Optional WNBA_TRAINING_START_DATE override in YYYY-MM-DD format.")
    parser.add_argument("--max-workers", type=int, help="Optional WNBA_TRAINING_MAX_WORKERS override.")
    parser.add_argument("--skip-prewarm", action="store_true", help="Only save Model Lab metrics; do not prewarm projection model caches.")
    args = parser.parse_args()

    _set_default_env(args)

    from backend.app.db import connect
    from backend.app.player_prop_model import prewarm_model_cache
    from backend.app.training import run_walk_forward_training

    with connect() as conn:
        run = run_walk_forward_training(conn)
        prewarm = None if args.skip_prewarm else prewarm_model_cache(conn)
        payload = {
            "status": run.get("status"),
            "model_version": run.get("model_version"),
            "run_type": run.get("run_type"),
            "training_rows": run.get("training_rows"),
            "started_at": run.get("started_at"),
            "finished_at": run.get("finished_at"),
            "db_path": os.environ["WNBA_DB_PATH"],
            "cache_dir": os.environ["WNBA_CACHE_DIR"],
            "training_start_date": os.getenv("WNBA_TRAINING_START_DATE"),
            "max_workers": os.getenv("WNBA_TRAINING_MAX_WORKERS"),
            "prewarm": prewarm,
        }
        print(json.dumps(payload, indent=2, default=_json_default))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
