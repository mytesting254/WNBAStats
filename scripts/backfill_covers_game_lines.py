#!/usr/bin/env python3
"""Backfill historical game spreads/totals from Covers matchup pages only."""

from __future__ import annotations

import argparse
import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path

from backend.app.covers_import import _fetch_text, _metadata_from_page, covers_matchup_links


def _iso_date(value: str) -> str:
    return date.fromisoformat(value).isoformat()


def _fetch_date(selected_date: str) -> tuple[str, list[tuple[object, str]], list[str]]:
    pages: list[tuple[object, str]] = []
    errors: list[str] = []
    try:
        games = covers_matchup_links(selected_date)
    except Exception as exc:
        return selected_date, pages, [f"matchups: {exc}"]
    for game in games:
        try:
            url = game.matchup_url or game.odds_url.removesuffix("/odds")
            pages.append((game, _fetch_text(url)))
        except Exception as exc:
            errors.append(f"{game.event_id}: {exc}")
    return selected_date, pages, errors


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--db-path", type=Path, required=True)
    parser.add_argument("--start-date", type=_iso_date, required=True)
    parser.add_argument("--end-date", type=_iso_date, required=True)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument(
        "--all-final-dates",
        action="store_true",
        help="Backfill every missing final-game date instead of only dates with prop lines.",
    )
    args = parser.parse_args()

    conn = sqlite3.connect(args.db_path)
    conn.row_factory = sqlite3.Row
    prop_filter = "" if args.all_final_dates else "AND EXISTS (SELECT 1 FROM prop_lines pl WHERE pl.game_id = g.id)"
    missing_rows = conn.execute(
        f"""
        SELECT g.id, g.game_date
        FROM games g
        WHERE g.status = 'final'
          AND g.spread_home IS NULL
          AND g.game_date BETWEEN ? AND ?
          {prop_filter}
        ORDER BY g.game_date, g.id
        """,
        (args.start_date, args.end_date),
    ).fetchall()
    missing_ids = {int(row["id"]) for row in missing_rows}
    selected_dates = sorted({str(row["game_date"]) for row in missing_rows})
    errors: list[dict[str, object]] = []
    fetched_events = 0

    with ThreadPoolExecutor(max_workers=max(1, args.workers)) as executor:
        futures = {executor.submit(_fetch_date, item): item for item in selected_dates}
        for future in as_completed(futures):
            selected_date, pages, fetch_errors = future.result()
            fetched_events += len(pages)
            for message in fetch_errors:
                errors.append({"date": selected_date, "error": message})
            for game, page in pages:
                try:
                    _metadata_from_page(conn, game, page)
                except Exception as exc:
                    errors.append({"date": selected_date, "event_id": game.event_id, "error": str(exc)})
            conn.commit()

    repaired = 0
    if missing_ids:
        placeholders = ",".join("?" for _ in missing_ids)
        repaired = int(
            conn.execute(
                f"SELECT COUNT(*) FROM games WHERE id IN ({placeholders}) AND spread_home IS NOT NULL",
                tuple(sorted(missing_ids)),
            ).fetchone()[0]
        )
    conn.close()
    print(
        json.dumps(
            {
                "dates": len(selected_dates),
                "missing_games_before": len(missing_ids),
                "fetched_events": fetched_events,
                "repaired_games": repaired,
                "remaining_games": len(missing_ids) - repaired,
                "errors": errors,
            },
            indent=2,
        )
    )
    return 0 if repaired == len(missing_ids) else 1


if __name__ == "__main__":
    raise SystemExit(main())
