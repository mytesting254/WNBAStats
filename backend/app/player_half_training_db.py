from __future__ import annotations

import json
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .paths import get_player_half_training_db_path


PLAYER_HALF_TRAINING_DB_VERSION = "v1"
_PLAYER_HALF_TRAINING_DB_LOCK = threading.RLock()
DEFAULT_TEAM_FIRST_HALF_SHARE = float(os.getenv("WNBA_DEFAULT_TEAM_FIRST_HALF_SHARE", "0.5"))


def ensure_player_half_training_db(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    allow_rebuild: bool = True,
) -> dict[str, object]:
    training_db_path = get_player_half_training_db_path()
    training_db_path.parent.mkdir(parents=True, exist_ok=True)
    expected_signature = _source_signature(conn)

    if not training_db_path.exists():
        if not allow_rebuild:
            return {
                "path": str(training_db_path),
                "rebuilt": False,
                "player_rows": 0,
                "prop_rows": 0,
                "candidate_player_rows": 0,
                "candidate_prop_rows": 0,
                "built_at": None,
                "source_signature": expected_signature,
                "stale": True,
                "missing": True,
            }

    with _PLAYER_HALF_TRAINING_DB_LOCK:
        with _connect_training_db(training_db_path) as training_conn:
            _init_training_db(training_conn)
            metadata = _read_metadata(training_conn)
            current_prop_rows = int(metadata.get("prop_rows") or 0)
            current_player_rows = int(metadata.get("player_rows") or 0)
            if (
                not force
                and metadata.get("source_signature") == expected_signature
                and metadata.get("db_version") == PLAYER_HALF_TRAINING_DB_VERSION
                and current_player_rows > 0
            ):
                return {
                    "path": str(training_db_path),
                    "rebuilt": False,
                    "player_rows": current_player_rows,
                    "prop_rows": current_prop_rows,
                    "candidate_player_rows": int(metadata.get("candidate_player_rows") or 0),
                    "candidate_prop_rows": int(metadata.get("candidate_prop_rows") or 0),
                    "built_at": metadata.get("built_at"),
                    "source_signature": expected_signature,
                }

            if not allow_rebuild:
                return {
                    "path": str(training_db_path),
                    "rebuilt": False,
                    "player_rows": current_player_rows,
                    "prop_rows": current_prop_rows,
                    "candidate_player_rows": int(metadata.get("candidate_player_rows") or 0),
                    "candidate_prop_rows": int(metadata.get("candidate_prop_rows") or 0),
                    "built_at": metadata.get("built_at"),
                    "source_signature": str(metadata.get("source_signature") or ""),
                    "stale": True,
                    "missing": False,
                }

            _rebuild_player_half_training_examples(
                source_conn=conn,
                training_conn=training_conn,
                source_signature=expected_signature,
            )
            metadata = _read_metadata(training_conn)
            return {
                "path": str(training_db_path),
                "rebuilt": True,
                "player_rows": int(metadata.get("player_rows") or 0),
                "prop_rows": int(metadata.get("prop_rows") or 0),
                "candidate_player_rows": int(metadata.get("candidate_player_rows") or 0),
                "candidate_prop_rows": int(metadata.get("candidate_prop_rows") or 0),
                "built_at": metadata.get("built_at"),
                "source_signature": expected_signature,
            }


