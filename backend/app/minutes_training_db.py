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
            availability_status TEXT,
            did_not_play INTEGER NOT NULL DEFAULT 0,
            status_reason TEXT,
            minutes_text TEXT,
            team_margin REAL,
            blowout_margin_flag INTEGER NOT NULL DEFAULT 0,
            low_minutes_outlier_flag INTEGER NOT NULL DEFAULT 0,
            high_minutes_outlier_flag INTEGER NOT NULL DEFAULT 0,
            returner_risk_flag INTEGER NOT NULL DEFAULT 0,
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
            quality_flags_json TEXT,
            is_clean INTEGER NOT NULL DEFAULT 1,
            exclusion_reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source_player_id, source_game_id)
        );

        CREATE INDEX IF NOT EXISTS idx_minutes_training_clean_bucket
        ON minutes_training_examples(is_clean, role_bucket, game_date);
        """
    )
    _ensure_training_example_columns(conn)
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
    exclusion_counts: dict[str, int] = {}
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
            availability = _availability_row(source_conn, player_id=player_id, game_id=int(current["game_id"]))
            team_margin = _team_margin_value(current)
            quality_flags: dict[str, object] = {
                "has_availability_row": availability is not None,
                "did_not_play": int(availability["did_not_play"] or 0) if availability is not None else 0,
                "coach_decision_dnp": _coach_decision_dnp_flag(availability),
                "blowout_margin_flag": int(team_margin is not None and abs(team_margin) >= 20.0),
                "extreme_blowout_margin_flag": int(team_margin is not None and abs(team_margin) >= 25.0),
                "low_minutes_outlier_flag": 0,
                "high_minutes_outlier_flag": 0,
                "returner_risk_flag": 0,
                "injury_exit_risk_flag": 0,
                "overtime_like_spike_flag": 0,
                "low_minutes_collapse_flag": 0,
            }
            if ppm._before_training_start(game_date, training_start):
                exclusion_reason = "before_training_start"
            elif idx < 5:
                exclusion_reason = "missing_history_window"
            elif availability is not None and int(availability["did_not_play"] or 0) == 1:
                exclusion_reason = "did_not_play"
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
            opportunity_context = [0.0, 0.0]
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
                    quality_flags["returner_risk_flag"] = int(recent_absence_days is not None and recent_absence_days >= 10.0)
                    lineup_context = ppm._minutes_lineup_context_from_player_rows(
                        source_conn,
                        player_id=player_id,
                        team_id=int(context["team_id"]),
                        player_rows=player_rows,
                        row_index=idx,
                    )
                    opportunity_context = ppm._historical_minutes_opportunity_context(
                        source_conn,
                        player_id=player_id,
                        game_id=int(current["game_id"]),
                        team_id=int(context["team_id"]),
                        position=str(current["position"] or ""),
                        before_game_date=game_date,
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
                        opportunity_context=opportunity_context,
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
                        opportunity_context=opportunity_context,
                    )
                    recent_blend = ppm._minutes_recent_blend(
                        recent_minutes_avg=recent_minutes_avg,
                        last_10_minutes_avg=last_10_minutes_avg,
                    )
                    quality_flags["low_minutes_outlier_flag"] = int(
                        recent_blend is not None
                        and target_minutes <= 6.0
                        and recent_blend >= 14.0
                    )
                    quality_flags["high_minutes_outlier_flag"] = int(
                        recent_blend is not None
                        and target_minutes >= recent_blend + 10.0
                    )
                    quality_flags["injury_exit_risk_flag"] = int(
                        _injury_exit_risk_flag(
                            availability=availability,
                            target_minutes=target_minutes,
                            recent_blend=recent_blend,
                        )
                    )
                    quality_flags["overtime_like_spike_flag"] = int(
                        _overtime_like_spike_flag(
                            target_minutes=target_minutes,
                            recent_blend=recent_blend,
                            team_margin=team_margin,
                        )
                    )
                    quality_flags["low_minutes_collapse_flag"] = int(
                        _low_minutes_collapse_flag(
                            target_minutes=target_minutes,
                            recent_blend=recent_blend,
                            recent_absence_days=recent_absence_days,
                            team_margin=team_margin,
                        )
                    )
                    exclusion_reason = _derive_cleanup_exclusion_reason(
                        availability=availability,
                        target_minutes=target_minutes,
                        recent_blend=recent_blend,
                        recent_absence_days=recent_absence_days,
                        team_margin=team_margin,
                        quality_flags=quality_flags,
                        existing_reason=exclusion_reason,
                    )
                    feature_values_json = json.dumps(feature_values, separators=(",", ":"))

            is_clean = 0 if exclusion_reason else 1
            if is_clean:
                included_rows += 1
            else:
                excluded_rows += 1
                exclusion_counts[exclusion_reason] = exclusion_counts.get(exclusion_reason, 0) + 1

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
                    str(availability["source"] or "available") if availability is not None else "available",
                    int(availability["did_not_play"] or 0) if availability is not None else 0,
                    str(availability["status_reason"] or "") if availability is not None else "",
                    str(availability["minutes_text"] or "") if availability is not None else "",
                    float(team_margin) if team_margin is not None else None,
                    int(quality_flags["blowout_margin_flag"]),
                    int(quality_flags["low_minutes_outlier_flag"]),
                    int(quality_flags["high_minutes_outlier_flag"]),
                    int(quality_flags["returner_risk_flag"]),
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
                    json.dumps(quality_flags, separators=(",", ":")),
                    is_clean,
                    exclusion_reason,
                    built_at,
                )
            )

    training_conn.executemany(
        """
        INSERT INTO minutes_training_examples (
            source_player_id, source_game_id, team_id, opponent_id, game_date, season,
            rotation_role, role_bucket, position, availability_status, did_not_play, status_reason, minutes_text,
            team_margin, blowout_margin_flag, low_minutes_outlier_flag, high_minutes_outlier_flag, returner_risk_flag,
            target_minutes, recent_blend, target_delta,
            minute_volatility, recent_minutes_avg, last_10_minutes_avg, recent_absence_days,
            recent_team_minute_share, recent_minute_rank, recent_position_minute_share,
            games_since_joining_team, new_team_minutes_trend, teammate_minutes_redistribution,
            rotation_stability, features_json, quality_flags_json, is_clean, exclusion_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
            "exclusion_counts": exclusion_counts,
            "local_date": datetime.now(APP_TIMEZONE).date().isoformat(),
        },
    )
    training_conn.commit()


