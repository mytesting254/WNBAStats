from __future__ import annotations

import json
import math
import os
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    import fcntl
except ImportError:  # pragma: no cover - production runs on Linux
    fcntl = None

from .db import connect
from .espn_history import fetch_summary
from .paths import get_db_path
from .player_prop_model import (
    COMPONENT_MODEL_VERSION,
    _historical_blowout_weight,
    _prune_extreme_blowout_history_rows,
    _rest_factor,
    _shared_projection_context,
    _winsorize_history_values,
    feature_snapshot,
)
from .timezone_utils import APP_TIMEZONE, local_game_date, local_today_iso


_SNAPSHOT_JOB_LOCK = threading.Lock()
_SNAPSHOT_JOB_RUNNING = False
_PREP_JOB_LOCK = threading.Lock()
_PREP_JOB_RUNNING = False
_STOCKS_PREP_THREAD_LOCK = threading.RLock()
_SCHEMA_INIT_LOCK = threading.Lock()
_STOCKS_RECENT_WINDOW_GAMES = 12
_STOCKS_STABILITY_WINDOW_GAMES = 24
_STOCKS_PROMOTION_WINDOW_GAMES = 3
_SNAPSHOT_DELTA_TOLERANCE = 1e-4
SPECIALS_DEFAULT_HIGH_THRESHOLD = 0.55
SPECIALS_DEFAULT_WATCH_THRESHOLD = 0.45
SPECIALS_MIN_SETTLED_THRESHOLD_ROWS = 40
SPECIALS_MIN_THRESHOLD_BUCKET_ROWS = 10
SPECIALS_MIN_THRESHOLD_BUCKET_HITS = 4


def _clamp(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, value))


def _stocks_recent_history_blend_weight(projected_stocks: float) -> float:
    if projected_stocks < 1.25:
        return 0.12
    if projected_stocks < 1.75:
        return 0.16
    if projected_stocks < 2.5:
        return 0.20
    return 0.18


def _stocks_probability_context_penalty(
    *,
    projected_stocks: float,
    prepared_stocks_row: sqlite3.Row | None,
    matchup_context: sqlite3.Row | dict[str, object] | None,
    recent_hit_rate: float | None,
) -> float:
    penalty = 1.0
    if prepared_stocks_row is None:
        penalty *= 0.96 if projected_stocks < 2.0 else 0.97
    if matchup_context is None:
        penalty *= 0.97
    if recent_hit_rate is None:
        penalty *= 0.98
    return penalty


def get_tracking_db_path() -> Path:
    configured = os.getenv("WNBA_STOCKS_TRACKING_DB", "").strip()
    if configured:
        return Path(configured)
    return get_db_path().with_name("stocks_tracking.sqlite")


