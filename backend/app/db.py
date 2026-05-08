from __future__ import annotations

import os
import sqlite3
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[2]
DEFAULT_DB_PATH = ROOT_DIR / "data" / "wnba.sqlite"


def get_db_path() -> Path:
    return Path(os.getenv("WNBA_DB_PATH", DEFAULT_DB_PATH))


def connect() -> sqlite3.Connection:
    db_path = get_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


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
    settled_at TEXT NOT NULL,
    FOREIGN KEY (prop_line_id) REFERENCES prop_lines(id)
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
CREATE INDEX IF NOT EXISTS idx_model_runs_started ON model_runs(started_at);
"""