def load_player_half_prop_examples(
    conn: sqlite3.Connection,
    *,
    market: str | None = None,
    force_rebuild: bool = False,
    allow_rebuild: bool = True,
) -> tuple[list[sqlite3.Row], dict[str, object]]:
    info = ensure_player_half_training_db(conn, force=force_rebuild, allow_rebuild=allow_rebuild)
    training_db_path = Path(str(info["path"]))
    if not training_db_path.exists():
        return [], info
    with _connect_training_db(training_db_path) as training_conn:
        try:
            if market:
                rows = training_conn.execute(
                    """
                    SELECT *
                    FROM player_half_prop_examples
                    WHERE market = ?
                    ORDER BY game_date ASC, source_prop_line_id ASC
                    """,
                    (str(market),),
                ).fetchall()
            else:
                rows = training_conn.execute(
                    """
                    SELECT *
                    FROM player_half_prop_examples
                    ORDER BY game_date ASC, source_prop_line_id ASC
                    """
                ).fetchall()
        except sqlite3.OperationalError:
            return [], info
    return rows, info


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
        CREATE TABLE IF NOT EXISTS player_half_training_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS player_half_player_examples (
            id INTEGER PRIMARY KEY,
            source_game_id INTEGER NOT NULL,
            source_player_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            opponent_team_id INTEGER NOT NULL,
            game_date TEXT NOT NULL,
            season TEXT NOT NULL,
            is_home INTEGER NOT NULL,
            rotation_role TEXT,
            full_minutes REAL NOT NULL,
            full_points REAL NOT NULL,
            full_rebounds REAL NOT NULL,
            full_assists REAL NOT NULL,
            full_threes REAL NOT NULL,
            full_steals REAL NOT NULL,
            full_blocks REAL NOT NULL,
            full_turnovers REAL NOT NULL,
            team_points REAL NOT NULL,
            opponent_points REAL NOT NULL,
            team_first_half_points REAL NOT NULL,
            opponent_first_half_points REAL NOT NULL,
            team_first_half_share REAL NOT NULL,
            estimated_first_half_minutes REAL NOT NULL,
            estimated_first_half_minutes_share REAL NOT NULL,
            estimated_first_half_points REAL NOT NULL,
            estimated_first_half_rebounds REAL NOT NULL,
            estimated_first_half_assists REAL NOT NULL,
            estimated_first_half_threes REAL NOT NULL,
            estimated_first_half_steals REAL NOT NULL,
            estimated_first_half_blocks REAL NOT NULL,
            estimated_first_half_turnovers REAL NOT NULL,
            estimated_first_half_points_rebounds REAL NOT NULL,
            estimated_first_half_points_assists REAL NOT NULL,
            estimated_first_half_rebounds_assists REAL NOT NULL,
            estimated_first_half_points_rebounds_assists REAL NOT NULL,
            estimated_first_half_blocks_steals REAL NOT NULL,
            share_details_json TEXT NOT NULL,
            created_at TEXT NOT NULL,
            UNIQUE(source_game_id, source_player_id)
        );

        CREATE TABLE IF NOT EXISTS player_half_prop_examples (
            id INTEGER PRIMARY KEY,
            source_prop_line_id INTEGER NOT NULL UNIQUE,
            source_game_id INTEGER NOT NULL,
            source_player_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            opponent_team_id INTEGER NOT NULL,
            game_date TEXT NOT NULL,
            season TEXT NOT NULL,
            market TEXT NOT NULL,
            line_value REAL NOT NULL,
            over_odds INTEGER NOT NULL,
            under_odds INTEGER NOT NULL,
            final_actual_result REAL NOT NULL,
            estimated_first_half_result REAL NOT NULL,
            estimated_first_half_minutes REAL NOT NULL,
            estimated_first_half_minutes_share REAL NOT NULL,
            team_first_half_share REAL NOT NULL,
            line_progress_ratio REAL,
            normalized_pace_ratio REAL,
            halftime_margin_to_line REAL,
            on_track_by_half INTEGER NOT NULL DEFAULT 0,
            winning_side TEXT,
            final_margin REAL,
            share_details_json TEXT NOT NULL,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_player_half_player_examples_game_date
        ON player_half_player_examples(game_date, source_game_id, source_player_id);

        CREATE INDEX IF NOT EXISTS idx_player_half_prop_examples_market_date
        ON player_half_prop_examples(market, game_date, source_prop_line_id);
        """
    )
    conn.commit()


def _read_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM player_half_training_metadata").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _write_metadata(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    conn.executemany(
        """
        INSERT INTO player_half_training_metadata (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        [(key, json.dumps(value) if isinstance(value, (dict, list)) else str(value)) for key, value in values.items()],
    )


def _rebuild_player_half_training_examples(
    *,
    source_conn: sqlite3.Connection,
    training_conn: sqlite3.Connection,
    source_signature: str,
) -> None:
    built_at = datetime.now(timezone.utc).isoformat()
    training_conn.execute("DELETE FROM player_half_player_examples")
    training_conn.execute("DELETE FROM player_half_prop_examples")

    player_rows = source_conn.execute(
        """
        SELECT
            pgs.game_id AS source_game_id,
            pgs.player_id AS source_player_id,
            pgs.minutes,
            pgs.points,
            pgs.rebounds,
            pgs.assists,
            pgs.threes,
            pgs.steals,
            pgs.blocks,
            pgs.turnovers,
            g.game_date,
            g.home_team_id,
            g.away_team_id,
            COALESCE(pth.team_id, p.team_id) AS team_id,
            p.rotation_role,
            tgr.points AS team_points,
            tgr.opponent_points,
            seg.home_1h_points,
            seg.away_1h_points,
            pfh.first_half_points,
            pfh.first_half_rebounds,
            pfh.first_half_assists,
            pfh.first_half_threes,
            pfh.first_half_steals,
            pfh.first_half_blocks,
            pfh.first_half_turnovers,
            pfh.first_half_minutes,
            pfh.minutes_source
        FROM player_game_stats pgs
        JOIN players p ON p.id = pgs.player_id
        JOIN games g ON g.id = pgs.game_id
        LEFT JOIN game_segment_results seg ON seg.game_id = pgs.game_id
        LEFT JOIN player_first_half_stats pfh
          ON pfh.game_id = pgs.game_id
         AND pfh.player_id = pgs.player_id
        LEFT JOIN player_team_history pth ON pth.player_id = pgs.player_id AND pth.game_id = pgs.game_id
        JOIN team_game_results tgr ON tgr.game_id = pgs.game_id AND tgr.team_id = COALESCE(pth.team_id, p.team_id)
        WHERE g.status = 'final'
        ORDER BY g.game_date ASC, pgs.game_id ASC, pgs.player_id ASC
        """
    ).fetchall()

    player_insert_rows: list[tuple[object, ...]] = []
    player_map: dict[tuple[int, int], dict[str, object]] = {}
    for row in player_rows:
        row_payload = _build_player_half_row(row, built_at)
        if row_payload is None:
            continue
        player_insert_rows.append(row_payload)
        game_id = int(row["source_game_id"])
        player_id = int(row["source_player_id"])
        player_map[(game_id, player_id)] = {
            "estimated_first_half_minutes": row_payload[21],
            "estimated_first_half_minutes_share": row_payload[22],
            "estimated_first_half_points": row_payload[23],
            "estimated_first_half_rebounds": row_payload[24],
            "estimated_first_half_assists": row_payload[25],
            "estimated_first_half_threes": row_payload[26],
            "estimated_first_half_steals": row_payload[27],
            "estimated_first_half_blocks": row_payload[28],
            "estimated_first_half_turnovers": row_payload[29],
            "estimated_first_half_points_rebounds": row_payload[30],
            "estimated_first_half_points_assists": row_payload[31],
            "estimated_first_half_rebounds_assists": row_payload[32],
            "estimated_first_half_points_rebounds_assists": row_payload[33],
            "estimated_first_half_blocks_steals": row_payload[34],
            "team_first_half_share": row_payload[20],
            "team_id": row_payload[2],
            "opponent_team_id": row_payload[3],
            "game_date": row_payload[4],
            "season": row_payload[5],
            "share_details_json": row_payload[35],
        }

    if player_insert_rows:
        training_conn.executemany(
            """
            INSERT INTO player_half_player_examples (
                source_game_id, source_player_id, team_id, opponent_team_id, game_date, season, is_home,
                rotation_role, full_minutes, full_points, full_rebounds, full_assists, full_threes,
                full_steals, full_blocks, full_turnovers, team_points, opponent_points, team_first_half_points,
                opponent_first_half_points, team_first_half_share, estimated_first_half_minutes,
                estimated_first_half_minutes_share, estimated_first_half_points, estimated_first_half_rebounds,
                estimated_first_half_assists, estimated_first_half_threes, estimated_first_half_steals,
                estimated_first_half_blocks, estimated_first_half_turnovers, estimated_first_half_points_rebounds,
                estimated_first_half_points_assists, estimated_first_half_rebounds_assists,
                estimated_first_half_points_rebounds_assists, estimated_first_half_blocks_steals,
                share_details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            player_insert_rows,
        )

    prop_rows = source_conn.execute(
        """
        SELECT
            pl.id AS source_prop_line_id,
            pl.game_id AS source_game_id,
            pl.player_id AS source_player_id,
            pl.market,
            pl.line AS line_value,
            pl.over_odds,
            pl.under_odds,
            g.game_date,
            sp.actual_result AS final_actual_result,
            sp.winning_side,
            sp.margin AS final_margin
        FROM settled_props sp
        JOIN prop_lines pl ON pl.id = sp.prop_line_id
        JOIN games g ON g.id = pl.game_id
        WHERE g.status = 'final'
        ORDER BY g.game_date ASC, pl.id ASC
        """
    ).fetchall()

    prop_insert_rows: list[tuple[object, ...]] = []
    for row in prop_rows:
        lookup = player_map.get((int(row["source_game_id"]), int(row["source_player_id"])))
        if lookup is None:
            continue
        market = str(row["market"] or "").strip().lower()
        estimated_half = _estimated_market_value(lookup, market)
        if estimated_half is None:
            continue
        line_value = float(row["line_value"] or 0.0)
        expected_halfway_line = line_value * 0.5 if line_value else None
        line_progress_ratio = estimated_half / line_value if line_value else None
        normalized_pace_ratio = (
            estimated_half / expected_halfway_line
            if expected_halfway_line and abs(expected_halfway_line) > 1e-9
            else None
        )
        halftime_margin_to_line = estimated_half - expected_halfway_line if expected_halfway_line is not None else None
        on_track = bool(normalized_pace_ratio is not None and normalized_pace_ratio >= 1.0)
        prop_insert_rows.append(
            (
                int(row["source_prop_line_id"]),
                int(row["source_game_id"]),
                int(row["source_player_id"]),
                int(lookup["team_id"]),
                int(lookup["opponent_team_id"]),
                str(lookup["game_date"]),
                str(lookup["season"]),
                market,
                line_value,
                int(row["over_odds"] or 0),
                int(row["under_odds"] or 0),
                float(row["final_actual_result"] or 0.0),
                float(estimated_half),
                float(lookup["estimated_first_half_minutes"]),
                float(lookup["estimated_first_half_minutes_share"]),
                float(lookup["team_first_half_share"]),
                line_progress_ratio,
                normalized_pace_ratio,
                halftime_margin_to_line,
                1 if on_track else 0,
                str(row["winning_side"] or ""),
                float(row["final_margin"] or 0.0),
                str(lookup["share_details_json"]),
                built_at,
            )
        )

    if prop_insert_rows:
        training_conn.executemany(
            """
            INSERT INTO player_half_prop_examples (
                source_prop_line_id, source_game_id, source_player_id, team_id, opponent_team_id, game_date, season,
                market, line_value, over_odds, under_odds, final_actual_result, estimated_first_half_result,
                estimated_first_half_minutes, estimated_first_half_minutes_share, team_first_half_share,
                line_progress_ratio, normalized_pace_ratio, halftime_margin_to_line, on_track_by_half,
                winning_side, final_margin, share_details_json, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            prop_insert_rows,
        )

    _write_metadata(
        training_conn,
        {
            "db_version": PLAYER_HALF_TRAINING_DB_VERSION,
            "source_signature": source_signature,
            "candidate_player_rows": len(player_rows),
            "candidate_prop_rows": len(prop_rows),
            "player_rows": len(player_insert_rows),
            "prop_rows": len(prop_insert_rows),
            "built_at": built_at,
        },
    )
    training_conn.commit()


def _build_player_half_row(row: sqlite3.Row, built_at: str) -> tuple[object, ...] | None:
    team_id = int(row["team_id"])
    home_team_id = int(row["home_team_id"])
    away_team_id = int(row["away_team_id"])
    if team_id == home_team_id:
        opponent_team_id = away_team_id
        is_home = 1
    elif team_id == away_team_id:
        opponent_team_id = home_team_id
        is_home = 0
    else:
        return None

    team_points_raw = float(row["team_points"] or 0.0)
    opponent_points_raw = float(row["opponent_points"] or 0.0)
    team_points = max(team_points_raw, 1.0)
    segment_available = row["home_1h_points"] is not None and row["away_1h_points"] is not None
    if segment_available:
        if team_id == home_team_id:
            team_first_half_points = float(row["home_1h_points"] or 0.0)
            opponent_first_half_points = float(row["away_1h_points"] or 0.0)
        else:
            team_first_half_points = float(row["away_1h_points"] or 0.0)
            opponent_first_half_points = float(row["home_1h_points"] or 0.0)
        team_first_half_share = _clip(team_first_half_points / team_points, 0.2, 0.8)
    else:
        team_first_half_share = _clip(DEFAULT_TEAM_FIRST_HALF_SHARE, 0.2, 0.8)
        team_first_half_points = round(team_points_raw * team_first_half_share, 3)
        opponent_first_half_points = round(opponent_points_raw * team_first_half_share, 3)
    rotation_role = str(row["rotation_role"] or "").strip().lower()
    minute_share = _estimated_minute_share(rotation_role, team_first_half_share)
    share_map = _estimated_stat_shares(team_first_half_share)

    full_minutes = float(row["minutes"] or 0.0)
    full_points = float(row["points"] or 0.0)
    full_rebounds = float(row["rebounds"] or 0.0)
    full_assists = float(row["assists"] or 0.0)
    full_threes = float(row["threes"] or 0.0)
    full_steals = float(row["steals"] or 0.0)
    full_blocks = float(row["blocks"] or 0.0)
    full_turnovers = float(row["turnovers"] or 0.0)
    observed_available = row["first_half_points"] is not None
    if observed_available:
        est_minutes = round(float(row["first_half_minutes"] or 0.0), 3)
        est_points = round(float(row["first_half_points"] or 0.0), 3)
        est_rebounds = round(float(row["first_half_rebounds"] or 0.0), 3)
        est_assists = round(float(row["first_half_assists"] or 0.0), 3)
        est_threes = round(float(row["first_half_threes"] or 0.0), 3)
        est_steals = round(float(row["first_half_steals"] or 0.0), 3)
        est_blocks = round(float(row["first_half_blocks"] or 0.0), 3)
        est_turnovers = round(float(row["first_half_turnovers"] or 0.0), 3)
        minute_share = _clip((est_minutes / full_minutes) if full_minutes > 0 else minute_share, 0.0, 1.0)
    else:
        est_minutes = round(full_minutes * minute_share, 3)
        est_points = round(full_points * share_map["points"], 3)
        est_rebounds = round(full_rebounds * share_map["rebounds"], 3)
        est_assists = round(full_assists * share_map["assists"], 3)
        est_threes = round(full_threes * share_map["threes"], 3)
        est_steals = round(full_steals * share_map["steals"], 3)
        est_blocks = round(full_blocks * share_map["blocks"], 3)
        est_turnovers = round(full_turnovers * share_map["turnovers"], 3)

    share_details = {
        "team_first_half_share": team_first_half_share,
        "estimated_first_half_minutes_share": minute_share,
        "stat_shares": share_map,
        "rotation_role": rotation_role,
        "derived": not observed_available,
        "segment_source": "game_segment_results" if segment_available else "fallback_half_share",
        "player_half_source": str(row["minutes_source"] or "espn_playbyplay") if observed_available else None,
        "fallback_team_first_half_share": None if segment_available else DEFAULT_TEAM_FIRST_HALF_SHARE,
    }
    game_date = str(row["game_date"] or "")
    season = game_date[:4]
    return (
        int(row["source_game_id"]),
        int(row["source_player_id"]),
        team_id,
        opponent_team_id,
        game_date,
        season,
        is_home,
        str(row["rotation_role"] or ""),
        full_minutes,
        full_points,
        full_rebounds,
        full_assists,
        full_threes,
        full_steals,
        full_blocks,
        full_turnovers,
        float(row["team_points"] or 0.0),
        float(row["opponent_points"] or 0.0),
        team_first_half_points,
        opponent_first_half_points,
        team_first_half_share,
        est_minutes,
        minute_share,
        est_points,
        est_rebounds,
        est_assists,
        est_threes,
        est_steals,
        est_blocks,
        est_turnovers,
        round(est_points + est_rebounds, 3),
        round(est_points + est_assists, 3),
        round(est_rebounds + est_assists, 3),
        round(est_points + est_rebounds + est_assists, 3),
        round(est_blocks + est_steals, 3),
        json.dumps(share_details, sort_keys=True),
        built_at,
    )


def _estimated_minute_share(rotation_role: str, team_first_half_share: float) -> float:
    base = {
        "star": 0.53,
        "starter": 0.515,
        "rotation": 0.495,
        "bench": 0.465,
        "reserve": 0.45,
    }.get(rotation_role, 0.5)
    return _clip(base + ((team_first_half_share - 0.5) * 0.2), 0.36, 0.64)


def _estimated_stat_shares(team_first_half_share: float) -> dict[str, float]:
    centered = team_first_half_share - 0.5
    return {
        "points": team_first_half_share,
        "threes": _clip(0.5 + (centered * 0.9), 0.2, 0.8),
        "rebounds": _clip(0.5 + (centered * 0.45), 0.2, 0.8),
        "assists": _clip(0.5 + (centered * 0.6), 0.2, 0.8),
        "steals": _clip(0.5 + (centered * 0.35), 0.2, 0.8),
        "blocks": _clip(0.5 + (centered * 0.3), 0.2, 0.8),
        "turnovers": _clip(0.5 + (centered * 0.4), 0.2, 0.8),
    }


def _estimated_market_value(player_row: dict[str, object], market: str) -> float | None:
    mapping = {
        "points": player_row["estimated_first_half_points"],
        "rebounds": player_row["estimated_first_half_rebounds"],
        "assists": player_row["estimated_first_half_assists"],
        "threes": player_row["estimated_first_half_threes"],
        "steals": player_row["estimated_first_half_steals"],
        "blocks": player_row["estimated_first_half_blocks"],
        "points_rebounds": player_row["estimated_first_half_points_rebounds"],
        "points_assists": player_row["estimated_first_half_points_assists"],
        "rebounds_assists": player_row["estimated_first_half_rebounds_assists"],
        "points_rebounds_assists": player_row["estimated_first_half_points_rebounds_assists"],
        "blocks_steals": player_row["estimated_first_half_blocks_steals"],
    }
    value = mapping.get(str(market or "").strip().lower())
    return float(value) if value is not None else None


def _clip(value: float, lower: float, upper: float) -> float:
    return max(lower, min(upper, float(value)))


def _source_signature(conn: sqlite3.Connection) -> str:
    payload = {
        "player_game_stats_count": int(conn.execute("SELECT COUNT(*) FROM player_game_stats").fetchone()[0]),
        "game_segment_results_count": int(conn.execute("SELECT COUNT(*) FROM game_segment_results").fetchone()[0]),
        "settled_props_count": int(conn.execute("SELECT COUNT(*) FROM settled_props").fetchone()[0]),
        "latest_game_date": conn.execute("SELECT MAX(game_date) FROM games").fetchone()[0] or "",
        "latest_settlement_id": int(conn.execute("SELECT COALESCE(MAX(id), 0) FROM settled_props").fetchone()[0]),
        "db_version": PLAYER_HALF_TRAINING_DB_VERSION,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    import hashlib

    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]
