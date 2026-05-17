from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.app import covers_import as covers_import_module
from backend.app.bootstrap import ensure_teams
from backend.app.accuracy_analysis import build_accuracy_report, get_best_predictions, get_worst_predictions
from backend.app.covers_import import CoversGame, _event_rows, _metadata_from_page, _records_from_page
from backend.app.db import connect, init_db
from backend.app.espn_history import import_espn_player_boxscores, import_espn_scoreboard
from backend.app.game_prediction_tracking import save_game_prediction, settle_completed_game_predictions
from backend.app.game_predictions import project_game
from backend.app.history_import import determine_ats_result
from backend.app.main import app, import_espn_history as import_espn_history_endpoint, model_performance
from backend.app.odds import american_to_implied_probability, expected_value
from backend.app.odds_import import RAW_CACHE_NAME, _merge_event_cache, import_the_odds_api_props, line_discrepancies, sync_prop_lines_from_sportsbook
from backend.app.player_prop_model import _market_value as learned_market_value
from backend.app.player_prop_model import train_market_model
from backend.app.projections import _market_value as component_market_value
from backend.app.projections import rebuild_predictions
from backend.app.settlement import settle_completed_props
from backend.app.training import run_walk_forward_training


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_DB", "true")
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


def test_ats_result_uses_home_spread_sign() -> None:
    assert determine_ats_result(82, 78, -5.5) == "no_cover"
    assert determine_ats_result(82, 78, -4.0) == "push"
    assert determine_ats_result(82, 78, -3.5) == "cover"
    assert determine_ats_result(78, 82, 5.5) == "cover"
    assert determine_ats_result(78, 82, 3.5) == "no_cover"


def test_covers_import_route_is_registered() -> None:
    methods_by_path = {route.path: getattr(route, "methods", set()) for route in app.routes}
    assert "POST" in methods_by_path["/api/covers/import"]


def test_espn_history_settles_before_rebuilding_prop_lines(monkeypatch) -> None:
    calls: list[str] = []

    def fake_scoreboard(conn, season, force_refresh=False, selected_date=None):
        assert selected_date is not None
        return {"season": season}

    def fake_settle(conn):
        calls.append("settle")
        return {"settled": 1}

    def fake_sync(conn):
        calls.append("sync")
        return 0

    def fake_rebuild(conn):
        calls.append("rebuild")
        return [object()]

    monkeypatch.setattr("backend.app.main.import_espn_scoreboard", fake_scoreboard)
    monkeypatch.setattr("backend.app.main.settle_completed_props", fake_settle)
    monkeypatch.setattr("backend.app.main.sync_prop_lines_from_sportsbook", fake_sync)
    monkeypatch.setattr("backend.app.main.rebuild_predictions", fake_rebuild)

    result = import_espn_history_endpoint(season=2026, include_player_stats=False, include_previous_season=False)

    assert calls == ["settle", "sync", "rebuild"]
    assert result["settlements"] == {"settled": 1}
    assert result["predictions"] == 1
    assert result["selected_date"] is not None


def test_espn_history_accepts_batch_dates(monkeypatch) -> None:
    scoreboard_dates: list[tuple[int, str | None]] = []
    boxscore_dates: list[tuple[int, str | None]] = []

    def fake_scoreboard(conn, season, force_refresh=False, selected_date=None):
        scoreboard_dates.append((season, selected_date))
        return {"season": season, "selected_date": selected_date}

    def fake_boxscores(conn, season, force_refresh=False, missing_only=False, selected_date=None):
        boxscore_dates.append((season, selected_date))
        return {"season": season, "selected_date": selected_date}

    monkeypatch.setattr("backend.app.main.import_espn_scoreboard", fake_scoreboard)
    monkeypatch.setattr("backend.app.main.import_espn_player_boxscores", fake_boxscores)
    monkeypatch.setattr("backend.app.main.settle_completed_props", lambda conn: {"settled": 0})
    monkeypatch.setattr("backend.app.main.settle_completed_game_predictions", lambda conn: {"settled": 0})
    monkeypatch.setattr("backend.app.main.sync_prop_lines_from_sportsbook", lambda conn: 0)
    monkeypatch.setattr("backend.app.main.rebuild_predictions", lambda conn: [])

    result = import_espn_history_endpoint(
        season=2026,
        force_refresh=True,
        selected_dates=["2026-05-14", "2026-05-15, 2026-05-14"],
    )

    assert result["selected_date"] is None
    assert result["selected_dates"] == ["2026-05-14", "2026-05-15"]
    assert scoreboard_dates == [(2026, "2026-05-14"), (2026, "2026-05-15")]
    assert boxscore_dates == [(2026, "2026-05-14"), (2026, "2026-05-15")]


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


