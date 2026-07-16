from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .game_pregame_features import GAME_PREGAME_FEATURE_VERSION
from .paths import get_training_db_path


GAME_TRAINING_DB_VERSION = "v3"
_GAME_TRAINING_DB_LOCK = threading.RLock()


def ensure_game_training_db(conn: sqlite3.Connection, *, force: bool = False) -> dict[str, object]:
    training_db_path = get_training_db_path()
    training_db_path.parent.mkdir(parents=True, exist_ok=True)
    expected_signature = _source_signature(conn)

    with _GAME_TRAINING_DB_LOCK:
        with _connect_training_db(training_db_path) as training_conn:
            _init_training_db(training_conn)
            metadata = _read_metadata(training_conn)
            current_row_count = int(metadata.get("included_rows") or 0)
            if (
                not force
                and metadata.get("source_signature") == expected_signature
                and metadata.get("db_version") == GAME_TRAINING_DB_VERSION
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

            _rebuild_game_training_examples(
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


def load_game_training_rows(
    conn: sqlite3.Connection,
    *,
    target: str,
    force_rebuild: bool = False,
) -> tuple[list[tuple[list[float], float]], dict[str, object]]:
    info = ensure_game_training_db(conn, force=force_rebuild)
    training_db_path = Path(str(info["path"]))
    query = """
        SELECT features_json,
               target_margin,
               target_total,
               target_home_win,
               target_spread_edge,
               target_total_edge,
               baseline_margin,
               baseline_total,
               spread_home,
               game_total,
               home_moneyline,
               away_moneyline
        FROM game_training_examples
        WHERE is_clean = 1
    """
    params: list[object] = []
    if target == "margin":
        query += " AND has_margin_target = 1"
    elif target == "total":
        query += " AND has_total_target = 1"
    elif target == "total_market":
        query += " AND has_total_market_target = 1"
    elif target == "ats_market":
        query += " AND has_spread_target = 1"
    elif target == "moneyline":
        query += " AND has_moneyline_target = 1"
    else:
        raise ValueError(f"Unsupported game training target: {target}")
    query += " ORDER BY game_date ASC, source_game_id ASC"

    with _connect_training_db(training_db_path) as training_conn:
        rows = training_conn.execute(query, params).fetchall()

    samples: list[tuple[list[float], float]] = []
    for row in rows:
        features = list(json.loads(str(row["features_json"])))
        if target == "margin":
            samples.append((features, float(row["target_margin"])))
        elif target == "total":
            samples.append((features, float(row["target_total"])))
        elif target == "total_market":
            samples.append((
                [*features, float(row["baseline_total"]) - float(row["game_total"]), float(row["game_total"])],
                float(row["target_total_edge"]),
            ))
        elif target == "ats_market":
            samples.append((
                [*features, float(row["baseline_margin"]) + float(row["spread_home"]), float(row["spread_home"])],
                float(row["target_spread_edge"]),
            ))
        elif target == "moneyline":
            samples.append((
                [*features, float(row["home_moneyline"]), float(row["away_moneyline"])],
                float(row["target_home_win"]),
            ))
    return samples, info


def game_training_db_signature(conn: sqlite3.Connection) -> str:
    info = ensure_game_training_db(conn, force=False)
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
        CREATE TABLE IF NOT EXISTS game_training_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS game_training_examples (
            id INTEGER PRIMARY KEY,
            source_game_id INTEGER NOT NULL,
            game_date TEXT NOT NULL,
            season TEXT NOT NULL,
            home_team_id INTEGER NOT NULL,
            away_team_id INTEGER NOT NULL,
            rest_days_home INTEGER,
            rest_days_away INTEGER,
            spread_home REAL,
            game_total REAL,
            home_moneyline REAL,
            away_moneyline REAL,
            home_points REAL,
            away_points REAL,
            baseline_home_points REAL,
            baseline_away_points REAL,
            baseline_margin REAL,
            baseline_total REAL,
            target_margin REAL,
            target_total REAL,
            target_home_win REAL,
            target_spread_edge REAL,
            target_total_edge REAL,
            home_implied_prob REAL,
            away_implied_prob REAL,
            vig_free_home_prob REAL,
            has_margin_target INTEGER NOT NULL DEFAULT 0,
            has_total_target INTEGER NOT NULL DEFAULT 0,
            has_spread_target INTEGER NOT NULL DEFAULT 0,
            has_total_market_target INTEGER NOT NULL DEFAULT 0,
            has_moneyline_target INTEGER NOT NULL DEFAULT 0,
            features_json TEXT,
            quality_flags_json TEXT,
            is_clean INTEGER NOT NULL DEFAULT 1,
            exclusion_reason TEXT,
            created_at TEXT NOT NULL,
            UNIQUE(source_game_id)
        );

        CREATE INDEX IF NOT EXISTS idx_game_training_clean_target
        ON game_training_examples(is_clean, game_date, source_game_id);
        """
    )
    conn.commit()


def _read_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM game_training_metadata").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _write_metadata(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    conn.executemany(
        """
        INSERT INTO game_training_metadata (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        [(key, json.dumps(value) if isinstance(value, (dict, list)) else str(value)) for key, value in values.items()],
    )


def _rebuild_game_training_examples(
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

    training_conn.execute("DELETE FROM game_training_examples")
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
            home_result.points AS home_points,
            away_result.points AS away_points,
            home_result.possessions AS home_possessions,
            away_result.possessions AS away_possessions
        FROM games g
        JOIN team_game_results home_result ON home_result.game_id = g.id AND home_result.team_id = g.home_team_id
        JOIN team_game_results away_result ON away_result.game_id = g.id AND away_result.team_id = g.away_team_id
        WHERE g.status = 'final'
          AND home_result.points IS NOT NULL
          AND away_result.points IS NOT NULL
        ORDER BY g.game_date ASC, g.start_time ASC, g.id ASC
        """
    ).fetchall()

    history: dict[int, dict[str, object]] = {}
    rows_to_insert: list[tuple[object, ...]] = []
    for row in rows:
        candidate_rows += 1
        game_date = str(row["game_date"] or "")
        season = game_date[:4]
        home_team_id = int(row["home_team_id"])
        away_team_id = int(row["away_team_id"])
        home_context = gp._team_history_context(history.get(home_team_id))
        away_context = gp._team_history_context(history.get(away_team_id))
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
        features = gp._assemble_direct_game_features(
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
        )
        baseline_home = gp._baseline_points_from_context(
            team_context=home_context,
            opponent_context=away_context,
            is_home=True,
            rest_days=int(row["rest_days_home"] or 2),
        )
        baseline_away = gp._baseline_points_from_context(
            team_context=away_context,
            opponent_context=home_context,
            is_home=False,
            rest_days=int(row["rest_days_away"] or 2),
        )
        baseline_margin = baseline_home - baseline_away
        baseline_total = baseline_home + baseline_away
        home_points = float(row["home_points"])
        away_points = float(row["away_points"])
        spread_home = gp._coerce_float(row["spread_home"])
        game_total = gp._coerce_float(row["game_total"])
        home_moneyline = gp._coerce_float(row["home_moneyline"])
        away_moneyline = gp._coerce_float(row["away_moneyline"])
        implied_home = _moneyline_implied_probability(home_moneyline)
        implied_away = _moneyline_implied_probability(away_moneyline)
        vig_free_home = _vig_free_home_probability(home_moneyline, away_moneyline)
        quality_flags = {
            "home_history_games": int(home_context["games"]),
            "away_history_games": int(away_context["games"]),
            "has_spread": spread_home is not None,
            "has_total": game_total is not None and game_total > 0,
            "has_moneyline": home_moneyline is not None and away_moneyline is not None,
        }
        has_margin_target = int(exclusion_reason is None)
        has_total_target = int(exclusion_reason is None)
        has_spread_target = int(exclusion_reason is None and spread_home is not None)
        has_total_market_target = int(exclusion_reason is None and game_total is not None and game_total > 0)
        has_moneyline_target = int(exclusion_reason is None and home_moneyline is not None and away_moneyline is not None)
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
                spread_home,
                game_total,
                home_moneyline,
                away_moneyline,
                home_points,
                away_points,
                baseline_home,
                baseline_away,
                baseline_margin,
                baseline_total,
                home_points - away_points,
                home_points + away_points,
                1.0 if home_points > away_points else 0.0,
                (home_points - away_points) + spread_home if spread_home is not None else None,
                (home_points + away_points) - game_total if game_total is not None and game_total > 0 else None,
                implied_home,
                implied_away,
                vig_free_home,
                has_margin_target,
                has_total_target,
                has_spread_target,
                has_total_market_target,
                has_moneyline_target,
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
            points=home_points,
            opponent_points=away_points,
            possessions=float(row["home_possessions"] or row["away_possessions"] or 78.0),
        )
        gp._append_team_history(
            history,
            team_id=away_team_id,
            game_date=game_date,
            points=away_points,
            opponent_points=home_points,
            possessions=float(row["away_possessions"] or row["home_possessions"] or 78.0),
        )

    training_conn.executemany(
        """
        INSERT INTO game_training_examples (
            source_game_id, game_date, season, home_team_id, away_team_id,
            rest_days_home, rest_days_away, spread_home, game_total,
            home_moneyline, away_moneyline, home_points, away_points,
            baseline_home_points, baseline_away_points, baseline_margin, baseline_total,
            target_margin, target_total, target_home_win, target_spread_edge, target_total_edge,
            home_implied_prob, away_implied_prob, vig_free_home_prob,
            has_margin_target, has_total_target, has_spread_target, has_total_market_target, has_moneyline_target,
            features_json, quality_flags_json, is_clean, exclusion_reason, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
    _write_metadata(
        training_conn,
        {
            "built_at": built_at,
            "db_version": GAME_TRAINING_DB_VERSION,
            "candidate_rows": candidate_rows,
            "included_rows": included_rows,
            "excluded_rows": excluded_rows,
            "exclusion_counts": exclusion_counts,
            "source_signature": source_signature,
        },
    )
    training_conn.commit()


def _source_signature(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS count,
            COALESCE(MAX(id), 0) AS max_id,
            COALESCE(MAX(game_date), '') AS max_game_date
        FROM games
        WHERE status = 'final'
        """
    ).fetchone()
    payload = {
        "db_version": GAME_TRAINING_DB_VERSION,
        "pregame_feature_version": GAME_PREGAME_FEATURE_VERSION,
        "games_final_count": int(row["count"] or 0),
        "games_final_max_id": int(row["max_id"] or 0),
        "games_final_max_date": str(row["max_game_date"] or ""),
    }
    result_row = conn.execute(
        """
        SELECT
            COUNT(*) AS row_count,
            ROUND(COALESCE(SUM(points + opponent_points), 0), 3) AS scoring_sum,
            ROUND(COALESCE(SUM(possessions), 0), 3) AS possessions_sum,
            SUM(CASE WHEN COALESCE(possessions_source, 'fallback') != 'fallback' THEN 1 ELSE 0 END) AS sourced_rows
        FROM team_game_results
        """
    ).fetchone()
    boxscore_row = conn.execute(
        """
        SELECT
            COUNT(*) AS row_count,
            ROUND(COALESCE(SUM(field_goals_attempted + free_throws_attempted + offensive_rebounds), 0), 3) AS volume_sum,
            ROUND(COALESCE(SUM(possessions), 0), 3) AS possessions_sum
        FROM team_game_boxscores
        """
    ).fetchone()
    market_row = conn.execute(
        """
        SELECT
            SUM(CASE WHEN spread_home IS NOT NULL THEN 1 ELSE 0 END) AS spread_rows,
            SUM(CASE WHEN game_total IS NOT NULL AND game_total > 0 THEN 1 ELSE 0 END) AS total_rows,
            SUM(CASE WHEN home_moneyline IS NOT NULL AND away_moneyline IS NOT NULL THEN 1 ELSE 0 END) AS moneyline_rows
        FROM games
        WHERE status = 'final'
        """
    ).fetchone()
    payload["market_rows"] = {
        "spread": int(market_row["spread_rows"] or 0),
        "total": int(market_row["total_rows"] or 0),
        "moneyline": int(market_row["moneyline_rows"] or 0),
    }
    payload["team_results"] = {
        "row_count": int(result_row["row_count"] or 0),
        "scoring_sum": float(result_row["scoring_sum"] or 0.0),
        "possessions_sum": float(result_row["possessions_sum"] or 0.0),
        "sourced_rows": int(result_row["sourced_rows"] or 0),
    }
    payload["team_boxscores"] = {
        "row_count": int(boxscore_row["row_count"] or 0),
        "volume_sum": float(boxscore_row["volume_sum"] or 0.0),
        "possessions_sum": float(boxscore_row["possessions_sum"] or 0.0),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    import hashlib
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _moneyline_implied_probability(value: float | None) -> float | None:
    if value is None:
        return None
    if value > 0:
        return 100.0 / (value + 100.0)
    if value < 0:
        return (-value) / ((-value) + 100.0)
    return None


def _vig_free_home_probability(home_moneyline: float | None, away_moneyline: float | None) -> float | None:
    home_prob = _moneyline_implied_probability(home_moneyline)
    away_prob = _moneyline_implied_probability(away_moneyline)
    if home_prob is None or away_prob is None:
        return None
    denom = home_prob + away_prob
    if denom <= 0:
        return None
    return home_prob / denom