def _source_signature(conn: sqlite3.Connection) -> str:
    parts = [
        _table_signature(
            conn,
            "games",
            "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, COALESCE(MAX(game_date), '') AS max_game_date",
        ),
        _table_signature(
            conn,
            "player_game_stats",
            (
                "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, "
                "ROUND(COALESCE(SUM(minutes), 0), 3) AS minutes_sum"
            ),
        ),
        _table_signature(
            conn,
            "players",
            "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, COALESCE(MAX(team_id), 0) AS max_team_id",
        ),
        _table_signature(
            conn,
            "player_team_history",
            "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, COALESCE(MAX(game_id), 0) AS max_game_id",
        ),
        _table_signature(
            conn,
            "player_game_availability",
            (
                "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, "
                "SUM(CASE WHEN did_not_play = 1 THEN 1 ELSE 0 END) AS dnp_rows"
            ),
        ),
        _table_signature(
            conn,
            "team_game_results",
            (
                "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, "
                "ROUND(COALESCE(SUM(points + opponent_points), 0), 3) AS scoring_sum, "
                "ROUND(COALESCE(SUM(possessions), 0), 3) AS possessions_sum, "
                "SUM(CASE WHEN COALESCE(possessions_source, 'fallback') != 'fallback' THEN 1 ELSE 0 END) AS sourced_rows"
            ),
        ),
    ]
    return "|".join(parts)


def _table_signature(conn: sqlite3.Connection, table: str, select_sql: str) -> str:
    row = conn.execute(f"SELECT {select_sql} FROM {table}").fetchone()
    values = [f"{key}={row[key]}" for key in row.keys()]
    return f"{table}:" + ",".join(values)


