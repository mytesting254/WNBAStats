from __future__ import annotations

import csv
import sqlite3
from pathlib import Path
from typing import Optional

from .bootstrap import TEAM_ALIASES, ensure_teams
from .db import connect


def normalize_team_key(team_name: str) -> Optional[str]:
    if not team_name:
        return None
    key = team_name.strip()
    if not key:
        return None

    alias = TEAM_ALIASES.get(key.lower())
    if alias:
        return alias

    normalized = key.upper()
    if len(normalized) <= 3:
        return normalized
    return TEAM_ALIASES.get(key.lower(), normalized if normalized in TEAM_ALIASES.values() else None)


def resolve_team_id(conn: sqlite3.Connection, team_name: str) -> int:
    key = normalize_team_key(team_name)
    if not key:
        raise ValueError(f"Unable to normalize team name: {team_name!r}")

    row = conn.execute(
        "SELECT id FROM teams WHERE upper(abbreviation) = ? OR lower(name) = ?",
        (key.upper(), team_name.strip().lower()),
    ).fetchone()
    if not row:
        raise ValueError(f"Team not found in database: {team_name!r} (resolved as {key})")
    return int(row["id"])


def parse_int(value: str | None) -> Optional[int]:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return int(value)
    except ValueError:
        return None


def parse_float(value: str | None) -> Optional[float]:
    if value is None:
        return None
    value = str(value).strip()
    if not value:
        return None
    try:
        return float(value)
    except ValueError:
        return None


def determine_ats_result(home_points: int, away_points: int, spread_home: Optional[float]) -> str:
    if spread_home is None:
        spread_home = 0.0
    spread_margin = (home_points - away_points) + spread_home
    if spread_margin == 0:
        return "push"
    return "cover" if spread_margin > 0 else "no_cover"


def determine_total_result(home_points: int, away_points: int, game_total: Optional[float]) -> str:
    if game_total is None:
        return "push"
    total_score = home_points + away_points
    if total_score == game_total:
        return "push"
    return "over" if total_score > game_total else "under"


def import_game_history(conn: sqlite3.Connection, csv_path: str | Path, clear_existing: bool = False) -> dict:
    csv_path = Path(csv_path)
    if not csv_path.exists():
        raise FileNotFoundError(f"Game history CSV not found: {csv_path}")

    if clear_existing:
        # Delete in order to avoid foreign key constraints
        conn.execute("DELETE FROM settled_props")
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines")
        conn.execute("DELETE FROM player_game_stats")
        conn.execute("DELETE FROM sportsbook_prop_lines")
        conn.execute("DELETE FROM team_game_results")
        conn.execute("DELETE FROM games")
    ensure_teams(conn)

    inserted_games = 0
    inserted_results = 0

    with csv_path.open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        for row in reader:
            # Skip comment lines
            if row.get("game_id", "").startswith("#"):
                continue
            home_team_id = resolve_team_id(conn, row.get("home_team", ""))
            away_team_id = resolve_team_id(conn, row.get("away_team", ""))
            game_id = parse_int(row.get("game_id"))
            game_date = row.get("game_date", "").strip()
            start_time = row.get("start_time", "").strip() or f"{game_date}T00:00:00"
            status = row.get("status", "").strip().lower() or "scheduled"
            rest_days_home = parse_int(row.get("rest_days_home")) or 2
            rest_days_away = parse_int(row.get("rest_days_away")) or 2
            spread_home = parse_float(row.get("spread_home"))
            game_total = parse_float(row.get("game_total"))
            home_points = parse_int(row.get("home_points"))
            away_points = parse_int(row.get("away_points"))

            insert_sql = (
                "INSERT OR REPLACE INTO games "
                "(id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            )
            cursor = conn.execute(
                insert_sql,
                (
                    game_id,
                    game_date,
                    start_time,
                    home_team_id,
                    away_team_id,
                    status,
                    rest_days_home,
                    rest_days_away,
                    spread_home,
                    game_total,
                ),
            )
            inserted_games += 1
            game_row_id = cursor.lastrowid

            if status == "final" and home_points is not None and away_points is not None:
                closing_spread = spread_home if spread_home is not None else 0.0
                closing_total = game_total if game_total is not None else float(home_points + away_points)
                ats_result = determine_ats_result(home_points, away_points, closing_spread)
                total_result = determine_total_result(home_points, away_points, game_total)
                possessions = parse_float(row.get("possessions")) or 78.0

                result_rows = [
                    (
                        None,
                        home_team_id,
                        game_row_id,
                        1,
                        home_points,
                        away_points,
                        possessions,
                        "historical_csv" if row.get("possessions") not in (None, "") else "fallback",
                        closing_spread,
                        closing_total,
                        ats_result,
                        total_result,
                    ),
                    (
                        None,
                        away_team_id,
                        game_row_id,
                        0,
                        away_points,
                        home_points,
                        possessions,
                        "historical_csv" if row.get("possessions") not in (None, "") else "fallback",
                        -closing_spread,
                        closing_total,
                        "cover" if ats_result == "no_cover" else "no_cover" if ats_result == "cover" else "push",
                        total_result,
                    ),
                ]
                conn.executemany(
                    """
                    INSERT INTO team_game_results (
                        id, team_id, game_id, is_home, points, opponent_points, possessions,
                        possessions_source, closing_spread, closing_total, ats_result, total_result
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    result_rows,
                )
                inserted_results += 2

    conn.commit()
    return {
        "inserted_games": inserted_games,
        "inserted_team_game_results": inserted_results,
        "source_file": str(csv_path),
        "clear_existing": clear_existing,
    }


def import_game_history_file(csv_path: str | Path, clear_existing: bool = False) -> dict:
    with connect() as conn:
        return import_game_history(conn, csv_path, clear_existing)
