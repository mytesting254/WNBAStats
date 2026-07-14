from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path

from backend.app.paths import get_training_db_path


def _query(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> list[sqlite3.Row]:
    return conn.execute(sql, params).fetchall()


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect the curated player-prop training dataset.")
    parser.add_argument("--training-db-path", type=Path, help="Path to the shared curated training DB.")
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
                FROM player_prop_training_metadata
                WHERE key IN ('built_at', 'db_version', 'candidate_rows', 'included_rows', 'excluded_rows', 'diagnostics_by_market', 'exclusions_by_market', 'source_signature')
                """,
            )
        }
        market_rows = _query(
            conn,
            """
            SELECT market, sample_kind, COUNT(*) AS rows
            FROM player_prop_training_examples
            GROUP BY market, sample_kind
            ORDER BY market ASC, sample_kind ASC
            """,
        )
        exclusion_rows = _query(
            conn,
            """
            SELECT market, sample_kind, reason_code, COUNT(*) AS rows
            FROM player_prop_training_exclusions
            GROUP BY market, sample_kind, reason_code
            ORDER BY market ASC, sample_kind ASC, reason_code ASC
            """,
        )
    finally:
        conn.close()

    print(
        json.dumps(
            {
                "training_db_path": str(path),
                "metadata": {
                    "built_at": metadata.get("built_at"),
                    "db_version": metadata.get("db_version"),
                    "candidate_rows": int(metadata.get("candidate_rows") or 0),
                    "included_rows": int(metadata.get("included_rows") or 0),
                    "excluded_rows": int(metadata.get("excluded_rows") or 0),
                    "source_signature": metadata.get("source_signature"),
                    "diagnostics_by_market": json.loads(metadata["diagnostics_by_market"]) if metadata.get("diagnostics_by_market") else {},
                    "exclusions_by_market": json.loads(metadata["exclusions_by_market"]) if metadata.get("exclusions_by_market") else {},
                },
                "rows_by_market_kind": [
                    {
                        "market": str(row["market"]),
                        "sample_kind": str(row["sample_kind"]),
                        "rows": int(row["rows"]),
                    }
                    for row in market_rows
                ],
                "exclusions_by_market_kind": [
                    {
                        "market": str(row["market"]),
                        "sample_kind": str(row["sample_kind"]),
                        "reason_code": str(row["reason_code"]),
                        "rows": int(row["rows"]),
                    }
                    for row in exclusion_rows
                ],
            },
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
