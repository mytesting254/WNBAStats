import json
import sqlite3

import pytest

from backend.app.scouting_snapshots import latest_snapshot_before, rebuild_snapshots


def _game(conn, cache_dir, game_id, date, phase, home_points, away_points):
    (cache_dir / f"espn_wnba_summary_{game_id}.json").write_text(json.dumps({
        "header": {"season": {"type": phase}, "gameNote": ""}
    }))
    conn.execute("INSERT INTO games VALUES (?, ?, ?, ?)", (game_id, date, date + "T19:00:00Z", game_id))
    for team_id, points, opponent_points in ((1, home_points, away_points), (2, away_points, home_points)):
        conn.execute("INSERT INTO team_game_results VALUES (?, ?, ?, ?, ?)",
                     (game_id, team_id, points, 80, "boxscore"))
        conn.execute("INSERT INTO team_game_boxscores VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                     (game_id, team_id, points, 30, 70, 8, 20, 16, 20, 10, 25, 12, 80, None, None))


def test_backfill_preserves_season_and_pregame_boundaries(tmp_path):
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.executescript("""
        CREATE TABLE games(id INTEGER, game_date TEXT, start_time TEXT, espn_event_id INTEGER);
        CREATE TABLE team_game_results(game_id INTEGER, team_id INTEGER, points INTEGER,
                                       possessions REAL, possessions_source TEXT);
        CREATE TABLE team_game_boxscores(game_id INTEGER, team_id INTEGER, points INTEGER,
            field_goals_made INTEGER, field_goals_attempted INTEGER, threes_made INTEGER,
            threes_attempted INTEGER, free_throws_made INTEGER, free_throws_attempted INTEGER,
            offensive_rebounds INTEGER, defensive_rebounds INTEGER, total_turnovers INTEGER,
            possessions REAL, turnovers INTEGER, team_turnovers INTEGER);
    """)
    _game(conn, tmp_path, 1, "2025-05-01", 2, 90, 80)
    _game(conn, tmp_path, 2, "2025-09-15", 3, 100, 90)
    _game(conn, tmp_path, 3, "2026-05-01", 2, 70, 60)
    result = rebuild_snapshots(conn, tmp_path)
    assert result["snapshots"] == 6
    after_regular = latest_snapshot_before(conn, team_id=1, season=2025, start_time="2025-09-15T19:00:00Z")
    assert after_regular["regular_games"] == 1
    assert after_regular["postseason_games"] == 0
    assert after_regular["offense"]["efficiency"] == pytest.approx(111.386, abs=0.001)
    assert after_regular["offense"]["off_reb_pct"] == pytest.approx(28.571, abs=0.001)
    after_playoff = latest_snapshot_before(conn, team_id=1, season=2025, start_time="2025-09-16T19:00:00Z")
    assert after_playoff["games_played"] == 2
    assert after_playoff["postseason_games"] == 1
    assert latest_snapshot_before(conn, team_id=1, season=2026, start_time="2026-05-01T19:00:00Z") is None
    assert rebuild_snapshots(conn, tmp_path) == result
    assert conn.execute("SELECT COUNT(*) FROM team_scouting_snapshots").fetchone()[0] == 6
