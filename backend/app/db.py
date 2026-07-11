from __future__ import annotations

import os
import sqlite3
import threading
from collections.abc import Mapping
from contextlib import contextmanager
from typing import Any, Iterable, Sequence

import requests
from dotenv import load_dotenv

from .auth import ensure_auth_schema
from .paths import ROOT_DIR, get_db_path

load_dotenv(ROOT_DIR / ".env")
_SQLITE_WRITE_LOCK = threading.RLock()


class ManagedSqliteConnection(sqlite3.Connection):
    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            super().__exit__(exc_type, exc, tb)
        finally:
            self.close()


def get_turso_database_url() -> str | None:
    return os.getenv("TURSO_DATABASE_URL")


def using_turso() -> bool:
    return os.getenv("USE_TURSO", "").strip().lower() in {"1", "true", "yes"}


def connect() -> Any:
    if using_turso():
        database_url = get_turso_database_url()
        if not database_url:
            raise RuntimeError("TURSO_DATABASE_URL is required when USE_TURSO=true.")
        auth_token = os.getenv("TURSO_AUTH_TOKEN")
        if not auth_token:
            raise RuntimeError("TURSO_AUTH_TOKEN is required when USE_TURSO=true.")
        return TursoHttpConnection(database_url, auth_token)

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=30, factory=ManagedSqliteConnection)
    _configure_connection(conn)
    return conn


@contextmanager
def sqlite_write_lock():
    with _SQLITE_WRITE_LOCK:
        yield


def _configure_connection(conn: Any) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -8000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA busy_timeout = 30000")


class TursoRow(Mapping):
    def __init__(self, columns: Sequence[str], values: Sequence[Any]):
        self._columns = list(columns)
        self._values = list(values)
        self._index = {column: index for index, column in enumerate(self._columns)}

    def __getitem__(self, key: str | int) -> Any:
        if isinstance(key, int):
            return self._values[key]
        return self._values[self._index[key]]

    def __iter__(self):
        return iter(self._columns)

    def __len__(self) -> int:
        return len(self._columns)

    def keys(self) -> list[str]:
        return list(self._columns)


class TursoHttpCursor:
    def __init__(self, columns: Sequence[str], rows: Sequence[TursoRow], lastrowid: int | None = None):
        self._rows = list(rows)
        self._index = 0
        self.lastrowid = lastrowid

    def fetchall(self) -> list[TursoRow]:
        rows = self._rows[self._index :]
        self._index = len(self._rows)
        return rows

    def fetchone(self) -> TursoRow | None:
        if self._index >= len(self._rows):
            return None
        row = self._rows[self._index]
        self._index += 1
        return row

    def __iter__(self):
        return iter(self._rows)


class TursoHttpConnection:
    BATCH_SIZE = 100
    EXECUTEMANY_BATCH_SIZE = 75

    def __init__(self, database_url: str, auth_token: str):
        self._pipeline_url = _http_pipeline_url(database_url)
        self._auth_token = auth_token
        self.row_factory = None
        self._session = requests.Session()
        self._session.headers.update({
            "Authorization": f"Bearer {self._auth_token}",
            "Content-Type": "application/json",
        })

    def execute(self, sql: str, params: Sequence[Any] = ()) -> TursoHttpCursor:
        result = self._request_many([{"type": "execute", "stmt": _stmt(sql, params)}, {"type": "close"}])[0]
        return _cursor_from_result(result)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> TursoHttpCursor:
        last_cursor = TursoHttpCursor([], [])
        batch: list[dict] = []
        for params in seq_of_params:
            batch.append({"type": "execute", "stmt": _stmt(sql, params)})
            if len(batch) >= self.EXECUTEMANY_BATCH_SIZE:
                last_cursor = self._request_batch(batch)
                batch = []
        if batch:
            last_cursor = self._request_batch(batch)
        return last_cursor

    def _execute_batch(self, batch: list[dict]) -> TursoHttpCursor:
        return self._request_batch(batch)

    def _request_batch(self, batch: list[dict]) -> TursoHttpCursor:
        results = self._request_many([*batch, {"type": "close"}])
        if not results:
            return TursoHttpCursor([], [])
        return _cursor_from_result(results[len(batch) - 1])

    def executescript(self, script: str) -> TursoHttpCursor:
        last_cursor = TursoHttpCursor([], [])
        statement = ""
        for line in script.splitlines():
            statement += line + "\n"
            if sqlite3.complete_statement(statement):
                sql = statement.strip()
                statement = ""
                if sql:
                    last_cursor = self.execute(sql)
        if statement.strip():
            last_cursor = self.execute(statement.strip())
        return last_cursor

    def commit(self) -> None:
        return None

    def rollback(self) -> None:
        return None

    def close(self) -> None:
        self._session.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _request(self, requests_payload: list[dict]) -> list[dict]:
        return self._request_many(requests_payload)

    def _request_many(self, requests_payload: list[dict]) -> list[dict]:
        response = self._session.post(
            self._pipeline_url,
            json={"requests": requests_payload},
            timeout=30,
        )
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            raise RuntimeError(f"Turso request failed: {response.text}") from exc
        payload = response.json()
        results = []
        for result in payload["results"]:
            if result.get("type") != "ok":
                raise RuntimeError(f"Turso query failed: {result}")
            response_result = result.get("response", {}).get("result")
            if response_result is not None:
                results.append(response_result)
        return results


