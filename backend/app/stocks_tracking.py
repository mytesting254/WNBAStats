from __future__ import annotations

import math
import os
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .db import connect
from .paths import get_db_path
from .player_prop_model import COMPONENT_MODEL_VERSION, _rest_factor, feature_snapshot
from .timezone_utils import APP_TIMEZONE, local_today_iso


_SNAPSHOT_JOB_LOCK = threading.Lock()
_SNAPSHOT_JOB_RUNNING = False
_PREP_JOB_LOCK = threading.Lock()
_PREP_JOB_RUNNING = False
_SCHEMA_INIT_LOCK = threading.Lock()
_STOCKS_RECENT_WINDOW_GAMES = 10
_STOCKS_STABILITY_WINDOW_GAMES = 20


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def get_tracking_db_path() -> Path:
    configured = os.getenv("WNBA_STOCKS_TRACKING_DB", "").strip()
    if configured:
        return Path(configured)
    return get_db_path().with_name("stocks_tracking.sqlite")


def ensure_tracking_schema() -> Path:
    path = get_tracking_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with _SCHEMA_INIT_LOCK:
        with sqlite3.connect(path) as conn:
            conn.executescript(
                """
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS projection_snapshots (
                    id INTEGER PRIMARY KEY,
                    game_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    player_name TEXT NOT NULL,
                    game_date TEXT NOT NULL,
                    captured_at TEXT NOT NULL,
                    model_version TEXT NOT NULL,
                    projected_steals REAL NOT NULL,
                    projected_blocks REAL NOT NULL,
                    projected_stocks REAL NOT NULL,
                    steal_prob_1_plus REAL NOT NULL,
                    steal_prob_2_plus REAL NOT NULL,
                    block_prob_1_plus REAL NOT NULL,
                    block_prob_2_plus REAL NOT NULL,
                    stocks_prob_2_plus REAL NOT NULL,
                    stocks_prob_3_plus REAL NOT NULL DEFAULT 0,
                    data_quality TEXT NOT NULL,
                    UNIQUE(game_id, player_id, model_version, captured_at)
                );
                CREATE TABLE IF NOT EXISTS settlements (
                    snapshot_id INTEGER PRIMARY KEY,
                    actual_steals INTEGER NOT NULL,
                    actual_blocks INTEGER NOT NULL,
                    settled_at TEXT NOT NULL,
                    FOREIGN KEY(snapshot_id) REFERENCES projection_snapshots(id)
                );
                CREATE TABLE IF NOT EXISTS prepared_games (
                    game_id INTEGER PRIMARY KEY,
                    game_date TEXT NOT NULL,
                    start_time TEXT NOT NULL,
                    home_team_id INTEGER NOT NULL,
                    away_team_id INTEGER NOT NULL,
                    espn_event_id INTEGER,
                    prepared_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS candidate_players (
                    game_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    player_name TEXT NOT NULL,
                    game_date TEXT NOT NULL,
                    team_id INTEGER,
                    team_abbr TEXT,
                    rotation_role TEXT,
                    recent_minutes_avg REAL NOT NULL DEFAULT 0,
                    recent_stocks_avg REAL NOT NULL DEFAULT 0,
                    recent_games INTEGER NOT NULL DEFAULT 0,
                    candidate_reason TEXT NOT NULL,
                    built_at TEXT NOT NULL,
                    PRIMARY KEY (game_id, player_id)
                );
                CREATE TABLE IF NOT EXISTS player_prep_features (
                    game_id INTEGER NOT NULL,
                    player_id INTEGER NOT NULL,
                    player_name TEXT NOT NULL,
                    game_date TEXT NOT NULL,
                    market TEXT NOT NULL,
                    base_projection REAL NOT NULL DEFAULT 0,
                    contextual_projection REAL NOT NULL DEFAULT 0,
                    recent_avg REAL NOT NULL DEFAULT 0,
                    stability_avg REAL NOT NULL DEFAULT 0,
                    same_venue_avg REAL NOT NULL DEFAULT 0,
                    same_venue_games INTEGER NOT NULL DEFAULT 0,
                    recent_hit_rate_2_plus REAL,
                    rest_days INTEGER NOT NULL DEFAULT 2,
                    is_home INTEGER,
                    built_at TEXT NOT NULL,
                    PRIMARY KEY (game_id, player_id, market)
                );
                CREATE TABLE IF NOT EXISTS team_prep_context (
                    game_id INTEGER NOT NULL,
                    game_date TEXT NOT NULL,
                    team_id INTEGER NOT NULL,
                    opponent_id INTEGER NOT NULL,
                    is_home INTEGER NOT NULL,
                    pace_factor REAL NOT NULL DEFAULT 1,
                    steals_allowed_factor REAL NOT NULL DEFAULT 1,
                    blocks_allowed_factor REAL NOT NULL DEFAULT 1,
                    stocks_allowed_factor REAL NOT NULL DEFAULT 1,
                    turnover_pressure_factor REAL NOT NULL DEFAULT 1,
                    built_at TEXT NOT NULL,
                    PRIMARY KEY (game_id, team_id)
                );
                CREATE INDEX IF NOT EXISTS idx_stocks_snapshots_game ON projection_snapshots(game_id, player_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_prepared_games_date ON prepared_games(game_date, start_time, game_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_candidates_game ON candidate_players(game_id, player_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_player_prep_game ON player_prep_features(game_id, player_id, market);
                CREATE INDEX IF NOT EXISTS idx_stocks_player_prep_date ON player_prep_features(game_date, game_id, player_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_team_context_game ON team_prep_context(game_id, team_id);
                """
            )
            columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(projection_snapshots)").fetchall()}
            if "stocks_prob_3_plus" not in columns:
                try:
                    conn.execute(
                        "ALTER TABLE projection_snapshots ADD COLUMN stocks_prob_3_plus REAL NOT NULL DEFAULT 0"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            stale_three_plus_rows = conn.execute(
                """
                SELECT id, projected_stocks
                FROM projection_snapshots
                WHERE stocks_prob_3_plus = 0
                  AND projected_stocks > 0
                """
            ).fetchall()
            if stale_three_plus_rows:
                conn.executemany(
                    "UPDATE projection_snapshots SET stocks_prob_3_plus = ? WHERE id = ?",
                    [
                        (_poisson_at_least(float(projected_stocks), 3), int(snapshot_id))
                        for snapshot_id, projected_stocks in stale_three_plus_rows
                    ],
                )
    return path


def _open_tracking_connection() -> sqlite3.Connection:
    path = ensure_tracking_schema()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _stocks_market_value(row: sqlite3.Row, market: str) -> float:
    if market == "blocks_steals":
        return float((row["blocks"] or 0.0) + (row["steals"] or 0.0))
    return float(row[market] or 0.0)


def _stocks_market_column(market: str) -> str:
    if market == "blocks_steals":
        return "(COALESCE(s.blocks, 0) + COALESCE(s.steals, 0))"
    return f"COALESCE(s.{market}, 0)"


def _stocks_avg_scalar(conn: sqlite3.Connection, sql: str, params: tuple[object, ...] = ()) -> float | None:
    row = conn.execute(sql, params).fetchone()
    if row is None:
        return None
    value = row[0]
    if value is None:
        return None
    return float(value)


def _stocks_pace_factor(conn: sqlite3.Connection, team_id: int, opponent_id: int) -> float:
    league_pace = _stocks_avg_scalar(conn, "SELECT AVG(possessions) FROM team_game_results") or 78.0
    team_pace = _stocks_avg_scalar(conn, "SELECT AVG(possessions) FROM team_game_results WHERE team_id = ?", (team_id,)) or league_pace
    opponent_pace = _stocks_avg_scalar(conn, "SELECT AVG(possessions) FROM team_game_results WHERE team_id = ?", (opponent_id,)) or league_pace
    return _clamp(((team_pace + opponent_pace) / 2.0) / league_pace, 0.94, 1.06)


def _stocks_opponent_allowed_factor(conn: sqlite3.Connection, opponent_id: int, market: str) -> float:
    market_sql = _stocks_market_column(market)
    opponent_allowed = _stocks_avg_scalar(
        conn,
        f"""
        SELECT AVG({market_sql})
        FROM player_game_stats s
        JOIN players p ON p.id = s.player_id
        JOIN games g ON g.id = s.game_id
        WHERE CASE
            WHEN p.team_id = g.home_team_id THEN g.away_team_id
            ELSE g.home_team_id
        END = ?
        """,
        (int(opponent_id),),
    )
    league_allowed = _stocks_avg_scalar(
        conn,
        f"""
        SELECT AVG({market_sql})
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        """
    )
    if opponent_allowed is None or league_allowed is None or league_allowed <= 0:
        return 1.0
    return _clamp(opponent_allowed / league_allowed, 0.90, 1.10)


def _stocks_turnover_pressure_factor(conn: sqlite3.Connection, opponent_id: int) -> float:
    opponent_turnovers = _stocks_avg_scalar(
        conn,
        """
        SELECT AVG(COALESCE(s.turnovers, 0))
        FROM player_game_stats s
        JOIN players p ON p.id = s.player_id
        JOIN games g ON g.id = s.game_id
        WHERE CASE
            WHEN p.team_id = g.home_team_id THEN g.away_team_id
            ELSE g.home_team_id
        END = ?
        """,
        (int(opponent_id),),
    )
    league_turnovers = _stocks_avg_scalar(conn, "SELECT AVG(COALESCE(turnovers, 0)) FROM player_game_stats")
    if opponent_turnovers is None or league_turnovers is None or league_turnovers <= 0:
        return 1.0
    return _clamp(opponent_turnovers / league_turnovers, 0.90, 1.12)


def _stocks_matchup_context_multiplier(context: sqlite3.Row | dict[str, object] | None, market: str) -> float:
    if context is None:
        return 1.0
    pace_factor = float(context["pace_factor"] or 1.0)
    steals_allowed_factor = float(context["steals_allowed_factor"] or 1.0)
    blocks_allowed_factor = float(context["blocks_allowed_factor"] or 1.0)
    stocks_allowed_factor = float(context["stocks_allowed_factor"] or 1.0)
    turnover_pressure_factor = float(context["turnover_pressure_factor"] or 1.0)
    if market == "steals":
        return _clamp(
            (0.50 * steals_allowed_factor)
            + (0.25 * turnover_pressure_factor)
            + (0.25 * pace_factor),
            0.90,
            1.12,
        )
    if market == "blocks":
        return _clamp(
            (0.65 * blocks_allowed_factor)
            + (0.35 * pace_factor),
            0.90,
            1.12,
        )
    return _clamp(
        (0.45 * stocks_allowed_factor)
        + (0.20 * steals_allowed_factor)
        + (0.20 * blocks_allowed_factor)
        + (0.15 * pace_factor),
        0.90,
        1.12,
    )


def _player_stocks_target_context(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    game_id: int,
 ) -> tuple[int | None, int]:
    target_row = conn.execute(
        """
        SELECT
            CASE
                WHEN p.team_id = g.home_team_id THEN 1
                WHEN p.team_id = g.away_team_id THEN 0
                ELSE NULL
            END AS is_home,
            CASE
                WHEN p.team_id = g.home_team_id THEN COALESCE(g.rest_days_home, 2)
                WHEN p.team_id = g.away_team_id THEN COALESCE(g.rest_days_away, 2)
                ELSE 2
            END AS rest_days
        FROM games g
        JOIN players p ON p.id = ?
        WHERE g.id = ?
        LIMIT 1
        """,
        (int(player_id), int(game_id)),
    ).fetchone()
    if target_row is None:
        return None, 2
    target_is_home = target_row["is_home"]
    return (None if target_is_home is None else int(target_is_home), int(target_row["rest_days"] or 2))


def _player_stocks_history_rows(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    game_date: str,
    game_id: int,
) -> list[sqlite3.Row]:
    return conn.execute(
        f"""
        SELECT
            s.steals,
            s.blocks,
            CASE
                WHEN COALESCE((
                    SELECT h.team_id
                    FROM player_team_history h
                    WHERE h.player_id = s.player_id
                      AND h.game_id = s.game_id
                    ORDER BY h.id DESC
                    LIMIT 1
                ), p.team_id) = g.home_team_id THEN 1
                WHEN COALESCE((
                    SELECT h.team_id
                    FROM player_team_history h
                    WHERE h.player_id = s.player_id
                      AND h.game_id = s.game_id
                    ORDER BY h.id DESC
                    LIMIT 1
                ), p.team_id) = g.away_team_id THEN 0
                ELSE NULL
            END AS is_home,
            CASE
                WHEN COALESCE((
                    SELECT h.team_id
                    FROM player_team_history h
                    WHERE h.player_id = s.player_id
                      AND h.game_id = s.game_id
                    ORDER BY h.id DESC
                    LIMIT 1
                ), p.team_id) = g.home_team_id THEN COALESCE(g.rest_days_home, 2)
                WHEN COALESCE((
                    SELECT h.team_id
                    FROM player_team_history h
                    WHERE h.player_id = s.player_id
                      AND h.game_id = s.game_id
                    ORDER BY h.id DESC
                    LIMIT 1
                ), p.team_id) = g.away_team_id THEN COALESCE(g.rest_days_away, 2)
                ELSE 2
            END AS rest_days
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN players p ON p.id = s.player_id
        WHERE s.player_id = ?
          AND g.game_date < ?
          AND s.game_id <> ?
        ORDER BY
            CASE
                WHEN substr(g.game_date, 1, 4) = substr(?, 1, 4) THEN 0
                ELSE 1
            END,
            g.game_date DESC,
            s.game_id DESC
        LIMIT {_STOCKS_STABILITY_WINDOW_GAMES}
        """,
        (int(player_id), str(game_date), int(game_id), str(game_date)),
    ).fetchall()


def _build_player_stocks_feature_bundle(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    game_id: int,
    game_date: str,
    market: str,
    base_projection: float,
    matchup_context: sqlite3.Row | dict[str, object] | None = None,
) -> dict[str, float | int | None]:
    target_is_home, target_rest_days = _player_stocks_target_context(conn, player_id=player_id, game_id=game_id)
    rows = _player_stocks_history_rows(conn, player_id=player_id, game_date=game_date, game_id=game_id)
    if target_is_home is None:
        return {
            "contextual_projection": float(base_projection),
            "recent_avg": 0.0,
            "stability_avg": 0.0,
            "same_venue_avg": 0.0,
            "same_venue_games": 0,
            "recent_hit_rate_2_plus": None,
            "rest_days": target_rest_days,
            "is_home": None,
        }
    if not rows:
        return {
            "contextual_projection": max(0.0, float(base_projection) * _rest_factor(target_rest_days)),
            "recent_avg": 0.0,
            "stability_avg": 0.0,
            "same_venue_avg": 0.0,
            "same_venue_games": 0,
            "recent_hit_rate_2_plus": None,
            "rest_days": target_rest_days,
            "is_home": target_is_home,
        }
    values = [_stocks_market_value(row, market) for row in rows]
    recent_values = values[:_STOCKS_RECENT_WINDOW_GAMES]
    recent_avg = sum(recent_values) / len(recent_values)
    stability_avg = sum(values) / len(values)
    all_anchor = (
        (0.65 * recent_avg)
        + (0.35 * stability_avg)
    )
    same_venue_rows = [row for row in rows if row["is_home"] is not None and int(row["is_home"]) == target_is_home]
    same_venue_avg = 0.0
    if len(same_venue_rows) >= 3:
        same_venue_values = [_stocks_market_value(row, market) for row in same_venue_rows]
        same_venue_avg = sum(same_venue_values) / len(same_venue_values)
        venue_recent = same_venue_values[:_STOCKS_RECENT_WINDOW_GAMES]
        venue_anchor = (
            (0.70 * (sum(venue_recent) / len(venue_recent)))
            + (0.30 * same_venue_avg)
        )
        contextual_anchor = (0.65 * all_anchor) + (0.35 * venue_anchor)
    elif same_venue_rows:
        same_venue_values = [_stocks_market_value(row, market) for row in same_venue_rows]
        same_venue_avg = sum(same_venue_values) / len(same_venue_values)
        contextual_anchor = (0.85 * all_anchor) + (0.15 * same_venue_avg)
    else:
        contextual_anchor = all_anchor
    rest_adjusted_anchor = contextual_anchor * _rest_factor(target_rest_days)
    stabilized = (0.72 * float(base_projection)) + (0.28 * float(rest_adjusted_anchor))
    matchup_multiplier = _stocks_matchup_context_multiplier(matchup_context, market)
    stabilized *= matchup_multiplier
    overall_hits = [1.0 if (_stocks_market_value(row, "blocks_steals")) >= 2.0 else 0.0 for row in rows]
    recent_hits = overall_hits[:_STOCKS_RECENT_WINDOW_GAMES]
    overall_rate = (
        (0.65 * (sum(recent_hits) / len(recent_hits)))
        + (0.35 * (sum(overall_hits) / len(overall_hits)))
    )
    same_venue_hits = [
        1.0 if _stocks_market_value(row, "blocks_steals") >= 2.0 else 0.0
        for row in rows
        if row["is_home"] is not None and int(row["is_home"]) == target_is_home
    ]
    recent_hit_rate = overall_rate
    if len(same_venue_hits) >= 3:
        venue_recent_hits = same_venue_hits[:_STOCKS_RECENT_WINDOW_GAMES]
        venue_rate = (
            (0.70 * (sum(venue_recent_hits) / len(venue_recent_hits)))
            + (0.30 * (sum(same_venue_hits) / len(same_venue_hits)))
        )
        recent_hit_rate = (0.70 * overall_rate) + (0.30 * venue_rate)
    elif same_venue_hits:
        recent_hit_rate = (0.85 * overall_rate) + (0.15 * (sum(same_venue_hits) / len(same_venue_hits)))
    return {
        "contextual_projection": max(0.0, stabilized),
        "recent_avg": recent_avg,
        "stability_avg": stability_avg,
        "same_venue_avg": same_venue_avg,
        "same_venue_games": len(same_venue_rows),
        "recent_hit_rate_2_plus": recent_hit_rate,
        "rest_days": target_rest_days,
        "is_home": target_is_home,
    }


def _contextual_stocks_component_projection(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    game_id: int,
    game_date: str,
    market: str,
    base_projection: float,
    matchup_context: sqlite3.Row | dict[str, object] | None = None,
) -> float:
    bundle = _build_player_stocks_feature_bundle(
        conn,
        player_id=player_id,
        game_id=game_id,
        game_date=game_date,
        market=market,
        base_projection=base_projection,
        matchup_context=matchup_context,
    )
    return float(bundle["contextual_projection"] or 0.0)


def rebuild_prepared_games(
    conn: sqlite3.Connection,
    *,
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
) -> int:
    tracking = _open_tracking_connection()
    try:
        normalized_game_ids = tuple(sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0}))
        normalized_dates = tuple(sorted({str(game_date).strip() for game_date in (target_dates or []) if str(game_date).strip()}))
        filter_sql = ""
        params: tuple[object, ...] = ()
        if normalized_game_ids:
            placeholders = ",".join("?" for _ in normalized_game_ids)
            filter_sql = f" AND g.id IN ({placeholders})"
            params = normalized_game_ids
        elif normalized_dates:
            placeholders = ",".join("?" for _ in normalized_dates)
            filter_sql = f" AND g.game_date IN ({placeholders})"
            params = normalized_dates
        rows = conn.execute(
            """
            SELECT
                g.id AS game_id,
                g.game_date,
                g.start_time,
                g.home_team_id,
                g.away_team_id,
                g.espn_event_id
            FROM games g
            WHERE g.status = 'scheduled'
            """
            + filter_sql
            + """
            ORDER BY g.game_date, g.start_time, g.id
            """,
            params,
        ).fetchall()
        prepared_at = datetime.now(timezone.utc).isoformat()
        prepared_rows = [
            (
                int(row["game_id"]),
                str(row["game_date"]),
                str(row["start_time"]),
                int(row["home_team_id"]),
                int(row["away_team_id"]),
                int(row["espn_event_id"]) if row["espn_event_id"] is not None else None,
                prepared_at,
            )
            for row in rows
        ]
        with tracking:
            if normalized_game_ids:
                placeholders = ",".join("?" for _ in normalized_game_ids)
                tracking.execute(f"DELETE FROM prepared_games WHERE game_id IN ({placeholders})", normalized_game_ids)
            elif normalized_dates:
                placeholders = ",".join("?" for _ in normalized_dates)
                tracking.execute(f"DELETE FROM prepared_games WHERE game_date IN ({placeholders})", normalized_dates)
            else:
                tracking.execute("DELETE FROM prepared_games")
            if prepared_rows:
                tracking.executemany(
                    """
                    INSERT INTO prepared_games (
                        game_id,
                        game_date,
                        start_time,
                        home_team_id,
                        away_team_id,
                        espn_event_id,
                        prepared_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    prepared_rows,
                )
        return len(prepared_rows)
    finally:
        tracking.close()


def rebuild_candidate_players(
    conn: sqlite3.Connection,
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
) -> int:
    tracking = _open_tracking_connection()
    try:
        normalized_game_ids = tuple(sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0}))
        normalized_dates = tuple(sorted({str(game_date).strip() for game_date in (target_dates or []) if str(game_date).strip()}))
        prepared_filter = ""
        prepared_params: tuple[object, ...] = ()
        if normalized_game_ids:
            placeholders = ",".join("?" for _ in normalized_game_ids)
            prepared_filter = f" AND pg.game_id IN ({placeholders})"
            prepared_params = normalized_game_ids
        elif normalized_dates:
            placeholders = ",".join("?" for _ in normalized_dates)
            prepared_filter = f" AND pg.game_date IN ({placeholders})"
            prepared_params = normalized_dates
        prepared_count_row = tracking.execute(
            """
            SELECT COUNT(*)
            FROM prepared_games pg
            WHERE 1 = 1
            """
            + prepared_filter,
            prepared_params,
        ).fetchone()
        prepared_count = int(prepared_count_row[0]) if prepared_count_row is not None else 0
        if prepared_count <= 0:
            rebuild_prepared_games(
                conn,
                game_ids=list(normalized_game_ids) if normalized_game_ids else None,
                target_dates=list(normalized_dates) if normalized_dates else None,
            )
        selected_games = tracking.execute(
            """
            SELECT
                pg.game_id,
                pg.game_date
            FROM prepared_games pg
            WHERE 1 = 1
            """
            + prepared_filter
            + """
            ORDER BY pg.game_date, pg.start_time, pg.game_id
            """,
            prepared_params,
        ).fetchall()
        selected_game_ids = tuple(sorted({int(row["game_id"]) for row in selected_games}))
        if not selected_game_ids:
            if normalized_game_ids:
                placeholders = ",".join("?" for _ in normalized_game_ids)
                tracking.execute(f"DELETE FROM candidate_players WHERE game_id IN ({placeholders})", normalized_game_ids)
            elif normalized_dates:
                placeholders = ",".join("?" for _ in normalized_dates)
                tracking.execute(f"DELETE FROM candidate_players WHERE game_date IN ({placeholders})", normalized_dates)
            else:
                tracking.execute("DELETE FROM candidate_players")
            return 0
        game_filter = ""
        params: tuple[object, ...] = ()
        placeholders = ",".join("?" for _ in selected_game_ids)
        game_filter = f" AND g.id IN ({placeholders})"
        params = selected_game_ids
        rows = conn.execute(
            """
            WITH scheduled_games AS (
                SELECT
                    g.id AS game_id,
                    g.game_date,
                    g.home_team_id,
                    g.away_team_id
                FROM games g
                WHERE g.status = 'scheduled'
            """
            + game_filter
            + """
            ),
            recent_stats AS (
                SELECT
                    s.player_id,
                    COUNT(*) AS recent_games,
                    AVG(s.minutes) AS recent_minutes_avg,
                    AVG(COALESCE(s.steals, 0) + COALESCE(s.blocks, 0)) AS recent_stocks_avg
                FROM (
                    SELECT
                        s.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY s.player_id
                            ORDER BY g.game_date DESC, s.game_id DESC
                        ) AS stat_rank
                    FROM player_game_stats s
                    JOIN games g ON g.id = s.game_id
                ) s
                WHERE s.stat_rank <= 10
                GROUP BY s.player_id
            ),
            latest_injuries AS (
                SELECT
                    i.player_id,
                    lower(trim(i.status)) AS status,
                    ROW_NUMBER() OVER (
                        PARTITION BY i.player_id
                        ORDER BY i.captured_at DESC, i.id DESC
                    ) AS injury_rank
                FROM injuries i
            )
            SELECT
                sg.game_id,
                sg.game_date,
                p.id AS player_id,
                p.full_name AS player_name,
                p.team_id,
                t.abbreviation AS team_abbr,
                p.rotation_role,
                COALESCE(rs.recent_minutes_avg, 0.0) AS recent_minutes_avg,
                COALESCE(rs.recent_stocks_avg, 0.0) AS recent_stocks_avg,
                COALESCE(rs.recent_games, 0) AS recent_games
            FROM scheduled_games sg
            JOIN players p ON p.team_id IN (sg.home_team_id, sg.away_team_id)
            JOIN teams t ON t.id = p.team_id
            JOIN recent_stats rs ON rs.player_id = p.id
            LEFT JOIN latest_injuries li ON li.player_id = p.id AND li.injury_rank = 1
            WHERE COALESCE(li.status, 'available') NOT IN ('out', 'inactive', 'suspended', 'unavailable')
              AND (
                    COALESCE(rs.recent_minutes_avg, 0.0) >= 14.0
                 OR COALESCE(rs.recent_stocks_avg, 0.0) >= 1.5
                 OR (
                        COALESCE(rs.recent_games, 0) >= 4
                    AND COALESCE(rs.recent_minutes_avg, 0.0) >= 10.0
                 )
              )
            ORDER BY sg.game_id, rs.recent_stocks_avg DESC, rs.recent_minutes_avg DESC, p.full_name
            """,
            params,
        ).fetchall()
        built_at = datetime.now(timezone.utc).isoformat()
        candidate_rows: list[tuple[object, ...]] = []
        touched_game_ids = sorted({int(row["game_id"]) for row in rows})
        for row in rows:
            recent_minutes_avg = float(row["recent_minutes_avg"] or 0.0)
            recent_stocks_avg = float(row["recent_stocks_avg"] or 0.0)
            if recent_stocks_avg >= 1.8:
                candidate_reason = "recent stocks specialist"
            elif recent_minutes_avg >= 22.0:
                candidate_reason = "stable rotation minutes"
            else:
                candidate_reason = "rotation candidate"
            candidate_rows.append(
                (
                    int(row["game_id"]),
                    int(row["player_id"]),
                    str(row["player_name"]),
                    str(row["game_date"]),
                    int(row["team_id"]) if row["team_id"] is not None else None,
                    str(row["team_abbr"] or ""),
                    str(row["rotation_role"] or ""),
                    recent_minutes_avg,
                    recent_stocks_avg,
                    int(row["recent_games"] or 0),
                    candidate_reason,
                    built_at,
                )
            )
        with tracking:
            if normalized_game_ids:
                placeholders = ",".join("?" for _ in normalized_game_ids)
                tracking.execute(f"DELETE FROM candidate_players WHERE game_id IN ({placeholders})", normalized_game_ids)
            elif normalized_dates:
                placeholders = ",".join("?" for _ in normalized_dates)
                tracking.execute(f"DELETE FROM candidate_players WHERE game_date IN ({placeholders})", normalized_dates)
            else:
                tracking.execute("DELETE FROM candidate_players")
            if candidate_rows:
                tracking.executemany(
                    """
                    INSERT INTO candidate_players (
                        game_id,
                        player_id,
                        player_name,
                        game_date,
                        team_id,
                        team_abbr,
                        rotation_role,
                        recent_minutes_avg,
                        recent_stocks_avg,
                        recent_games,
                        candidate_reason,
                        built_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    candidate_rows,
                )
        return len(candidate_rows) if normalized_game_ids or touched_game_ids else 0
    finally:
        tracking.close()


