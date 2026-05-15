from __future__ import annotations

import os
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any, Iterable, Sequence

import requests
from dotenv import load_dotenv

ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "wnba.sqlite"


def get_db_path() -> Path:
    return Path(os.getenv("WNBA_DB_PATH", DEFAULT_DB_PATH))


def get_turso_database_url() -> str | None:
    load_dotenv(ROOT_DIR / ".env")
    return os.getenv("TURSO_DATABASE_URL")


def using_turso() -> bool:
    return "WNBA_DB_PATH" not in os.environ


def connect() -> Any:
    if using_turso():
        database_url = get_turso_database_url()
        if not database_url:
            raise RuntimeError("TURSO_DATABASE_URL is required. Set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN in .env.")
        auth_token = os.getenv("TURSO_AUTH_TOKEN")
        if not auth_token:
            raise RuntimeError("TURSO_AUTH_TOKEN is required when TURSO_DATABASE_URL is set.")
        return TursoHttpConnection(database_url, auth_token)

    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    _configure_connection(conn)
    return conn


def _configure_connection(conn: Any) -> None:
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")


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

    def __init__(self, database_url: str, auth_token: str):
        self._pipeline_url = _http_pipeline_url(database_url)
        self._auth_token = auth_token
        self.row_factory = None

    def execute(self, sql: str, params: Sequence[Any] = ()) -> TursoHttpCursor:
        result = self._request([{"type": "execute", "stmt": _stmt(sql, params)}, {"type": "close"}])[0]
        return _cursor_from_result(result)

    def executemany(self, sql: str, seq_of_params: Iterable[Sequence[Any]]) -> TursoHttpCursor:
        last_cursor = TursoHttpCursor([], [])
        batch = []
        for params in seq_of_params:
            batch.append({"type": "execute", "stmt": _stmt(sql, params)})
            if len(batch) >= self.BATCH_SIZE:
                last_cursor = self._execute_batch(batch)
                batch = []
        if batch:
            last_cursor = self._execute_batch(batch)
        return last_cursor

    def _execute_batch(self, batch: list[dict]) -> TursoHttpCursor:
        results = self._request([*batch, {"type": "close"}])
        return _cursor_from_result(results[-1]) if results else TursoHttpCursor([], [])

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
        return None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def _request(self, requests_payload: list[dict]) -> list[dict]:
        response = requests.post(
            self._pipeline_url,
            headers={
                "Authorization": f"Bearer {self._auth_token}",
                "Content-Type": "application/json",
            },
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
            response = result.get("response", {})
            if "result" in response:
                results.append(response["result"])
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
        columns = {row["name"] for row in conn.execute("PRAGMA table_info(teams)").fetchall()}
        if "logo_url" not in columns:
            conn.execute("ALTER TABLE teams ADD COLUMN logo_url TEXT")
        player_columns = {row["name"] for row in conn.execute("PRAGMA table_info(players)").fetchall()}
        if "rotation_role" not in player_columns:
            conn.execute("ALTER TABLE players ADD COLUMN rotation_role TEXT DEFAULT 'starter'")
        game_columns = {row["name"] for row in conn.execute("PRAGMA table_info(games)").fetchall()}
        if "rest_days_home" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN rest_days_home INTEGER DEFAULT 2")
        if "rest_days_away" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN rest_days_away INTEGER DEFAULT 2")
        if "spread_home" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN spread_home REAL")
        if "game_total" not in game_columns:
            conn.execute("ALTER TABLE games ADD COLUMN game_total REAL")
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

CREATE INDEX IF NOT EXISTS idx_player_stats_player_game ON player_game_stats(player_id, game_id);
CREATE INDEX IF NOT EXISTS idx_team_results_team_game ON team_game_results(team_id, game_id);
CREATE INDEX IF NOT EXISTS idx_prop_lines_game ON prop_lines(game_id);
CREATE INDEX IF NOT EXISTS idx_predictions_prop ON prop_predictions(prop_line_id);
CREATE INDEX IF NOT EXISTS idx_game_predictions_game ON game_predictions(game_id);
CREATE INDEX IF NOT EXISTS idx_settled_game_predictions_game ON settled_game_predictions(game_id);
CREATE INDEX IF NOT EXISTS idx_model_runs_started ON model_runs(started_at);
"""