def _http_pipeline_url(database_url: str) -> str:
    base_url = database_url.strip().rstrip("/")
    if base_url.startswith("libsql://"):
        base_url = "https://" + base_url.removeprefix("libsql://")
    if not base_url.startswith("https://"):
        raise RuntimeError("TURSO_DATABASE_URL must be a libsql:// or https:// Turso database URL.")
    if base_url.endswith("/v2/pipeline"):
        return base_url
    return f"{base_url}/v2/pipeline"


def _stmt(sql: str, params: Sequence[Any] = ()) -> dict:
    statement = {"sql": sql}
    if params:
        statement["args"] = [_arg(param) for param in params]
    return statement


def _arg(value: Any) -> dict:
    if value is None:
        return {"type": "null"}
    if isinstance(value, bool):
        return {"type": "integer", "value": "1" if value else "0"}
    if isinstance(value, int):
        return {"type": "integer", "value": str(value)}
    if isinstance(value, float):
        return {"type": "float", "value": value}
    if isinstance(value, bytes):
        raise TypeError("Blob parameters are not supported by this Turso HTTP adapter.")
    return {"type": "text", "value": str(value)}


def _cursor_from_result(result: dict) -> TursoHttpCursor:
    columns = [_column_name(column) for column in result.get("cols", [])]
    rows = [
        TursoRow(columns, [_value(cell) for cell in row])
        for row in result.get("rows", [])
    ]
    lastrowid = result.get("last_insert_rowid")
    return TursoHttpCursor(columns, rows, int(lastrowid) if lastrowid is not None else None)


def _column_name(column: Any) -> str:
    if isinstance(column, dict):
        return str(column.get("name") or column.get("column") or "")
    return str(column)


def _value(cell: Any) -> Any:
    if not isinstance(cell, dict):
        return cell
    cell_type = cell.get("type")
    if cell_type == "null":
        return None
    value = cell.get("value")
    if cell_type == "integer":
        return int(value)
    if cell_type == "float":
        return float(value)
    return value