def _candidate_players_for_snapshot(
    conn: sqlite3.Connection,
    tracking: sqlite3.Connection,
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
) -> list[sqlite3.Row]:
    normalized_game_ids = tuple(sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0}))
    normalized_dates = tuple(sorted({str(game_date).strip() for game_date in (target_dates or []) if str(game_date).strip()}))
    filter_sql = ""
    params: tuple[object, ...] = ()
    if normalized_game_ids:
        placeholders = ",".join("?" for _ in normalized_game_ids)
        filter_sql = f" WHERE cp.game_id IN ({placeholders})"
        params = normalized_game_ids
    elif normalized_dates:
        placeholders = ",".join("?" for _ in normalized_dates)
        filter_sql = f" WHERE cp.game_date IN ({placeholders})"
        params = normalized_dates
    rows = tracking.execute(
        """
        SELECT
            cp.game_id,
            cp.game_date,
            cp.player_id,
            cp.player_name,
            cp.team_id
        FROM candidate_players cp
        """
        + filter_sql
        + """
        ORDER BY cp.game_id, cp.recent_stocks_avg DESC, cp.recent_minutes_avg DESC, cp.player_name
        """,
        params,
    ).fetchall()
    if rows:
        return rows
    rebuild_candidate_players(
        conn,
        game_ids=list(normalized_game_ids) if normalized_game_ids else None,
        target_dates=list(normalized_dates) if normalized_dates else None,
    )
    return tracking.execute(
        """
        SELECT
            cp.game_id,
            cp.game_date,
            cp.player_id,
            cp.player_name,
            cp.team_id
        FROM candidate_players cp
        """
        + filter_sql
        + """
        ORDER BY cp.game_id, cp.recent_stocks_avg DESC, cp.recent_minutes_avg DESC, cp.player_name
        """,
        params,
    ).fetchall()


