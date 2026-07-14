from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from backend.app.paths import get_training_db_path


def _query(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the curated game training dataset.")
    parser.add_argument("--training-db-path", type=Path, help="Path to the shared curated training DB.")
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
                FROM game_training_metadata
                WHERE key IN ('built_at', 'candidate_rows', 'included_rows', 'excluded_rows', 'exclusion_counts', 'source_signature')
                """,
            )
        }
        season_rows = _query(
            conn,
            """
            SELECT season,
                   SUM(CASE WHEN is_clean = 1 THEN 1 ELSE 0 END) AS clean_rows,
                   SUM(CASE WHEN is_clean = 0 THEN 1 ELSE 0 END) AS excluded_rows,
                   SUM(has_spread_target) AS spread_rows,
                   SUM(has_total_market_target) AS total_market_rows,
                   SUM(has_moneyline_target) AS moneyline_rows
            FROM game_training_examples
            GROUP BY season
            ORDER BY season ASC
            """,
        )
        exclusion_rows = _query(
            conn,
            """
            SELECT exclusion_reason, COUNT(*) AS rows
            FROM game_training_examples
            WHERE is_clean = 0
            GROUP BY exclusion_reason
            ORDER BY rows DESC, exclusion_reason ASC
            """,
        )
        sample_rows = _query(
            conn,
            """
            SELECT source_game_id, game_date, season, home_team_id, away_team_id,
                   spread_home, game_total, home_moneyline, away_moneyline,
                   exclusion_reason, quality_flags_json
            FROM game_training_examples
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
                "rows_by_season": [
                    {
                        "season": str(row["season"]),
                        "clean_rows": int(row["clean_rows"] or 0),
                        "excluded_rows": int(row["excluded_rows"] or 0),
                        "spread_rows": int(row["spread_rows"] or 0),
                        "total_market_rows": int(row["total_market_rows"] or 0),
                        "moneyline_rows": int(row["moneyline_rows"] or 0),
                    }
                    for row in season_rows
                ],
                "excluded_rows_by_reason": [
                    {"exclusion_reason": str(row["exclusion_reason"]), "rows": int(row["rows"])}
                    for row in exclusion_rows
                ],
                "excluded_row_samples": [
                    {
                        "source_game_id": int(row["source_game_id"]),
                        "game_date": str(row["game_date"]),
                        "season": str(row["season"]),
                        "home_team_id": int(row["home_team_id"]),
                        "away_team_id": int(row["away_team_id"]),
                        "spread_home": float(row["spread_home"]) if row["spread_home"] is not None else None,
                        "game_total": float(row["game_total"]) if row["game_total"] is not None else None,
                        "home_moneyline": float(row["home_moneyline"]) if row["home_moneyline"] is not None else None,
                        "away_moneyline": float(row["away_moneyline"]) if row["away_moneyline"] is not None else None,
                        "exclusion_reason": str(row["exclusion_reason"]),
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
