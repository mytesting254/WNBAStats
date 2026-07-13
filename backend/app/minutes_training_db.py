from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from .paths import get_training_db_path
from .timezone_utils import APP_TIMEZONE


def ensure_minutes_training_db(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, object]:
    from . import player_prop_model as ppm

    training_db_path = get_training_db_path()
    training_db_path.parent.mkdir(parents=True, exist_ok=True)
    expected_signature = _source_signature(conn)
    expected_training_start = ppm._training_start_date()
    expected_model_version = ppm.MODEL_VERSION

    with sqlite3.connect(training_db_path) as training_conn:
        training_conn.row_factory = sqlite3.Row
        _init_training_db(training_conn)
        metadata = _read_metadata(training_conn)
        current_row_count = int(metadata.get("included_rows") or 0)
        if (
            not force
            and metadata.get("source_signature") == expected_signature
            and metadata.get("training_start") == expected_training_start
            and metadata.get("model_version") == expected_model_version
            and current_row_count > 0
        ):
            return {
                "path": str(training_db_path),
                "rebuilt": False,
                "included_rows": current_row_count,
                "candidate_rows": int(metadata.get("candidate_rows") or 0),
                "excluded_rows": int(metadata.get("excluded_rows") or 0),
                "built_at": metadata.get("built_at"),
                "source_signature": expected_signature,
            }

        _rebuild_minutes_training_examples(
            source_conn=conn,
            training_conn=training_conn,
            source_signature=expected_signature,
            training_start=expected_training_start,
            model_version=expected_model_version,
        )
        metadata = _read_metadata(training_conn)
        return {
            "path": str(training_db_path),
            "rebuilt": True,
            "included_rows": int(metadata.get("included_rows") or 0),
            "candidate_rows": int(metadata.get("candidate_rows") or 0),
            "excluded_rows": int(metadata.get("excluded_rows") or 0),
            "built_at": metadata.get("built_at"),
            "source_signature": expected_signature,
        }


def load_minutes_training_examples(
    conn: sqlite3.Connection,
    *,
    role_bucket: str | None = None,
    force_rebuild: bool = False,
) -> tuple[list[tuple[list[float], float]], dict[str, object]]:
    info = ensure_minutes_training_db(conn, force=force_rebuild)
    training_db_path = Path(str(info["path"]))
    query = """
        SELECT features_json, target_delta
        FROM minutes_training_examples
        WHERE is_clean = 1
    """
    params: list[object] = []
    if role_bucket:
        query += " AND role_bucket = ?"
        params.append(role_bucket)
    query += " ORDER BY game_date ASC, source_game_id ASC"

    with sqlite3.connect(training_db_path) as training_conn:
        training_conn.row_factory = sqlite3.Row
        rows = training_conn.execute(query, params).fetchall()
    samples = [
        (list(json.loads(str(row["features_json"]))), float(row["target_delta"]))
        for row in rows
    ]
    return samples, info


def minutes_training_db_signature(conn: sqlite3.Connection) -> str:
    info = ensure_minutes_training_db(conn, force=False)
    return str(info["source_signature"])


def _init_training_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS minutes_training_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS minutes_training_examples (
            id INTEGER PRIMARY KEY,
            source_player_id INTEGER NOT NULL,
            source_game_id INTEGER NOT NULL,
            team_id INTEGER,
            opponent_id INTEGER,
            game_date TEXT NOT NULL,
            season TEXT NOT NULL,
            rotation_role TEXT NOT NULL,
            role_bucket TEXT,
            position TEXT,
            target_minutes REAL,
            recent_blend REAL,
            target_delta REAL,
            minute_volatility REAL,
            recent_minutes_avg REAL,
            last_10_minutes_avg REAL,
            recent_absence_days REAL,
            recent_team_minute_share REAL,
            recent_minute_rank REAL,
            recent_position_minute_share REAL,
            games_since_joining_team REAL,
            new_team_minutes_trend REAL,
            teammate_minutes_redistribution REAL,
            rotation_stability REAL,
            features_json TEXT,
            is_clean INTEGER NOT NULL DEFAULT 1,
            exclusion_reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source_player_id, source_game_id)
        );

        CREATE INDEX IF NOT EXISTS idx_minutes_training_clean_bucket
        ON minutes_training_examples(is_clean, role_bucket, game_date);
        """
    )
    conn.commit()


def _read_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM minutes_training_metadata").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _write_metadata(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    conn.executemany(
        """
        INSERT INTO minutes_training_metadata (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        [(key, json.dumps(value) if isinstance(value, (dict, list)) else str(value)) for key, value in values.items()],
    )


