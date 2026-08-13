from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from backend.app.db import connect, init_db
from backend.app.paths import get_db_path
from backend.app.player_role_curation import (
    BALL_HANDLER_BUCKETS,
    REBOUND_BUCKETS,
    SHOT_VOLUME_BUCKETS,
    list_player_role_overrides,
    upsert_player_role_override,
)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Manage curated player role bucket overrides.")
    parser.add_argument("--db-path", type=Path, help="Source app SQLite DB path. Defaults to the active WNBA_DB_PATH/runtime DB.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    add_parser = subparsers.add_parser("add", help="Insert a curated player role override.")
    add_parser.add_argument("--player-id", type=int, required=True)
    add_parser.add_argument("--team-id", type=int)
    add_parser.add_argument("--source", default="manual")
    add_parser.add_argument("--ball-handler-bucket", choices=BALL_HANDLER_BUCKETS)
    add_parser.add_argument("--rebound-bucket", choices=REBOUND_BUCKETS)
    add_parser.add_argument("--shot-volume-bucket", choices=SHOT_VOLUME_BUCKETS)
    add_parser.add_argument("--offensive-rebound-bias", type=float, default=1.0)
    add_parser.add_argument("--defensive-rebound-bias", type=float, default=1.0)
    add_parser.add_argument("--field-goal-attempt-bias", type=float, default=1.0)
    add_parser.add_argument("--priority", type=int, default=100)
    add_parser.add_argument("--effective-start-date")
    add_parser.add_argument("--effective-end-date")
    add_parser.add_argument("--notes")

    list_parser = subparsers.add_parser("list", help="List curated player role overrides.")
    list_parser.add_argument("--player-id", type=int)
    list_parser.add_argument("--team-id", type=int)

    return parser


def main() -> None:
    parser = _build_parser()
    args = parser.parse_args()

    if args.db_path:
        os.environ["WNBA_DB_PATH"] = str(args.db_path.resolve())

    init_db()
    with connect() as conn:
        if args.command == "add":
            row_id = upsert_player_role_override(
                conn,
                player_id=int(args.player_id),
                team_id=int(args.team_id) if args.team_id is not None else None,
                source=str(args.source),
                ball_handler_bucket=args.ball_handler_bucket,
                rebound_bucket=args.rebound_bucket,
                shot_volume_bucket=args.shot_volume_bucket,
                offensive_rebound_bias=float(args.offensive_rebound_bias),
                defensive_rebound_bias=float(args.defensive_rebound_bias),
                field_goal_attempt_bias=float(args.field_goal_attempt_bias),
                priority=int(args.priority),
                effective_start_date=args.effective_start_date,
                effective_end_date=args.effective_end_date,
                notes=args.notes,
            )
            conn.commit()
            print(
                json.dumps(
                    {
                        "db_path": str(get_db_path()),
                        "inserted_id": row_id,
                        "player_id": int(args.player_id),
                        "team_id": int(args.team_id) if args.team_id is not None else None,
                    },
                    indent=2,
                )
            )
            return

        rows = list_player_role_overrides(
            conn,
            player_id=int(args.player_id) if args.player_id is not None else None,
            team_id=int(args.team_id) if args.team_id is not None else None,
        )
        print(
            json.dumps(
                {
                    "db_path": str(get_db_path()),
                    "rows": [
                        {
                            "id": int(row["id"]),
                            "player_id": int(row["player_id"]),
                            "player_name": str(row["player_name"]),
                            "team_id": int(row["team_id"]) if row["team_id"] is not None else None,
                            "team_name": str(row["team_name"]) if row["team_name"] is not None else None,
                            "source": str(row["source"]),
                            "ball_handler_bucket": row["ball_handler_bucket"],
                            "rebound_bucket": row["rebound_bucket"],
                            "shot_volume_bucket": row["shot_volume_bucket"],
                            "offensive_rebound_bias": float(row["offensive_rebound_bias"] or 1.0),
                            "defensive_rebound_bias": float(row["defensive_rebound_bias"] or 1.0),
                            "field_goal_attempt_bias": float(row["field_goal_attempt_bias"] or 1.0),
                            "priority": int(row["priority"] or 100),
                            "effective_start_date": row["effective_start_date"],
                            "effective_end_date": row["effective_end_date"],
                            "notes": row["notes"],
                            "updated_at": str(row["updated_at"]),
                        }
                        for row in rows
                    ],
                },
                indent=2,
            )
        )


if __name__ == "__main__":
    main()
