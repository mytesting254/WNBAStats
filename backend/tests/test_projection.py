from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app.bootstrap import ensure_teams
from backend.app.accuracy_analysis import build_accuracy_report, get_best_predictions, get_worst_predictions
from backend.app.db import connect, init_db
from backend.app.game_predictions import project_game
from backend.app.odds import american_to_implied_probability, expected_value
from backend.app.odds_import import RAW_CACHE_NAME, _merge_event_cache, import_the_odds_api_props, line_discrepancies
from backend.app.projections import rebuild_predictions
from backend.app.training import run_walk_forward_training


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("WNBA_DB_PATH", str(tmp_path / "wnba-test.sqlite"))
    init_db()
    with connect() as conn:
        ensure_teams(conn)
    yield


def load_test_history() -> None:
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.executemany(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            [
                (1001, "Breanna Stewart", 10, "F", "star"),
                (1002, "Sonia Citron", 13, "G", "starter"),
                (1003, "Test Guard", 3, "G", "starter"),
                (1004, "Washington Forward", 14, "F", "starter"),
            ],
        )
        stat_rows = []
        result_rows = []
        start = datetime(2026, 4, 1, 19, 0, tzinfo=timezone.utc)
        for idx in range(16):
            game_id = 100 + idx
            game_date = (start + timedelta(days=idx)).date().isoformat()
            start_time = (start + timedelta(days=idx)).isoformat()
            home_team_id = 10 if idx % 2 == 0 else 3
            away_team_id = 3 if idx % 2 == 0 else 10
            home_points = 82 + (idx % 7)
            away_points = 76 + (idx % 6)
            conn.execute(
                """
                INSERT INTO games (
                    id, game_date, start_time, home_team_id, away_team_id, status,
                    rest_days_home, rest_days_away, spread_home, game_total
                ) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, ?, ?)
                """,
                (game_id, game_date, start_time, home_team_id, away_team_id, -4.5, float(home_points + away_points)),
            )
            result_rows.extend(
                [
                    (home_team_id, game_id, 1, home_points, away_points, 78.0, -4.5, float(home_points + away_points), "push", "push"),
                    (away_team_id, game_id, 0, away_points, home_points, 78.0, 4.5, float(home_points + away_points), "push", "push"),
                ]
            )
            stat_rows.extend(
                [
                    (1001, game_id, 32 + (idx % 3), 19 + (idx % 8), 7 + (idx % 5), 3 + (idx % 3), 2, 1, 1, 2),
                    (1003, game_id, 30 + (idx % 4), 12 + (idx % 6), 4 + (idx % 3), 5 + (idx % 4), 1, 1, 0, 2),
                ]
            )

        for idx in range(12):
            game_id = 200 + idx
            game_date = (start + timedelta(days=idx)).date().isoformat()
            start_time = (start + timedelta(days=idx)).isoformat()
            conn.execute(
                """
                INSERT INTO games (
                    id, game_date, start_time, home_team_id, away_team_id, status,
                    rest_days_home, rest_days_away, spread_home, game_total
                ) VALUES (?, ?, ?, 13, 14, 'final', 2, 2, ?, ?)
                """,
                (game_id, game_date, start_time, -2.5, 159.5),
            )
            result_rows.extend(
                [
                    (13, game_id, 1, 80 + (idx % 5), 75 + (idx % 4), 77.0, -2.5, 159.5, "push", "push"),
                    (14, game_id, 0, 75 + (idx % 4), 80 + (idx % 5), 77.0, 2.5, 159.5, "push", "push"),
                ]
            )
            stat_rows.extend(
                [
                    (1002, game_id, 31 + (idx % 3), 14 + (idx % 5), 4 + (idx % 3), 3 + (idx % 4), 1, 1, 0, 2),
                    (1004, game_id, 29 + (idx % 3), 11 + (idx % 5), 6 + (idx % 3), 2 + (idx % 2), 0, 1, 1, 2),
                ]
            )

        conn.executemany(
            """
            INSERT INTO team_game_results (
                team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            result_rows,
        )
        conn.executemany(
            """
            INSERT INTO player_game_stats (
                player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            stat_rows,
        )
        scheduled_games = [
            (2010, "2026-05-08", "2026-05-08T23:30:00Z", 10, 3, "scheduled", 2, 2, -5.5, 164.5),
            (2020, "2026-05-08", "2026-05-08T23:30:00Z", 13, 14, "scheduled", 2, 2, -3.5, 158.5),
        ]
        conn.executemany(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            scheduled_games,
        )
        conn.executemany(
            """
            INSERT INTO prop_lines (
                game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (2010, 1001, "DraftKings", "points", 21.5, -110, -110, captured_at),
                (2010, 1001, "FanDuel", "rebounds", 8.5, -115, -105, captured_at),
                (2020, 1002, "DraftKings", "assists", 2.5, -145, 114, captured_at),
            ],
        )
        rebuild_predictions(conn)


def test_american_odds_helpers() -> None:
    assert round(american_to_implied_probability(-110), 4) == 0.5238
    assert round(american_to_implied_probability(150), 4) == 0.4
    assert expected_value(0.55, -110) > 0


def test_fixture_builds_ranked_predictions() -> None:
    load_test_history()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT pp.*, pl.market
            FROM prop_predictions pp
            JOIN prop_lines pl ON pl.id = pp.prop_line_id
            ORDER BY pp.expected_value DESC
            """
        ).fetchall()
    assert len(rows) == 3
    assert rows[0]["recommended_side"] in {"over", "under"}
    assert rows[0]["projection"] > 0
    assert rows[0]["market"] in {"points", "rebounds", "assists"}


def test_fixture_builds_matchup_results() -> None:
    load_test_history()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM team_game_results").fetchall()
        scheduled = conn.execute("SELECT * FROM games WHERE status = 'scheduled'").fetchall()
    assert len(rows) == 56
    assert len(scheduled) == 2


def test_walk_forward_training_saves_model_run() -> None:
    load_test_history()
    with connect() as conn:
        result = run_walk_forward_training(conn)
        rows = conn.execute("SELECT * FROM model_runs").fetchall()
    assert result["status"] == "completed"
    assert result["training_rows"] > 0
    assert len(rows) == 2
    assert {row["model_version"] for row in rows} == {"adaptive-context-v1", "component-pregame-v2"}


def test_accuracy_analysis_uses_completed_player_stats() -> None:
    load_test_history()
    with connect() as conn:
        captured_at = datetime.now(timezone.utc).isoformat()
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9001, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9001, 'adaptive-context-v1', ?, 18.0, 'over', 0.56, 0.52, 0.04, 0.07, 'medium', 'test')
            """,
            (captured_at,),
        )
        report = build_accuracy_report(conn, "adaptive-context-v1")
        best = get_best_predictions(conn, model_version="adaptive-context-v1")
        worst = get_worst_predictions(conn, model_version="adaptive-context-v1")

    assert report.total_predictions == 1
    assert report.mae == 1.0
    assert report.market_breakdown["points"]["predictions"] == 1
    assert best[0].actual_result == 19.0
    assert best[0].correct_side is False
    assert worst[0].abs_error == 1.0


def test_game_projection_returns_picks() -> None:
    load_test_history()
    with connect() as conn:
        game = conn.execute(
            """
            SELECT
                g.*,
                home.abbreviation AS home_team,
                away.abbreviation AS away_team
            FROM games g
            JOIN teams home ON home.id = g.home_team_id
            JOIN teams away ON away.id = g.away_team_id
            WHERE g.status = 'scheduled'
            ORDER BY g.start_time
            LIMIT 1
            """
        ).fetchone()
        result = project_game(conn, game)
    assert result["winner_pick"] in {"NY", "CON"}
    assert result["projected_total"] > 0
    assert result["ats_pick"] != "N/A"


def test_odds_import_requires_api_key(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    monkeypatch.setattr("backend.app.odds_import.read_json_cache", lambda _: None)
    monkeypatch.setattr("backend.app.odds_import.load_dotenv", lambda: None)
    load_test_history()
    with connect() as conn:
        result = import_the_odds_api_props(conn)
    assert result["status"] == "missing_api_key"
    assert result["imported"] == 0


def test_odds_import_loads_saved_json_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    load_test_history()
    monkeypatch.setattr(
        "backend.app.odds_import.read_json_cache",
        lambda name: [
            {
                "id": "cached-event",
                "commence_time": "2026-05-08T23:30:00Z",
                "home_team": "New York Liberty",
                "away_team": "Connecticut Sun",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "player_points",
                                "outcomes": [
                                    {"name": "Over", "description": "Breanna Stewart", "price": -110, "point": 21.5},
                                    {"name": "Under", "description": "Breanna Stewart", "price": -110, "point": 21.5},
                                ],
                            }
                        ],
                    }
                ],
            }
        ] if name == RAW_CACHE_NAME else None,
    )
    with connect() as conn:
        result = import_the_odds_api_props(conn)
        rows = conn.execute("SELECT * FROM sportsbook_prop_lines").fetchall()
    assert result["status"] == "loaded_from_cache"
    assert result["source"] == "cache"
    assert result["imported"] == 2
    assert len(rows) == 2


def test_odds_import_syncs_model_prop_lines_from_sportsbook(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    load_test_history()
    monkeypatch.setattr(
        "backend.app.odds_import.read_json_cache",
        lambda name: [
            {
                "id": "cached-event",
                "commence_time": "2026-05-08T23:30:00Z",
                "home_team": "Toronto Tempo",
                "away_team": "Washington Mystics",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "player_assists",
                                "outcomes": [
                                    {"name": "Over", "description": "Sonia Citron", "price": -145, "point": 2.5},
                                    {"name": "Under", "description": "Sonia Citron", "price": 114, "point": 2.5},
                                ],
                            }
                        ],
                    }
                ],
            }
        ] if name == RAW_CACHE_NAME else None,
    )
    with connect() as conn:
        result = import_the_odds_api_props(conn)
        row = conn.execute(
            """
            SELECT pl.*
            FROM prop_lines pl
            JOIN players p ON p.id = pl.player_id
            WHERE p.full_name = 'Sonia Citron'
              AND pl.market = 'assists'
            """
        ).fetchone()
    assert result["synced_props"] == 1
    assert row["line"] == 2.5
    assert row["over_odds"] == -145
    assert row["under_odds"] == 114


def test_odds_refresh_merges_cached_future_events() -> None:
    cached = [
        {"id": "future-a", "commence_time": "2026-05-10T00:00:00Z", "bookmakers": []},
        {"id": "replace-me", "commence_time": "2026-05-09T00:00:00Z", "bookmakers": [{"key": "old"}]},
    ]
    fetched = [
        {"id": "replace-me", "commence_time": "2026-05-09T00:00:00Z", "bookmakers": [{"key": "new"}]},
        {"id": "new-event", "commence_time": "2026-05-08T23:30:00Z", "bookmakers": []},
    ]
    merged = _merge_event_cache(cached, fetched)
    by_id = {event["id"]: event for event in merged}
    assert set(by_id) == {"future-a", "replace-me", "new-event"}
    assert by_id["replace-me"]["bookmakers"][0]["key"] == "new"


def test_line_discrepancies_group_books() -> None:
    load_test_history()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("test", "evt", 2010, "2026-05-08", "2026-05-08T19:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Test Player", "over", 18.5, -110, "now"),
                ("test", "evt", 2010, "2026-05-08", "2026-05-08T19:30:00Z", "New York Liberty", "Connecticut Sun", "fd", "FanDuel", "player_points", "points", "Test Player", "over", 19.5, +105, "now"),
            ],
        )
        rows = line_discrepancies(conn, 2010)
    assert rows[0]["player_name"] == "Test Player"
    assert rows[0]["line_gap"] == 1.0
    assert rows[0]["best_price"]["sportsbook"] == "FanDuel"
