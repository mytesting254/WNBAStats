from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .game_pregame_features import build_matchup_pregame_features
from .game_pregame_features import GAME_PREGAME_FEATURE_VERSION
from .paths import get_segment_training_db_path


SEGMENT_TRAINING_DB_VERSION = "v3"
_SEGMENT_TRAINING_DB_LOCK = threading.RLock()
SEGMENT_EXTRA_FEATURE_NAMES = [
    "home_avg_q1_points",
    "away_avg_q1_points",
    "home_avg_q1_allowed",
    "away_avg_q1_allowed",
    "home_recent_q1_points",
    "away_recent_q1_points",
    "home_recent_q1_allowed",
    "away_recent_q1_allowed",
    "home_avg_first_half_points",
    "away_avg_first_half_points",
    "home_avg_first_half_allowed",
    "away_avg_first_half_allowed",
    "home_recent_first_half_points",
    "away_recent_first_half_points",
    "home_recent_first_half_allowed",
    "away_recent_first_half_allowed",
    "home_q1_points_split",
    "away_q1_points_split",
    "home_q1_allowed_split",
    "away_q1_allowed_split",
    "q1_recent_off_vs_def_delta",
    "q1_recent_away_off_vs_home_def_delta",
    "q1_avg_off_vs_def_delta",
    "q1_avg_away_off_vs_home_def_delta",
    "home_q1_points_volatility",
    "away_q1_points_volatility",
    "home_q1_allowed_volatility",
    "away_q1_allowed_volatility",
    "home_q1_fast_start_rate",
    "away_q1_fast_start_rate",
]


