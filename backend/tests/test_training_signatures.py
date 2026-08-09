from __future__ import annotations

import os
from datetime import datetime, timezone
from pathlib import Path

import pytest

from backend.app.bootstrap import ensure_teams
from backend.app.db import connect, init_db
from backend.app import dfs_model as dfs_model_module
from backend.app import game_training_db as game_training_db_module
from backend.app import minutes_training_db as minutes_training_db_module
from backend.app import player_half_training_db as player_half_training_db_module
from backend.app import player_prop_model as player_prop_model_module


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_DB", "true")
    monkeypatch.setenv("WNBA_DB_PATH", str(tmp_path / "wnba-test.sqlite"))
    monkeypatch.setenv("WNBA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("WNBA_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    monkeypatch.setenv("WNBA_TRAINING_DB_PATH", str(tmp_path / "wnba-training.sqlite"))
    monkeypatch.setenv("WNBA_PLAYER_HALF_TRAINING_DB_PATH", str(tmp_path / "wnba-player-half-training.sqlite"))
    init_db()
    with connect() as conn:
        ensure_teams(conn)
    yield


def test_game_training_signature_changes_after_possessions_repair() -> None:
    with connect() as conn:
        _seed_final_game(conn)
        first = game_training_db_module._source_signature(conn)
        conn.execute(
            """
            UPDATE team_game_results
            SET possessions = ?, possessions_source = ?
            WHERE game_id = ? AND team_id = ?
            """,
            (90.36, "espn_summary", 1001, 2),
        )
        second = game_training_db_module._source_signature(conn)

    assert second != first


def test_minutes_training_signature_changes_after_availability_update() -> None:
    observed_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        _seed_final_game(conn)
        conn.execute(
            """
            INSERT INTO players (id, full_name, team_id, position, rotation_role)
            VALUES (?, ?, ?, ?, ?)
            """,
            (9001, "Test Player", 2, "G", "starter"),
        )
        conn.execute(
            """
            INSERT INTO player_game_stats (
                player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9001, 1001, 31.0, 18, 4, 6, 2, 1, 0, 2),
        )
        first = minutes_training_db_module._source_signature(conn)
        conn.execute(
            """
            INSERT INTO player_game_availability (
                player_id, game_id, team_id, source, is_active, did_not_play, status_reason, minutes_text, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9001, 1001, 2, "espn_boxscore", 0, 1, "Coach decision", "DNP-CD", observed_at),
        )
        second = minutes_training_db_module._source_signature(conn)

    assert second != first


def test_player_model_fingerprint_changes_after_team_boxscore_ingest() -> None:
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        _seed_final_game(conn)
        first = player_prop_model_module._model_fingerprint(conn)
        conn.execute(
            """
            INSERT INTO team_game_boxscores (
                game_id, team_id, is_home, points, offensive_rebounds, turnovers,
                total_turnovers, field_goals_attempted, free_throws_attempted,
                possessions, source, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1001, 2, 1, 88, 9, 11, 12, 67, 18, 90.36, "espn_summary", captured_at),
        )
        second = player_prop_model_module._model_fingerprint(conn)

    assert second != first


def test_player_model_fingerprint_changes_after_game_market_repair() -> None:
    with connect() as conn:
        _seed_final_game(conn)
        first = player_prop_model_module._model_fingerprint(conn)
        conn.execute(
            "UPDATE games SET spread_home = ?, game_total = ? WHERE id = ?",
            (-5.5, 168.5, 1001),
        )
        second = player_prop_model_module._model_fingerprint(conn)

    assert second != first


def test_dfs_path_fingerprint_ignores_runtime_db_mtime() -> None:
    db_path = os.environ["WNBA_DB_PATH"]
    first = dfs_model_module._path_fingerprint(db_path)
    stat = os.stat(db_path)
    os.utime(db_path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000_000))
    second = dfs_model_module._path_fingerprint(db_path)

    assert second == first


def test_player_half_signature_uses_existing_training_metadata_when_source_table_missing() -> None:
    training_db_path = os.environ["WNBA_PLAYER_HALF_TRAINING_DB_PATH"]
    with player_half_training_db_module._connect_training_db(Path(training_db_path)) as training_conn:
        player_half_training_db_module._init_training_db(training_conn)
        training_conn.executemany(
            "INSERT OR REPLACE INTO player_half_training_metadata(key, value) VALUES(?, ?)",
            [
                ("source_signature", "cached-source"),
                ("player_rows", "10"),
                ("prop_rows", "20"),
                ("candidate_player_rows", "12"),
                ("candidate_prop_rows", "24"),
                ("built_at", "2026-08-09T10:00:00+00:00"),
                ("db_version", player_half_training_db_module.PLAYER_HALF_TRAINING_DB_VERSION),
            ],
        )
        training_conn.commit()
    with connect() as conn:
        conn.execute("DROP TABLE player_first_half_stats")
        signature = player_half_training_db_module.player_half_training_db_signature(conn, allow_rebuild=False)

    assert isinstance(signature, str)
    assert len(signature) == 16


def _seed_final_game(conn) -> None:
    conn.execute(
        """
        INSERT INTO games (
            id, game_date, start_time, home_team_id, away_team_id, status,
            rest_days_home, rest_days_away, spread_home, game_total
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (1001, "2026-07-15", "2026-07-15T19:00:00Z", 2, 12, "final", 2, 2, -3.5, 165.5),
    )
    conn.executemany(
        """
        INSERT INTO team_game_results (
            team_id, game_id, is_home, points, opponent_points, possessions,
            possessions_source, closing_spread, closing_total, ats_result, total_result
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (2, 1001, 1, 88, 82, 78.0, "fallback", -3.5, 165.5, "cover", "over"),
            (12, 1001, 0, 82, 88, 78.0, "fallback", 3.5, 165.5, "no_cover", "over"),
        ],
    )
    conn.commit()