def _poisson_at_least(mean: float, threshold: int) -> float:
    safe_mean = max(0.0, float(mean))
    return 1 - sum(math.exp(-safe_mean) * safe_mean**k / math.factorial(k) for k in range(threshold))


def _calibrated_stocks_two_plus_probability(
    tracking: sqlite3.Connection,
    *,
    projected_stocks: float,
    game_date: str,
) -> float:
    raw_probability = _poisson_at_least(projected_stocks, 2)
    rows = tracking.execute(
        """
        WITH settled_latest AS (
            SELECT
                ps.projected_stocks,
                st.actual_steals,
                st.actual_blocks,
                ROW_NUMBER() OVER (
                    PARTITION BY ps.game_id, ps.player_id
                    ORDER BY ps.captured_at DESC, ps.id DESC
                ) AS snapshot_rank
            FROM projection_snapshots ps
            JOIN settlements st ON st.snapshot_id = ps.id
            WHERE ps.game_date < ?
        )
        SELECT
            projected_stocks,
            actual_steals,
            actual_blocks
        FROM settled_latest
        WHERE snapshot_rank = 1
        ORDER BY ABS(projected_stocks - ?) ASC
        LIMIT 80
        """,
        (str(game_date), float(projected_stocks)),
    ).fetchall()
    if len(rows) < 20:
        return raw_probability
    hits = 0
    weighted_hits = 0.0
    total_weight = 0.0
    for row in rows:
        actual_stocks = float(row["actual_steals"] or 0.0) + float(row["actual_blocks"] or 0.0)
        hit = 1.0 if actual_stocks >= 2.0 else 0.0
        hits += int(hit)
        distance = abs(float(row["projected_stocks"] or 0.0) - float(projected_stocks))
        weight = 1.0 / (1.0 + distance)
        weighted_hits += hit * weight
        total_weight += weight
    empirical_probability = weighted_hits / total_weight if total_weight > 0 else (hits / len(rows))
    sample_weight = min(0.75, len(rows) / 80.0)
    blended_probability = ((1.0 - sample_weight) * raw_probability) + (sample_weight * empirical_probability)
    return max(0.0, min(1.0, blended_probability))