def test_train_market_model_uses_active_non_sqlite_connection(monkeypatch) -> None:
    class FakeRemoteConnection:
        pass

    calls = []

    def fake_uncached(conn, market):
        calls.append((conn, market))
        return None

    monkeypatch.setattr("backend.app.player_prop_model._train_market_model_uncached", fake_uncached)

    conn = FakeRemoteConnection()
    result = train_market_model(conn, "points")

    assert result is None
    assert calls == [(conn, "points")]


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


def test_settle_completed_props_writes_actual_results() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9101, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        first = settle_completed_props(conn)
        second = settle_completed_props(conn)
        row = conn.execute("SELECT * FROM settled_props WHERE prop_line_id = 9101").fetchone()

    assert first["settled"] == 1
    assert second["settled"] == 0
    assert row["actual_result"] == 19.0
    assert row["winning_side"] == "under"
    assert row["margin"] == -1.5
    assert row["player_minutes"] == 32.0
    assert row["game_margin"] == 6.0
    assert row["team_margin"] == 6.0
    assert row["team_spread"] == -4.5
    assert row["blowout_result"] == "no"


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


def test_game_predictions_are_saved_and_settled() -> None:
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
            WHERE g.id = 2010
            """
        ).fetchone()
        prediction = project_game(conn, game)
        prediction_id = save_game_prediction(conn, game, prediction)
        saved = conn.execute("SELECT * FROM game_predictions WHERE id = ?", (prediction_id,)).fetchone()

        conn.execute("UPDATE games SET status = 'final' WHERE id = 2010")
        conn.executemany(
            """
            INSERT INTO team_game_results (
                team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, 2010, ?, ?, ?, 78.0, ?, ?, 'push', 'push')
            """,
            [
                (10, 1, 82, 76, -5.5, 164.5),
                (3, 0, 76, 82, 5.5, 164.5),
            ],
        )
        result = settle_completed_game_predictions(conn)
        settled = conn.execute("SELECT * FROM settled_game_predictions WHERE game_prediction_id = ?", (prediction_id,)).fetchone()

    assert saved["winner_pick"] in {"NY", "CON"}
    assert saved["total_pick"] in {"Over", "Under"}
    assert result["settled"] == 1
    assert settled["actual_winner"] == "NY"
    assert settled["actual_margin"] == 6.0
    assert settled["actual_total"] == 158.0
    assert settled["actual_ats_pick"] == "NY"
    assert settled["actual_total_result"] == "Under"


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


def test_odds_sync_prefers_covers_lines_when_available() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines")
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("the_odds_api", "odds-api-event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "draftkings", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 21.5, -110, captured_at),
                ("the_odds_api", "odds-api-event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "draftkings", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 21.5, -110, captured_at),
                ("covers", "covers-event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "draftkings", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 20.5, -105, captured_at),
                ("covers", "covers-event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "draftkings", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 20.5, -115, captured_at),
            ],
        )

        synced = sync_prop_lines_from_sportsbook(conn)
        rows = conn.execute(
            """
            SELECT pl.*
            FROM prop_lines pl
            JOIN players p ON p.id = pl.player_id
            WHERE p.full_name = 'Breanna Stewart'
              AND pl.market = 'points'
            ORDER BY pl.line
            """
        ).fetchall()

    assert synced == 1
    assert len(rows) == 1
    assert rows[0]["line"] == 20.5
    assert rows[0]["over_odds"] == -105
    assert rows[0]["under_odds"] == -115


def test_odds_sync_keeps_one_model_line_with_best_available_odds() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines")
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("the_odds_api", "event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "draftkings", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 21.5, -115, captured_at),
                ("the_odds_api", "event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "draftkings", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 21.5, -105, captured_at),
                ("the_odds_api", "event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "fanduel", "FanDuel", "player_points", "points", "Breanna Stewart", "over", 21.5, +100, captured_at),
                ("the_odds_api", "event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "fanduel", "FanDuel", "player_points", "points", "Breanna Stewart", "under", 21.5, -120, captured_at),
                ("the_odds_api", "event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "caesars", "Caesars", "player_points", "points", "Breanna Stewart", "over", 22.5, +110, captured_at),
                ("the_odds_api", "event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "caesars", "Caesars", "player_points", "points", "Breanna Stewart", "under", 22.5, -130, captured_at),
            ],
        )

        synced = sync_prop_lines_from_sportsbook(conn)
        rows = conn.execute(
            """
            SELECT pl.*
            FROM prop_lines pl
            JOIN players p ON p.id = pl.player_id
            WHERE p.full_name = 'Breanna Stewart'
              AND pl.market = 'points'
            ORDER BY pl.line
            """
        ).fetchall()

    assert synced == 2
    assert len(rows) == 2
    assert rows[0]["sportsbook"] == "Best Available"
    assert rows[0]["line"] == 21.5
    assert rows[0]["over_odds"] == 100
    assert rows[0]["under_odds"] == -105
    assert rows[1]["sportsbook"] == "Caesars"
    assert rows[1]["line"] == 22.5


def test_odds_sync_preserves_settled_prop_lines() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9201, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        settlements = settle_completed_props(conn)
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("test", "evt", 100, "2026-04-01", "2026-04-01T19:00:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 20.5, -110, captured_at),
                ("test", "evt", 100, "2026-04-01", "2026-04-01T19:00:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 20.5, -110, captured_at),
            ],
        )

        synced = sync_prop_lines_from_sportsbook(conn)
        matching_lines = conn.execute(
            """
            SELECT pl.id
            FROM prop_lines pl
            WHERE pl.game_id = 100
              AND pl.player_id = 1001
              AND pl.sportsbook = 'DraftKings'
              AND pl.market = 'points'
              AND pl.line = 20.5
            """
        ).fetchall()
        settled = conn.execute("SELECT * FROM settled_props WHERE prop_line_id = 9201").fetchone()

    assert settlements["settled"] >= 1
    assert synced == 1
    assert [row["id"] for row in matching_lines] == [9201]
    assert settled is not None


def test_odds_sync_preserves_tracking_for_completed_unsettled_props() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9301, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9301, 'adaptive-context-v1', 'pregame', 18.0, 'over', 0.56, 0.52, 0.04, 0.07, 'medium', 'test')
            """
        )
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("test", "evt", 100, "2026-04-01", "2026-04-01T19:00:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 20.5, -110, captured_at),
                ("test", "evt", 100, "2026-04-01", "2026-04-01T19:00:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 20.5, -110, captured_at),
            ],
        )

        sync_prop_lines_from_sportsbook(conn)
        line = conn.execute("SELECT * FROM prop_lines WHERE id = 9301").fetchone()
        prediction = conn.execute("SELECT * FROM prop_predictions WHERE prop_line_id = 9301").fetchone()
        matching_lines = conn.execute(
            """
            SELECT id
            FROM prop_lines
            WHERE game_id = 100
              AND player_id = 1001
              AND sportsbook = 'DraftKings'
              AND market = 'points'
              AND line = 20.5
            ORDER BY id
            """
        ).fetchall()

    assert line is not None
    assert prediction["prediction_time"] == "pregame"
    assert [row["id"] for row in matching_lines] == [9301]


