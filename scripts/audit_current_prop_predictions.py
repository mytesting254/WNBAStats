#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.paths import get_db_path


def _connect(db_path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _target_rows(
    conn: sqlite3.Connection,
    *,
    markets: list[str],
    prop_line_ids: list[int],
    statuses: list[str],
) -> list[dict[str, Any]]:
    where_clauses = ["g.status IN ({})".format(",".join("?" for _ in statuses))]
    params: list[Any] = list(statuses)
    if markets:
        where_clauses.append("pl.market IN ({})".format(",".join("?" for _ in markets)))
        params.extend(markets)
    if prop_line_ids:
        where_clauses.append("pl.id IN ({})".format(",".join("?" for _ in prop_line_ids)))
        params.extend(prop_line_ids)
    where_sql = " AND ".join(where_clauses)
    rows = conn.execute(
        f"""
        WITH ranked AS (
          SELECT
            pp.prop_line_id,
            pp.model_version,
            pp.prediction_time,
            pp.recommended_side,
            pp.edge,
            pp.projection,
            pl.market,
            pl.line,
            g.game_date,
            g.status,
            ROW_NUMBER() OVER (
              PARTITION BY pp.prop_line_id
              ORDER BY pp.prediction_time DESC, pp.id DESC
            ) AS rn
          FROM prop_predictions pp
          JOIN prop_lines pl ON pl.id = pp.prop_line_id
          JOIN games g ON g.id = pl.game_id
          WHERE {where_sql}
        )
        SELECT
          prop_line_id,
          market,
          status,
          game_date,
          model_version,
          prediction_time,
          recommended_side,
          ROUND(edge * 100.0, 2) AS edge_pct,
          ROUND(projection, 2) AS projection,
          ROUND(line, 2) AS line
        FROM ranked
        WHERE rn = 1
        ORDER BY market, prop_line_id
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _cmd_snapshot(args: argparse.Namespace) -> int:
    db_path = Path(args.db_path).expanduser().resolve()
    with _connect(db_path) as conn:
        rows = _target_rows(
            conn,
            markets=list(args.market or []),
            prop_line_ids=[int(value) for value in args.prop_line_id or []],
            statuses=list(args.status or []),
        )
    payload = {
        "db_path": str(db_path),
        "row_count": len(rows),
        "filters": {
            "market": list(args.market or []),
            "prop_line_id": [int(value) for value in args.prop_line_id or []],
            "status": list(args.status or []),
        },
        "rows": rows,
    }
    output = Path(args.output).expanduser().resolve()
    output.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(json.dumps({"output": str(output), "row_count": len(rows)}, indent=2))
    return 0


def _load_snapshot(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).expanduser().resolve().read_text(encoding="utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("rows"), list):
        raise RuntimeError(f"Invalid snapshot payload: {path}")
    return payload


def _cmd_diff(args: argparse.Namespace) -> int:
    before = _load_snapshot(args.before)
    after = _load_snapshot(args.after)
    before_rows = {int(row["prop_line_id"]): row for row in before["rows"]}
    after_rows = {int(row["prop_line_id"]): row for row in after["rows"]}

    flipped: list[dict[str, Any]] = []
    changed: list[dict[str, Any]] = []
    added: list[dict[str, Any]] = []
    removed: list[dict[str, Any]] = []

    for prop_line_id in sorted(after_rows):
        current = after_rows[prop_line_id]
        previous = before_rows.get(prop_line_id)
        if previous is None:
            added.append(current)
            continue
        if previous.get("recommended_side") != current.get("recommended_side"):
            flipped.append(
                {
                    "prop_line_id": prop_line_id,
                    "market": current.get("market"),
                    "before_side": previous.get("recommended_side"),
                    "after_side": current.get("recommended_side"),
                    "before_edge_pct": previous.get("edge_pct"),
                    "after_edge_pct": current.get("edge_pct"),
                }
            )
        elif previous.get("edge_pct") != current.get("edge_pct"):
            changed.append(
                {
                    "prop_line_id": prop_line_id,
                    "market": current.get("market"),
                    "side": current.get("recommended_side"),
                    "before_edge_pct": previous.get("edge_pct"),
                    "after_edge_pct": current.get("edge_pct"),
                }
            )

    for prop_line_id in sorted(before_rows):
        if prop_line_id not in after_rows:
            removed.append(before_rows[prop_line_id])

    print(
        json.dumps(
            {
                "before_rows": len(before_rows),
                "after_rows": len(after_rows),
                "flipped_count": len(flipped),
                "edge_changed_count": len(changed),
                "added_count": len(added),
                "removed_count": len(removed),
                "flipped": flipped,
                "edge_changed": changed,
                "added": added,
                "removed": removed,
            },
            indent=2,
        )
    )
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot and diff current prop_predictions rows by prop_line_id.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    snapshot_parser = subparsers.add_parser("snapshot", help="Write the latest current prediction rows to JSON.")
    snapshot_parser.add_argument("--db-path", default=str(get_db_path()))
    snapshot_parser.add_argument("--output", required=True)
    snapshot_parser.add_argument("--market", action="append", default=[])
    snapshot_parser.add_argument("--prop-line-id", action="append", default=[])
    snapshot_parser.add_argument("--status", action="append", default=["scheduled", "in_progress"])
    snapshot_parser.set_defaults(func=_cmd_snapshot)

    diff_parser = subparsers.add_parser("diff", help="Compare two snapshot JSON files.")
    diff_parser.add_argument("before")
    diff_parser.add_argument("after")
    diff_parser.set_defaults(func=_cmd_diff)

    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
