from __future__ import annotations

from datetime import datetime, timedelta, timezone
import sqlite3

from .bootstrap import ensure_team
from .timezone_utils import APP_TIMEZONE


LOCAL_TZ = APP_TIMEZONE
MATCH_TOLERANCE_SECONDS = 6 * 60 * 60
TEAM_RECENT_WINDOW = 5
H2H_WINDOW = 5


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
    home_moneyline: float | None = None,
    away_moneyline: float | None = None,
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
                home_moneyline=home_moneyline,
                away_moneyline=away_moneyline,
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
                home_moneyline=home_moneyline,
                away_moneyline=away_moneyline,
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
            _update_game_market(conn, game_id, spread_home, game_total, home_moneyline, away_moneyline)
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
    _update_game_market(conn, game_id, spread_home, game_total, home_moneyline, away_moneyline)
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
    home_moneyline: float | None,
    away_moneyline: float | None,
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
    _update_game_market(conn, game_id, spread_home, game_total, home_moneyline, away_moneyline)


def _update_game_market(
    conn: sqlite3.Connection,
    game_id: int,
    spread_home: float | None,
    game_total: float | None,
    home_moneyline: float | None,
    away_moneyline: float | None,
) -> None:
    updates = []
    params: list[object] = []
    if spread_home is not None:
        updates.append("spread_home = ?")
        params.append(float(spread_home))
    if game_total is not None and float(game_total) > 0:
        updates.append("game_total = ?")
        params.append(float(game_total))
    if home_moneyline is not None:
        updates.append("home_moneyline = ?")
        params.append(float(home_moneyline))
    if away_moneyline is not None:
        updates.append("away_moneyline = ?")
        params.append(float(away_moneyline))
    if updates:
        params.append(int(game_id))
        conn.execute(f"UPDATE games SET {', '.join(updates)} WHERE id = ?", params)
    if game_total is None or float(game_total) <= 0:
        _backfill_game_total_if_missing(conn, int(game_id))


def _backfill_game_total_if_missing(conn: sqlite3.Connection, game_id: int) -> None:
    row = conn.execute(
        """
        SELECT home_team_id, away_team_id, game_total
        FROM games
        WHERE id = ?
        LIMIT 1
        """,
        (game_id,),
    ).fetchone()
    if not row:
        return
    current_total = row["game_total"]
    if current_total is not None and float(current_total) > 0:
        return
    home_team_id = int(row["home_team_id"])
    away_team_id = int(row["away_team_id"])
    estimate = _estimate_game_total(conn, game_id=game_id, home_team_id=home_team_id, away_team_id=away_team_id)
    if estimate is None:
        return
    conn.execute(
        """
        UPDATE games
        SET game_total = ?
        WHERE id = ?
          AND (game_total IS NULL OR game_total <= 0)
        """,
        (round(float(estimate), 1), game_id),
    )


def _estimate_game_total(conn: sqlite3.Connection, *, game_id: int, home_team_id: int, away_team_id: int) -> float | None:
    home_recent = _recent_team_totals(conn, team_id=home_team_id, is_home=True, limit=TEAM_RECENT_WINDOW, exclude_game_id=game_id)
    away_recent = _recent_team_totals(conn, team_id=away_team_id, is_home=False, limit=TEAM_RECENT_WINDOW, exclude_game_id=game_id)
    h2h_recent = _recent_h2h_totals(
        conn,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        limit=H2H_WINDOW,
        exclude_game_id=game_id,
    )

    components: list[tuple[float, float]] = []
    if home_recent:
        components.append((sum(home_recent) / len(home_recent), 0.4))
    if away_recent:
        components.append((sum(away_recent) / len(away_recent), 0.4))
    if h2h_recent:
        components.append((sum(h2h_recent) / len(h2h_recent), 0.2))
    if not components:
        return None
    weight_sum = sum(weight for _, weight in components)
    return sum(value * (weight / weight_sum) for value, weight in components)


def _recent_team_totals(
    conn: sqlite3.Connection,
    *,
    team_id: int,
    is_home: bool,
    limit: int,
    exclude_game_id: int | None = None,
) -> list[float]:
    rows = conn.execute(
        """
        SELECT r.points, r.opponent_points
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        WHERE r.team_id = ?
          AND r.is_home = ?
          AND (? IS NULL OR r.game_id != ?)
          AND lower(g.status) IN ('final', 'completed')
        ORDER BY g.game_date DESC, r.game_id DESC
        LIMIT ?
        """,
        (int(team_id), 1 if is_home else 0, exclude_game_id, exclude_game_id, int(limit)),
    ).fetchall()
    return [float(row["points"]) + float(row["opponent_points"]) for row in rows]


def _recent_h2h_totals(
    conn: sqlite3.Connection,
    *,
    home_team_id: int,
    away_team_id: int,
    limit: int,
    exclude_game_id: int | None = None,
) -> list[float]:
    rows = conn.execute(
        """
        SELECT r.points, r.opponent_points
        FROM games g
        JOIN team_game_results r
          ON r.game_id = g.id
         AND r.team_id = g.home_team_id
        WHERE (
                (g.home_team_id = ? AND g.away_team_id = ?)
             OR (g.home_team_id = ? AND g.away_team_id = ?)
              )
          AND (? IS NULL OR g.id != ?)
          AND lower(g.status) IN ('final', 'completed')
        ORDER BY g.game_date DESC, g.id DESC
        LIMIT ?
        """,
        (
            int(home_team_id),
            int(away_team_id),
            int(away_team_id),
            int(home_team_id),
            exclude_game_id,
            exclude_game_id,
            int(limit),
        ),
    ).fetchall()
    return [float(row["points"]) + float(row["opponent_points"]) for row in rows]


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