def _ensure_training_example_columns(conn: sqlite3.Connection) -> None:
    columns = {
        row["name"]: str(row["type"] or "")
        for row in conn.execute("PRAGMA table_info(minutes_training_examples)").fetchall()
    }
    additions = [
        ("availability_status", "TEXT"),
        ("did_not_play", "INTEGER NOT NULL DEFAULT 0"),
        ("status_reason", "TEXT"),
        ("minutes_text", "TEXT"),
        ("team_margin", "REAL"),
        ("blowout_margin_flag", "INTEGER NOT NULL DEFAULT 0"),
        ("low_minutes_outlier_flag", "INTEGER NOT NULL DEFAULT 0"),
        ("high_minutes_outlier_flag", "INTEGER NOT NULL DEFAULT 0"),
        ("returner_risk_flag", "INTEGER NOT NULL DEFAULT 0"),
        ("quality_flags_json", "TEXT"),
    ]
    for name, ddl in additions:
        if name not in columns:
            conn.execute(f"ALTER TABLE minutes_training_examples ADD COLUMN {name} {ddl}")


def _availability_row(conn: sqlite3.Connection, *, player_id: int, game_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT source, did_not_play, status_reason, minutes_text
        FROM player_game_availability
        WHERE player_id = ? AND game_id = ?
        ORDER BY observed_at DESC, id DESC
        LIMIT 1
        """,
        (player_id, game_id),
    ).fetchone()


def _team_margin_value(row: sqlite3.Row) -> float | None:
    value = row["team_margin"] if "team_margin" in row.keys() else None
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _coach_decision_dnp_flag(availability: sqlite3.Row | None) -> int:
    if availability is None:
        return 0
    reason = str(availability["status_reason"] or "").strip().lower()
    if not reason:
        return 0
    return int("coach" in reason or "decision" in reason)


def _injury_exit_risk_flag(
    *,
    availability: sqlite3.Row | None,
    target_minutes: float,
    recent_blend: float | None,
) -> bool:
    if availability is None or recent_blend is None:
        return False
    if target_minutes > max(12.0, recent_blend - 8.0):
        return False
    status_reason = str(availability["status_reason"] or "").strip().lower()
    minutes_text = str(availability["minutes_text"] or "").strip().lower()
    injury_terms = (
        "inj",
        "ankle",
        "knee",
        "hamstring",
        "groin",
        "back",
        "shoulder",
        "wrist",
        "foot",
        "leg",
        "illness",
        "concussion",
    )
    return any(term in status_reason or term in minutes_text for term in injury_terms)


def _overtime_like_spike_flag(
    *,
    target_minutes: float,
    recent_blend: float | None,
    team_margin: float | None,
) -> bool:
    if recent_blend is None:
        return False
    if team_margin is not None and abs(team_margin) >= 15.0:
        return False
    return target_minutes >= 40.0 and target_minutes >= recent_blend + 7.0


def _low_minutes_collapse_flag(
    *,
    target_minutes: float,
    recent_blend: float | None,
    recent_absence_days: float | None,
    team_margin: float | None,
) -> bool:
    if recent_blend is None:
        return False
    if recent_absence_days is not None and recent_absence_days >= 7.0:
        return False
    if team_margin is not None and abs(team_margin) >= 20.0:
        return False
    return recent_blend >= 18.0 and target_minutes <= 8.0 and (recent_blend - target_minutes) >= 10.0


def _derive_cleanup_exclusion_reason(
    *,
    availability: sqlite3.Row | None,
    target_minutes: float,
    recent_blend: float | None,
    recent_absence_days: float | None,
    team_margin: float | None,
    quality_flags: dict[str, object],
    existing_reason: str | None,
) -> str | None:
    if existing_reason:
        return existing_reason
    if availability is not None and int(availability["did_not_play"] or 0) == 1:
        return "did_not_play"
    if recent_blend is None:
        return "missing_recent_blend"
    if (
        int(quality_flags.get("low_minutes_outlier_flag") or 0) == 1
        and recent_absence_days is None
        and (team_margin is None or abs(team_margin) < 20.0)
    ):
        return "low_minutes_rotation_anomaly"
    if int(quality_flags.get("injury_exit_risk_flag") or 0) == 1:
        return "injury_exit_low_minutes"
    if int(quality_flags.get("overtime_like_spike_flag") or 0) == 1:
        return "overtime_like_spike"
    if int(quality_flags.get("low_minutes_collapse_flag") or 0) == 1:
        return "low_minutes_role_collapse"
    if (
        int(quality_flags.get("high_minutes_outlier_flag") or 0) == 1
        and team_margin is not None
        and abs(team_margin) >= 25.0
    ):
        return "blowout_spike_outlier"
    if (
        recent_absence_days is not None
        and recent_absence_days >= 21.0
        and target_minutes <= 8.0
    ):
        return "deep_return_ramp_game"
    return None