def _player_recent_stocks_hit_rate(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    game_id: int,
    game_date: str,
) -> float | None:
    bundle = _build_player_stocks_feature_bundle(
        conn,
        player_id=player_id,
        game_id=game_id,
        game_date=game_date,
        market="blocks_steals",
        base_projection=0.0,
    )
    return None if bundle["recent_hit_rate_2_plus"] is None else float(bundle["recent_hit_rate_2_plus"])


def rebuild_team_prep_context(
    conn: sqlite3.Connection,
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
) -> int:
    tracking = _open_tracking_connection()
    try:
        normalized_game_ids = tuple(sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0}))
        normalized_dates = tuple(sorted({str(game_date).strip() for game_date in (target_dates or []) if str(game_date).strip()}))
        filter_sql = ""
        params: tuple[object, ...] = ()
        if normalized_game_ids:
            placeholders = ",".join("?" for _ in normalized_game_ids)
            filter_sql = f" WHERE pg.game_id IN ({placeholders})"
            params = normalized_game_ids
        elif normalized_dates:
            placeholders = ",".join("?" for _ in normalized_dates)
            filter_sql = f" WHERE pg.game_date IN ({placeholders})"
            params = normalized_dates
        rows = tracking.execute(
            """
            SELECT
                pg.game_id,
                pg.game_date,
                pg.home_team_id,
                pg.away_team_id
            FROM prepared_games pg
            """
            + filter_sql
            + """
            ORDER BY pg.game_date, pg.start_time, pg.game_id
            """,
            params,
        ).fetchall()
        built_at = datetime.now(timezone.utc).isoformat()
        context_rows: list[tuple[object, ...]] = []
        for row in rows:
            game_id = int(row["game_id"])
            game_date = str(row["game_date"])
            home_team_id = int(row["home_team_id"])
            away_team_id = int(row["away_team_id"])
            for team_id, opponent_id, is_home in (
                (home_team_id, away_team_id, 1),
                (away_team_id, home_team_id, 0),
            ):
                context_rows.append(
                    (
                        game_id,
                        game_date,
                        team_id,
                        opponent_id,
                        is_home,
                        _stocks_pace_factor(conn, team_id, opponent_id),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "steals"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks_steals"),
                        _stocks_turnover_pressure_factor(conn, opponent_id),
                        built_at,
                    )
                )
        with tracking:
            if normalized_game_ids:
                placeholders = ",".join("?" for _ in normalized_game_ids)
                tracking.execute(f"DELETE FROM team_prep_context WHERE game_id IN ({placeholders})", normalized_game_ids)
            elif normalized_dates:
                placeholders = ",".join("?" for _ in normalized_dates)
                tracking.execute(f"DELETE FROM team_prep_context WHERE game_date IN ({placeholders})", normalized_dates)
            else:
                tracking.execute("DELETE FROM team_prep_context")
            if context_rows:
                tracking.executemany(
                    """
                    INSERT INTO team_prep_context (
                        game_id,
                        game_date,
                        team_id,
                        opponent_id,
                        is_home,
                        pace_factor,
                        steals_allowed_factor,
                        blocks_allowed_factor,
                        stocks_allowed_factor,
                        turnover_pressure_factor,
                        built_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    context_rows,
                )
        return len(context_rows)
    finally:
        tracking.close()


def _team_prep_context_for_game(
    tracking: sqlite3.Connection,
    *,
    game_id: int,
    team_id: int | None,
) -> sqlite3.Row | None:
    if team_id is None:
        return None
    return tracking.execute(
        """
        SELECT *
        FROM team_prep_context
        WHERE game_id = ?
          AND team_id = ?
        LIMIT 1
        """,
        (int(game_id), int(team_id)),
    ).fetchone()


def rebuild_player_prep_features(
    conn: sqlite3.Connection,
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
) -> int:
    tracking = _open_tracking_connection()
    try:
        shared_runtime_cache: dict[str, dict[tuple, object]] = runtime_cache if runtime_cache is not None else {}
        component_snapshot_cache = shared_runtime_cache.setdefault("stocks_component_snapshot", {})
        rows = _candidate_players_for_snapshot(conn, tracking, game_ids=game_ids, target_dates=target_dates)
        normalized_game_ids = tuple(sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0}))
        normalized_dates = tuple(sorted({str(game_date).strip() for game_date in (target_dates or []) if str(game_date).strip()}))
        built_at = datetime.now(timezone.utc).isoformat()
        prep_rows: list[tuple[object, ...]] = []
        for row in rows:
            game_id = int(row["game_id"])
            game_date = str(row["game_date"])
            player_id = int(row["player_id"])
            player_name = str(row["player_name"])
            team_id = int(row["team_id"]) if row["team_id"] is not None else None
            matchup_context = _team_prep_context_for_game(tracking, game_id=game_id, team_id=team_id)
            for market in ("steals", "blocks", "blocks_steals"):
                cache_key = (player_id, market, game_id)
                snapshot = component_snapshot_cache.get(cache_key)
                if snapshot is None:
                    snapshot = feature_snapshot(
                        conn,
                        player_id,
                        market,
                        game_id,
                        allow_training=False,
                        use_live_minutes_context=False,
                        runtime_cache=shared_runtime_cache,
                    )
                    component_snapshot_cache[cache_key] = snapshot
                base_projection = float(snapshot.component_projection)
                bundle = _build_player_stocks_feature_bundle(
                    conn,
                    player_id=player_id,
                    game_id=game_id,
                    game_date=game_date,
                    market=market,
                    base_projection=base_projection,
                    matchup_context=matchup_context,
                )
                prep_rows.append(
                    (
                        game_id,
                        player_id,
                        player_name,
                        game_date,
                        market,
                        base_projection,
                        float(bundle["contextual_projection"] or 0.0),
                        float(bundle["recent_avg"] or 0.0),
                        float(bundle["stability_avg"] or 0.0),
                        float(bundle["same_venue_avg"] or 0.0),
                        int(bundle["same_venue_games"] or 0),
                        (
                            None
                            if bundle["recent_hit_rate_2_plus"] is None
                            else float(bundle["recent_hit_rate_2_plus"])
                        ),
                        int(bundle["rest_days"] or 2),
                        bundle["is_home"],
                        built_at,
                    )
                )
        with tracking:
            if normalized_game_ids:
                placeholders = ",".join("?" for _ in normalized_game_ids)
                tracking.execute(f"DELETE FROM player_prep_features WHERE game_id IN ({placeholders})", normalized_game_ids)
            elif normalized_dates:
                placeholders = ",".join("?" for _ in normalized_dates)
                tracking.execute(f"DELETE FROM player_prep_features WHERE game_date IN ({placeholders})", normalized_dates)
            else:
                tracking.execute("DELETE FROM player_prep_features")
            if prep_rows:
                tracking.executemany(
                    """
                    INSERT INTO player_prep_features (
                        game_id,
                        player_id,
                        player_name,
                        game_date,
                        market,
                        base_projection,
                        contextual_projection,
                        recent_avg,
                        stability_avg,
                        same_venue_avg,
                        same_venue_games,
                        recent_hit_rate_2_plus,
                        rest_days,
                        is_home,
                        built_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    prep_rows,
                )
        return len(prep_rows)
    finally:
        tracking.close()


def _player_prep_features_for_game(
    tracking: sqlite3.Connection,
    *,
    game_id: int,
    player_id: int,
) -> dict[str, sqlite3.Row]:
    rows = tracking.execute(
        """
        SELECT
            market,
            contextual_projection,
            recent_hit_rate_2_plus
        FROM player_prep_features
        WHERE game_id = ?
          AND player_id = ?
        """,
        (int(game_id), int(player_id)),
    ).fetchall()
    return {str(row["market"]): row for row in rows}


def snapshot_stocks(
    conn: sqlite3.Connection,
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
) -> int:
    tracking = _open_tracking_connection()
    try:
        shared_runtime_cache: dict[str, dict[tuple, object]] = runtime_cache if runtime_cache is not None else {}
        component_snapshot_cache = shared_runtime_cache.setdefault("stocks_component_snapshot", {})
        rows = _candidate_players_for_snapshot(conn, tracking, game_ids=game_ids, target_dates=target_dates)
        now = datetime.now(timezone.utc).isoformat()
        rows_to_insert: list[tuple[object, ...]] = []
        for row in rows:
            game_id = int(row["game_id"])
            game_date = str(row["game_date"])
            player_id = int(row["player_id"])
            player_name = str(row["player_name"])
            team_id = int(row["team_id"]) if row["team_id"] is not None else None
            prepared_features = _player_prep_features_for_game(tracking, game_id=game_id, player_id=player_id)
            matchup_context = _team_prep_context_for_game(tracking, game_id=game_id, team_id=team_id)
            def _component_projection(market: str) -> float:
                prepared_row = prepared_features.get(market)
                if prepared_row is not None:
                    return float(prepared_row["contextual_projection"] or 0.0)
                cache_key = (player_id, market, game_id)
                snapshot = component_snapshot_cache.get(cache_key)
                if snapshot is None:
                    snapshot = feature_snapshot(
                        conn,
                        player_id,
                        market,
                        game_id,
                        allow_training=False,
                        use_live_minutes_context=False,
                        runtime_cache=shared_runtime_cache,
                    )
                    component_snapshot_cache[cache_key] = snapshot
                return _contextual_stocks_component_projection(
                    conn,
                    player_id=player_id,
                    game_id=game_id,
                    game_date=game_date,
                    market=market,
                    base_projection=float(snapshot.component_projection),
                    matchup_context=matchup_context,
                )

            steals = _component_projection("steals")
            blocks = _component_projection("blocks")
            projected_stocks = _component_projection("blocks_steals")
            projected_stocks = float(projected_stocks)
            stocks_prob_2_plus = _calibrated_stocks_two_plus_probability(
                tracking,
                projected_stocks=projected_stocks,
                game_date=str(game_date),
            )
            prepared_stocks_row = prepared_features.get("blocks_steals")
            recent_hit_rate = (
                None
                if prepared_stocks_row is None or prepared_stocks_row["recent_hit_rate_2_plus"] is None
                else float(prepared_stocks_row["recent_hit_rate_2_plus"])
            )
            if recent_hit_rate is None:
                recent_hit_rate = _player_recent_stocks_hit_rate(
                    conn,
                    player_id=player_id,
                    game_id=game_id,
                    game_date=str(game_date),
                )
            if recent_hit_rate is not None:
                history_weight = 0.18 if projected_stocks < 1.5 else 0.28 if projected_stocks < 2.5 else 0.22
                stocks_prob_2_plus = ((1.0 - history_weight) * stocks_prob_2_plus) + (
                    history_weight * float(recent_hit_rate)
                )
                stocks_prob_2_plus = max(0.0, min(1.0, stocks_prob_2_plus))
            stocks_prob_3_plus = _poisson_at_least(projected_stocks, 3)
            rows_to_insert.append(
                (
                    int(game_id),
                    int(player_id),
                    str(player_name),
                    str(game_date),
                    now,
                    COMPONENT_MODEL_VERSION,
                    float(steals),
                    float(blocks),
                    projected_stocks,
                    _poisson_at_least(float(steals), 1),
                    _poisson_at_least(float(steals), 2),
                    _poisson_at_least(float(blocks), 1),
                    _poisson_at_least(float(blocks), 2),
                    stocks_prob_2_plus,
                    stocks_prob_3_plus,
                    "model_only",
                )
            )
        if rows_to_insert:
            tracking.executemany(
                """
                INSERT OR IGNORE INTO projection_snapshots (
                    game_id,
                    player_id,
                    player_name,
                    game_date,
                    captured_at,
                    model_version,
                    projected_steals,
                    projected_blocks,
                    projected_stocks,
                    steal_prob_1_plus,
                    steal_prob_2_plus,
                    block_prob_1_plus,
                    block_prob_2_plus,
                    stocks_prob_2_plus,
                    stocks_prob_3_plus,
                    data_quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
            written = tracking.total_changes
        else:
            written = 0
        tracking.commit()
        return written
    finally:
        tracking.close()


def queue_snapshot_stocks(game_ids: list[int] | None = None) -> bool:
    global _SNAPSHOT_JOB_RUNNING
    normalized_game_ids = sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0})
    with _SNAPSHOT_JOB_LOCK:
        if _SNAPSHOT_JOB_RUNNING:
            return False
        _SNAPSHOT_JOB_RUNNING = True

    def _run() -> None:
        global _SNAPSHOT_JOB_RUNNING
        try:
            with connect() as conn:
                snapshot_stocks(conn, game_ids=normalized_game_ids or None, runtime_cache=None)
        except Exception:
            # Model-only tracking is best-effort and must never block sportsbook rebuilds.
            pass
        finally:
            with _SNAPSHOT_JOB_LOCK:
                _SNAPSHOT_JOB_RUNNING = False

    threading.Thread(target=_run, daemon=True).start()
    return True


def default_prep_dates(*, include_tomorrow: bool = True) -> list[str]:
    today = datetime.now(APP_TIMEZONE).date()
    dates = [today.isoformat()]
    if include_tomorrow:
        dates.append((today + timedelta(days=1)).isoformat())
    return dates


def prepare_stocks_data(
    conn: sqlite3.Connection,
    *,
    target_dates: list[str] | None = None,
) -> dict[str, Any]:
    normalized_dates = sorted(
        {
            str(value).strip()
            for value in (target_dates or default_prep_dates())
            if str(value).strip()
        }
    )
    if not normalized_dates:
        return {
            "selected_dates": [],
            "game_ids": [],
            "prepared_games": 0,
            "candidate_players": 0,
            "team_prep_context": 0,
            "player_prep_features": 0,
            "snapshots_written": 0,
        }
    prepared_games = rebuild_prepared_games(conn, target_dates=normalized_dates)
    tracking = _open_tracking_connection()
    try:
        placeholders = ",".join("?" for _ in normalized_dates)
        rows = tracking.execute(
            f"""
            SELECT game_id
            FROM prepared_games
            WHERE game_date IN ({placeholders})
            ORDER BY game_date, start_time, game_id
            """,
            tuple(normalized_dates),
        ).fetchall()
    finally:
        tracking.close()
    game_ids = [int(row["game_id"]) for row in rows if int(row["game_id"]) > 0]
    candidate_players = rebuild_candidate_players(conn, game_ids=game_ids, target_dates=normalized_dates)
    team_prep_context = rebuild_team_prep_context(conn, game_ids=game_ids, target_dates=normalized_dates)
    player_prep_features = rebuild_player_prep_features(
        conn,
        game_ids=game_ids,
        target_dates=normalized_dates,
        runtime_cache=None,
    )
    snapshots_written = snapshot_stocks(conn, game_ids=game_ids, target_dates=normalized_dates, runtime_cache=None)
    return {
        "selected_dates": normalized_dates,
        "game_ids": game_ids,
        "prepared_games": prepared_games,
        "candidate_players": candidate_players,
        "team_prep_context": team_prep_context,
        "player_prep_features": player_prep_features,
        "snapshots_written": snapshots_written,
        "prepared_at": datetime.now(timezone.utc).isoformat(),
    }


def queue_prepare_stocks_data(target_dates: list[str] | None = None) -> bool:
    global _PREP_JOB_RUNNING
    normalized_dates = sorted(
        {
            str(value).strip()
            for value in (target_dates or default_prep_dates())
            if str(value).strip()
        }
    )
    with _PREP_JOB_LOCK:
        if _PREP_JOB_RUNNING:
            return False
        _PREP_JOB_RUNNING = True

    def _run() -> None:
        global _PREP_JOB_RUNNING
        try:
            with connect() as conn:
                prepare_stocks_data(conn, target_dates=normalized_dates)
        except Exception:
            pass
        finally:
            with _PREP_JOB_LOCK:
                _PREP_JOB_RUNNING = False

    threading.Thread(target=_run, daemon=True).start()
    return True


def _delete_explicit_dnp_snapshots(
    conn: sqlite3.Connection,
    tracking: sqlite3.Connection,
    *,
    target_dates: list[str] | None = None,
) -> int:
    filter_sql = ""
    params: list[object] = []
    if target_dates:
        placeholders = ",".join("?" for _ in target_dates)
        filter_sql = f" AND ps.game_date IN ({placeholders})"
        params.extend(target_dates)
    rows = tracking.execute(
        """
        SELECT
            ps.id,
            ps.game_id,
            ps.player_id
        FROM projection_snapshots ps
        LEFT JOIN settlements st ON st.snapshot_id = ps.id
        WHERE st.snapshot_id IS NULL
        """
        + filter_sql,
        tuple(params),
    ).fetchall()
    snapshot_ids: list[int] = []
    for row in rows:
        game_id = int(row["game_id"])
        player_id = int(row["player_id"])
        game_row = conn.execute("SELECT status FROM games WHERE id = ?", (game_id,)).fetchone()
        if game_row is None or str(game_row["status"] or "").lower() != "final":
            continue
        dnp_row = conn.execute(
            """
            SELECT 1
            FROM player_game_availability
            WHERE game_id = ?
              AND player_id = ?
              AND did_not_play = 1
              AND source IN ('espn_boxscore', 'espn_summary_injury', 'manual_review')
            LIMIT 1
            """,
            (game_id, player_id),
        ).fetchone()
        if dnp_row is not None:
            snapshot_ids.append(int(row["id"]))
    if not snapshot_ids:
        return 0
    placeholders = ",".join("?" for _ in snapshot_ids)
    tracking.execute(f"DELETE FROM projection_snapshots WHERE id IN ({placeholders})", tuple(snapshot_ids))
    tracking.commit()
    return len(snapshot_ids)


def _delete_stale_pending_snapshots(
    conn: sqlite3.Connection,
    tracking: sqlite3.Connection,
    *,
    target_dates: list[str] | None = None,
) -> int:
    filter_sql = ""
    params: list[object] = []
    if target_dates:
        placeholders = ",".join("?" for _ in target_dates)
        filter_sql = f" AND ps.game_date IN ({placeholders})"
        params.extend(target_dates)
    rows = tracking.execute(
        """
        SELECT
            ps.id,
            ps.game_id,
            ps.player_id,
            ps.game_date
        FROM projection_snapshots ps
        LEFT JOIN settlements st ON st.snapshot_id = ps.id
        WHERE st.snapshot_id IS NULL
        """
        + filter_sql,
        tuple(params),
    ).fetchall()
    today_iso = datetime.now(timezone.utc).date().isoformat()
    snapshot_ids: list[int] = []
    for row in rows:
        game_date = str(row["game_date"] or "").strip()
        if not game_date or game_date >= today_iso:
            continue
        game_id = int(row["game_id"])
        player_id = int(row["player_id"])
        game_row = conn.execute("SELECT status FROM games WHERE id = ?", (game_id,)).fetchone()
        active_prop_row = conn.execute(
            """
            SELECT 1
            FROM prop_lines
            WHERE game_id = ? AND player_id = ?
            LIMIT 1
            """,
            (game_id, player_id),
        ).fetchone()
        if active_prop_row is not None:
            continue
        if game_row is None or str(game_row["status"] or "").lower() == "scheduled":
            snapshot_ids.append(int(row["id"]))
    if not snapshot_ids:
        return 0
    placeholders = ",".join("?" for _ in snapshot_ids)
    tracking.execute(f"DELETE FROM projection_snapshots WHERE id IN ({placeholders})", tuple(snapshot_ids))
    tracking.commit()
    return len(snapshot_ids)


def prune_special_snapshots(
    conn: sqlite3.Connection,
    *,
    selected_date: str | None = None,
    selected_dates: list[str] | None = None,
) -> dict[str, int]:
    target_dates = sorted(
        {
            str(value).strip()
            for value in ([selected_date] if selected_date else []) + list(selected_dates or [])
            if str(value).strip()
        }
    )
    tracking = _open_tracking_connection()
    try:
        deleted_dnp = _delete_explicit_dnp_snapshots(conn, tracking, target_dates=target_dates)
        deleted_stale = _delete_stale_pending_snapshots(conn, tracking, target_dates=target_dates)
        return {
            "deleted_dnp": deleted_dnp,
            "deleted_stale": deleted_stale,
        }
    finally:
        tracking.close()


def list_special_stocks(*, include_history: bool = False) -> list[dict[str, Any]]:
    tracking = _open_tracking_connection()
    try:
        if include_history:
            rows = tracking.execute(
                """
                SELECT
                    ps.*,
                    st.actual_steals,
                    st.actual_blocks,
                    CASE
                        WHEN st.snapshot_id IS NULL THEN NULL
                        ELSE st.actual_steals + st.actual_blocks
                    END AS actual_stocks,
                    st.settled_at
                FROM projection_snapshots ps
                LEFT JOIN settlements st ON st.snapshot_id = ps.id
                ORDER BY ps.game_date DESC, ps.projected_stocks DESC, ps.captured_at DESC
                """
            ).fetchall()
        else:
            rows = tracking.execute(
                """
                WITH latest AS (
                    SELECT
                        ps.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY ps.game_id, ps.player_id
                            ORDER BY ps.captured_at DESC, ps.id DESC
                        ) AS snapshot_rank
                    FROM projection_snapshots ps
                )
                SELECT
                    latest.*,
                    st.actual_steals,
                    st.actual_blocks,
                    CASE
                        WHEN st.snapshot_id IS NULL THEN NULL
                        ELSE st.actual_steals + st.actual_blocks
                    END AS actual_stocks,
                    st.settled_at
                FROM latest
                LEFT JOIN settlements st ON st.snapshot_id = latest.id
                WHERE latest.snapshot_rank = 1
                ORDER BY latest.game_date, latest.projected_stocks DESC, latest.player_name
                """
            ).fetchall()
        items = [dict(row) for row in rows]
        for item in items:
            projected_stocks = float(item.get("projected_stocks") or 0.0)
            if projected_stocks > 0 and float(item.get("stocks_prob_3_plus") or 0.0) <= 0.0:
                item["stocks_prob_3_plus"] = _poisson_at_least(projected_stocks, 3)
        return items
    finally:
        tracking.close()


def settle_stocks(
    conn: sqlite3.Connection,
    *,
    selected_date: str | None = None,
    selected_dates: list[str] | None = None,
) -> dict[str, Any]:
    target_dates = sorted(
        {
            str(value).strip()
            for value in ([selected_date] if selected_date else []) + list(selected_dates or [])
            if str(value).strip()
        }
    )
    tracking = _open_tracking_connection()
    try:
        deleted_dnp = _delete_explicit_dnp_snapshots(conn, tracking, target_dates=target_dates)
        deleted_stale = _delete_stale_pending_snapshots(conn, tracking, target_dates=target_dates)
        filter_sql = ""
        params: tuple[str, ...] = ()
        if target_dates:
            placeholders = ",".join("?" for _ in target_dates)
            filter_sql = f" AND ps.game_date IN ({placeholders})"
            params = tuple(target_dates)
        snapshot_rows = tracking.execute(
            """
            SELECT
                ps.id,
                ps.game_id,
                ps.player_id,
                ps.game_date
            FROM projection_snapshots ps
            LEFT JOIN settlements st ON st.snapshot_id = ps.id
            WHERE st.snapshot_id IS NULL
            """
            + filter_sql,
            params,
        ).fetchall()
        settled_at = datetime.now(timezone.utc).isoformat()
        inserts: list[tuple[int, int, int, str]] = []
        for row in snapshot_rows:
            game_row = conn.execute("SELECT status FROM games WHERE id = ?", (int(row["game_id"]),)).fetchone()
            if game_row is None or str(game_row["status"] or "").lower() != "final":
                continue
            stat_row = conn.execute(
                """
                SELECT steals, blocks
                FROM player_game_stats
                WHERE game_id = ? AND player_id = ?
                """,
                (int(row["game_id"]), int(row["player_id"])),
            ).fetchone()
            if stat_row is None:
                continue
            inserts.append(
                (
                    int(row["id"]),
                    int(stat_row["steals"] or 0),
                    int(stat_row["blocks"] or 0),
                    settled_at,
                )
            )
        if inserts:
            tracking.executemany(
                """
                INSERT INTO settlements (snapshot_id, actual_steals, actual_blocks, settled_at)
                VALUES (?, ?, ?, ?)
                """,
                inserts,
            )
            tracking.commit()
        return {
            "eligible": len(snapshot_rows),
            "settled": len(inserts),
            "deleted_dnp": deleted_dnp,
            "deleted_stale": deleted_stale,
            "settled_at": settled_at,
            "selected_date": target_dates[0] if len(target_dates) == 1 else None,
            "selected_dates": target_dates,
        }
    finally:
        tracking.close()