def test_rebuild_predictions_does_not_overwrite_completed_game_tracking() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9401, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9401, 'adaptive-context-v1', 'pregame', 18.0, 'over', 0.56, 0.52, 0.04, 0.07, 'medium', 'test')
            """
        )

        projections = rebuild_predictions(conn)
        prediction = conn.execute("SELECT * FROM prop_predictions WHERE prop_line_id = 9401").fetchone()

    assert len(projections) == 3
    assert prediction["prediction_time"] == "pregame"
    assert prediction["projection"] == 18.0


def test_model_performance_counts_settled_props_without_predictions() -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)"
            , (2001, "Test Player", 10, "G", "starter")
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, ?, ?)"
            , (20001, "2026-05-01", "2026-05-01T19:00:00Z", 10, 3, -3.5, 149.5)
        )
        conn.execute(
            "INSERT INTO team_game_results (team_id, game_id, is_home, points, opponent_points, possessions, closing_spread, closing_total, ats_result, total_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            , (10, 20001, 1, 75, 70, 78.0, -3.5, 149.5, "cover", "under")
        )
        conn.execute(
            "INSERT INTO player_game_stats (player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
            , (2001, 20001, 34, 24, 6, 5, 1, 2, 0, 1)
        )
        conn.execute(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
            , (20001, 20001, 2001, 'DraftKings', 'points', 23.5, -110, -110, datetime.now(timezone.utc).isoformat())
        )
        settled = settle_completed_props(conn)

    assert settled["settled"] == 1
    performance = model_performance()
    assert performance["settled"] == 0
    assert performance["win_rate"] is None
    assert performance["average_ev"] is None
    assert "no matching model predictions" in performance["message"].lower()


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


def test_covers_parser_extracts_player_prop_rows() -> None:
    html = """
    <script type="application/ld+json">
    {"startDate": "05/10/2026 17:00:00 &#x2B;00:00"}
    </script>
    <article id="901">
      <h2 class="fs-9">Points Scored</h2>
      <div class="playerContainer">
        <span class="category fw-bold text-nowrap">Brittney Griner</span>
      </div>
      <caption class="visually-hidden">Game Odds Seattle Storm vs. Connecticut Sun</caption>
      <a data-linkcont="matchup-odds-compare_odds-click-wnba-points_scored-over-bet365">
        <span class="fw-bold fs-12">o13.5</span>
        <span class="fw-bold americanOdds fs-13">&#x2B;102</span>
      </a>
      <a data-linkcont="matchup-odds-compare_odds-click-wnba-points_scored-under-bet365">
        <span class="fw-bold fs-12">u13.5</span>
        <span class="fw-bold americanOdds fs-13">-130</span>
      </a>
    </article>
    """
    with connect() as conn:
        metadata = _metadata_from_page(conn, CoversGame("373849", "https://example.test/odds"), html)
        rows = _event_rows(metadata, html, "2026-05-10T12:00:00+00:00")

    assert metadata.away_team == "Seattle Storm"
    assert metadata.home_team == "Connecticut Sun"
    assert metadata.commence_time == "2026-05-10T17:00:00+00:00"
    assert len(rows) == 2
    assert rows[0][0] == "covers"
    assert rows[0][8] == "bet365"
    assert rows[0][10] == "points"
    assert rows[0][11] == "Brittney Griner"
    assert rows[0][12] == "over"
    assert rows[0][13] == 13.5
    assert rows[0][14] == 102
    assert rows[1][12] == "under"
    assert rows[1][14] == -130


def test_covers_parser_extracts_matchup_line_and_total() -> None:
    html = """
    <script type="application/ld+json">
    {"startDate": "05/10/2026 20:30:00 &#x2B;00:00", "name": "Phoenix Mercury vs Golden State Valkyries"}
    </script>
    <section>
      <h2>PHO vs GS Records</h2>
      <div>Team Spread Total ML</div>
      <div>PHO +2.5 -105 o158.5 -110 +125</div>
      <div>GS -2.5 -115 u158.5 -110 -150</div>
    </section>
    """
    with connect() as conn:
        metadata = _metadata_from_page(conn, CoversGame("373853", "https://example.test/odds"), html)
        game = conn.execute("SELECT * FROM games WHERE id = ?", (metadata.game_id,)).fetchone()

    assert metadata.spread_home == -2.5
    assert metadata.game_total == 158.5
    assert game["spread_home"] == -2.5
    assert game["game_total"] == 158.5


def test_covers_metadata_prefers_odds_page_market_over_matchup_page_noise() -> None:
    matchup_html = """
    <script type="application/ld+json">
    {"startDate": "05/17/2026 17:30:00 &#x2B;00:00", "name": "Las Vegas Aces vs Atlanta Dream"}
    </script>
    <section>
      <div>LV o14.5 -160</div>
      <div>ATL u15.5 -105</div>
    </section>
    """
    odds_html = """
    <script type="application/ld+json">
    {"startDate": "05/17/2026 17:30:00 &#x2B;00:00", "name": "Las Vegas Aces vs Atlanta Dream"}
    </script>
    <section>
      <h2>LV vs ATL Game Odds</h2>
      <div>Team Spread Total ML</div>
      <div>LV -2.5 -110 o172.5 -110 -155</div>
      <div>ATL +2.5 -110 u172.5 -110 +130</div>
    </section>
    """
    with connect() as conn:
        metadata = _metadata_from_page(
            conn,
            CoversGame("373868", "https://example.test/odds"),
            matchup_html,
            fallback_page=odds_html,
        )

    assert metadata.spread_home == 2.5
    assert metadata.game_total == 172.5


def test_covers_records_parser_extracts_h2h_and_team_last_10() -> None:
    html = """
    <section class="both-team-section">
      <table class="last-10-table">
        <caption>Head-To-Head</caption>
        <tbody>
          <tr>
            <td>Aug 27, &#x27;25</td>
            <td>ATL</td>
            <td><img alt="Las Vegas logo" /> <a>81 - 75</a></td>
            <td><b>LV</b> 3.5</td>
            <td>u162.5</td>
          </tr>
        </tbody>
      </table>
    </section>
    <section class="away-team-section">
      <table class="last-10-table">
        <caption>Team - Last 10</caption>
        <tbody>
          <tr>
            <td>May 15, &#x27;26</td>
            <td><span class="last-10-team"><span>@</span><a>CON</a></span></td>
            <td><b>W </b><a>101 - 94</a></td>
            <td><b>L</b> -14.5</td>
            <td>o172.5</td>
          </tr>
        </tbody>
      </table>
    </section>
    <section class="home-team-section">
      <table class="last-10-table">
        <caption>Team - Last 10</caption>
        <tbody>
          <tr>
            <td>May 12, &#x27;26</td>
            <td><span class="last-10-team"><span></span><a>DAL</a></span></td>
            <td><b>W </b><a>77 - 72</a></td>
            <td><b>W</b> -1.5</td>
            <td>u180.5</td>
          </tr>
        </tbody>
      </table>
    </section>
    """

    records = _records_from_page(html)

    assert records["head_to_head"][0] == {
        "date": "Aug 27, '25",
        "home": "ATL",
        "winner": "Las Vegas",
        "score": "81 - 75",
        "ats": "LV 3.5",
        "total": "u162.5",
    }
    assert records["away_last_10"][0] == {
        "date": "May 15, '26",
        "opponent": "CON",
        "location": "away",
        "result": "W",
        "score": "101 - 94",
        "ats": "L -14.5",
        "total": "o172.5",
    }
    assert records["home_last_10"][0]["location"] == "home"


def test_covers_import_keeps_cached_rows_when_fresh_scrape_returns_no_rows(monkeypatch) -> None:
    cached_payload = {
        "provider": "covers",
        "rows": [{"provider_event_id": "saved-row"}],
        "games": [{"game_id": 1, "records": {"head_to_head": [], "away_last_10": [], "home_last_10": []}}],
    }

    monkeypatch.setattr(covers_import_module, "read_json_cache", lambda name: cached_payload)
    monkeypatch.setattr(
        covers_import_module,
        "covers_matchup_links",
        lambda selected_date=None: [CoversGame("373868", "https://example.test/odds", "https://example.test/matchup")],
    )

    def raise_timeout(url: str) -> str:
        raise TimeoutError("slow covers response")

    monkeypatch.setattr(covers_import_module, "_fetch_text", raise_timeout)
    monkeypatch.setattr(
        covers_import_module,
        "_replace_covers_rows",
        lambda conn, rows, games=None: {"events": 1, "imported": len(rows), "captured_at": "saved"},
    )
    monkeypatch.setattr(covers_import_module, "sync_prop_lines_from_sportsbook", lambda conn: 4)

    with connect() as conn:
        result = covers_import_module.import_covers_props(conn, force_refresh=True)

    assert result["status"] == "loaded_from_cache"
    assert result["source"] == "cache"
    assert result["imported"] == 1
    assert result["synced_props"] == 4
    assert result["errors"][0]["event_id"] == "373868"


def test_espn_boxscore_missing_only_skips_games_with_stats(monkeypatch) -> None:
    load_test_history()
    fetched_game_ids: list[int] = []

    def fake_fetch_summary(game_id: int, force_refresh: bool = False) -> dict:
        fetched_game_ids.append(game_id)
        return {"boxscore": {"players": []}}

    monkeypatch.setattr("backend.app.espn_history.fetch_summary", fake_fetch_summary)

    with connect() as conn:
        result = import_espn_player_boxscores(conn, 2026, missing_only=True)

    assert result["missing_only"] is True
    assert result["games_checked"] == 0
    assert fetched_game_ids == []


def test_espn_boxscore_uses_mapped_event_id(monkeypatch) -> None:
    fetched_game_ids: list[int] = []

    def fake_fetch_summary(game_id: int, force_refresh: bool = False) -> dict:
        fetched_game_ids.append(game_id)
        return {
            "boxscore": {
                "players": [
                    {
                        "team": {"abbreviation": "CON"},
                        "statistics": [
                            {
                                "labels": ["MIN", "PTS", "REB", "AST", "3PT", "STL", "BLK", "TO"],
                                "athletes": [
                                    {
                                        "athlete": {
                                            "id": "3001",
                                            "displayName": "Mapped Player",
                                            "position": {"abbreviation": "G"},
                                        },
                                        "starter": True,
                                        "stats": ["31", "18", "5", "4", "2-5", "1", "0", "2"],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            }
        }

    monkeypatch.setattr("backend.app.espn_history.fetch_summary", fake_fetch_summary)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (2232, '2026-05-10', '2026-05-10T17:00Z', 13, 3, 'final', 2, 2, 1.5, 171.0, 401999999)
            """
        )
        result = import_espn_player_boxscores(conn, 2026, missing_only=True)
        stat = conn.execute("SELECT * FROM player_game_stats WHERE game_id = 2232 AND player_id = 3001").fetchone()

    assert fetched_game_ids == [401999999]
    assert result["inserted_player_game_stats"] == 1
    assert stat["points"] == 18


