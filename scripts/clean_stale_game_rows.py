from __future__ import annotations

import shutil
import sqlite3
import sys
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect, get_db_path, init_db
from backend.app.projections import rebuild_predictions


def is_espn_game_id(game_id: int) -> bool:
    return game_id >= 400_000_000


def clean_stale_game_rows() -> dict:
    init_db()
    db_path = get_db_path()
    backup_path = db_path.with_name(f"{db_path.stem}.backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}{db_path.suffix}")
    shutil.copy2(db_path, backup_path)

    with connect() as conn:
        conn.execute("PRAGMA foreign_keys = ON")
        duplicate_groups = _duplicate_groups(conn)
        merged = []
        for group in duplicate_groups:
            games = _games_for_group(conn, group)
            canonical = _canonical_game(games)
            stale_games = [game for game in games if int(game["id"]) != int(canonical["id"])]
            for stale in stale_games:
                _merge_game(conn, stale_id=int(stale["id"]), canonical_id=int(canonical["id"]))
                merged.append(
                    {
                        "from": int(stale["id"]),
                        "to": int(canonical["id"]),
                        "game_date": group[0],
                        "home": group[1],
                        "away": group[2],
                    }
                )
        predictions = rebuild_predictions(conn)
        counts = _counts(conn)
        remaining_duplicate_groups = len(_duplicate_groups(conn))
        conn.commit()

    return {
        "backup": str(backup_path),
        "merged_games": merged,
        "remaining_duplicate_groups": remaining_duplicate_groups,
        "counts": counts,
        "rebuilt_predictions": len(predictions),
    }


def _duplicate_groups(conn: sqlite3.Connection) -> list[tuple[str, str, str]]:
    rows = conn.execute(
        """
        SELECT
          g.game_date,
          h.abbreviation AS home,
          a.abbreviation AS away
        FROM games g
        JOIN teams h ON h.id = g.home_team_id
        JOIN teams a ON a.id = g.away_team_id
        WHERE g.game_date >= '2026-01-01'
          AND g.game_date < '2027-01-01'
        """
    ).fetchall()
    counts = Counter((row["game_date"], row["home"], row["away"]) for row in rows)
    return [key for key, count in counts.items() if count > 1]


def _games_for_group(conn: sqlite3.Connection, group: tuple[str, str, str]) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
          g.*,
          h.abbreviation AS home,
          a.abbreviation AS away
        FROM games g
        JOIN teams h ON h.id = g.home_team_id
        JOIN teams a ON a.id = g.away_team_id
        WHERE g.game_date = ?
          AND h.abbreviation = ?
          AND a.abbreviation = ?
        ORDER BY g.id
        """,
        group,
    ).fetchall()


def _canonical_game(games: list[sqlite3.Row]) -> sqlite3.Row:
    espn_games = [game for game in games if is_espn_game_id(int(game["id"]))]
    if espn_games:
        return espn_games[0]
    final_games = [game for game in games if game["status"] == "final"]
    if final_games:
        return final_games[0]
    return games[0]


def _merge_game(conn: sqlite3.Connection, stale_id: int, canonical_id: int) -> None:
    stale = conn.execute("SELECT * FROM games WHERE id = ?", (stale_id,)).fetchone()
    canonical = conn.execute("SELECT * FROM games WHERE id = ?", (canonical_id,)).fetchone()
    if not stale or not canonical:
        return

    status = "final" if stale["status"] == "final" or canonical["status"] == "final" else canonical["status"]
    game_total = canonical["game_total"] if canonical["game_total"] is not None else stale["game_total"]
    spread_home = canonical["spread_home"] if canonical["spread_home"] is not None else stale["spread_home"]
    conn.execute(
        """
        UPDATE games
        SET status = ?,
            game_total = ?,
            spread_home = ?
        WHERE id = ?
        """,
        (status, game_total, spread_home, canonical_id),
    )

    for table in ("sportsbook_prop_lines", "prop_lines", "player_game_stats", "team_game_results"):
        conn.execute(f"UPDATE {table} SET game_id = ? WHERE game_id = ?", (canonical_id, stale_id))
    conn.execute("DELETE FROM games WHERE id = ?", (stale_id,))


def _counts(conn: sqlite3.Connection) -> dict:
    return {
        table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
        for table in (
            "games",
            "team_game_results",
            "player_game_stats",
            "sportsbook_prop_lines",
            "prop_lines",
            "prop_predictions",
        )
    }


if __name__ == "__main__":
    result = clean_stale_game_rows()
    print(f"Backup: {result['backup']}")
    print(f"Merged games: {len(result['merged_games'])}")
    for item in result["merged_games"]:
        print(f"{item['from']} -> {item['to']} {item['game_date']} {item['away']} at {item['home']}")
    print(f"Remaining duplicate groups: {result['remaining_duplicate_groups']}")
    print(f"Rebuilt predictions: {result['rebuilt_predictions']}")
    for table, count in result["counts"].items():
        print(f"{table}: {count}")
