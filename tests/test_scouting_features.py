import json
import sqlite3

from backend.app.scouting_features import MATCHUP_FEATURE_NAMES, matchup_scouting_features, team_scouting_features


def test_scouting_features_use_only_prior_dates_and_current_season():
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("""
        CREATE TABLE team_scouting_snapshots (
            game_id INTEGER, team_id INTEGER, season INTEGER, game_date TEXT,
            start_time TEXT, games_played INTEGER, offense_json TEXT,
            defense_json TEXT, pace REAL
        )
    """)
    for game_id in range(1, 8):
        game_date = f"2026-05-{game_id:02d}"
        conn.execute("INSERT INTO team_scouting_snapshots VALUES (?, 1, 2026, ?, ?, ?, ?, ?, ?)", (
            game_id, game_date, f"{game_date}T19:00:00Z", game_id,
            json.dumps({"efficiency": 100 + game_id, "effective_fg_pct": 50 + game_id,
                        "turnover_pct": 12, "three_attempt_rate": 30}),
            json.dumps({"efficiency": 90 + game_id, "effective_fg_pct": 45 + game_id,
                        "turnover_pct": 14, "three_attempt_rate": 35}),
            95 + game_id,
        ))
    conn.execute("INSERT INTO team_scouting_snapshots VALUES (99, 2, 2026, '2026-05-06', '2026-05-06T20:00:00Z', 1, ?, ?, 94)", (
        json.dumps({"efficiency": 105}), json.dumps({"efficiency": 95}),
    ))

    before = team_scouting_features(conn, team_id=1, game_date="2026-05-07")
    assert before["games_played"] == 6
    assert before["off_efficiency"] == 106
    assert before["off_efficiency_last5_delta"] == 5
    assert before["def_efficiency_last5_delta"] == 5
    assert team_scouting_features(conn, team_id=1, game_date="2027-05-07")["available"] == 0

    matchup = matchup_scouting_features(conn, home_team_id=1, away_team_id=2, game_date="2026-05-07")
    assert tuple(matchup) == MATCHUP_FEATURE_NAMES
    assert matchup["scout_home_attack_vs_away_defense"] == 11
    assert matchup["scout_away_attack_vs_home_defense"] == 9


def test_scouting_features_handle_missing_table():
    conn = sqlite3.connect(":memory:")
    features = matchup_scouting_features(conn, home_team_id=1, away_team_id=2, game_date="2026-05-07")
    assert tuple(features) == MATCHUP_FEATURE_NAMES
    assert all(value == 0 for value in features.values())