def init_db() -> None:
    with connect() as conn:
        conn.executescript(SCHEMA)
        ensure_auth_schema(conn)
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(teams)").fetchall()}
        if "logo_url" not in columns:
            conn.execute("ALTER TABLE teams ADD COLUMN logo_url TEXT")
        player_columns = {row["name"] for row in conn.execute("PRAGMA table_info(players)").fetchall()}
        if "rotation_role" not in player_columns:
            conn.execute("ALTER TABLE players ADD COLUMN rotation_role TEXT DEFAULT 'starter'")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_team_history (
                id INTEGER PRIMARY KEY,
                player_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                game_id INTEGER,
                source TEXT NOT NULL,
                confidence REAL NOT NULL DEFAULT 1.0,
                observed_at TEXT NOT NULL,
                FOREIGN KEY (player_id) REFERENCES players(id),
                FOREIGN KEY (team_id) REFERENCES teams(id),
                FOREIGN KEY (game_id) REFERENCES games(id),
                UNIQUE(player_id, game_id, source)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_team_history_player_game
            ON player_team_history(player_id, game_id, id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_team_history_team
            ON player_team_history(team_id)
            """
        )
        game_columns = {row["name"] for row in conn.execute("PRAGMA table_info(games)").fetchall()}
        if "rest_days_home" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN rest_days_home INTEGER DEFAULT 2")
        if "rest_days_away" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN rest_days_away INTEGER DEFAULT 2")
        if "spread_home" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN spread_home REAL")
        if "game_total" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN game_total REAL")
        if "home_moneyline" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN home_moneyline REAL")
        if "away_moneyline" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN away_moneyline REAL")
        if "home_spread_price" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN home_spread_price REAL")
        if "away_spread_price" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN away_spread_price REAL")
        if "over_price" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN over_price REAL")
        if "under_price" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN under_price REAL")
        if "espn_event_id" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN espn_event_id INTEGER")
        result_columns = {row["name"] for row in conn.execute("PRAGMA table_info(team_game_results)").fetchall()}
        if result_columns and "possessions" not in result_columns:
            conn.execute("ALTER TABLE team_game_results ADD COLUMN possessions REAL NOT NULL DEFAULT 78.0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS sportsbook_prop_lines (
                id INTEGER PRIMARY KEY,
                provider TEXT NOT NULL,
                provider_event_id TEXT NOT NULL,
                game_id INTEGER,
                game_date TEXT NOT NULL,
                commence_time TEXT NOT NULL,
                home_team TEXT NOT NULL,
                away_team TEXT NOT NULL,
                bookmaker_key TEXT NOT NULL,
                sportsbook TEXT NOT NULL,
                market_key TEXT NOT NULL,
                market TEXT NOT NULL,
                player_name TEXT NOT NULL,
                side TEXT NOT NULL,
                line REAL NOT NULL,
                price INTEGER NOT NULL,
                captured_at TEXT NOT NULL,
                FOREIGN KEY (game_id) REFERENCES games(id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_sportsbook_props_game_market
            ON sportsbook_prop_lines(game_id, player_name, market)
            """
        )
        sportsbook_columns = {row["name"] for row in conn.execute("PRAGMA table_info(sportsbook_prop_lines)").fetchall()}
        if "provider_player_id" not in sportsbook_columns:
            conn.execute("ALTER TABLE sportsbook_prop_lines ADD COLUMN provider_player_id INTEGER")
        if "provider_game_id" not in sportsbook_columns:
            conn.execute("ALTER TABLE sportsbook_prop_lines ADD COLUMN provider_game_id INTEGER")
        settled_columns = {row["name"] for row in conn.execute("PRAGMA table_info(settled_props)").fetchall()}
        if "player_minutes" not in settled_columns:
            conn.execute("ALTER TABLE settled_props ADD COLUMN player_minutes REAL")
        if "game_margin" not in settled_columns:
            conn.execute("ALTER TABLE settled_props ADD COLUMN game_margin REAL")
        if "team_margin" not in settled_columns:
            conn.execute("ALTER TABLE settled_props ADD COLUMN team_margin REAL")
        if "team_spread" not in settled_columns:
            conn.execute("ALTER TABLE settled_props ADD COLUMN team_spread REAL")
        if "blowout_result" not in settled_columns:
            conn.execute("ALTER TABLE settled_props ADD COLUMN blowout_result TEXT")
        if "blowout_threshold" not in settled_columns:
            conn.execute("ALTER TABLE settled_props ADD COLUMN blowout_threshold REAL DEFAULT 15.0")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS game_predictions (
                id INTEGER PRIMARY KEY,
                game_id INTEGER NOT NULL,
                model_version TEXT NOT NULL,
                prediction_time TEXT NOT NULL,
                home_projected_points REAL,
                away_projected_points REAL,
                projected_margin REAL,
                projected_total REAL,
                winner_pick TEXT NOT NULL,
                ats_pick TEXT NOT NULL,
                ats_edge REAL,
                total_pick TEXT NOT NULL,
                total_edge REAL,
                confidence TEXT NOT NULL,
                reason TEXT NOT NULL,
                spread_home REAL,
                game_total REAL,
                home_rest_days INTEGER,
                away_rest_days INTEGER,
                UNIQUE(game_id, model_version),
                FOREIGN KEY (game_id) REFERENCES games(id)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settled_game_predictions (
                id INTEGER PRIMARY KEY,
                game_prediction_id INTEGER NOT NULL UNIQUE,
                game_id INTEGER NOT NULL,
                home_score INTEGER NOT NULL,
                away_score INTEGER NOT NULL,
                actual_winner TEXT NOT NULL,
                actual_margin REAL NOT NULL,
                actual_total REAL NOT NULL,
                actual_ats_pick TEXT,
                actual_total_result TEXT,
                winner_correct INTEGER NOT NULL,
                ats_correct INTEGER,
                total_correct INTEGER,
                settled_at TEXT NOT NULL,
                FOREIGN KEY (game_prediction_id) REFERENCES game_predictions(id),
                FOREIGN KEY (game_id) REFERENCES games(id)
            )
            """
        )
        conn.execute(
            """
            CREATE UNIQUE INDEX IF NOT EXISTS idx_player_stats_unique_player_game
            ON player_game_stats(player_id, game_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_stats_game
            ON player_game_stats(game_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS player_game_availability (
                id INTEGER PRIMARY KEY,
                player_id INTEGER NOT NULL,
                game_id INTEGER NOT NULL,
                team_id INTEGER NOT NULL,
                source TEXT NOT NULL,
                is_active INTEGER NOT NULL DEFAULT 1,
                did_not_play INTEGER NOT NULL DEFAULT 0,
                status_reason TEXT,
                minutes_text TEXT,
                observed_at TEXT NOT NULL,
                FOREIGN KEY (player_id) REFERENCES players(id),
                FOREIGN KEY (game_id) REFERENCES games(id),
                FOREIGN KEY (team_id) REFERENCES teams(id),
                UNIQUE(player_id, game_id, source)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_player_game_availability_game
            ON player_game_availability(game_id, player_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_games_status_date
            ON games(status, game_date)
            """
        )
        conn.execute(
            """
            INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at)
            SELECT p.id, p.team_id, NULL, 'bootstrap_players', 0.4, datetime('now')
            FROM players p
            WHERE NOT EXISTS (
                SELECT 1
                FROM player_team_history h
                WHERE h.player_id = p.id
            )
            """
        )
        conn.execute(
            """
            UPDATE players
            SET team_id = COALESCE(
                (
                    SELECT resolved.team_id
                    FROM (
                        WITH recent_games AS (
                            SELECT
                                s.player_id,
                                g.home_team_id,
                                g.away_team_id,
                                ROW_NUMBER() OVER (
                                    PARTITION BY s.player_id
                                    ORDER BY g.game_date DESC, g.id DESC
                                ) AS rn
                            FROM player_game_stats s
                            JOIN games g ON g.id = s.game_id
                            WHERE s.player_id = players.id
                        ),
                        recent_window AS (
                            SELECT *
                            FROM recent_games
                            WHERE rn <= 8
                        ),
                        side_counts AS (
                            SELECT home_team_id AS team_id, COUNT(*) AS appearances
                            FROM recent_window
                            GROUP BY home_team_id
                            UNION ALL
                            SELECT away_team_id AS team_id, COUNT(*) AS appearances
                            FROM recent_window
                            GROUP BY away_team_id
                        ),
                        collapsed AS (
                            SELECT team_id, SUM(appearances) AS appearances
                            FROM side_counts
                            GROUP BY team_id
                        ),
                        ranked AS (
                            SELECT
                                team_id,
                                appearances,
                                ROW_NUMBER() OVER (ORDER BY appearances DESC, team_id DESC) AS rn,
                                LEAD(appearances) OVER (ORDER BY appearances DESC, team_id DESC) AS next_appearances
                            FROM collapsed
                        )
                        SELECT team_id
                        FROM ranked
                        WHERE rn = 1
                          AND appearances >= 3
                          AND appearances > COALESCE(next_appearances, 0)
                    ) resolved
                ),
                (
                    SELECT hist.team_id
                    FROM (
                        SELECT
                            h.team_id,
                            COUNT(*) AS team_count,
                            MAX(g.game_date) AS last_seen
                        FROM player_team_history h
                        JOIN games g ON g.id = h.game_id
                        WHERE h.player_id = players.id
                        GROUP BY h.team_id
                        ORDER BY team_count DESC, last_seen DESC, h.team_id DESC
                        LIMIT 1
                    ) hist
                ),
                players.team_id
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gem_snapshots (
                id INTEGER PRIMARY KEY,
                snapshot_date TEXT NOT NULL,
                preset TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                min_ev REAL NOT NULL,
                min_edge REAL NOT NULL,
                allow_low INTEGER NOT NULL DEFAULT 0,
                min_score REAL NOT NULL DEFAULT 0.0,
                item_count INTEGER NOT NULL DEFAULT 0,
                settled_count INTEGER NOT NULL DEFAULT 0,
                wins_count INTEGER NOT NULL DEFAULT 0,
                UNIQUE(snapshot_date, preset)
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS gem_snapshot_items (
                id INTEGER PRIMARY KEY,
                snapshot_id INTEGER NOT NULL,
                prop_line_id INTEGER NOT NULL,
                game_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                market TEXT NOT NULL,
                side TEXT NOT NULL,
                line REAL NOT NULL,
                edge REAL NOT NULL,
                expected_value REAL NOT NULL,
                confidence TEXT NOT NULL,
                line_gap REAL NOT NULL DEFAULT 0.0,
                price_gap INTEGER NOT NULL DEFAULT 0,
                gem_score REAL NOT NULL DEFAULT 0.0,
                is_settled INTEGER NOT NULL DEFAULT 0,
                winning_side TEXT,
                settled_at TEXT,
                FOREIGN KEY (snapshot_id) REFERENCES gem_snapshots(id),
                FOREIGN KEY (prop_line_id) REFERENCES prop_lines(id),
                UNIQUE(snapshot_id, prop_line_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gem_snapshots_date_preset
            ON gem_snapshots(snapshot_date, preset)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gem_snapshot_items_snapshot
            ON gem_snapshot_items(snapshot_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_gem_snapshot_items_prop_line
            ON gem_snapshot_items(prop_line_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_snapshots (
                id INTEGER PRIMARY KEY,
                snapshot_date TEXT NOT NULL UNIQUE,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                min_ev REAL NOT NULL,
                min_edge REAL NOT NULL,
                max_edge REAL NOT NULL,
                item_count INTEGER NOT NULL DEFAULT 0,
                settled_count INTEGER NOT NULL DEFAULT 0,
                wins_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS watchlist_snapshot_items (
                id INTEGER PRIMARY KEY,
                snapshot_id INTEGER NOT NULL,
                prop_line_id INTEGER NOT NULL,
                prediction_id INTEGER NOT NULL,
                game_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                market TEXT NOT NULL,
                side TEXT NOT NULL,
                line REAL NOT NULL,
                edge REAL NOT NULL,
                expected_value REAL NOT NULL,
                confidence TEXT NOT NULL,
                is_settled INTEGER NOT NULL DEFAULT 0,
                winning_side TEXT,
                settled_at TEXT,
                FOREIGN KEY (snapshot_id) REFERENCES watchlist_snapshots(id),
                FOREIGN KEY (prop_line_id) REFERENCES prop_lines(id),
                FOREIGN KEY (prediction_id) REFERENCES prop_predictions(id),
                UNIQUE(snapshot_id, prop_line_id)
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_watchlist_snapshots_date
            ON watchlist_snapshots(snapshot_date)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_watchlist_snapshot_items_snapshot
            ON watchlist_snapshot_items(snapshot_id)
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_watchlist_snapshot_items_prop_line
            ON watchlist_snapshot_items(prop_line_id)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS prop_sync_jobs (
                id INTEGER PRIMARY KEY,
                scope TEXT NOT NULL,
                status TEXT NOT NULL,
                started_at TEXT NOT NULL,
                finished_at TEXT,
                stage TEXT,
                stage_index INTEGER NOT NULL DEFAULT 0,
                stage_total INTEGER NOT NULL DEFAULT 1,
                current_count INTEGER NOT NULL DEFAULT 0,
                total_count INTEGER NOT NULL DEFAULT 0,
                percent REAL NOT NULL DEFAULT 0.0,
                message TEXT,
                last_error TEXT,
                last_result_json TEXT,
                target_game_ids_json TEXT NOT NULL DEFAULT '[]',
                updated_at TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_prop_sync_jobs_started_at
            ON prop_sync_jobs(started_at DESC, id DESC)
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS cache_events (
                id INTEGER PRIMARY KEY,
                created_at TEXT NOT NULL,
                views_json TEXT NOT NULL
            )
            """
        )
        conn.execute(
            """
            CREATE INDEX IF NOT EXISTS idx_cache_events_created_at
            ON cache_events(created_at DESC, id DESC)
            """
        )


SCHEMA = """
CREATE TABLE IF NOT EXISTS teams (
    id INTEGER PRIMARY KEY,
    abbreviation TEXT NOT NULL UNIQUE,
    name TEXT NOT NULL,
    logo_url TEXT
);

CREATE TABLE IF NOT EXISTS players (
    id INTEGER PRIMARY KEY,
    full_name TEXT NOT NULL,
    team_id INTEGER NOT NULL,
    position TEXT,
    rotation_role TEXT DEFAULT 'starter',
    FOREIGN KEY (team_id) REFERENCES teams(id)
);

CREATE TABLE IF NOT EXISTS player_team_history (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    game_id INTEGER,
    source TEXT NOT NULL,
    confidence REAL NOT NULL DEFAULT 1.0,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (team_id) REFERENCES teams(id),
    FOREIGN KEY (game_id) REFERENCES games(id),
    UNIQUE(player_id, game_id, source)
);

CREATE TABLE IF NOT EXISTS games (
    id INTEGER PRIMARY KEY,
    game_date TEXT NOT NULL,
    start_time TEXT NOT NULL,
    home_team_id INTEGER NOT NULL,
    away_team_id INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'scheduled',
    rest_days_home INTEGER DEFAULT 2,
    rest_days_away INTEGER DEFAULT 2,
    spread_home REAL,
    game_total REAL,
    home_moneyline REAL,
    away_moneyline REAL,
    home_spread_price REAL,
    away_spread_price REAL,
    over_price REAL,
    under_price REAL,
    espn_event_id INTEGER,
    FOREIGN KEY (home_team_id) REFERENCES teams(id),
    FOREIGN KEY (away_team_id) REFERENCES teams(id)
);

CREATE TABLE IF NOT EXISTS player_game_stats (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    game_id INTEGER NOT NULL,
    minutes REAL NOT NULL,
    points INTEGER NOT NULL,
    rebounds INTEGER NOT NULL,
    assists INTEGER NOT NULL,
    threes INTEGER NOT NULL,
    steals INTEGER NOT NULL,
    blocks INTEGER NOT NULL,
    turnovers INTEGER NOT NULL,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (game_id) REFERENCES games(id)
);

CREATE TABLE IF NOT EXISTS player_game_availability (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    game_id INTEGER NOT NULL,
    team_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    did_not_play INTEGER NOT NULL DEFAULT 0,
    status_reason TEXT,
    minutes_text TEXT,
    observed_at TEXT NOT NULL,
    FOREIGN KEY (player_id) REFERENCES players(id),
    FOREIGN KEY (game_id) REFERENCES games(id),
    FOREIGN KEY (team_id) REFERENCES teams(id),
    UNIQUE(player_id, game_id, source)
);

CREATE TABLE IF NOT EXISTS team_game_results (
    id INTEGER PRIMARY KEY,
    team_id INTEGER NOT NULL,
    game_id INTEGER NOT NULL,
    is_home INTEGER NOT NULL,
    points INTEGER NOT NULL,
    opponent_points INTEGER NOT NULL,
    possessions REAL NOT NULL DEFAULT 78.0,
    closing_spread REAL NOT NULL,
    closing_total REAL NOT NULL,
    ats_result TEXT NOT NULL,
    total_result TEXT NOT NULL,
    FOREIGN KEY (team_id) REFERENCES teams(id),
    FOREIGN KEY (game_id) REFERENCES games(id)
);

CREATE TABLE IF NOT EXISTS injuries (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    status TEXT NOT NULL,
    note TEXT,
    captured_at TEXT NOT NULL,
    FOREIGN KEY (player_id) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS manual_adjustments (
    id INTEGER PRIMARY KEY,
    player_id INTEGER NOT NULL,
    projected_minutes REAL,
    usage_multiplier REAL NOT NULL DEFAULT 1.0,
    note TEXT,
    expires_at TEXT,
    FOREIGN KEY (player_id) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS prop_lines (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    sportsbook TEXT NOT NULL,
    market TEXT NOT NULL,
    line REAL NOT NULL,
    over_odds INTEGER NOT NULL,
    under_odds INTEGER NOT NULL,
    captured_at TEXT NOT NULL,
    FOREIGN KEY (game_id) REFERENCES games(id),
    FOREIGN KEY (player_id) REFERENCES players(id)
);

CREATE TABLE IF NOT EXISTS prop_predictions (
    id INTEGER PRIMARY KEY,
    prop_line_id INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    prediction_time TEXT NOT NULL,
    projection REAL NOT NULL,
    recommended_side TEXT NOT NULL,
    model_probability REAL NOT NULL,
    implied_probability REAL NOT NULL,
    edge REAL NOT NULL,
    expected_value REAL NOT NULL,
    confidence TEXT NOT NULL,
    reason TEXT NOT NULL,
    FOREIGN KEY (prop_line_id) REFERENCES prop_lines(id)
);

CREATE TABLE IF NOT EXISTS settled_props (
    id INTEGER PRIMARY KEY,
    prop_line_id INTEGER NOT NULL UNIQUE,
    actual_result REAL NOT NULL,
    winning_side TEXT NOT NULL,
    margin REAL NOT NULL,
    player_minutes REAL,
    game_margin REAL,
    team_margin REAL,
    team_spread REAL,
    blowout_result TEXT,
    blowout_threshold REAL DEFAULT 15.0,
    settled_at TEXT NOT NULL,
    FOREIGN KEY (prop_line_id) REFERENCES prop_lines(id)
);

CREATE TABLE IF NOT EXISTS game_predictions (
    id INTEGER PRIMARY KEY,
    game_id INTEGER NOT NULL,
    model_version TEXT NOT NULL,
    prediction_time TEXT NOT NULL,
    home_projected_points REAL,
    away_projected_points REAL,
    projected_margin REAL,
    projected_total REAL,
    winner_pick TEXT NOT NULL,
    ats_pick TEXT NOT NULL,
    ats_edge REAL,
    total_pick TEXT NOT NULL,
    total_edge REAL,
    confidence TEXT NOT NULL,
    reason TEXT NOT NULL,
    spread_home REAL,
    game_total REAL,
    home_rest_days INTEGER,
    away_rest_days INTEGER,
    UNIQUE(game_id, model_version),
    FOREIGN KEY (game_id) REFERENCES games(id)
);

CREATE TABLE IF NOT EXISTS settled_game_predictions (
    id INTEGER PRIMARY KEY,
    game_prediction_id INTEGER NOT NULL UNIQUE,
    game_id INTEGER NOT NULL,
    home_score INTEGER NOT NULL,
    away_score INTEGER NOT NULL,
    actual_winner TEXT NOT NULL,
    actual_margin REAL NOT NULL,
    actual_total REAL NOT NULL,
    actual_ats_pick TEXT,
    actual_total_result TEXT,
    winner_correct INTEGER NOT NULL,
    ats_correct INTEGER,
    total_correct INTEGER,
    settled_at TEXT NOT NULL,
    FOREIGN KEY (game_prediction_id) REFERENCES game_predictions(id),
    FOREIGN KEY (game_id) REFERENCES games(id)
);

CREATE TABLE IF NOT EXISTS model_runs (
    id INTEGER PRIMARY KEY,
    model_version TEXT NOT NULL,
    run_type TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    training_rows INTEGER NOT NULL DEFAULT 0,
    markets TEXT NOT NULL,
    metrics_json TEXT NOT NULL,
    notes TEXT
);

CREATE TABLE IF NOT EXISTS gem_snapshots (
    id INTEGER PRIMARY KEY,
    snapshot_date TEXT NOT NULL,
    preset TEXT NOT NULL,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    min_ev REAL NOT NULL,
    min_edge REAL NOT NULL,
    allow_low INTEGER NOT NULL DEFAULT 0,
    min_score REAL NOT NULL DEFAULT 0.0,
    item_count INTEGER NOT NULL DEFAULT 0,
    settled_count INTEGER NOT NULL DEFAULT 0,
    wins_count INTEGER NOT NULL DEFAULT 0,
    UNIQUE(snapshot_date, preset)
);

CREATE TABLE IF NOT EXISTS gem_snapshot_items (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL,
    prop_line_id INTEGER NOT NULL,
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    line REAL NOT NULL,
    edge REAL NOT NULL,
    expected_value REAL NOT NULL,
    confidence TEXT NOT NULL,
    line_gap REAL NOT NULL DEFAULT 0.0,
    price_gap INTEGER NOT NULL DEFAULT 0,
    gem_score REAL NOT NULL DEFAULT 0.0,
    is_settled INTEGER NOT NULL DEFAULT 0,
    winning_side TEXT,
    settled_at TEXT,
    FOREIGN KEY (snapshot_id) REFERENCES gem_snapshots(id),
    FOREIGN KEY (prop_line_id) REFERENCES prop_lines(id),
    UNIQUE(snapshot_id, prop_line_id)
);

CREATE TABLE IF NOT EXISTS watchlist_snapshots (
    id INTEGER PRIMARY KEY,
    snapshot_date TEXT NOT NULL UNIQUE,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    min_ev REAL NOT NULL,
    min_edge REAL NOT NULL,
    max_edge REAL NOT NULL,
    item_count INTEGER NOT NULL DEFAULT 0,
    settled_count INTEGER NOT NULL DEFAULT 0,
    wins_count INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS watchlist_snapshot_items (
    id INTEGER PRIMARY KEY,
    snapshot_id INTEGER NOT NULL,
    prop_line_id INTEGER NOT NULL,
    prediction_id INTEGER NOT NULL,
    game_id INTEGER NOT NULL,
    player_id INTEGER NOT NULL,
    market TEXT NOT NULL,
    side TEXT NOT NULL,
    line REAL NOT NULL,
    edge REAL NOT NULL,
    expected_value REAL NOT NULL,
    confidence TEXT NOT NULL,
    is_settled INTEGER NOT NULL DEFAULT 0,
    winning_side TEXT,
    settled_at TEXT,
    FOREIGN KEY (snapshot_id) REFERENCES watchlist_snapshots(id),
    FOREIGN KEY (prop_line_id) REFERENCES prop_lines(id),
    FOREIGN KEY (prediction_id) REFERENCES prop_predictions(id),
    UNIQUE(snapshot_id, prop_line_id)
);

CREATE TABLE IF NOT EXISTS prop_sync_jobs (
    id INTEGER PRIMARY KEY,
    scope TEXT NOT NULL,
    status TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    stage TEXT,
    stage_index INTEGER NOT NULL DEFAULT 0,
    stage_total INTEGER NOT NULL DEFAULT 1,
    current_count INTEGER NOT NULL DEFAULT 0,
    total_count INTEGER NOT NULL DEFAULT 0,
    percent REAL NOT NULL DEFAULT 0.0,
    message TEXT,
    last_error TEXT,
    last_result_json TEXT,
    target_game_ids_json TEXT NOT NULL DEFAULT '[]',
    updated_at TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_player_stats_player_game ON player_game_stats(player_id, game_id);
CREATE INDEX IF NOT EXISTS idx_team_results_team_game ON team_game_results(team_id, game_id);
CREATE INDEX IF NOT EXISTS idx_player_game_availability_game ON player_game_availability(game_id, player_id);
CREATE INDEX IF NOT EXISTS idx_prop_lines_game ON prop_lines(game_id);
CREATE INDEX IF NOT EXISTS idx_predictions_prop ON prop_predictions(prop_line_id);
CREATE INDEX IF NOT EXISTS idx_game_predictions_game ON game_predictions(game_id);
CREATE INDEX IF NOT EXISTS idx_settled_game_predictions_game ON settled_game_predictions(game_id);
CREATE INDEX IF NOT EXISTS idx_prop_sync_jobs_started_at ON prop_sync_jobs(started_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_model_runs_started ON model_runs(started_at);
CREATE INDEX IF NOT EXISTS idx_gem_snapshots_date_preset ON gem_snapshots(snapshot_date, preset);
CREATE INDEX IF NOT EXISTS idx_gem_snapshot_items_snapshot ON gem_snapshot_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_gem_snapshot_items_prop_line ON gem_snapshot_items(prop_line_id);
CREATE INDEX IF NOT EXISTS idx_watchlist_snapshots_date ON watchlist_snapshots(snapshot_date);
CREATE INDEX IF NOT EXISTS idx_watchlist_snapshot_items_snapshot ON watchlist_snapshot_items(snapshot_id);
CREATE INDEX IF NOT EXISTS idx_watchlist_snapshot_items_prop_line ON watchlist_snapshot_items(prop_line_id);
"""
