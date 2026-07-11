#!/usr/bin/env python3
"""Initialize the isolated in-house steals/blocks tracking database."""

import os
import sqlite3
from pathlib import Path


path = Path(os.environ.get("WNBA_STOCKS_TRACKING_DB", "data/stocks_tracking.sqlite"))
path.parent.mkdir(parents=True, exist_ok=True)
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
        CREATE INDEX IF NOT EXISTS idx_stocks_snapshots_game ON projection_snapshots(game_id, player_id);
        """
    )
print(path)
