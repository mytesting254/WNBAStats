from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from .bootstrap import ensure_team


LOCAL_TZ = timezone(timedelta(hours=-4))
MATCH_TOLERANCE_SECONDS = 6 * 60 * 60


def resolve_or_create_game(
    conn: sqlite3.Connection,
    *,
    home_team: str,
    away_team: str,
    start_time: str,
    game_date: str | None = None,
    espn_event_id: int | None = None,
    spread_home: float | None = None,
    game_total: float | None = None,
) -> int | None:
    home_team_id = ensure_team(conn, home_team)
    away_team_id = ensure_team(conn, away_team)
    if not home_team_id or not away_team_id:
        return None

    parsed_start = _parse_game_start(start_time)
    resolved_date = game_date or _game_date_from_start(start_time)

    if espn_event_id is not None:
        row = conn.execute(
            "SELECT id FROM games WHERE espn_event_id = ? LIMIT 1",
            (int(espn_event_id),),
        ).fetchone()
        if row:
            game_id = int(row["id"])
            _update_game_identity(
                conn,
                game_id=game_id,
                game_date=resolved_date,
                start_time=start_time,
                home_team_id=int(home_team_id),
                away_team_id=int(away_team_id),
                espn_event_id=int(espn_event_id),
                spread_home=spread_home,
                game_total=game_total,
            )
            return game_id

    rows = conn.execute(
        """
        SELECT id, start_time
        FROM games
        WHERE home_team_id = ?
          AND away_team_id = ?
        ORDER BY start_time
        """,
        (int(home_team_id), int(away_team_id)),
    ).fetchall()
    for row in rows:
        existing_start = _parse_game_start(str(row["start_time"]))
        if parsed_start and existing_start and abs((existing_start - parsed_start).total_seconds()) <= MATCH_TOLERANCE_SECONDS:
            game_id = int(row["id"])
            _update_game_identity(
                conn,
                game_id=game_id,
                game_date=resolved_date,
                start_time=start_time,
                home_team_id=int(home_team_id),
                away_team_id=int(away_team_id),
                espn_event_id=espn_event_id,
                spread_home=spread_home,
                game_total=game_total,
            )
            return game_id

    if espn_event_id is not None:
        id_taken = conn.execute("SELECT 1 FROM games WHERE id = ? LIMIT 1", (int(espn_event_id),)).fetchone()
        if not id_taken:
            conn.execute(
                """
                INSERT INTO games (
                    id, game_date, start_time, home_team_id, away_team_id, status,
                    rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
                ) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, NULL, NULL, ?)
                """,
                (int(espn_event_id), resolved_date, start_time, int(home_team_id), int(away_team_id), int(espn_event_id)),
            )
            game_id = int(espn_event_id)
            _update_game_market(conn, game_id, spread_home, game_total)
            return game_id

    cursor = conn.execute(
        """
        INSERT INTO games (
            game_date, start_time, home_team_id, away_team_id, status,
            rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
        ) VALUES (?, ?, ?, ?, 'scheduled', 2, 2, NULL, NULL, ?)
        """,
        (resolved_date, start_time, int(home_team_id), int(away_team_id), espn_event_id),
    )
    game_id = int(cursor.lastrowid)
    _update_game_market(conn, game_id, spread_home, game_total)
    return game_id


def _update_game_identity(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    game_date: str,
    start_time: str,
    home_team_id: int,
    away_team_id: int,
    espn_event_id: int | None,
    spread_home: float | None,
    game_total: float | None,
) -> None:
    conn.execute(
        """
        UPDATE games
        SET game_date = ?,
            start_time = ?,
            home_team_id = ?,
            away_team_id = ?,
            espn_event_id = COALESCE(?, espn_event_id)
        WHERE id = ?
        """,
        (game_date, start_time, home_team_id, away_team_id, espn_event_id, game_id),
    )
    _update_game_market(conn, game_id, spread_home, game_total)


def _update_game_market(conn: sqlite3.Connection, game_id: int, spread_home: float | None, game_total: float | None) -> None:
    updates = []
    params: list[object] = []
    if spread_home is not None:
        updates.append("spread_home = ?")
        params.append(float(spread_home))
    if game_total is not None and float(game_total) > 0:
        updates.append("game_total = ?")
        params.append(float(game_total))
    if not updates:
        return
    params.append(int(game_id))
    conn.execute(f"UPDATE games SET {', '.join(updates)} WHERE id = ?", params)


def _parse_game_start(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc)


def _game_date_from_start(start_time: str) -> str:
    parsed = _parse_game_start(start_time)
    if parsed is None:
        return start_time[:10]
    return parsed.astimezone(LOCAL_TZ).date().isoformat()
