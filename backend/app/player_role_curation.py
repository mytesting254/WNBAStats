from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass

BALL_HANDLER_BUCKETS = (
    "primary",
    "secondary",
    "connector",
    "finisher",
    "interior",
    "low_usage",
)

REBOUND_BUCKETS = (
    "crash_big",
    "wing_rebounder",
    "guard_rebounder",
    "leak_out",
    "low_rebound",
)

SHOT_VOLUME_BUCKETS = (
    "alpha",
    "secondary",
    "tertiary",
    "cleanup",
    "low_usage",
)


@dataclass(frozen=True)
class PlayerRoleProfile:
    player_id: int
    team_id: int | None
    source: str
    ball_handler_bucket: str | None
    rebound_bucket: str | None
    shot_volume_bucket: str | None
    offensive_rebound_bias: float
    defensive_rebound_bias: float
    field_goal_attempt_bias: float
    notes: str | None
    effective_start_date: str | None
    effective_end_date: str | None


def ensure_player_role_curation_schema(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS player_role_bucket_overrides (
            id INTEGER PRIMARY KEY,
            player_id INTEGER NOT NULL,
            team_id INTEGER,
            source TEXT NOT NULL DEFAULT 'manual',
            ball_handler_bucket TEXT,
            rebound_bucket TEXT,
            shot_volume_bucket TEXT,
            offensive_rebound_bias REAL NOT NULL DEFAULT 1.0,
            defensive_rebound_bias REAL NOT NULL DEFAULT 1.0,
            field_goal_attempt_bias REAL NOT NULL DEFAULT 1.0,
            priority INTEGER NOT NULL DEFAULT 100,
            effective_start_date TEXT,
            effective_end_date TEXT,
            notes TEXT,
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            FOREIGN KEY (player_id) REFERENCES players(id),
            FOREIGN KEY (team_id) REFERENCES teams(id)
        );

        CREATE INDEX IF NOT EXISTS idx_player_role_bucket_player_date
        ON player_role_bucket_overrides(player_id, effective_start_date, effective_end_date, priority, id);

        CREATE INDEX IF NOT EXISTS idx_player_role_bucket_team
        ON player_role_bucket_overrides(team_id, effective_start_date, effective_end_date);
        """
    )


def upsert_player_role_override(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    team_id: int | None = None,
    source: str = "manual",
    ball_handler_bucket: str | None = None,
    rebound_bucket: str | None = None,
    shot_volume_bucket: str | None = None,
    offensive_rebound_bias: float = 1.0,
    defensive_rebound_bias: float = 1.0,
    field_goal_attempt_bias: float = 1.0,
    priority: int = 100,
    effective_start_date: str | None = None,
    effective_end_date: str | None = None,
    notes: str | None = None,
) -> int:
    _validate_bucket("ball_handler_bucket", ball_handler_bucket, BALL_HANDLER_BUCKETS)
    _validate_bucket("rebound_bucket", rebound_bucket, REBOUND_BUCKETS)
    _validate_bucket("shot_volume_bucket", shot_volume_bucket, SHOT_VOLUME_BUCKETS)
    cursor = conn.execute(
        """
        INSERT INTO player_role_bucket_overrides (
            player_id,
            team_id,
            source,
            ball_handler_bucket,
            rebound_bucket,
            shot_volume_bucket,
            offensive_rebound_bias,
            defensive_rebound_bias,
            field_goal_attempt_bias,
            priority,
            effective_start_date,
            effective_end_date,
            notes,
            created_at,
            updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)
        """,
        (
            int(player_id),
            int(team_id) if team_id is not None else None,
            str(source or "manual"),
            _normalize_bucket(ball_handler_bucket),
            _normalize_bucket(rebound_bucket),
            _normalize_bucket(shot_volume_bucket),
            float(offensive_rebound_bias),
            float(defensive_rebound_bias),
            float(field_goal_attempt_bias),
            int(priority),
            effective_start_date,
            effective_end_date,
            str(notes) if notes else None,
        ),
    )
    return int(cursor.lastrowid or 0)


def list_player_role_overrides(
    conn: sqlite3.Connection,
    *,
    player_id: int | None = None,
    team_id: int | None = None,
) -> list[sqlite3.Row]:
    clauses: list[str] = []
    params: list[object] = []
    if player_id is not None:
        clauses.append("o.player_id = ?")
        params.append(int(player_id))
    if team_id is not None:
        clauses.append("(o.team_id = ? OR o.team_id IS NULL)")
        params.append(int(team_id))
    where_sql = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    return conn.execute(
        f"""
        SELECT
            o.*,
            p.full_name AS player_name,
            t.name AS team_name
        FROM player_role_bucket_overrides o
        JOIN players p ON p.id = o.player_id
        LEFT JOIN teams t ON t.id = o.team_id
        {where_sql}
        ORDER BY p.full_name ASC, o.priority ASC, o.effective_start_date DESC, o.id DESC
        """,
        tuple(params),
    ).fetchall()


def get_player_role_profile(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    team_id: int | None = None,
    on_date: str | None = None,
) -> PlayerRoleProfile | None:
    params: list[object] = [int(player_id)]
    date_sql = ""
    if on_date:
        date_sql = """
          AND (o.effective_start_date IS NULL OR o.effective_start_date <= ?)
          AND (o.effective_end_date IS NULL OR o.effective_end_date >= ?)
        """
        params.extend([str(on_date), str(on_date)])
    team_sql = ""
    if team_id is not None:
        team_sql = " AND (o.team_id = ? OR o.team_id IS NULL)"
        params.append(int(team_id))
    row = conn.execute(
        """
        SELECT
            o.player_id,
            o.team_id,
            o.source,
            o.ball_handler_bucket,
            o.rebound_bucket,
            o.shot_volume_bucket,
            o.offensive_rebound_bias,
            o.defensive_rebound_bias,
            o.field_goal_attempt_bias,
            o.notes,
            o.effective_start_date,
            o.effective_end_date
        FROM player_role_bucket_overrides o
        WHERE o.player_id = ?
        """
        + date_sql
        + team_sql
        + """
        ORDER BY
            CASE WHEN o.team_id IS NULL THEN 1 ELSE 0 END ASC,
            o.priority ASC,
            COALESCE(o.effective_start_date, '') DESC,
            o.id DESC
        LIMIT 1
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        return None
    return PlayerRoleProfile(
        player_id=int(row["player_id"]),
        team_id=int(row["team_id"]) if row["team_id"] is not None else None,
        source=str(row["source"] or "manual"),
        ball_handler_bucket=_nullable_text(row["ball_handler_bucket"]),
        rebound_bucket=_nullable_text(row["rebound_bucket"]),
        shot_volume_bucket=_nullable_text(row["shot_volume_bucket"]),
        offensive_rebound_bias=float(row["offensive_rebound_bias"] or 1.0),
        defensive_rebound_bias=float(row["defensive_rebound_bias"] or 1.0),
        field_goal_attempt_bias=float(row["field_goal_attempt_bias"] or 1.0),
        notes=_nullable_text(row["notes"]),
        effective_start_date=_nullable_text(row["effective_start_date"]),
        effective_end_date=_nullable_text(row["effective_end_date"]),
    )


def player_role_curation_signature(conn: sqlite3.Connection) -> str:
    ensure_player_role_curation_schema(conn)
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS row_count,
            COALESCE(MAX(updated_at), '') AS max_updated_at,
            COALESCE(SUM(player_id), 0) AS player_id_sum,
            COALESCE(SUM(COALESCE(team_id, 0)), 0) AS team_id_sum,
            COALESCE(SUM(priority), 0) AS priority_sum
        FROM player_role_bucket_overrides
        """
    ).fetchone()
    payload = {
        "row_count": int(row["row_count"] or 0),
        "max_updated_at": str(row["max_updated_at"] or ""),
        "player_id_sum": int(row["player_id_sum"] or 0),
        "team_id_sum": int(row["team_id_sum"] or 0),
        "priority_sum": int(row["priority_sum"] or 0),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _validate_bucket(name: str, value: str | None, allowed: tuple[str, ...]) -> None:
    normalized = _normalize_bucket(value)
    if normalized is None:
        return
    if normalized not in allowed:
        raise ValueError(f"{name} must be one of {allowed}; got {value!r}")


def _normalize_bucket(value: str | None) -> str | None:
    if value is None:
        return None
    cleaned = str(value).strip().lower()
    return cleaned or None


def _nullable_text(value: object) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None
