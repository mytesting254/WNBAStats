from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from backend.app.paths import ROOT_DIR, get_training_db_path


def _query(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the curated minutes training SQLite database.")
    parser.add_argument("--training-db-path", type=Path, help="Path to the curated minutes training DB.")
    parser.add_argument("--sample-limit", type=int, default=10, help="Number of excluded-row samples to show.")
    args = parser.parse_args()

    path = Path(args.training_db_path or get_training_db_path()).resolve()
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        metadata = {
            row["key"]: row["value"]
            for row in _query(
                conn,
                """
                SELECT key, value
                FROM minutes_training_metadata
                WHERE key IN ('built_at', 'candidate_rows', 'included_rows', 'excluded_rows', 'exclusion_counts', 'source_signature')
                """,
            )
        }
        role_rows = _query(
            conn,
            """
            SELECT COALESCE(role_bucket, 'unknown') AS role_bucket, COUNT(*) AS rows
            FROM minutes_training_examples
            WHERE is_clean = 1
            GROUP BY COALESCE(role_bucket, 'unknown')
            ORDER BY rows DESC, role_bucket ASC
            """,
        )
        season_rows = _query(
            conn,
            """
            SELECT season,
                   SUM(CASE WHEN is_clean = 1 THEN 1 ELSE 0 END) AS clean_rows,
                   SUM(CASE WHEN is_clean = 0 THEN 1 ELSE 0 END) AS excluded_rows
            FROM minutes_training_examples
            GROUP BY season
            ORDER BY season ASC
            """,
        )
        exclusion_rows = _query(
            conn,
            """
            SELECT exclusion_reason, COUNT(*) AS rows
            FROM minutes_training_examples
            WHERE is_clean = 0
            GROUP BY exclusion_reason
            ORDER BY rows DESC, exclusion_reason ASC
            """,
        )
        sample_rows = _query(
            conn,
            """
            SELECT source_player_id, source_game_id, game_date, rotation_role, position,
                   target_minutes, recent_blend, exclusion_reason, status_reason, minutes_text, quality_flags_json
            FROM minutes_training_examples
            WHERE is_clean = 0
            ORDER BY game_date DESC, source_game_id DESC
            LIMIT ?
            """,
            (int(args.sample_limit),),
        )
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "training_db_path": str(path),
                "metadata": {
                    "built_at": metadata.get("built_at"),
                    "candidate_rows": int(metadata.get("candidate_rows") or 0),
                    "included_rows": int(metadata.get("included_rows") or 0),
                    "excluded_rows": int(metadata.get("excluded_rows") or 0),
                    "source_signature": metadata.get("source_signature"),
                    "exclusion_counts": json.loads(metadata["exclusion_counts"]) if metadata.get("exclusion_counts") else {},
                },
                "clean_rows_by_role_bucket": [
                    {"role_bucket": str(row["role_bucket"]), "rows": int(row["rows"])}
                    for row in role_rows
                ],
                "rows_by_season": [
                    {
                        "season": str(row["season"]),
                        "clean_rows": int(row["clean_rows"] or 0),
                        "excluded_rows": int(row["excluded_rows"] or 0),
                    }
                    for row in season_rows
                ],
                "excluded_rows_by_reason": [
                    {"exclusion_reason": str(row["exclusion_reason"]), "rows": int(row["rows"])}
                    for row in exclusion_rows
                ],
                "excluded_row_samples": [
                    {
                        "source_player_id": int(row["source_player_id"]),
                        "source_game_id": int(row["source_game_id"]),
                        "game_date": str(row["game_date"]),
                        "rotation_role": str(row["rotation_role"]),
                        "position": str(row["position"] or ""),
                        "target_minutes": float(row["target_minutes"]) if row["target_minutes"] is not None else None,
                        "recent_blend": float(row["recent_blend"]) if row["recent_blend"] is not None else None,
                        "exclusion_reason": str(row["exclusion_reason"]),
                        "status_reason": str(row["status_reason"] or ""),
                        "minutes_text": str(row["minutes_text"] or ""),
                        "quality_flags": json.loads(str(row["quality_flags_json"] or "{}")),
                    }
                    for row in sample_rows
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