@contextmanager
def _stocks_prep_lock():
    """Serialize the complete stocks rebuild across threads and processes.

    The lock file lives beside the tracking DB, so it is shared by cron, API
    workers, and containers attached to the same runtime volume. The existing
    in-process job lock is not sufficient for those separate processes.
    """
    with _STOCKS_PREP_THREAD_LOCK:
        lock_path = get_tracking_db_path().with_suffix(".prep.lock")
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        with lock_path.open("a+") as lock_file:
            if fcntl is not None:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
            try:
                yield
            finally:
                if fcntl is not None:
                    fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


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
                    snapshot_team_id INTEGER,
                    snapshot_team_abbr TEXT,
                    snapshot_rotation_role TEXT,
                    snapshot_candidate_reason TEXT,
                    snapshot_recent_minutes_avg REAL NOT NULL DEFAULT 0,
                    snapshot_recent_stocks_avg REAL NOT NULL DEFAULT 0,
                    snapshot_recent_games INTEGER NOT NULL DEFAULT 0,
                    snapshot_projected_minutes REAL NOT NULL DEFAULT 0,
                    snapshot_minute_volatility REAL NOT NULL DEFAULT 0,
                    snapshot_recent_hit_rate_2_plus REAL,
                    snapshot_injury_status TEXT NOT NULL DEFAULT 'available',
                    snapshot_injury_availability_factor REAL NOT NULL DEFAULT 1,
                    snapshot_injury_usage_multiplier REAL NOT NULL DEFAULT 1,
                    snapshot_injury_minutes_delta REAL NOT NULL DEFAULT 0,
                    snapshot_opportunity_unavailable REAL NOT NULL DEFAULT 0,
                    snapshot_opportunity_key_outs REAL NOT NULL DEFAULT 0,
                    snapshot_opportunity_persistence REAL NOT NULL DEFAULT 0,
                    snapshot_opportunity_competition REAL NOT NULL DEFAULT 0,
                    snapshot_pace_factor REAL,
                    snapshot_stocks_allowed_factor REAL,
                    snapshot_turnover_pressure_factor REAL,
                    snapshot_has_prep_context INTEGER NOT NULL DEFAULT 0,
                    snapshot_has_matchup_context INTEGER NOT NULL DEFAULT 0,
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
                    projected_minutes REAL NOT NULL DEFAULT 0,
                    minute_volatility REAL NOT NULL DEFAULT 0,
                    injury_status TEXT NOT NULL DEFAULT 'available',
                    injury_availability_factor REAL NOT NULL DEFAULT 1,
                    injury_usage_multiplier REAL NOT NULL DEFAULT 1,
                    injury_minutes_delta REAL NOT NULL DEFAULT 0,
                    opportunity_unavailable REAL NOT NULL DEFAULT 0,
                    opportunity_key_outs REAL NOT NULL DEFAULT 0,
                    opportunity_persistence REAL NOT NULL DEFAULT 0,
                    opportunity_competition REAL NOT NULL DEFAULT 0,
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
                    steals_allowed_guard_factor REAL NOT NULL DEFAULT 1,
                    steals_allowed_wing_factor REAL NOT NULL DEFAULT 1,
                    steals_allowed_big_factor REAL NOT NULL DEFAULT 1,
                    blocks_allowed_factor REAL NOT NULL DEFAULT 1,
                    blocks_allowed_guard_factor REAL NOT NULL DEFAULT 1,
                    blocks_allowed_wing_factor REAL NOT NULL DEFAULT 1,
                    blocks_allowed_big_factor REAL NOT NULL DEFAULT 1,
                    stocks_allowed_factor REAL NOT NULL DEFAULT 1,
                    stocks_allowed_guard_factor REAL NOT NULL DEFAULT 1,
                    stocks_allowed_wing_factor REAL NOT NULL DEFAULT 1,
                    stocks_allowed_big_factor REAL NOT NULL DEFAULT 1,
                    team_turnover_rate_factor REAL NOT NULL DEFAULT 1,
                    forced_turnover_rate_factor REAL NOT NULL DEFAULT 1,
                    turnover_pressure_factor REAL NOT NULL DEFAULT 1,
                    built_at TEXT NOT NULL,
                    PRIMARY KEY (game_id, team_id)
                );
                CREATE TABLE IF NOT EXISTS game_board_summaries (
                    game_id INTEGER PRIMARY KEY,
                    game_date TEXT NOT NULL,
                    player_count INTEGER NOT NULL DEFAULT 0,
                    candidate_count_50_plus INTEGER NOT NULL DEFAULT 0,
                    candidate_threshold REAL NOT NULL DEFAULT 0.5,
                    candidate_count_threshold INTEGER NOT NULL DEFAULT 0,
                    avg_prob_2_plus REAL NOT NULL DEFAULT 0,
                    avg_prob_3_plus REAL NOT NULL DEFAULT 0,
                    top_projected_stocks REAL NOT NULL DEFAULT 0,
                    top_prob_2_plus REAL NOT NULL DEFAULT 0,
                    top_player_name TEXT,
                    built_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS prep_runs (
                    id INTEGER PRIMARY KEY,
                    status TEXT NOT NULL,
                    scope TEXT NOT NULL,
                    target_dates_json TEXT NOT NULL,
                    game_ids_json TEXT NOT NULL,
                    selected_dates_json TEXT NOT NULL,
                    selected_game_ids_json TEXT NOT NULL,
                    prepared_games INTEGER NOT NULL DEFAULT 0,
                    candidate_players INTEGER NOT NULL DEFAULT 0,
                    team_prep_context INTEGER NOT NULL DEFAULT 0,
                    player_prep_features INTEGER NOT NULL DEFAULT 0,
                    game_board_summaries INTEGER NOT NULL DEFAULT 0,
                    snapshots_written INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT NOT NULL,
                    finished_at TEXT NOT NULL,
                    error_message TEXT
                );
                CREATE INDEX IF NOT EXISTS idx_stocks_snapshots_game ON projection_snapshots(game_id, player_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_prepared_games_date ON prepared_games(game_date, start_time, game_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_candidates_game ON candidate_players(game_id, player_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_player_prep_game ON player_prep_features(game_id, player_id, market);
                CREATE INDEX IF NOT EXISTS idx_stocks_player_prep_date ON player_prep_features(game_date, game_id, player_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_team_context_game ON team_prep_context(game_id, team_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_board_summaries_date ON game_board_summaries(game_date, game_id);
                CREATE INDEX IF NOT EXISTS idx_stocks_prep_runs_finished ON prep_runs(finished_at, id);
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
            for column_name, column_sql in (
                ("snapshot_team_id", "INTEGER"),
                ("snapshot_team_abbr", "TEXT"),
                ("snapshot_rotation_role", "TEXT"),
                ("snapshot_candidate_reason", "TEXT"),
                ("snapshot_recent_minutes_avg", "REAL NOT NULL DEFAULT 0"),
                ("snapshot_recent_stocks_avg", "REAL NOT NULL DEFAULT 0"),
                ("snapshot_recent_games", "INTEGER NOT NULL DEFAULT 0"),
                ("snapshot_projected_minutes", "REAL NOT NULL DEFAULT 0"),
                ("snapshot_minute_volatility", "REAL NOT NULL DEFAULT 0"),
                ("snapshot_recent_hit_rate_2_plus", "REAL"),
                ("snapshot_injury_status", "TEXT NOT NULL DEFAULT 'available'"),
                ("snapshot_injury_availability_factor", "REAL NOT NULL DEFAULT 1"),
                ("snapshot_injury_usage_multiplier", "REAL NOT NULL DEFAULT 1"),
                ("snapshot_injury_minutes_delta", "REAL NOT NULL DEFAULT 0"),
                ("snapshot_opportunity_unavailable", "REAL NOT NULL DEFAULT 0"),
                ("snapshot_opportunity_key_outs", "REAL NOT NULL DEFAULT 0"),
                ("snapshot_opportunity_persistence", "REAL NOT NULL DEFAULT 0"),
                ("snapshot_opportunity_competition", "REAL NOT NULL DEFAULT 0"),
                ("snapshot_pace_factor", "REAL"),
                ("snapshot_stocks_allowed_factor", "REAL"),
                ("snapshot_turnover_pressure_factor", "REAL"),
                ("snapshot_has_prep_context", "INTEGER NOT NULL DEFAULT 0"),
                ("snapshot_has_matchup_context", "INTEGER NOT NULL DEFAULT 0"),
            ):
                if column_name in columns:
                    continue
                try:
                    conn.execute(
                        f"ALTER TABLE projection_snapshots ADD COLUMN {column_name} {column_sql}"
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
            board_summary_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(game_board_summaries)").fetchall()
            }
            player_prep_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(player_prep_features)").fetchall()
            }
            if "candidate_threshold" not in board_summary_columns:
                try:
                    conn.execute(
                        "ALTER TABLE game_board_summaries ADD COLUMN candidate_threshold REAL NOT NULL DEFAULT 0.5"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            if "candidate_count_threshold" not in board_summary_columns:
                try:
                    conn.execute(
                        "ALTER TABLE game_board_summaries ADD COLUMN candidate_count_threshold INTEGER NOT NULL DEFAULT 0"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            for column_name, column_sql in (
                ("projected_minutes", "REAL NOT NULL DEFAULT 0"),
                ("minute_volatility", "REAL NOT NULL DEFAULT 0"),
                ("injury_status", "TEXT NOT NULL DEFAULT 'available'"),
                ("injury_availability_factor", "REAL NOT NULL DEFAULT 1"),
                ("injury_usage_multiplier", "REAL NOT NULL DEFAULT 1"),
                ("injury_minutes_delta", "REAL NOT NULL DEFAULT 0"),
                ("opportunity_unavailable", "REAL NOT NULL DEFAULT 0"),
                ("opportunity_key_outs", "REAL NOT NULL DEFAULT 0"),
                ("opportunity_persistence", "REAL NOT NULL DEFAULT 0"),
                ("opportunity_competition", "REAL NOT NULL DEFAULT 0"),
            ):
                if column_name in player_prep_columns:
                    continue
                try:
                    conn.execute(
                        f"ALTER TABLE player_prep_features ADD COLUMN {column_name} {column_sql}"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
            team_context_columns = {
                str(row[1]) for row in conn.execute("PRAGMA table_info(team_prep_context)").fetchall()
            }
            for column_name in (
                "steals_allowed_guard_factor",
                "steals_allowed_wing_factor",
                "steals_allowed_big_factor",
                "blocks_allowed_guard_factor",
                "blocks_allowed_wing_factor",
                "blocks_allowed_big_factor",
                "stocks_allowed_guard_factor",
                "stocks_allowed_wing_factor",
                "stocks_allowed_big_factor",
                "team_turnover_rate_factor",
                "forced_turnover_rate_factor",
            ):
                if column_name in team_context_columns:
                    continue
                try:
                    conn.execute(
                        f"ALTER TABLE team_prep_context ADD COLUMN {column_name} REAL NOT NULL DEFAULT 1"
                    )
                except sqlite3.OperationalError as exc:
                    if "duplicate column name" not in str(exc).lower():
                        raise
    return path


def _open_tracking_connection() -> sqlite3.Connection:
    path = ensure_tracking_schema()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _record_prep_run(
    *,
    status: str,
    scope: str,
    target_dates: list[str],
    game_ids: list[int],
    selected_dates: list[str],
    selected_game_ids: list[int],
    prepared_games: int,
    candidate_players: int,
    team_prep_context: int,
    player_prep_features: int,
    game_board_summaries: int,
    snapshots_written: int,
    started_at: str,
    finished_at: str,
    error_message: str | None = None,
) -> None:
    tracking = _open_tracking_connection()
    try:
        tracking.execute(
            """
            INSERT INTO prep_runs (
                status,
                scope,
                target_dates_json,
                game_ids_json,
                selected_dates_json,
                selected_game_ids_json,
                prepared_games,
                candidate_players,
                team_prep_context,
                player_prep_features,
                game_board_summaries,
                snapshots_written,
                started_at,
                finished_at,
                error_message
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                str(status),
                str(scope),
                json.dumps(list(target_dates)),
                json.dumps([int(game_id) for game_id in game_ids]),
                json.dumps(list(selected_dates)),
                json.dumps([int(game_id) for game_id in selected_game_ids]),
                int(prepared_games),
                int(candidate_players),
                int(team_prep_context),
                int(player_prep_features),
                int(game_board_summaries),
                int(snapshots_written),
                str(started_at),
                str(finished_at),
                None if error_message is None else str(error_message),
            ),
        )
        tracking.commit()
    finally:
        tracking.close()


def _snapshot_rows_match(
    latest_row: sqlite3.Row | None,
    candidate_row: tuple[object, ...],
) -> bool:
    if latest_row is None:
        return False
    comparable_columns = (
        ("player_name", 2),
        ("game_date", 3),
        ("model_version", 5),
        ("projected_steals", 6),
        ("projected_blocks", 7),
        ("projected_stocks", 8),
        ("steal_prob_1_plus", 9),
        ("steal_prob_2_plus", 10),
        ("block_prob_1_plus", 11),
        ("block_prob_2_plus", 12),
        ("stocks_prob_2_plus", 13),
        ("stocks_prob_3_plus", 14),
        ("snapshot_team_id", 15),
        ("snapshot_team_abbr", 16),
        ("snapshot_rotation_role", 17),
        ("snapshot_candidate_reason", 18),
        ("snapshot_recent_minutes_avg", 19),
        ("snapshot_recent_stocks_avg", 20),
        ("snapshot_recent_games", 21),
        ("snapshot_projected_minutes", 22),
        ("snapshot_minute_volatility", 23),
        ("snapshot_recent_hit_rate_2_plus", 24),
        ("snapshot_injury_status", 25),
        ("snapshot_injury_availability_factor", 26),
        ("snapshot_injury_usage_multiplier", 27),
        ("snapshot_injury_minutes_delta", 28),
        ("snapshot_opportunity_unavailable", 29),
        ("snapshot_opportunity_key_outs", 30),
        ("snapshot_opportunity_persistence", 31),
        ("snapshot_opportunity_competition", 32),
        ("snapshot_pace_factor", 33),
        ("snapshot_stocks_allowed_factor", 34),
        ("snapshot_turnover_pressure_factor", 35),
        ("snapshot_has_prep_context", 36),
        ("snapshot_has_matchup_context", 37),
        ("data_quality", 38),
    )
    for column_name, tuple_index in comparable_columns:
        latest_value = latest_row[column_name]
        current_value = candidate_row[tuple_index]
        if isinstance(current_value, float):
            if abs(float(latest_value or 0.0) - float(current_value)) > _SNAPSHOT_DELTA_TOLERANCE:
                return False
        else:
            if latest_value != current_value:
                return False
    return True


def _latest_snapshot_rows_by_pair(
    tracking: sqlite3.Connection,
    *,
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
) -> dict[tuple[int, int], sqlite3.Row]:
    normalized_game_ids = tuple(sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0}))
    normalized_dates = tuple(sorted({str(game_date).strip() for game_date in (target_dates or []) if str(game_date).strip()}))
    filter_sql = ""
    params: tuple[object, ...] = ()
    if normalized_game_ids:
        placeholders = ",".join("?" for _ in normalized_game_ids)
        filter_sql = f" WHERE ps.game_id IN ({placeholders})"
        params = normalized_game_ids
    elif normalized_dates:
        placeholders = ",".join("?" for _ in normalized_dates)
        filter_sql = f" WHERE ps.game_date IN ({placeholders})"
        params = normalized_dates
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
        """
        + filter_sql
        + """
        )
        SELECT
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
            snapshot_team_id,
            snapshot_team_abbr,
            snapshot_rotation_role,
            snapshot_candidate_reason,
            snapshot_recent_minutes_avg,
            snapshot_recent_stocks_avg,
            snapshot_recent_games,
            snapshot_projected_minutes,
            snapshot_minute_volatility,
            snapshot_recent_hit_rate_2_plus,
            snapshot_injury_status,
            snapshot_injury_availability_factor,
            snapshot_injury_usage_multiplier,
            snapshot_injury_minutes_delta,
            snapshot_opportunity_unavailable,
            snapshot_opportunity_key_outs,
            snapshot_opportunity_persistence,
            snapshot_opportunity_competition,
            snapshot_pace_factor,
            snapshot_stocks_allowed_factor,
            snapshot_turnover_pressure_factor,
            snapshot_has_prep_context,
            snapshot_has_matchup_context,
            data_quality
        FROM latest
        WHERE snapshot_rank = 1
        """,
        params,
    ).fetchall()
    return {(int(row["game_id"]), int(row["player_id"])): row for row in rows}


def _stocks_market_value(row: sqlite3.Row, market: str) -> float:
    if market == "blocks_steals":
        return float((row["blocks"] or 0.0) + (row["steals"] or 0.0))
    return float(row[market] or 0.0)


def _stocks_history_weight(row: sqlite3.Row) -> float:
    return _historical_blowout_weight(row, str(row["rotation_role"] or "starter"))


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


def _stocks_role_bucket(position: str | None) -> str:
    text = str(position or "").strip().upper()
    if not text:
        return "wing"
    if "G" in text:
        return "guard"
    if "C" in text:
        return "big"
    return "wing"


def _player_stocks_role_bucket(conn: sqlite3.Connection, player_id: int) -> str:
    try:
        row = conn.execute("SELECT position FROM players WHERE id = ? LIMIT 1", (int(player_id),)).fetchone()
    except sqlite3.OperationalError:
        return "wing"
    return _stocks_role_bucket(None if row is None else row[0])


def _stocks_opponent_allowed_factor(
    conn: sqlite3.Connection,
    opponent_id: int,
    market: str,
    *,
    role_bucket: str | None = None,
) -> float:
    market_sql = _stocks_market_column(market)
    role_filter_sql = ""
    role_filter_params: tuple[object, ...] = ()
    normalized_bucket = str(role_bucket or "").strip().lower()
    if normalized_bucket in {"guard", "wing", "big"}:
        if normalized_bucket == "guard":
            role_filter_sql = " AND instr(upper(COALESCE(p.position, '')), 'G') > 0"
        elif normalized_bucket == "big":
            role_filter_sql = " AND instr(upper(COALESCE(p.position, '')), 'C') > 0 AND instr(upper(COALESCE(p.position, '')), 'G') = 0"
        else:
            role_filter_sql = " AND instr(upper(COALESCE(p.position, '')), 'G') = 0 AND instr(upper(COALESCE(p.position, '')), 'C') = 0"
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
        {role_filter_sql}
        """,
        (int(opponent_id), *role_filter_params),
    )
    league_allowed = _stocks_avg_scalar(
        conn,
        f"""
        SELECT AVG({market_sql})
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN players p ON p.id = s.player_id
        WHERE 1 = 1
        {role_filter_sql}
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


def _stocks_team_turnover_rate_factor(conn: sqlite3.Connection, team_id: int) -> float:
    team_rate = _stocks_avg_scalar(
        conn,
        """
        SELECT AVG(
            COALESCE(
                b.total_turnovers,
                COALESCE(b.turnovers, 0) + COALESCE(b.team_turnovers, 0),
                b.turnovers,
                b.team_turnovers
            ) / NULLIF(r.possessions, 0)
        )
        FROM team_game_results r
        JOIN team_game_boxscores b ON b.game_id = r.game_id AND b.team_id = r.team_id
        WHERE r.team_id = ?
        """,
        (int(team_id),),
    )
    league_rate = _stocks_avg_scalar(
        conn,
        """
        SELECT AVG(
            COALESCE(
                b.total_turnovers,
                COALESCE(b.turnovers, 0) + COALESCE(b.team_turnovers, 0),
                b.turnovers,
                b.team_turnovers
            ) / NULLIF(r.possessions, 0)
        )
        FROM team_game_results r
        JOIN team_game_boxscores b ON b.game_id = r.game_id AND b.team_id = r.team_id
        """
    )
    if team_rate is None or league_rate is None or league_rate <= 0:
        return 1.0
    return _clamp(team_rate / league_rate, 0.90, 1.12)


def _stocks_forced_turnover_rate_factor(conn: sqlite3.Connection, opponent_id: int) -> float:
    opponent_forced_rate = _stocks_avg_scalar(
        conn,
        """
        SELECT AVG(
            COALESCE(
                b.total_turnovers,
                COALESCE(b.turnovers, 0) + COALESCE(b.team_turnovers, 0),
                b.turnovers,
                b.team_turnovers
            ) / NULLIF(opp.possessions, 0)
        )
        FROM team_game_results opp
        JOIN games g ON g.id = opp.game_id
        JOIN team_game_results team_result
          ON team_result.game_id = opp.game_id
         AND team_result.team_id != opp.team_id
        JOIN team_game_boxscores b
          ON b.game_id = team_result.game_id
         AND b.team_id = team_result.team_id
        WHERE opp.team_id = ?
        """,
        (int(opponent_id),),
    )
    league_forced_rate = _stocks_avg_scalar(
        conn,
        """
        SELECT AVG(
            COALESCE(
                b.total_turnovers,
                COALESCE(b.turnovers, 0) + COALESCE(b.team_turnovers, 0),
                b.turnovers,
                b.team_turnovers
            ) / NULLIF(opp.possessions, 0)
        )
        FROM team_game_results opp
        JOIN games g ON g.id = opp.game_id
        JOIN team_game_results team_result
          ON team_result.game_id = opp.game_id
         AND team_result.team_id != opp.team_id
        JOIN team_game_boxscores b
          ON b.game_id = team_result.game_id
         AND b.team_id = team_result.team_id
        """
    )
    if opponent_forced_rate is None or league_forced_rate is None or league_forced_rate <= 0:
        return 1.0
    return _clamp(opponent_forced_rate / league_forced_rate, 0.90, 1.12)


def _role_specific_context_factor(
    context: sqlite3.Row | dict[str, object] | None,
    *,
    base_column: str,
    role_bucket: str | None,
) -> float:
    if context is None:
        return 1.0
    normalized_bucket = str(role_bucket or "").strip().lower()
    available_columns = set(context.keys()) if hasattr(context, "keys") else set(context)
    if normalized_bucket in {"guard", "wing", "big"}:
        role_column = f"{base_column}_{normalized_bucket}_factor"
        if role_column in available_columns:
            value = float(context[role_column] or 1.0)
            if value > 0:
                return value
    return float(context[f"{base_column}_factor"] or 1.0)


def _stocks_matchup_context_multiplier(
    context: sqlite3.Row | dict[str, object] | None,
    market: str,
    *,
    role_bucket: str | None = None,
) -> float:
    if context is None:
        return 1.0
    pace_factor = float(context["pace_factor"] or 1.0)
    steals_allowed_factor = _role_specific_context_factor(context, base_column="steals_allowed", role_bucket=role_bucket)
    blocks_allowed_factor = _role_specific_context_factor(context, base_column="blocks_allowed", role_bucket=role_bucket)
    stocks_allowed_factor = _role_specific_context_factor(context, base_column="stocks_allowed", role_bucket=role_bucket)
    team_turnover_rate_factor = float(context["team_turnover_rate_factor"] or 1.0)
    forced_turnover_rate_factor = float(context["forced_turnover_rate_factor"] or 1.0)
    turnover_pressure_factor = float(context["turnover_pressure_factor"] or 1.0)
    if market == "steals":
        return _clamp(
            (0.50 * steals_allowed_factor)
            + (0.20 * turnover_pressure_factor)
            + (0.15 * team_turnover_rate_factor)
            + (0.10 * forced_turnover_rate_factor)
            + (0.05 * pace_factor),
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
        (0.35 * stocks_allowed_factor)
        + (0.18 * steals_allowed_factor)
        + (0.17 * blocks_allowed_factor)
        + (0.10 * turnover_pressure_factor)
        + (0.10 * team_turnover_rate_factor)
        + (0.05 * forced_turnover_rate_factor)
        + (0.05 * pace_factor),
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
            END AS rest_days,
            p.rotation_role,
            (
                SELECT ABS(tgr.points - tgr.opponent_points)
                FROM team_game_results tgr
                WHERE tgr.game_id = s.game_id
                  AND tgr.team_id = COALESCE((
                    SELECT h.team_id
                    FROM player_team_history h
                    WHERE h.player_id = s.player_id
                      AND h.game_id = s.game_id
                    ORDER BY h.id DESC
                    LIMIT 1
                  ), p.team_id)
                LIMIT 1
            ) AS team_margin
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
    role_bucket = _player_stocks_role_bucket(conn, player_id)
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
    rotation_role = str(rows[0]["rotation_role"] or "starter")
    rows = _prune_extreme_blowout_history_rows(rows, rotation_role)
    values = _winsorize_history_values([_stocks_market_value(row, market) for row in rows], market)
    sample_weights = [_stocks_history_weight(row) for row in rows]
    recent_values = values[:_STOCKS_RECENT_WINDOW_GAMES]
    recent_weights = sample_weights[: len(recent_values)]
    recent_avg = sum(value * weight for value, weight in zip(recent_values, recent_weights)) / max(sum(recent_weights), 1e-9)
    stability_avg = sum(value * weight for value, weight in zip(values, sample_weights)) / max(sum(sample_weights), 1e-9)
    all_anchor = (
        (0.65 * recent_avg)
        + (0.35 * stability_avg)
    )
    same_venue_rows = [row for row in rows if row["is_home"] is not None and int(row["is_home"]) == target_is_home]
    same_venue_avg = 0.0
    if len(same_venue_rows) >= 3:
        same_venue_values = _winsorize_history_values([_stocks_market_value(row, market) for row in same_venue_rows], market)
        same_venue_weights = [_stocks_history_weight(row) for row in same_venue_rows]
        same_venue_avg = sum(value * weight for value, weight in zip(same_venue_values, same_venue_weights)) / max(sum(same_venue_weights), 1e-9)
        venue_recent = same_venue_values[:_STOCKS_RECENT_WINDOW_GAMES]
        venue_recent_weights = same_venue_weights[: len(venue_recent)]
        venue_anchor = (
            (0.70 * (sum(value * weight for value, weight in zip(venue_recent, venue_recent_weights)) / max(sum(venue_recent_weights), 1e-9)))
            + (0.30 * same_venue_avg)
        )
        contextual_anchor = (0.65 * all_anchor) + (0.35 * venue_anchor)
    elif same_venue_rows:
        same_venue_values = _winsorize_history_values([_stocks_market_value(row, market) for row in same_venue_rows], market)
        same_venue_weights = [_stocks_history_weight(row) for row in same_venue_rows]
        same_venue_avg = sum(value * weight for value, weight in zip(same_venue_values, same_venue_weights)) / max(sum(same_venue_weights), 1e-9)
        contextual_anchor = (0.85 * all_anchor) + (0.15 * same_venue_avg)
    else:
        contextual_anchor = all_anchor
    rest_adjusted_anchor = contextual_anchor * _rest_factor(target_rest_days)
    stabilized = (0.72 * float(base_projection)) + (0.28 * float(rest_adjusted_anchor))
    matchup_multiplier = _stocks_matchup_context_multiplier(matchup_context, market, role_bucket=role_bucket)
    stabilized *= matchup_multiplier
    overall_hits = [1.0 if (_stocks_market_value(row, "blocks_steals")) >= 2.0 else 0.0 for row in rows]
    recent_hits = overall_hits[:_STOCKS_RECENT_WINDOW_GAMES]
    overall_rate = (
        (0.65 * (sum(value * weight for value, weight in zip(recent_hits, recent_weights)) / max(sum(recent_weights), 1e-9)))
        + (0.35 * (sum(value * weight for value, weight in zip(overall_hits, sample_weights)) / max(sum(sample_weights), 1e-9)))
    )
    same_venue_hits = [
        1.0 if _stocks_market_value(row, "blocks_steals") >= 2.0 else 0.0
        for row in rows
        if row["is_home"] is not None and int(row["is_home"]) == target_is_home
    ]
    recent_hit_rate = overall_rate
    if len(same_venue_hits) >= 3:
        venue_recent_hits = same_venue_hits[:_STOCKS_RECENT_WINDOW_GAMES]
        same_venue_hit_weights = [_stocks_history_weight(row) for row in same_venue_rows]
        venue_recent_hit_weights = same_venue_hit_weights[: len(venue_recent_hits)]
        venue_rate = (
            (0.70 * (sum(value * weight for value, weight in zip(venue_recent_hits, venue_recent_hit_weights)) / max(sum(venue_recent_hit_weights), 1e-9)))
            + (0.30 * (sum(value * weight for value, weight in zip(same_venue_hits, same_venue_hit_weights)) / max(sum(same_venue_hit_weights), 1e-9)))
        )
        recent_hit_rate = (0.70 * overall_rate) + (0.30 * venue_rate)
    elif same_venue_hits:
        same_venue_hit_weights = [_stocks_history_weight(row) for row in same_venue_rows]
        recent_hit_rate = (0.85 * overall_rate) + (
            0.15 * (sum(value * weight for value, weight in zip(same_venue_hits, same_venue_hit_weights)) / max(sum(same_venue_hit_weights), 1e-9))
        )
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
              AND g.home_team_id != g.away_team_id
            """
            + filter_sql
            + """
            ORDER BY g.game_date, g.start_time, g.id
            """,
            params,
        ).fetchall()
        prepared_at = datetime.now(timezone.utc).isoformat()
        prepared_by_game_id = {
            int(row["game_id"]): (
                int(row["game_id"]),
                str(row["game_date"]),
                str(row["start_time"]),
                int(row["home_team_id"]),
                int(row["away_team_id"]),
                int(row["espn_event_id"]) if row["espn_event_id"] is not None else None,
                prepared_at,
            )
            for row in rows
        }
        prepared_rows = list(prepared_by_game_id.values())
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
                    ON CONFLICT(game_id) DO UPDATE SET
                        game_date = excluded.game_date,
                        start_time = excluded.start_time,
                        home_team_id = excluded.home_team_id,
                        away_team_id = excluded.away_team_id,
                        espn_event_id = excluded.espn_event_id,
                        prepared_at = excluded.prepared_at
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
        candidate_query = (
            f"""
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
            + f"""
            ),
            recent_stats AS (
                SELECT
                    s.player_id,
                    SUM(CASE WHEN s.stat_rank <= {_STOCKS_RECENT_WINDOW_GAMES} THEN 1 ELSE 0 END) AS recent_games,
                    AVG(CASE WHEN s.stat_rank <= {_STOCKS_RECENT_WINDOW_GAMES} THEN s.minutes END) AS recent_minutes_avg,
                    AVG(CASE WHEN s.stat_rank <= {_STOCKS_RECENT_WINDOW_GAMES} THEN COALESCE(s.steals, 0) + COALESCE(s.blocks, 0) END) AS recent_stocks_avg,
                    AVG(CASE WHEN s.stat_rank <= {_STOCKS_PROMOTION_WINDOW_GAMES} THEN s.minutes END) AS last3_minutes_avg,
                    AVG(CASE WHEN s.stat_rank <= {_STOCKS_PROMOTION_WINDOW_GAMES} THEN COALESCE(s.steals, 0) + COALESCE(s.blocks, 0) END) AS last3_stocks_avg,
                    AVG(CASE WHEN s.stat_rank <= {_STOCKS_STABILITY_WINDOW_GAMES} THEN s.minutes END) AS stability_minutes_avg
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
                WHERE s.stat_rank <= {_STOCKS_STABILITY_WINDOW_GAMES}
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
            ),
            team_unavailable AS (
                SELECT
                    p.team_id,
                    COUNT(*) AS unavailable_count
                FROM players p
                JOIN latest_injuries li ON li.player_id = p.id AND li.injury_rank = 1
                WHERE COALESCE(li.status, 'available') IN ('out', 'inactive', 'suspended', 'unavailable')
                GROUP BY p.team_id
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
                COALESCE(rs.recent_games, 0) AS recent_games,
                COALESCE(rs.last3_minutes_avg, 0.0) AS last3_minutes_avg,
                COALESCE(rs.last3_stocks_avg, 0.0) AS last3_stocks_avg,
                COALESCE(rs.stability_minutes_avg, 0.0) AS stability_minutes_avg,
                COALESCE(tu.unavailable_count, 0) AS team_unavailable_count
            FROM scheduled_games sg
            JOIN players p ON p.team_id IN (sg.home_team_id, sg.away_team_id)
            JOIN teams t ON t.id = p.team_id
            JOIN recent_stats rs ON rs.player_id = p.id
            LEFT JOIN latest_injuries li ON li.player_id = p.id AND li.injury_rank = 1
            LEFT JOIN team_unavailable tu ON tu.team_id = p.team_id
            WHERE COALESCE(li.status, 'available') NOT IN ('out', 'inactive', 'suspended', 'unavailable')
              AND (
                    COALESCE(rs.recent_minutes_avg, 0.0) >= 16.0
                 OR COALESCE(rs.recent_stocks_avg, 0.0) >= 1.7
                 OR (
                        COALESCE(rs.recent_games, 0) >= 4
                    AND COALESCE(rs.recent_minutes_avg, 0.0) >= 11.0
                 )
                 OR (
                        COALESCE(rs.recent_games, 0) >= 4
                    AND COALESCE(rs.recent_minutes_avg, 0.0) >= 8.0
                    AND COALESCE(rs.recent_stocks_avg, 0.0) >= 1.2
                    AND lower(trim(COALESCE(p.rotation_role, 'rotation'))) IN ('star', 'starter', 'rotation', 'bench')
                 )
                 OR (
                        COALESCE(rs.recent_games, 0) >= 3
                    AND COALESCE(rs.last3_minutes_avg, 0.0) >= 18.0
                    AND COALESCE(rs.last3_minutes_avg, 0.0) >= COALESCE(rs.stability_minutes_avg, 0.0) + 4.0
                 )
                 OR (
                        COALESCE(tu.unavailable_count, 0) >= 2
                    AND COALESCE(rs.recent_games, 0) >= 3
                    AND COALESCE(rs.recent_minutes_avg, 0.0) >= 10.0
                 )
              )
            ORDER BY
                sg.game_id,
                rs.recent_stocks_avg DESC,
                rs.last3_minutes_avg DESC,
                rs.recent_minutes_avg DESC,
                p.full_name
            """
        )
        rows = conn.execute(candidate_query, params).fetchall()
        built_at = datetime.now(timezone.utc).isoformat()
        candidate_rows: list[tuple[object, ...]] = []
        touched_game_ids = sorted({int(row["game_id"]) for row in rows})
        for row in rows:
            rotation_role = str(row["rotation_role"] or "").strip().lower()
            recent_minutes_avg = float(row["recent_minutes_avg"] or 0.0)
            recent_stocks_avg = float(row["recent_stocks_avg"] or 0.0)
            last3_minutes_avg = float(row["last3_minutes_avg"] or 0.0)
            stability_minutes_avg = float(row["stability_minutes_avg"] or 0.0)
            team_unavailable_count = int(row["team_unavailable_count"] or 0)
            is_defensive_specialist = (
                recent_stocks_avg >= 1.1
                and recent_minutes_avg >= 8.0
                and rotation_role in {"star", "starter", "rotation", "bench"}
            )
            if last3_minutes_avg >= 16.0 and last3_minutes_avg >= stability_minutes_avg + 4.0:
                candidate_reason = "recent lineup promotion"
            elif is_defensive_specialist:
                candidate_reason = "defensive specialist profile"
            elif team_unavailable_count >= 2 and recent_minutes_avg >= 8.0:
                candidate_reason = "injury opportunity replacement"
            elif recent_stocks_avg >= 1.8:
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
            cp.team_id,
            cp.team_abbr,
            cp.rotation_role,
            cp.recent_minutes_avg,
            cp.recent_stocks_avg,
            cp.recent_games,
            cp.candidate_reason
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
            cp.team_id,
            cp.team_abbr,
            cp.rotation_role,
            cp.recent_minutes_avg,
            cp.recent_stocks_avg,
            cp.recent_games,
            cp.candidate_reason
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


def _special_market_probability_config(market: str, threshold: int) -> dict[str, float | int]:
    if market == "steals":
        if threshold <= 1:
            return {"sample_limit": 36, "min_samples": 18, "min_history_rows": 48, "max_weight": 0.45}
        return {"sample_limit": 32, "min_samples": 16, "min_history_rows": 48, "max_weight": 0.40}
    if market == "blocks":
        if threshold <= 1:
            return {"sample_limit": 32, "min_samples": 16, "min_history_rows": 52, "max_weight": 0.38}
        return {"sample_limit": 24, "min_samples": 14, "min_history_rows": 56, "max_weight": 0.30}
    if threshold >= 3:
        return {"sample_limit": 24, "min_samples": 16, "min_history_rows": 60, "max_weight": 0.32}
    return {"sample_limit": 36, "min_samples": 18, "min_history_rows": 52, "max_weight": 0.48}


def _special_projected_column(market: str) -> str:
    if market == "steals":
        return "projected_steals"
    if market == "blocks":
        return "projected_blocks"
    return "projected_stocks"


def _special_actual_value(row: sqlite3.Row, market: str) -> float:
    if market == "steals":
        return float(row["actual_steals"] or 0.0)
    if market == "blocks":
        return float(row["actual_blocks"] or 0.0)
    return float(row["actual_steals"] or 0.0) + float(row["actual_blocks"] or 0.0)


def _calibrated_special_probability(
    tracking: sqlite3.Connection,
    *,
    market: str,
    threshold: int,
    projected_value: float,
    raw_probability: float,
    game_date: str,
    history_count_cache: dict[tuple[str, str], int] | None = None,
) -> float:
    config = _special_market_probability_config(market, threshold)
    projected_column = _special_projected_column(market)
    sample_limit = int(config["sample_limit"])
    min_samples = int(config["min_samples"])
    min_history_rows = int(config["min_history_rows"])
    max_weight = float(config["max_weight"])
    history_cache = history_count_cache if history_count_cache is not None else {}
    history_key = (str(market), str(game_date))
    total_history_rows = history_cache.get(history_key)
    if total_history_rows is None:
        history_row = tracking.execute(
            f"""
            WITH settled_latest AS (
                SELECT
                    ROW_NUMBER() OVER (
                        PARTITION BY ps.game_id, ps.player_id
                        ORDER BY ps.captured_at DESC, ps.id DESC
                    ) AS snapshot_rank
                FROM projection_snapshots ps
                JOIN settlements st ON st.snapshot_id = ps.id
                WHERE ps.game_date < ?
            )
            SELECT COUNT(*)
            FROM settled_latest
            WHERE snapshot_rank = 1
            """
            ,
            (str(game_date),),
        ).fetchone()
        total_history_rows = int(history_row[0] or 0) if history_row is not None else 0
        history_cache[history_key] = total_history_rows
    if total_history_rows < min_history_rows:
        return raw_probability
    rows = tracking.execute(
        f"""
        WITH settled_latest AS (
            SELECT
                ps.{projected_column} AS projected_value,
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
            projected_value,
            actual_steals,
            actual_blocks
        FROM settled_latest
        WHERE snapshot_rank = 1
        ORDER BY ABS(projected_value - ?) ASC
        LIMIT {sample_limit}
        """,
        (str(game_date), float(projected_value)),
    ).fetchall()
    if len(rows) < min_samples:
        return raw_probability
    weighted_hits = 0.0
    total_weight = 0.0
    for row in rows:
        actual_value = _special_actual_value(row, market)
        hit = 1.0 if actual_value >= float(threshold) else 0.0
        distance = abs(float(row["projected_value"] or 0.0) - float(projected_value))
        weight = 1.0 / (1.0 + distance)
        weighted_hits += hit * weight
        total_weight += weight
    empirical_probability = weighted_hits / total_weight if total_weight > 0 else raw_probability
    sample_weight = min(max_weight, len(rows) / float(sample_limit))
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


def default_special_threshold_recommendations() -> dict[str, Any]:
    return {
        "status": "insufficient_history",
        "high_confidence_threshold": SPECIALS_DEFAULT_HIGH_THRESHOLD,
        "watch_threshold": SPECIALS_DEFAULT_WATCH_THRESHOLD,
        "settled_rows": 0,
        "evaluated_thresholds": [],
    }


def fit_special_threshold_recommendations(settled_rows: list[dict[str, Any]]) -> dict[str, Any]:
    if len(settled_rows) < SPECIALS_MIN_SETTLED_THRESHOLD_ROWS:
        result = default_special_threshold_recommendations()
        result["settled_rows"] = len(settled_rows)
        return result
    threshold_candidates = [round(value, 2) for value in (0.38, 0.40, 0.42, 0.45, 0.48, 0.50, 0.53, 0.55, 0.58, 0.60)]
    evaluations: list[dict[str, Any]] = []
    for threshold in threshold_candidates:
        bucket = [row for row in settled_rows if float(row.get("stocks_prob_2_plus") or 0.0) >= threshold]
        support = len(bucket)
        hits = sum(1 for row in bucket if float(row.get("actual_stocks") or 0.0) >= 2.0)
        hit_rate = (hits / support) if support else None
        eligible = (
            support >= SPECIALS_MIN_THRESHOLD_BUCKET_ROWS
            and hits >= SPECIALS_MIN_THRESHOLD_BUCKET_HITS
            and hit_rate is not None
            and hit_rate >= threshold
        )
        evaluations.append(
            {
                "threshold": threshold,
                "support": support,
                "hits": hits,
                "hit_rate": round(hit_rate, 4) if hit_rate is not None else None,
                "eligible": eligible,
            }
        )
    eligible = [row for row in evaluations if bool(row["eligible"])]
    high_confidence_threshold = (
        max(float(row["threshold"]) for row in eligible)
        if eligible
        else SPECIALS_DEFAULT_HIGH_THRESHOLD
    )
    watch_candidates = [
        row for row in evaluations
        if float(row["threshold"]) < high_confidence_threshold
        and int(row["support"]) >= SPECIALS_MIN_THRESHOLD_BUCKET_ROWS
    ]
    watch_threshold = (
        max(float(row["threshold"]) for row in watch_candidates)
        if watch_candidates
        else SPECIALS_DEFAULT_WATCH_THRESHOLD
    )
    return {
        "status": "fit" if eligible else "fallback_defaults",
        "high_confidence_threshold": round(high_confidence_threshold, 2),
        "watch_threshold": round(min(watch_threshold, high_confidence_threshold - 0.01), 2)
        if watch_threshold >= high_confidence_threshold
        else round(watch_threshold, 2),
        "settled_rows": len(settled_rows),
        "evaluated_thresholds": evaluations,
    }


def settled_latest_special_rows(tracking: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = tracking.execute(
        """
        WITH latest AS (
            SELECT
                ps.*,
                st.actual_steals,
                st.actual_blocks,
                CASE
                    WHEN st.snapshot_id IS NULL THEN NULL
                    ELSE st.actual_steals + st.actual_blocks
                END AS actual_stocks,
                ROW_NUMBER() OVER (
                    PARTITION BY ps.game_id, ps.player_id
                    ORDER BY ps.captured_at DESC, ps.id DESC
                ) AS snapshot_rank
            FROM projection_snapshots ps
            JOIN settlements st ON st.snapshot_id = ps.id
        )
        SELECT *
        FROM latest
        WHERE snapshot_rank = 1
        ORDER BY game_date, captured_at, id
        """
    ).fetchall()
    return [dict(row) for row in rows]


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
            if home_team_id == away_team_id:
                continue
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
                        _stocks_opponent_allowed_factor(conn, opponent_id, "steals", role_bucket="guard"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "steals", role_bucket="wing"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "steals", role_bucket="big"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks", role_bucket="guard"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks", role_bucket="wing"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks", role_bucket="big"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks_steals"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks_steals", role_bucket="guard"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks_steals", role_bucket="wing"),
                        _stocks_opponent_allowed_factor(conn, opponent_id, "blocks_steals", role_bucket="big"),
                        _stocks_team_turnover_rate_factor(conn, team_id),
                        _stocks_forced_turnover_rate_factor(conn, opponent_id),
                        _stocks_turnover_pressure_factor(conn, opponent_id),
                        built_at,
                    )
                )
        context_by_key = {
            (int(row[0]), int(row[2])): row
            for row in context_rows
        }
        context_rows = list(context_by_key.values())
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
                        steals_allowed_guard_factor,
                        steals_allowed_wing_factor,
                        steals_allowed_big_factor,
                        blocks_allowed_factor,
                        blocks_allowed_guard_factor,
                        blocks_allowed_wing_factor,
                        blocks_allowed_big_factor,
                        stocks_allowed_factor,
                        stocks_allowed_guard_factor,
                        stocks_allowed_wing_factor,
                        stocks_allowed_big_factor,
                        team_turnover_rate_factor,
                        forced_turnover_rate_factor,
                        turnover_pressure_factor,
                        built_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id, team_id) DO UPDATE SET
                        game_date = excluded.game_date,
                        opponent_id = excluded.opponent_id,
                        is_home = excluded.is_home,
                        pace_factor = excluded.pace_factor,
                        steals_allowed_factor = excluded.steals_allowed_factor,
                        steals_allowed_guard_factor = excluded.steals_allowed_guard_factor,
                        steals_allowed_wing_factor = excluded.steals_allowed_wing_factor,
                        steals_allowed_big_factor = excluded.steals_allowed_big_factor,
                        blocks_allowed_factor = excluded.blocks_allowed_factor,
                        blocks_allowed_guard_factor = excluded.blocks_allowed_guard_factor,
                        blocks_allowed_wing_factor = excluded.blocks_allowed_wing_factor,
                        blocks_allowed_big_factor = excluded.blocks_allowed_big_factor,
                        stocks_allowed_factor = excluded.stocks_allowed_factor,
                        stocks_allowed_guard_factor = excluded.stocks_allowed_guard_factor,
                        stocks_allowed_wing_factor = excluded.stocks_allowed_wing_factor,
                        stocks_allowed_big_factor = excluded.stocks_allowed_big_factor,
                        team_turnover_rate_factor = excluded.team_turnover_rate_factor,
                        forced_turnover_rate_factor = excluded.forced_turnover_rate_factor,
                        turnover_pressure_factor = excluded.turnover_pressure_factor,
                        built_at = excluded.built_at
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
            shared_context = _shared_projection_context(
                conn,
                player_id=player_id,
                game_id=game_id,
                before_game_date=None,
                allow_training=False,
                use_injury_context=True,
                use_live_minutes_context=True,
                runtime_cache=shared_runtime_cache,
            )
            injury = dict(shared_context.get("injury") or {})
            opportunity_context = list(shared_context.get("opportunity_context") or [0.0, 0.0, 0.0, 0.0])
            projected_minutes = float(shared_context.get("projected_minutes") or 0.0)
            minute_volatility = float(shared_context.get("minute_volatility") or 0.0)
            injury_status = str(injury.get("status") or "available")
            injury_availability_factor = float(injury.get("availability_factor") or 1.0)
            injury_usage_multiplier = float(injury.get("usage_multiplier") or 1.0)
            injury_minutes_delta = float(injury.get("minutes_delta") or 0.0)
            opportunity_unavailable = float(opportunity_context[0] if len(opportunity_context) >= 1 else 0.0)
            opportunity_key_outs = float(opportunity_context[1] if len(opportunity_context) >= 2 else 0.0)
            opportunity_persistence = float(opportunity_context[2] if len(opportunity_context) >= 3 else 0.0)
            opportunity_competition = float(opportunity_context[3] if len(opportunity_context) >= 4 else 0.0)
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
                        projected_minutes,
                        minute_volatility,
                        injury_status,
                        injury_availability_factor,
                        injury_usage_multiplier,
                        injury_minutes_delta,
                        opportunity_unavailable,
                        opportunity_key_outs,
                        opportunity_persistence,
                        opportunity_competition,
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
                        projected_minutes,
                        minute_volatility,
                        injury_status,
                        injury_availability_factor,
                        injury_usage_multiplier,
                        injury_minutes_delta,
                        opportunity_unavailable,
                        opportunity_key_outs,
                        opportunity_persistence,
                        opportunity_competition,
                        built_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            recent_hit_rate_2_plus,
            projected_minutes,
            minute_volatility,
            injury_status,
            injury_availability_factor,
            injury_usage_multiplier,
            injury_minutes_delta,
            opportunity_unavailable,
            opportunity_key_outs,
            opportunity_persistence,
            opportunity_competition
        FROM player_prep_features
        WHERE game_id = ?
          AND player_id = ?
        """,
        (int(game_id), int(player_id)),
    ).fetchall()
    return {str(row["market"]): row for row in rows}


def rebuild_game_board_summaries(
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
) -> int:
    tracking = _open_tracking_connection()
    try:
        threshold_recommendations = fit_special_threshold_recommendations(settled_latest_special_rows(tracking))
        candidate_threshold = float(
            threshold_recommendations.get("high_confidence_threshold") or SPECIALS_DEFAULT_HIGH_THRESHOLD
        )
        normalized_game_ids = tuple(sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0}))
        normalized_dates = tuple(sorted({str(game_date).strip() for game_date in (target_dates or []) if str(game_date).strip()}))
        filter_sql = " WHERE latest.snapshot_rank = 1"
        params: tuple[object, ...] = ()
        if normalized_game_ids:
            placeholders = ",".join("?" for _ in normalized_game_ids)
            filter_sql += f" AND latest.game_id IN ({placeholders})"
            params = normalized_game_ids
        elif normalized_dates:
            placeholders = ",".join("?" for _ in normalized_dates)
            filter_sql += f" AND latest.game_date IN ({placeholders})"
            params = normalized_dates
        rows = tracking.execute(
            """
            WITH latest AS (
                SELECT
                    ps.game_id,
                    ps.game_date,
                    ps.player_name,
                    ps.projected_stocks,
                    ps.stocks_prob_2_plus,
                    ps.stocks_prob_3_plus,
                    ROW_NUMBER() OVER (
                        PARTITION BY ps.game_id, ps.player_id
                        ORDER BY ps.captured_at DESC, ps.id DESC
                    ) AS snapshot_rank
                FROM projection_snapshots ps
            )
            SELECT
                latest.game_id,
                latest.game_date,
                COUNT(*) AS player_count,
                SUM(CASE WHEN COALESCE(latest.stocks_prob_2_plus, 0) >= 0.5 THEN 1 ELSE 0 END) AS candidate_count_50_plus,
                SUM(CASE WHEN COALESCE(latest.stocks_prob_2_plus, 0) >= ? THEN 1 ELSE 0 END) AS candidate_count_threshold,
                AVG(COALESCE(latest.stocks_prob_2_plus, 0)) AS avg_prob_2_plus,
                AVG(COALESCE(latest.stocks_prob_3_plus, 0)) AS avg_prob_3_plus,
                MAX(COALESCE(latest.projected_stocks, 0)) AS top_projected_stocks,
                MAX(COALESCE(latest.stocks_prob_2_plus, 0)) AS top_prob_2_plus,
                (
                    SELECT latest_inner.player_name
                    FROM latest latest_inner
                    WHERE latest_inner.snapshot_rank = 1
                      AND latest_inner.game_id = latest.game_id
                    ORDER BY COALESCE(latest_inner.stocks_prob_2_plus, 0) DESC,
                             COALESCE(latest_inner.projected_stocks, 0) DESC,
                             latest_inner.player_name
                    LIMIT 1
                ) AS top_player_name
            FROM latest
            """
            + filter_sql
            + """
            GROUP BY latest.game_id, latest.game_date
            ORDER BY latest.game_date, latest.game_id
            """,
            (candidate_threshold, *params),
        ).fetchall()
        built_at = datetime.now(timezone.utc).isoformat()
        summary_rows = [
            (
                int(row["game_id"]),
                str(row["game_date"]),
                int(row["player_count"] or 0),
                int(row["candidate_count_50_plus"] or 0),
                candidate_threshold,
                int(row["candidate_count_threshold"] or 0),
                float(row["avg_prob_2_plus"] or 0.0),
                float(row["avg_prob_3_plus"] or 0.0),
                float(row["top_projected_stocks"] or 0.0),
                float(row["top_prob_2_plus"] or 0.0),
                str(row["top_player_name"] or "") if row["top_player_name"] is not None else None,
                built_at,
            )
            for row in rows
        ]
        with tracking:
            if normalized_game_ids:
                placeholders = ",".join("?" for _ in normalized_game_ids)
                tracking.execute(f"DELETE FROM game_board_summaries WHERE game_id IN ({placeholders})", normalized_game_ids)
            elif normalized_dates:
                placeholders = ",".join("?" for _ in normalized_dates)
                tracking.execute(f"DELETE FROM game_board_summaries WHERE game_date IN ({placeholders})", normalized_dates)
            else:
                tracking.execute("DELETE FROM game_board_summaries")
            if summary_rows:
                tracking.executemany(
                    """
                    INSERT INTO game_board_summaries (
                        game_id,
                        game_date,
                        player_count,
                        candidate_count_50_plus,
                        candidate_threshold,
                        candidate_count_threshold,
                        avg_prob_2_plus,
                        avg_prob_3_plus,
                        top_projected_stocks,
                        top_prob_2_plus,
                        top_player_name,
                        built_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(game_id) DO UPDATE SET
                        game_date = excluded.game_date,
                        player_count = excluded.player_count,
                        candidate_count_50_plus = excluded.candidate_count_50_plus,
                        candidate_threshold = excluded.candidate_threshold,
                        candidate_count_threshold = excluded.candidate_count_threshold,
                        avg_prob_2_plus = excluded.avg_prob_2_plus,
                        avg_prob_3_plus = excluded.avg_prob_3_plus,
                        top_projected_stocks = excluded.top_projected_stocks,
                        top_prob_2_plus = excluded.top_prob_2_plus,
                        top_player_name = excluded.top_player_name,
                        built_at = excluded.built_at
                    """,
                    summary_rows,
                )
        return len(summary_rows)
    finally:
        tracking.close()


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
        calibration_history_cache: dict[tuple[str, str], int] = {}
        rows = _candidate_players_for_snapshot(conn, tracking, game_ids=game_ids, target_dates=target_dates)
        now = datetime.now(timezone.utc).isoformat()
        latest_rows = _latest_snapshot_rows_by_pair(tracking, game_ids=game_ids, target_dates=target_dates)
        rows_to_insert: list[tuple[object, ...]] = []
        for row in rows:
            game_id = int(row["game_id"])
            game_date = str(row["game_date"])
            player_id = int(row["player_id"])
            player_name = str(row["player_name"])
            team_id = int(row["team_id"]) if row["team_id"] is not None else None
            team_abbr = str(row["team_abbr"] or "")
            rotation_role = str(row["rotation_role"] or "")
            candidate_reason = str(row["candidate_reason"] or "")
            recent_minutes_avg = float(row["recent_minutes_avg"] or 0.0)
            recent_stocks_avg = float(row["recent_stocks_avg"] or 0.0)
            recent_games = int(row["recent_games"] or 0)
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
            steal_prob_1_plus = _calibrated_special_probability(
                tracking,
                market="steals",
                threshold=1,
                projected_value=float(steals),
                raw_probability=_poisson_at_least(float(steals), 1),
                game_date=str(game_date),
                history_count_cache=calibration_history_cache,
            )
            steal_prob_2_plus = _calibrated_special_probability(
                tracking,
                market="steals",
                threshold=2,
                projected_value=float(steals),
                raw_probability=_poisson_at_least(float(steals), 2),
                game_date=str(game_date),
                history_count_cache=calibration_history_cache,
            )
            block_prob_1_plus = _calibrated_special_probability(
                tracking,
                market="blocks",
                threshold=1,
                projected_value=float(blocks),
                raw_probability=_poisson_at_least(float(blocks), 1),
                game_date=str(game_date),
                history_count_cache=calibration_history_cache,
            )
            block_prob_2_plus = _calibrated_special_probability(
                tracking,
                market="blocks",
                threshold=2,
                projected_value=float(blocks),
                raw_probability=_poisson_at_least(float(blocks), 2),
                game_date=str(game_date),
                history_count_cache=calibration_history_cache,
            )
            projected_stocks = float(projected_stocks)
            stocks_prob_2_plus = _calibrated_special_probability(
                tracking,
                market="blocks_steals",
                threshold=2,
                projected_value=projected_stocks,
                raw_probability=_poisson_at_least(projected_stocks, 2),
                game_date=str(game_date),
                history_count_cache=calibration_history_cache,
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
                history_weight = _stocks_recent_history_blend_weight(projected_stocks)
                stocks_prob_2_plus = ((1.0 - history_weight) * stocks_prob_2_plus) + (
                    history_weight * float(recent_hit_rate)
                )
                stocks_prob_2_plus = max(0.0, min(1.0, stocks_prob_2_plus))
            stocks_prob_3_plus = _calibrated_special_probability(
                tracking,
                market="blocks_steals",
                threshold=3,
                projected_value=projected_stocks,
                raw_probability=_poisson_at_least(projected_stocks, 3),
                game_date=str(game_date),
                history_count_cache=calibration_history_cache,
            )
            context_penalty = _stocks_probability_context_penalty(
                projected_stocks=projected_stocks,
                prepared_stocks_row=prepared_stocks_row,
                matchup_context=matchup_context,
                recent_hit_rate=recent_hit_rate,
            )
            if context_penalty < 1.0:
                stocks_prob_2_plus = max(0.0, min(1.0, stocks_prob_2_plus * context_penalty))
                stocks_prob_3_plus = max(0.0, min(1.0, stocks_prob_3_plus * context_penalty))
            snapshot_projected_minutes = 0.0
            snapshot_minute_volatility = 0.0
            snapshot_recent_hit_rate_2_plus = None
            snapshot_injury_status = "available"
            snapshot_injury_availability_factor = 1.0
            snapshot_injury_usage_multiplier = 1.0
            snapshot_injury_minutes_delta = 0.0
            snapshot_opportunity_unavailable = 0.0
            snapshot_opportunity_key_outs = 0.0
            snapshot_opportunity_persistence = 0.0
            snapshot_opportunity_competition = 0.0
            if prepared_stocks_row is not None:
                snapshot_projected_minutes = float(prepared_stocks_row["projected_minutes"] or 0.0)
                snapshot_minute_volatility = float(prepared_stocks_row["minute_volatility"] or 0.0)
                snapshot_recent_hit_rate_2_plus = (
                    None
                    if prepared_stocks_row["recent_hit_rate_2_plus"] is None
                    else float(prepared_stocks_row["recent_hit_rate_2_plus"])
                )
                snapshot_injury_status = str(prepared_stocks_row["injury_status"] or "available")
                snapshot_injury_availability_factor = float(prepared_stocks_row["injury_availability_factor"] or 1.0)
                snapshot_injury_usage_multiplier = float(prepared_stocks_row["injury_usage_multiplier"] or 1.0)
                snapshot_injury_minutes_delta = float(prepared_stocks_row["injury_minutes_delta"] or 0.0)
                snapshot_opportunity_unavailable = float(prepared_stocks_row["opportunity_unavailable"] or 0.0)
                snapshot_opportunity_key_outs = float(prepared_stocks_row["opportunity_key_outs"] or 0.0)
                snapshot_opportunity_persistence = float(prepared_stocks_row["opportunity_persistence"] or 0.0)
                snapshot_opportunity_competition = float(prepared_stocks_row["opportunity_competition"] or 0.0)
            snapshot_pace_factor = None if matchup_context is None else float(matchup_context["pace_factor"] or 1.0)
            snapshot_stocks_allowed_factor = (
                None if matchup_context is None else float(matchup_context["stocks_allowed_factor"] or 1.0)
            )
            snapshot_turnover_pressure_factor = (
                None if matchup_context is None else float(matchup_context["turnover_pressure_factor"] or 1.0)
            )
            candidate_snapshot = (
                int(game_id),
                int(player_id),
                str(player_name),
                str(game_date),
                now,
                COMPONENT_MODEL_VERSION,
                float(steals),
                float(blocks),
                projected_stocks,
                steal_prob_1_plus,
                steal_prob_2_plus,
                block_prob_1_plus,
                block_prob_2_plus,
                stocks_prob_2_plus,
                stocks_prob_3_plus,
                team_id,
                team_abbr,
                rotation_role,
                candidate_reason,
                recent_minutes_avg,
                recent_stocks_avg,
                recent_games,
                snapshot_projected_minutes,
                snapshot_minute_volatility,
                snapshot_recent_hit_rate_2_plus,
                snapshot_injury_status,
                snapshot_injury_availability_factor,
                snapshot_injury_usage_multiplier,
                snapshot_injury_minutes_delta,
                snapshot_opportunity_unavailable,
                snapshot_opportunity_key_outs,
                snapshot_opportunity_persistence,
                snapshot_opportunity_competition,
                snapshot_pace_factor,
                snapshot_stocks_allowed_factor,
                snapshot_turnover_pressure_factor,
                1 if prepared_stocks_row is not None else 0,
                1 if matchup_context is not None else 0,
                "model_only",
            )
            latest_row = latest_rows.get((game_id, player_id))
            if _snapshot_rows_match(latest_row, candidate_snapshot):
                continue
            rows_to_insert.append(candidate_snapshot)
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
                    snapshot_team_id,
                    snapshot_team_abbr,
                    snapshot_rotation_role,
                    snapshot_candidate_reason,
                    snapshot_recent_minutes_avg,
                    snapshot_recent_stocks_avg,
                    snapshot_recent_games,
                    snapshot_projected_minutes,
                    snapshot_minute_volatility,
                    snapshot_recent_hit_rate_2_plus,
                    snapshot_injury_status,
                    snapshot_injury_availability_factor,
                    snapshot_injury_usage_multiplier,
                    snapshot_injury_minutes_delta,
                    snapshot_opportunity_unavailable,
                    snapshot_opportunity_key_outs,
                    snapshot_opportunity_persistence,
                    snapshot_opportunity_competition,
                    snapshot_pace_factor,
                    snapshot_stocks_allowed_factor,
                    snapshot_turnover_pressure_factor,
                    snapshot_has_prep_context,
                    snapshot_has_matchup_context,
                    data_quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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


def _prepare_stocks_data_locked(
    conn: sqlite3.Connection,
    *,
    target_dates: list[str] | None = None,
    game_ids: list[int] | None = None,
) -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()
    normalized_game_ids = sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0})
    normalized_dates = sorted(
        {
            str(value).strip()
            for value in (target_dates or default_prep_dates())
            if str(value).strip()
        }
    )
    if not normalized_dates and not normalized_game_ids:
        return {
            "selected_dates": [],
            "game_ids": [],
            "prepared_games": 0,
            "candidate_players": 0,
            "team_prep_context": 0,
            "player_prep_features": 0,
            "game_board_summaries": 0,
            "snapshots_written": 0,
        }
    try:
        if normalized_game_ids:
            prepared_games = rebuild_prepared_games(conn, game_ids=normalized_game_ids)
        else:
            prepared_games = rebuild_prepared_games(conn, target_dates=normalized_dates)
        tracking = _open_tracking_connection()
        try:
            if normalized_game_ids:
                placeholders = ",".join("?" for _ in normalized_game_ids)
                rows = tracking.execute(
                    f"""
                    SELECT game_id, game_date
                    FROM prepared_games
                    WHERE game_id IN ({placeholders})
                    ORDER BY game_date, start_time, game_id
                    """,
                    tuple(normalized_game_ids),
                ).fetchall()
            else:
                placeholders = ",".join("?" for _ in normalized_dates)
                rows = tracking.execute(
                    f"""
                    SELECT game_id, game_date
                    FROM prepared_games
                    WHERE game_date IN ({placeholders})
                    ORDER BY game_date, start_time, game_id
                    """,
                    tuple(normalized_dates),
                ).fetchall()
        finally:
            tracking.close()
        selected_game_ids = [int(row["game_id"]) for row in rows if int(row["game_id"]) > 0]
        selected_dates = sorted({str(row["game_date"]).strip() for row in rows if str(row["game_date"]).strip()})
        candidate_players = rebuild_candidate_players(conn, game_ids=selected_game_ids, target_dates=selected_dates or None)
        team_prep_context = rebuild_team_prep_context(conn, game_ids=selected_game_ids, target_dates=selected_dates or None)
        player_prep_features = rebuild_player_prep_features(
            conn,
            game_ids=selected_game_ids,
            target_dates=selected_dates or None,
            runtime_cache=None,
        )
        snapshots_written = snapshot_stocks(
            conn,
            game_ids=selected_game_ids,
            target_dates=selected_dates or None,
            runtime_cache=None,
        )
        game_board_summaries = rebuild_game_board_summaries(
            game_ids=selected_game_ids,
            target_dates=selected_dates or None,
        )
        finished_at = datetime.now(timezone.utc).isoformat()
        _record_prep_run(
            status="completed",
            scope="game_ids" if normalized_game_ids else "dates",
            target_dates=normalized_dates,
            game_ids=normalized_game_ids,
            selected_dates=selected_dates,
            selected_game_ids=selected_game_ids,
            prepared_games=prepared_games,
            candidate_players=candidate_players,
            team_prep_context=team_prep_context,
            player_prep_features=player_prep_features,
            game_board_summaries=game_board_summaries,
            snapshots_written=snapshots_written,
            started_at=started_at,
            finished_at=finished_at,
            error_message=None,
        )
        return {
            "selected_dates": selected_dates,
            "game_ids": selected_game_ids,
            "prepared_games": prepared_games,
            "candidate_players": candidate_players,
            "team_prep_context": team_prep_context,
            "player_prep_features": player_prep_features,
            "game_board_summaries": game_board_summaries,
            "snapshots_written": snapshots_written,
            "prepared_at": finished_at,
        }
    except Exception as exc:
        finished_at = datetime.now(timezone.utc).isoformat()
        _record_prep_run(
            status="failed",
            scope="game_ids" if normalized_game_ids else "dates",
            target_dates=normalized_dates,
            game_ids=normalized_game_ids,
            selected_dates=[],
            selected_game_ids=[],
            prepared_games=0,
            candidate_players=0,
            team_prep_context=0,
            player_prep_features=0,
            game_board_summaries=0,
            snapshots_written=0,
            started_at=started_at,
            finished_at=finished_at,
            error_message=str(exc),
        )
        raise


def prepare_stocks_data(
    conn: sqlite3.Connection,
    *,
    target_dates: list[str] | None = None,
    game_ids: list[int] | None = None,
) -> dict[str, Any]:
    with _stocks_prep_lock():
        return _prepare_stocks_data_locked(conn, target_dates=target_dates, game_ids=game_ids)


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


def queue_prepare_stocks_games(game_ids: list[int] | None = None) -> bool:
    global _PREP_JOB_RUNNING
    normalized_game_ids = sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0})
    if not normalized_game_ids:
        return False
    with _PREP_JOB_LOCK:
        if _PREP_JOB_RUNNING:
            return False
        _PREP_JOB_RUNNING = True

    def _run() -> None:
        global _PREP_JOB_RUNNING
        try:
            with connect() as conn:
                prepare_stocks_data(conn, game_ids=normalized_game_ids)
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
    today_iso = local_today_iso()
    snapshot_ids: list[int] = []
    for row in rows:
        game_id = int(row["game_id"])
        player_id = int(row["player_id"])
        game_row = conn.execute("SELECT game_date, start_time, status FROM games WHERE id = ?", (game_id,)).fetchone()
        local_date = local_game_date(
            game_row["game_date"] if game_row is not None else row["game_date"],
            game_row["start_time"] if game_row is not None else None,
        )
        if not local_date or local_date >= today_iso:
            continue
        game_status = str(game_row["status"] or "").lower() if game_row is not None else ""
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
        if game_row is None or game_status == "scheduled":
            snapshot_ids.append(int(row["id"]))
            continue
        if game_status != "final":
            continue
        stat_row = conn.execute(
            """
            SELECT 1
            FROM player_game_stats
            WHERE game_id = ? AND player_id = ?
            LIMIT 1
            """,
            (game_id, player_id),
        ).fetchone()
        if stat_row is None:
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


def _espn_boxscore_player_status_map(payload: dict[str, Any]) -> dict[int, dict[str, Any]]:
    player_status: dict[int, dict[str, Any]] = {}
    teams = ((payload.get("boxscore") or {}).get("players") or [])
    for team_box in teams:
        for stat_group in team_box.get("statistics", []):
            for row in stat_group.get("athletes", []):
                athlete = row.get("athlete") or {}
                try:
                    player_id = int(str(athlete.get("id") or "").strip())
                except ValueError:
                    continue
                player_status[player_id] = {
                    "did_not_play": bool(row.get("didNotPlay")),
                    "active": bool(row.get("active")) if row.get("active") is not None else None,
                    "reason": str(row.get("reason") or "").strip() or None,
                    "name": str(athlete.get("displayName") or "").strip() or None,
                }
    return player_status


def delete_unavailable_special_snapshots_from_espn(
    conn: sqlite3.Connection,
    *,
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
    force_refresh: bool = False,
    dry_run: bool = False,
) -> dict[str, Any]:
    tracking = _open_tracking_connection()
    try:
        normalized_game_ids = tuple(sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0}))
        normalized_dates = tuple(sorted({str(game_date).strip() for game_date in (target_dates or []) if str(game_date).strip()}))
        filters: list[str] = []
        params: list[object] = []
        if normalized_game_ids:
            placeholders = ",".join("?" for _ in normalized_game_ids)
            filters.append(f"latest.game_id IN ({placeholders})")
            params.extend(normalized_game_ids)
        filter_sql = f" AND {' AND '.join(filters)}" if filters else ""
        rows = tracking.execute(
            """
            WITH latest AS (
                SELECT
                    ps.id,
                    ps.game_id,
                    ps.player_id,
                    ps.player_name,
                    ps.captured_at,
                    ROW_NUMBER() OVER (
                        PARTITION BY ps.game_id, ps.player_id
                        ORDER BY ps.captured_at DESC, ps.id DESC
                    ) AS snapshot_rank
                FROM projection_snapshots ps
                LEFT JOIN settlements st ON st.snapshot_id = ps.id
                WHERE st.snapshot_id IS NULL
            )
            SELECT
                latest.id,
                latest.game_id,
                latest.player_id,
                latest.player_name,
                latest.captured_at
            FROM latest
            WHERE latest.snapshot_rank = 1
            """
            + filter_sql
            + """
            ORDER BY latest.game_id, latest.player_name
            """,
            tuple(params),
        ).fetchall()
        by_game: dict[int, list[sqlite3.Row]] = {}
        for row in rows:
            by_game.setdefault(int(row["game_id"]), []).append(row)
        delete_ids: list[int] = []
        deleted_missing_boxscore = 0
        deleted_dnp = 0
        checked_games = 0
        skipped_games: list[dict[str, Any]] = []
        decisions: list[dict[str, Any]] = []
        for game_id, game_rows in by_game.items():
            game_row = conn.execute(
                """
                SELECT id, game_date, start_time, status, COALESCE(espn_event_id, id) AS summary_event_id
                FROM games
                WHERE id = ?
                LIMIT 1
                """,
                (game_id,),
            ).fetchone()
            if game_row is None:
                skipped_games.append({"game_id": game_id, "reason": "missing_game"})
                continue
            game_local_date = local_game_date(game_row["game_date"], game_row["start_time"])
            if normalized_dates and game_local_date not in normalized_dates:
                continue
            if str(game_row["status"] or "").strip().lower() != "final":
                skipped_games.append({"game_id": game_id, "reason": f"game_not_final:{game_row['status']}"})
                continue
            summary_event_id = int(game_row["summary_event_id"])
            try:
                payload = fetch_summary(summary_event_id, force_refresh=force_refresh)
            except Exception as exc:
                skipped_games.append({"game_id": game_id, "summary_event_id": summary_event_id, "reason": str(exc)})
                continue
            player_status = _espn_boxscore_player_status_map(payload)
            if not player_status:
                skipped_games.append({"game_id": game_id, "summary_event_id": summary_event_id, "reason": "empty_boxscore"})
                continue
            checked_games += 1
            for row in game_rows:
                player_id = int(row["player_id"])
                status = player_status.get(player_id)
                if status is None:
                    delete_ids.append(int(row["id"]))
                    deleted_missing_boxscore += 1
                    decisions.append(
                        {
                            "snapshot_id": int(row["id"]),
                            "game_id": game_id,
                            "player_id": player_id,
                            "player_name": str(row["player_name"]),
                            "decision": "delete_missing_from_boxscore",
                        }
                    )
                    continue
                if bool(status.get("did_not_play")):
                    delete_ids.append(int(row["id"]))
                    deleted_dnp += 1
                    decisions.append(
                        {
                            "snapshot_id": int(row["id"]),
                            "game_id": game_id,
                            "player_id": player_id,
                            "player_name": str(row["player_name"]),
                            "decision": "delete_did_not_play",
                            "reason": status.get("reason"),
                        }
                    )
                    continue
                decisions.append(
                    {
                        "snapshot_id": int(row["id"]),
                        "game_id": game_id,
                        "player_id": player_id,
                        "player_name": str(row["player_name"]),
                        "decision": "keep_boxscore_present",
                    }
                )
        if delete_ids and not dry_run:
            placeholders = ",".join("?" for _ in delete_ids)
            tracking.execute(f"DELETE FROM projection_snapshots WHERE id IN ({placeholders})", tuple(delete_ids))
            tracking.commit()
        return {
            "eligible_snapshots": len(rows),
            "eligible_games": len(by_game),
            "checked_games": checked_games,
            "deleted": len(delete_ids),
            "deleted_missing_boxscore": deleted_missing_boxscore,
            "deleted_did_not_play": deleted_dnp,
            "dry_run": bool(dry_run),
            "force_refresh": bool(force_refresh),
            "game_ids": [int(value) for value in normalized_game_ids],
            "target_dates": list(normalized_dates),
            "skipped_games": skipped_games,
            "decisions": decisions,
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


def list_game_board_summaries(
    *,
    game_ids: list[int] | None = None,
    target_dates: list[str] | None = None,
) -> list[dict[str, Any]]:
    tracking = _open_tracking_connection()
    try:
        normalized_game_ids = tuple(sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0}))
        normalized_dates = tuple(sorted({str(game_date).strip() for game_date in (target_dates or []) if str(game_date).strip()}))
        filter_sql = ""
        params: tuple[object, ...] = ()
        if normalized_game_ids:
            placeholders = ",".join("?" for _ in normalized_game_ids)
            filter_sql = f" WHERE game_id IN ({placeholders})"
            params = normalized_game_ids
        elif normalized_dates:
            placeholders = ",".join("?" for _ in normalized_dates)
            filter_sql = f" WHERE game_date IN ({placeholders})"
            params = normalized_dates
        rows = tracking.execute(
            """
            SELECT
                game_id,
                game_date,
                player_count,
                candidate_count_50_plus,
                candidate_threshold,
                candidate_count_threshold,
                avg_prob_2_plus,
                avg_prob_3_plus,
                top_projected_stocks,
                top_prob_2_plus,
                top_player_name,
                built_at
            FROM game_board_summaries
            """
            + filter_sql
            + """
            ORDER BY game_date, game_id
            """,
            params,
        ).fetchall()
        return [dict(row) for row in rows]
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