def ensure_segment_training_db(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, object]:
    training_db_path = get_segment_training_db_path()
    training_db_path.parent.mkdir(parents=True, exist_ok=True)
    expected_signature = _source_signature(conn)

    with _SEGMENT_TRAINING_DB_LOCK:
        with _connect_training_db(training_db_path) as training_conn:
            _init_training_db(training_conn)
            metadata = _read_metadata(training_conn)
            current_row_count = int(metadata.get("included_rows") or 0)
            if (
                not force
                and metadata.get("source_signature") == expected_signature
                and metadata.get("db_version") == SEGMENT_TRAINING_DB_VERSION
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

            _rebuild_segment_training_examples(
                source_conn=conn,
                training_conn=training_conn,
                source_signature=expected_signature,
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


def load_segment_training_rows(
    conn: sqlite3.Connection,
    *,
    target: str,
    force_rebuild: bool = False,
) -> tuple[list[tuple[list[float], float]], dict[str, object]]:
    info = ensure_segment_training_db(conn, force=force_rebuild)
    training_db_path = Path(str(info["path"]))
    target_column = {
        "home_q1_points": "target_home_q1_points",
        "away_q1_points": "target_away_q1_points",
        "q1_total": "target_q1_total",
        "q1_margin": "target_q1_margin",
        "home_1h_points": "target_home_1h_points",
        "away_1h_points": "target_away_1h_points",
        "first_half_total": "target_first_half_total",
        "first_half_margin": "target_first_half_margin",
    }.get(target)
    if target_column is None:
        raise ValueError(f"Unsupported segment training target: {target}")

    query = f"""
        SELECT features_json, {target_column} AS target_value
        FROM segment_training_examples
        WHERE is_clean = 1
          AND {target_column} IS NOT NULL
        ORDER BY game_date ASC, source_game_id ASC
    """
    with _connect_training_db(training_db_path) as training_conn:
        rows = training_conn.execute(query).fetchall()

    samples: list[tuple[list[float], float]] = []
    for row in rows:
        samples.append((list(json.loads(str(row["features_json"]))), float(row["target_value"])))
    return samples, info


def segment_training_db_signature(conn: sqlite3.Connection) -> str:
    info = ensure_segment_training_db(conn, force=False)
    return str(info["source_signature"])


def _connect_training_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -8000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _init_training_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS segment_training_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS segment_training_examples (
            id INTEGER PRIMARY KEY,
            source_game_id INTEGER NOT NULL,
            game_date TEXT NOT NULL,
            season TEXT NOT NULL,
            home_team_id INTEGER NOT NULL,
            away_team_id INTEGER NOT NULL,
            rest_days_home INTEGER,
            rest_days_away INTEGER,
            game_total REAL,
            home_moneyline REAL,
            away_moneyline REAL,
            target_home_q1_points REAL,
            target_away_q1_points REAL,
            target_q1_total REAL,
            target_q1_margin REAL,
            target_home_1h_points REAL,
            target_away_1h_points REAL,
            target_first_half_total REAL,
            target_first_half_margin REAL,
            features_json TEXT,
            quality_flags_json TEXT,
            is_clean INTEGER NOT NULL DEFAULT 1,
            exclusion_reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source_game_id)
        );

        CREATE INDEX IF NOT EXISTS idx_segment_training_clean_target
        ON segment_training_examples(is_clean, game_date, source_game_id);
        """
    )
    conn.commit()


def _read_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM segment_training_metadata").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _write_metadata(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    conn.executemany(
        """
        INSERT INTO segment_training_metadata (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        [(key, json.dumps(value) if isinstance(value, (dict, list)) else str(value)) for key, value in values.items()],
    )


def _rebuild_segment_training_examples(
    *,
    source_conn: sqlite3.Connection,
    training_conn: sqlite3.Connection,
    source_signature: str,
) -> None:
    from . import game_predictions as gp
    from .player_prop_model import _before_training_start, _training_start_date

    built_at = datetime.now(timezone.utc).isoformat()
    candidate_rows = 0
    included_rows = 0
    excluded_rows = 0
    exclusion_counts: dict[str, int] = {}
    training_start = _training_start_date()

    training_conn.execute("DELETE FROM segment_training_examples")
    rows = source_conn.execute(
        """
        SELECT
            g.id,
            g.game_date,
            g.start_time,
            g.home_team_id,
            g.away_team_id,
            g.rest_days_home,
            g.rest_days_away,
            g.spread_home,
            g.game_total,
            g.home_moneyline,
            g.away_moneyline,
            g.home_spread_price,
            g.away_spread_price,
            g.over_price,
            g.under_price,
            home_result.possessions AS home_possessions,
            away_result.possessions AS away_possessions,
            segments.home_q1_points,
            segments.away_q1_points,
            segments.home_1h_points,
            segments.away_1h_points
        FROM games g
        JOIN team_game_results home_result ON home_result.game_id = g.id AND home_result.team_id = g.home_team_id
        JOIN team_game_results away_result ON away_result.game_id = g.id AND away_result.team_id = g.away_team_id
        JOIN game_segment_results segments ON segments.game_id = g.id
        WHERE g.status = 'final'
          AND segments.home_q1_points IS NOT NULL
          AND segments.away_q1_points IS NOT NULL
          AND segments.home_1h_points IS NOT NULL
          AND segments.away_1h_points IS NOT NULL
        ORDER BY g.game_date ASC, g.start_time ASC, g.id ASC
        """
    ).fetchall()

    history: dict[int, dict[str, object]] = {}
    segment_history: dict[int, dict[str, object]] = {}
    runtime_cache: dict[str, dict[tuple, object]] = {}
    rows_to_insert: list[tuple[object, ...]] = []
    for row in rows:
        candidate_rows += 1
        game_date = str(row["game_date"] or "")
        season = game_date[:4]
        home_team_id = int(row["home_team_id"])
        away_team_id = int(row["away_team_id"])
        home_context = gp._team_history_context(history.get(home_team_id))
        away_context = gp._team_history_context(history.get(away_team_id))
        home_segment_context = _segment_history_context(segment_history.get(home_team_id), current_is_home=True)
        away_segment_context = _segment_history_context(segment_history.get(away_team_id), current_is_home=False)
        exclusion_reason: str | None = None

        if _before_training_start(game_date, training_start):
            exclusion_reason = "before_training_start"
        elif home_context["games"] < 3 or away_context["games"] < 3:
            exclusion_reason = "missing_history_window"

        pace_factor = gp._clamp(
            ((float(home_context["avg_possessions"]) + float(away_context["avg_possessions"])) / 2.0)
            / gp._historical_league_possessions(history),
            0.94,
            1.06,
        )
        matchup_pregame = build_matchup_pregame_features(
            source_conn,
            home_team_id=home_team_id,
            away_team_id=away_team_id,
            game_id=int(row["id"]),
            game_date=game_date,
            use_injury_context=False,
            runtime_cache=runtime_cache,
        )
        base_features = gp._assemble_direct_game_features(
            home_recent_points=float(home_context["recent_points"]),
            away_recent_points=float(away_context["recent_points"]),
            home_recent_allowed=float(home_context["recent_allowed"]),
            away_recent_allowed=float(away_context["recent_allowed"]),
            home_average_points=float(home_context["avg_points"]),
            away_average_points=float(away_context["avg_points"]),
            home_average_allowed=float(home_context["avg_allowed"]),
            away_average_allowed=float(away_context["avg_allowed"]),
            home_average_possessions=float(home_context["avg_possessions"]),
            away_average_possessions=float(away_context["avg_possessions"]),
            pace_factor=pace_factor,
            rest_days_home=int(row["rest_days_home"] or 2),
            rest_days_away=int(row["rest_days_away"] or 2),
            home_game_count=int(home_context["games"]),
            away_game_count=int(away_context["games"]),
            home_recent_possessions=float(home_context["recent_possessions"]),
            away_recent_possessions=float(away_context["recent_possessions"]),
            home_season_off_rating=gp._team_rating(float(home_context["avg_points"]), float(home_context["avg_possessions"])),
            away_season_off_rating=gp._team_rating(float(away_context["avg_points"]), float(away_context["avg_possessions"])),
            home_season_def_rating=gp._team_rating(float(home_context["avg_allowed"]), float(home_context["avg_possessions"])),
            away_season_def_rating=gp._team_rating(float(away_context["avg_allowed"]), float(away_context["avg_possessions"])),
            home_recent_off_rating=gp._team_rating(float(home_context["recent_points"]), float(home_context["recent_possessions"])),
            away_recent_off_rating=gp._team_rating(float(away_context["recent_points"]), float(away_context["recent_possessions"])),
            home_recent_def_rating=gp._team_rating(float(home_context["recent_allowed"]), float(home_context["recent_possessions"])),
            away_recent_def_rating=gp._team_rating(float(away_context["recent_allowed"]), float(away_context["recent_possessions"])),
            spread_home=gp._coerce_float(row["spread_home"]),
            game_total=gp._coerce_float(row["game_total"]),
            home_moneyline=gp._coerce_float(row["home_moneyline"]),
            away_moneyline=gp._coerce_float(row["away_moneyline"]),
            home_spread_price=gp._coerce_float(row["home_spread_price"]),
            away_spread_price=gp._coerce_float(row["away_spread_price"]),
            over_price=gp._coerce_float(row["over_price"]),
            under_price=gp._coerce_float(row["under_price"]),
            matchup_pregame=matchup_pregame,
        )
        features = [
            *base_features,
            float(home_segment_context["avg_q1_points"]),
            float(away_segment_context["avg_q1_points"]),
            float(home_segment_context["avg_q1_allowed"]),
            float(away_segment_context["avg_q1_allowed"]),
            float(home_segment_context["recent_q1_points"]),
            float(away_segment_context["recent_q1_points"]),
            float(home_segment_context["recent_q1_allowed"]),
            float(away_segment_context["recent_q1_allowed"]),
            float(home_segment_context["avg_first_half_points"]),
            float(away_segment_context["avg_first_half_points"]),
            float(home_segment_context["avg_first_half_allowed"]),
            float(away_segment_context["avg_first_half_allowed"]),
            float(home_segment_context["recent_first_half_points"]),
            float(away_segment_context["recent_first_half_points"]),
            float(home_segment_context["recent_first_half_allowed"]),
            float(away_segment_context["recent_first_half_allowed"]),
            float(home_segment_context["split_q1_points"]),
            float(away_segment_context["split_q1_points"]),
            float(home_segment_context["split_q1_allowed"]),
            float(away_segment_context["split_q1_allowed"]),
            float(home_segment_context["recent_q1_points"]) - float(away_segment_context["recent_q1_allowed"]),
            float(away_segment_context["recent_q1_points"]) - float(home_segment_context["recent_q1_allowed"]),
            float(home_segment_context["avg_q1_points"]) - float(away_segment_context["avg_q1_allowed"]),
            float(away_segment_context["avg_q1_points"]) - float(home_segment_context["avg_q1_allowed"]),
            float(home_segment_context["q1_points_volatility"]),
            float(away_segment_context["q1_points_volatility"]),
            float(home_segment_context["q1_allowed_volatility"]),
            float(away_segment_context["q1_allowed_volatility"]),
            float(home_segment_context["q1_fast_start_rate"]),
            float(away_segment_context["q1_fast_start_rate"]),
        ]

        target_home_q1_points = float(row["home_q1_points"])
        target_away_q1_points = float(row["away_q1_points"])
        target_home_1h_points = float(row["home_1h_points"])
        target_away_1h_points = float(row["away_1h_points"])
        target_q1_total = target_home_q1_points + target_away_q1_points
        target_q1_margin = target_home_q1_points - target_away_q1_points
        target_first_half_total = target_home_1h_points + target_away_1h_points
        target_first_half_margin = target_home_1h_points - target_away_1h_points

        quality_flags = {
            "home_history_games": int(home_context["games"]),
            "away_history_games": int(away_context["games"]),
            "home_segment_games": int(home_segment_context["games"]),
            "away_segment_games": int(away_segment_context["games"]),
            "has_game_total": gp._coerce_float(row["game_total"]) is not None and float(row["game_total"] or 0) > 0,
            "has_moneyline": row["home_moneyline"] is not None and row["away_moneyline"] is not None,
        }
        is_clean = 1 if exclusion_reason is None else 0
        if is_clean:
            included_rows += 1
        else:
            excluded_rows += 1
            exclusion_counts[exclusion_reason or "unknown"] = exclusion_counts.get(exclusion_reason or "unknown", 0) + 1

        rows_to_insert.append(
            (
                int(row["id"]),
                game_date,
                season,
                home_team_id,
                away_team_id,
                int(row["rest_days_home"] or 2),
                int(row["rest_days_away"] or 2),
                gp._coerce_float(row["game_total"]),
                gp._coerce_float(row["home_moneyline"]),
                gp._coerce_float(row["away_moneyline"]),
                target_home_q1_points,
                target_away_q1_points,
                target_q1_total,
                target_q1_margin,
                target_home_1h_points,
                target_away_1h_points,
                target_first_half_total,
                target_first_half_margin,
                json.dumps(features),
                json.dumps(quality_flags),
                is_clean,
                exclusion_reason,
                built_at,
            )
        )

        gp._append_team_history(
            history,
            team_id=home_team_id,
            game_date=game_date,
            points=float(target_home_1h_points),
            opponent_points=float(target_away_1h_points),
            possessions=float(row["home_possessions"] or row["away_possessions"] or 78.0) / 2.0,
        )
        gp._append_team_history(
            history,
            team_id=away_team_id,
            game_date=game_date,
            points=float(target_away_1h_points),
            opponent_points=float(target_home_1h_points),
            possessions=float(row["away_possessions"] or row["home_possessions"] or 78.0) / 2.0,
        )
        _append_segment_history(
            segment_history,
            team_id=home_team_id,
            game_date=game_date,
            is_home=True,
            q1_points=float(target_home_q1_points),
            q1_allowed=float(target_away_q1_points),
            first_half_points=float(target_home_1h_points),
            first_half_allowed=float(target_away_1h_points),
        )
        _append_segment_history(
            segment_history,
            team_id=away_team_id,
            game_date=game_date,
            is_home=False,
            q1_points=float(target_away_q1_points),
            q1_allowed=float(target_home_q1_points),
            first_half_points=float(target_away_1h_points),
            first_half_allowed=float(target_home_1h_points),
        )

    training_conn.executemany(
        """
        INSERT INTO segment_training_examples (
            source_game_id, game_date, season, home_team_id, away_team_id,
            rest_days_home, rest_days_away, game_total, home_moneyline, away_moneyline,
            target_home_q1_points, target_away_q1_points, target_q1_total, target_q1_margin,
            target_home_1h_points, target_away_1h_points, target_first_half_total, target_first_half_margin,
            features_json, quality_flags_json, is_clean, exclusion_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
    _write_metadata(
        training_conn,
        {
            "built_at": built_at,
            "db_version": SEGMENT_TRAINING_DB_VERSION,
            "candidate_rows": candidate_rows,
            "included_rows": included_rows,
            "excluded_rows": excluded_rows,
            "exclusion_counts": exclusion_counts,
            "source_signature": source_signature,
        },
    )
    training_conn.commit()


def _source_signature(conn: sqlite3.Connection) -> str:
    import hashlib

    row = conn.execute(
        """
        SELECT
            COUNT(*) AS count,
            COALESCE(MAX(g.id), 0) AS max_id,
            COALESCE(MAX(g.game_date), '') AS max_game_date
        FROM games g
        JOIN game_segment_results segments ON segments.game_id = g.id
        WHERE g.status = 'final'
        """
    ).fetchone()
    payload = {
        "db_version": SEGMENT_TRAINING_DB_VERSION,
        "pregame_feature_version": GAME_PREGAME_FEATURE_VERSION,
        "segment_final_count": int(row["count"] or 0),
        "segment_final_max_id": int(row["max_id"] or 0),
        "segment_final_max_date": str(row["max_game_date"] or ""),
    }
    segment_row = conn.execute(
        """
        SELECT
            COUNT(*) AS row_count,
            COALESCE(SUM(home_q1_points + away_q1_points + home_1h_points + away_1h_points), 0) AS scoring_sum
        FROM game_segment_results
        """
    ).fetchone()
    result_row = conn.execute(
        """
        SELECT
            COUNT(*) AS row_count,
            ROUND(COALESCE(SUM(possessions), 0), 3) AS possessions_sum
        FROM team_game_results
        """
    ).fetchone()
    payload["segment_results"] = {
        "row_count": int(segment_row["row_count"] or 0),
        "scoring_sum": float(segment_row["scoring_sum"] or 0.0),
    }
    payload["team_results"] = {
        "row_count": int(result_row["row_count"] or 0),
        "possessions_sum": float(result_row["possessions_sum"] or 0.0),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _segment_history_context(state: dict[str, object] | None, *, current_is_home: bool) -> dict[str, float]:
    if not state:
        return {
            "games": 0.0,
            "avg_q1_points": 20.0,
            "avg_q1_allowed": 20.0,
            "recent_q1_points": 20.0,
            "recent_q1_allowed": 20.0,
            "avg_first_half_points": 40.0,
            "avg_first_half_allowed": 40.0,
            "recent_first_half_points": 40.0,
            "recent_first_half_allowed": 40.0,
            "split_q1_points": 20.0,
            "split_q1_allowed": 20.0,
            "q1_points_volatility": 4.0,
            "q1_allowed_volatility": 4.0,
            "q1_fast_start_rate": 0.5,
        }
    games = max(float(state["games"]), 1.0)
    recent_q1_points = state["recent_q1_points"]
    recent_q1_allowed = state["recent_q1_allowed"]
    recent_first_half_points = state["recent_first_half_points"]
    recent_first_half_allowed = state["recent_first_half_allowed"]
    split_prefix = "home" if current_is_home else "away"
    split_points = state[f"recent_{split_prefix}_q1_points"]
    split_allowed = state[f"recent_{split_prefix}_q1_allowed"]
    split_games = max(float(state[f"{split_prefix}_games"]), 1.0)
    return {
        "games": float(state["games"]),
        "avg_q1_points": float(state["q1_points_sum"]) / games,
        "avg_q1_allowed": float(state["q1_allowed_sum"]) / games,
        "recent_q1_points": (sum(recent_q1_points) / len(recent_q1_points)) if recent_q1_points else float(state["q1_points_sum"]) / games,
        "recent_q1_allowed": (sum(recent_q1_allowed) / len(recent_q1_allowed)) if recent_q1_allowed else float(state["q1_allowed_sum"]) / games,
        "avg_first_half_points": float(state["first_half_points_sum"]) / games,
        "avg_first_half_allowed": float(state["first_half_allowed_sum"]) / games,
        "recent_first_half_points": (
            sum(recent_first_half_points) / len(recent_first_half_points)
            if recent_first_half_points
            else float(state["first_half_points_sum"]) / games
        ),
        "recent_first_half_allowed": (
            sum(recent_first_half_allowed) / len(recent_first_half_allowed)
            if recent_first_half_allowed
            else float(state["first_half_allowed_sum"]) / games
        ),
        "split_q1_points": (
            (sum(split_points) / len(split_points))
            if split_points
            else float(state[f"{split_prefix}_q1_points_sum"]) / split_games
        ),
        "split_q1_allowed": (
            (sum(split_allowed) / len(split_allowed))
            if split_allowed
            else float(state[f"{split_prefix}_q1_allowed_sum"]) / split_games
        ),
        "q1_points_volatility": _series_stddev(recent_q1_points),
        "q1_allowed_volatility": _series_stddev(recent_q1_allowed),
        "q1_fast_start_rate": _fast_start_rate(recent_q1_points, float(state["q1_points_sum"]) / games),
    }


def _append_segment_history(
    history: dict[int, dict[str, object]],
    *,
    team_id: int,
    game_date: str,
    is_home: bool,
    q1_points: float,
    q1_allowed: float,
    first_half_points: float,
    first_half_allowed: float,
) -> None:
    state = history.setdefault(
        team_id,
        {
            "games": 0,
            "q1_points_sum": 0.0,
            "q1_allowed_sum": 0.0,
            "first_half_points_sum": 0.0,
            "first_half_allowed_sum": 0.0,
            "recent_q1_points": [],
            "recent_q1_allowed": [],
            "recent_first_half_points": [],
            "recent_first_half_allowed": [],
            "home_games": 0,
            "home_q1_points_sum": 0.0,
            "home_q1_allowed_sum": 0.0,
            "recent_home_q1_points": [],
            "recent_home_q1_allowed": [],
            "away_games": 0,
            "away_q1_points_sum": 0.0,
            "away_q1_allowed_sum": 0.0,
            "recent_away_q1_points": [],
            "recent_away_q1_allowed": [],
            "last_game_date": None,
        },
    )
    state["games"] += 1
    state["q1_points_sum"] += q1_points
    state["q1_allowed_sum"] += q1_allowed
    state["first_half_points_sum"] += first_half_points
    state["first_half_allowed_sum"] += first_half_allowed
    state["recent_q1_points"].append(q1_points)
    state["recent_q1_allowed"].append(q1_allowed)
    state["recent_first_half_points"].append(first_half_points)
    state["recent_first_half_allowed"].append(first_half_allowed)
    split_prefix = "home" if is_home else "away"
    state[f"{split_prefix}_games"] += 1
    state[f"{split_prefix}_q1_points_sum"] += q1_points
    state[f"{split_prefix}_q1_allowed_sum"] += q1_allowed
    state[f"recent_{split_prefix}_q1_points"].append(q1_points)
    state[f"recent_{split_prefix}_q1_allowed"].append(q1_allowed)
    state["recent_q1_points"] = state["recent_q1_points"][-5:]
    state["recent_q1_allowed"] = state["recent_q1_allowed"][-5:]
    state["recent_first_half_points"] = state["recent_first_half_points"][-5:]
    state["recent_first_half_allowed"] = state["recent_first_half_allowed"][-5:]
    state["recent_home_q1_points"] = state["recent_home_q1_points"][-5:]
    state["recent_home_q1_allowed"] = state["recent_home_q1_allowed"][-5:]
    state["recent_away_q1_points"] = state["recent_away_q1_points"][-5:]
    state["recent_away_q1_allowed"] = state["recent_away_q1_allowed"][-5:]
    state["last_game_date"] = game_date


def _series_stddev(values: list[float]) -> float:
    if not values:
        return 4.0
    mean = sum(values) / len(values)
    variance = sum((value - mean) ** 2 for value in values) / len(values)
    return max(variance ** 0.5, 0.5)


def _fast_start_rate(values: list[float], baseline: float) -> float:
    if not values:
        return 0.5
    threshold = float(baseline)
    return sum(1 for value in values if value >= threshold) / len(values)