def _rebuild_minutes_training_examples(
    *,
    source_conn: sqlite3.Connection,
    training_conn: sqlite3.Connection,
    source_signature: str,
    training_start: str,
    model_version: str,
) -> None:
    from . import player_prop_model as ppm

    built_at = datetime.now(timezone.utc).isoformat()
    candidate_rows = 0
    included_rows = 0
    excluded_rows = 0
    training_conn.execute("DELETE FROM minutes_training_examples")

    players = source_conn.execute("SELECT id FROM players ORDER BY id").fetchall()
    rows_to_insert: list[tuple[object, ...]] = []
    for player in players:
        player_id = int(player["id"])
        player_rows = ppm._player_training_rows(source_conn, player_id)
        minutes = [float(row["minutes"]) for row in player_rows]
        for idx, current in enumerate(player_rows):
            candidate_rows += 1
            exclusion_reason: str | None = None
            target_minutes = float(current["minutes"] or 0.0)
            game_date = str(current["game_date"] or "")
            season = game_date[:4]
            if ppm._before_training_start(game_date, training_start):
                exclusion_reason = "before_training_start"
            elif idx < 5:
                exclusion_reason = "missing_history_window"
            elif target_minutes <= 0.0:
                exclusion_reason = "non_positive_minutes"
            elif target_minutes > 45.0:
                exclusion_reason = "minutes_outlier"

            role_bucket = None
            feature_values_json = None
            context = ppm._historical_game_context(current)
            minute_volatility = None
            recent_minutes_avg = None
            last_10_minutes_avg = None
            recent_absence_days = None
            lineup_context = [0.0, 0.5, 0.0]
            team_transition = [0.0, 0.0, 0.0, 0.5]
            recent_blend = None

            if exclusion_reason is None:
                newest_minutes = list(reversed(minutes[max(0, idx - 10):idx]))
                if len(newest_minutes) < 5:
                    exclusion_reason = "missing_history_window"
                elif int(current["resolved_team_id"] or 0) <= 0:
                    exclusion_reason = "missing_team_resolution"
                elif not str(current["position"] or "").strip():
                    exclusion_reason = "missing_position"
                elif context.get("team_id") is None or context.get("opponent_id") is None:
                    exclusion_reason = "incomplete_context"
                else:
                    previous_game_date = str(player_rows[idx - 1]["game_date"]) if idx > 0 else None
                    minute_volatility = ppm._minute_volatility(newest_minutes)
                    ewma_minutes = ppm._ewma_newest_first(newest_minutes, alpha=0.38)
                    minutes_trend = ppm._recent_trend(newest_minutes)
                    recent_minutes_avg = sum(newest_minutes[:5]) / min(len(newest_minutes), 5)
                    last_10_minutes_avg = sum(newest_minutes) / len(newest_minutes)
                    recent_absence_days = ppm._days_between_game_dates(previous_game_date, game_date)
                    lineup_context = ppm._minutes_lineup_context_from_player_rows(
                        source_conn,
                        player_id=player_id,
                        team_id=int(context["team_id"]),
                        player_rows=player_rows,
                        row_index=idx,
                    )
                    role_state = ppm._classify_minutes_role(
                        rotation_role=str(current["rotation_role"] or "starter"),
                        recent_minutes_avg=recent_minutes_avg,
                        last_10_minutes_avg=last_10_minutes_avg,
                        ewma_minutes=ewma_minutes,
                        minutes_trend=minutes_trend,
                        minute_volatility=minute_volatility,
                        injury_status="available",
                        injury_delta=0.0,
                        recent_absence_days=recent_absence_days,
                        lineup_context=lineup_context,
                    )
                    role_bucket = role_state.bucket
                    team_transition = ppm._team_transition_features_from_player_rows(
                        source_conn,
                        player_id=player_id,
                        team_id=int(context["team_id"]),
                        player_rows=player_rows,
                        row_index=idx,
                    )
                    feature_values = ppm._minutes_feature_values(
                        role_state=role_state,
                        ewma_minutes=ewma_minutes,
                        recent_minutes_avg=recent_minutes_avg,
                        last_10_minutes_avg=last_10_minutes_avg,
                        minutes_trend=minutes_trend,
                        minute_volatility=minute_volatility,
                        rest_days=int(context["rest_days"]),
                        is_home=bool(context["is_home"]),
                        spread_abs=abs(float(context["team_spread"])) if context.get("team_spread") is not None else 0.0,
                        injury_delta=0.0,
                        recent_absence_days=recent_absence_days,
                        team_transition=team_transition,
                        lineup_context=lineup_context,
                    )
                    recent_blend = ppm._minutes_recent_blend(
                        recent_minutes_avg=recent_minutes_avg,
                        last_10_minutes_avg=last_10_minutes_avg,
                    )
                    feature_values_json = json.dumps(feature_values, separators=(",", ":"))

            is_clean = 0 if exclusion_reason else 1
            if is_clean:
                included_rows += 1
            else:
                excluded_rows += 1

            rows_to_insert.append(
                (
                    player_id,
                    int(current["game_id"]),
                    int(context["team_id"]) if context.get("team_id") is not None else None,
                    int(context["opponent_id"]) if context.get("opponent_id") is not None else None,
                    game_date,
                    season,
                    str(current["rotation_role"] or "starter"),
                    role_bucket,
                    str(current["position"] or ""),
                    target_minutes if target_minutes > 0.0 else None,
                    recent_blend,
                    (target_minutes - recent_blend) if recent_blend is not None else None,
                    minute_volatility,
                    recent_minutes_avg,
                    last_10_minutes_avg,
                    recent_absence_days,
                    float(lineup_context[0]),
                    float(lineup_context[1]),
                    float(lineup_context[2]),
                    float(team_transition[0]),
                    float(team_transition[1]),
                    float(team_transition[2]),
                    float(team_transition[3]),
                    feature_values_json,
                    is_clean,
                    exclusion_reason,
                    built_at,
                )
            )

    training_conn.executemany(
        """
        INSERT INTO minutes_training_examples (
            source_player_id, source_game_id, team_id, opponent_id, game_date, season,
            rotation_role, role_bucket, position, target_minutes, recent_blend, target_delta,
            minute_volatility, recent_minutes_avg, last_10_minutes_avg, recent_absence_days,
            recent_team_minute_share, recent_minute_rank, recent_position_minute_share,
            games_since_joining_team, new_team_minutes_trend, teammate_minutes_redistribution,
            rotation_stability, features_json, is_clean, exclusion_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
    _write_metadata(
        training_conn,
        {
            "source_signature": source_signature,
            "training_start": training_start,
            "model_version": model_version,
            "built_at": built_at,
            "candidate_rows": candidate_rows,
            "included_rows": included_rows,
            "excluded_rows": excluded_rows,
            "local_date": datetime.now(APP_TIMEZONE).date().isoformat(),
        },
    )
    training_conn.commit()


def _source_signature(conn: sqlite3.Connection) -> str:
    tables = [
        "player_game_stats",
        "games",
        "players",
        "player_team_history",
        "injuries",
        "team_game_results",
    ]
    parts: list[str] = []
    for table in tables:
        row = conn.execute(
            f"SELECT COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id FROM {table}"
        ).fetchone()
        parts.append(f"{table}:{int(row['row_count'] or 0)}:{int(row['max_id'] or 0)}")
    return "|".join(parts)