def test_espn_scoreboard_imports_scheduled_games(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.espn_history.fetch_scoreboard",
        lambda season, force_refresh=False, selected_date=None: {
            "events": [
                {
                    "id": "777001",
                    "date": "2026-06-01T23:00:00Z",
                    "competitions": [
                        {
                            "status": {"type": {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False}},
                            "competitors": [
                                {"homeAway": "home", "score": "", "team": {"abbreviation": "NY", "displayName": "New York Liberty"}},
                                {"homeAway": "away", "score": "", "team": {"abbreviation": "CONN", "displayName": "Connecticut Sun"}},
                            ],
                        }
                    ],
                }
            ]
        },
    )

    with connect() as conn:
        result = import_espn_scoreboard(conn, 2026)
        game = conn.execute("SELECT * FROM games WHERE id = 777001").fetchone()
        result_rows = conn.execute("SELECT * FROM team_game_results WHERE game_id = 777001").fetchall()

    assert result["inserted_games"] == 1
    assert result["inserted_team_game_results"] == 0
    assert game["status"] == "scheduled"
    assert game["espn_event_id"] == 777001
    assert len(result_rows) == 0


def test_espn_scoreboard_preserves_covers_total_for_scheduled_zero_score(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.espn_history.fetch_scoreboard",
        lambda season, force_refresh=False, selected_date=None: {
            "events": [
                {
                    "id": "777002",
                    "date": "2026-06-01T23:00:00Z",
                    "competitions": [
                        {
                            "status": {"type": {"name": "STATUS_SCHEDULED", "state": "pre", "completed": False}},
                            "competitors": [
                                {"homeAway": "home", "score": "0", "team": {"abbreviation": "NY", "displayName": "New York Liberty"}},
                                {"homeAway": "away", "score": "0", "team": {"abbreviation": "CONN", "displayName": "Connecticut Sun"}},
                            ],
                        }
                    ],
                }
            ]
        },
    )

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (777002, '2026-06-01', '2026-06-01T23:00:00Z', 10, 3, 'scheduled', 2, 2, -4.5, 166.5)
            """
        )
        import_espn_scoreboard(conn, 2026)
        game = conn.execute("SELECT * FROM games WHERE id = 777002").fetchone()

    assert game["status"] == "scheduled"
    assert game["game_total"] == 166.5


def test_defensive_markets_are_projectable() -> None:
    load_test_history()
    with connect() as conn:
        row = conn.execute("SELECT * FROM player_game_stats WHERE player_id = 1001 LIMIT 1").fetchone()
    assert component_market_value(row, "steals") == 1.0
    assert component_market_value(row, "blocks") == 1.0
    assert component_market_value(row, "blocks_steals") == 2.0
    assert learned_market_value(row, "blocks_steals") == 2.0


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
