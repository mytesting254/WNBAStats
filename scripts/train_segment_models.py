from __future__ import annotations

import argparse
import os
from pathlib import Path

from backend.app.db import connect, init_db
from backend.app.paths import get_db_path, get_segment_training_db_path
from backend.app.segment_predictions import SEGMENT_TARGETS, train_segment_model


def main() -> None:
    parser = argparse.ArgumentParser(description="Train and evaluate Q1/1H segment models.")
    parser.add_argument("--db-path", type=Path, help="Source app SQLite DB path. Defaults to the active WNBA_DB_PATH/runtime DB.")
    parser.add_argument("--training-db-path", type=Path, help="Target segment training DB path.")
    parser.add_argument("--force", action="store_true", help="Force a rebuild of the segment training DB before training.")
    args = parser.parse_args()

    if args.db_path:
        os.environ["WNBA_DB_PATH"] = str(args.db_path.resolve())
    if args.training_db_path:
        os.environ["WNBA_SEGMENT_TRAINING_DB_PATH"] = str(args.training_db_path.resolve())

    init_db()
    with connect() as conn:
        results = []
        for target in SEGMENT_TARGETS:
            model, info = train_segment_model(conn, target=target, force_rebuild=bool(args.force))
            results.append(
                {
                    "target": target,
                    "trained": model is not None,
                    **info,
                }
            )

    print(
        {
            "db_path": str(get_db_path()),
            "segment_training_db_path": str(get_segment_training_db_path()),
            "results": results,
        }
    )


if __name__ == "__main__":
    main()
