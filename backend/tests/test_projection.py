from __future__ import annotations

import math
import sqlite3
import threading
import time
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import runpy
from types import SimpleNamespace
from urllib.error import URLError

import pytest
from fastapi import Response
from fastapi.responses import JSONResponse
from starlette.requests import Request

from backend.app import covers_import as covers_import_module
from backend.app import cache as cache_module
from backend.app import espn_history as espn_history_module
from backend.app import espn_roster as espn_roster_module
from backend.app import game_training_db as game_training_db_module
from backend.app import minutes_training_db as minutes_training_db_module
from backend.app import odds_import as odds_import_module
from backend.app import dfs_model as dfs_model_module
from backend.app import paths as paths_module
from backend.app import player_prop_model as player_prop_model_module
from backend.app import projections as projections_module
from backend.app import rotowire_import as rotowire_import_module
from backend.app import settlement as settlement_module
from backend.app import stocks_tracking as stocks_tracking_module
from backend.app import training as training_module
from backend.app.bootstrap import ensure_team, ensure_teams, normalize_team_abbreviation
from backend.app.accuracy_analysis import build_accuracy_report, get_best_predictions, get_worst_predictions
from backend.app.covers_import import (
    COVERS_CACHE_TTL_SECONDS,
    CoversGame,
    _covers_cache_is_current,
    _covers_matchup_hrefs,
    _event_rows,
    _market_from_title,
    _metadata_from_page,
    _records_from_page,
    covers_matchup_links,
)
from backend.app.db import connect, init_db
from backend.app.espn_history import backfill_espn_team_possessions, import_espn_player_boxscores, import_espn_scoreboard
from backend.app.history_expansion import audit_settled_prop_history_gaps, expand_settled_prop_history
from backend.app.game_prediction_tracking import MODEL_VERSION as GAME_MODEL_VERSION, save_game_prediction, settle_completed_game_predictions
from backend.app.game_predictions import evaluate_game_residual_models, project_game
from backend.app import game_predictions as game_predictions_module
from backend.app.history_import import determine_ats_result
from backend.app.history_import import normalize_team_key
from backend.app.main import app, import_espn_history as import_espn_history_endpoint, model_performance
from backend.app import main as main_module
from backend.app.minutes_training_db import ensure_minutes_training_db, load_minutes_training_examples
from backend.app.player_half_training_db import ensure_player_half_training_db, load_player_half_prop_examples

INIT_DB_SCRIPT = str(paths_module.ROOT_DIR / "scripts" / "init_db.py")
from backend.app.odds import american_to_implied_probability, expected_value
from backend.app.odds_import import (
    RAW_CACHE_NAME,
    SyncPropLinesResult,
    _merge_event_cache,
    _fuzzy_player_name_match,
    import_the_odds_api_props,
    line_discrepancies,
    list_sportsbook_props,
    sync_prop_lines_from_sportsbook,
)
from backend.app.player_prop_model import (
    COMPONENT_MODEL_VERSION,
    FEATURE_NAMES,
    FEATURE_INDEX,
    FeatureSnapshot,
    MINUTES_FEATURE_NAMES,
    RESIDUAL_PROMOTION_MIN_ROWS,
    RidgeModel,
    _component_market_line_weight,
    _market_depth_scale,
    _market_line_weight,
    _market_specific_component_projection,
    _market_weight,
    _minutes_feature_values,
    _player_market_weight,
    _residual_market_weight,
    _classify_minutes_role,
    _historical_minutes_opportunity_context,
    _historical_training_features,
    _injury_adjustment_for_prop,
    _player_archetype_profile,
    _project_minutes,
    _residual_model_passes_promotion_gate,
    _stabilize_learned_projection,
    clear_model_cache,
    feature_snapshot,
    predict_player_prop,
)
from backend.app.player_prop_model import _market_value as learned_market_value
from backend.app.player_prop_model import MODEL_VERSION
from backend.app.player_prop_model import train_market_model, train_minutes_model
from backend.app.projections import _market_value as component_market_value
from backend.app.projections import rebuild_predictions
from backend.app.rotowire_import import _parse_lineup_injuries
from backend.app.settlement import settle_completed_props
from backend.app.training import run_parameter_tuning, run_walk_forward_training


@pytest.fixture(autouse=True)
def isolated_db(tmp_path, monkeypatch):
    monkeypatch.setenv("USE_LOCAL_DB", "true")
    monkeypatch.setenv("WNBA_DB_PATH", str(tmp_path / "wnba-test.sqlite"))
    monkeypatch.setenv("WNBA_CACHE_DIR", str(tmp_path / "cache"))
    monkeypatch.setenv("WNBA_SNAPSHOT_DIR", str(tmp_path / "snapshots"))
    init_db()
    with connect() as conn:
        ensure_teams(conn)
    yield


def test_snapshot_current_dfs_first_half_estimates_upserts_rows() -> None:
    with connect() as conn:
        conn.execute("INSERT INTO teams(id, abbreviation, name) VALUES (201, 'AAA', 'Team A')")
        conn.execute("INSERT INTO teams(id, abbreviation, name) VALUES (202, 'BBB', 'Team B')")
        conn.execute(
            """
            INSERT INTO players(id, full_name, team_id, position)
            VALUES (303, 'Snapshot Player', 201, 'G')
            """
        )
        conn.execute(
            """
            INSERT INTO games(id, game_date, start_time, status, home_team_id, away_team_id, espn_event_id)
            VALUES (202, '2026-08-08', '2026-08-08T19:00:00+00:00', 'scheduled', 201, 202, 2202)
            """
        )
        conn.execute(
            """
            INSERT INTO prop_lines(
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (101, 202, 303, 'FanDuel', 'points', 15.5, -110, -110, '2026-08-08T18:00:00+00:00')
            """
        )
        first = dfs_model_module.snapshot_current_dfs_first_half_estimates(
            conn,
            [
                {
                    "prop_line_id": 101,
                    "game_id": 202,
                    "player_id": 303,
                    "sportsbook": "FanDuel",
                    "market": "points",
                    "line": 15.5,
                    "over_odds": -110,
                    "under_odds": -110,
                    "full_game_projection": 18.1,
                    "estimated_first_half_result": 8.7,
                    "expected_halfway_line": 7.75,
                    "pace_ratio": 1.12,
                    "halftime_margin_to_line": 0.95,
                    "on_track_probability": 0.61,
                    "recommended_side": "over",
                    "confidence": "medium",
                    "model_version": "dfs-first-half-ridge-v2",
                    "model_rows": 812,
                    "team_first_half_share": 0.49,
                    "estimated_first_half_minutes_share": 0.52,
                    "projected_first_half_total": 40.0,
                    "game_total": 81.0,
                    "start_time": "2026-08-08T19:00:00+00:00",
                }
            ],
        )
        second = dfs_model_module.snapshot_current_dfs_first_half_estimates(
            conn,
            [
                {
                    "prop_line_id": 101,
                    "game_id": 202,
                    "player_id": 303,
                    "sportsbook": "FanDuel",
                    "market": "points",
                    "line": 15.5,
                    "over_odds": -108,
                    "under_odds": -112,
                    "full_game_projection": 18.4,
                    "estimated_first_half_result": 9.1,
                    "expected_halfway_line": 7.75,
                    "pace_ratio": 1.17,
                    "halftime_margin_to_line": 1.35,
                    "on_track_probability": 0.66,
                    "recommended_side": "over",
                    "confidence": "high",
                    "model_version": "dfs-first-half-ridge-v2",
                    "model_rows": 900,
                    "team_first_half_share": 0.5,
                    "estimated_first_half_minutes_share": 0.54,
                    "projected_first_half_total": 41.0,
                    "game_total": 82.0,
                    "start_time": "2026-08-08T19:00:00+00:00",
                }
            ],
        )
        row = conn.execute(
            """
            SELECT over_odds, under_odds, estimated_first_half_result, confidence, capture_count
            FROM dfs_first_half_projection_snapshots
            WHERE prop_line_id = 101
            """
        ).fetchone()
    assert first == {"inserted": 1, "updated": 0, "skipped": 0}
    assert second == {"inserted": 0, "updated": 1, "skipped": 0}
    assert row is not None
    assert int(row["over_odds"]) == -108
    assert int(row["under_odds"]) == -112
    assert float(row["estimated_first_half_result"]) == pytest.approx(9.1)
    assert str(row["confidence"]) == "high"
    assert int(row["capture_count"]) == 2


def test_settle_dfs_first_half_projection_snapshots_uses_first_half_stats() -> None:
    with connect() as conn:
        conn.execute("INSERT INTO teams(id, abbreviation, name) VALUES (901, 'HOM', 'Home')")
        conn.execute("INSERT INTO teams(id, abbreviation, name) VALUES (902, 'AWY', 'Away')")
        conn.execute(
            """
            INSERT INTO players(id, full_name, team_id, position)
            VALUES (10, 'Test Player', 901, 'G')
            """
        )
        conn.execute(
            """
            INSERT INTO games(id, game_date, start_time, status, home_team_id, away_team_id, espn_event_id)
            VALUES (99, '2026-08-07', '2026-08-07T19:00:00+00:00', 'final', 901, 902, 9999)
            """
        )
        conn.execute(
            """
            INSERT INTO prop_lines(
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (77, 99, 10, 'FanDuel', 'points_rebounds', 13.5, -110, -110, '2026-08-07T18:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO player_first_half_stats(
                game_id, player_id, team_id, opponent_team_id, first_half_points, first_half_rebounds,
                first_half_assists, first_half_threes, first_half_steals, first_half_blocks,
                first_half_turnovers, captured_at
            ) VALUES (99, 10, 901, 902, 9, 5, 2, 1, 0, 0, 1, '2026-08-07T21:00:00+00:00')
            """
        )
        dfs_model_module.snapshot_current_dfs_first_half_estimates(
            conn,
            [
                {
                    "prop_line_id": 77,
                    "game_id": 99,
                    "player_id": 10,
                    "sportsbook": "FanDuel",
                    "market": "points_rebounds",
                    "line": 13.5,
                    "over_odds": -110,
                    "under_odds": -110,
                    "full_game_projection": 27.2,
                    "estimated_first_half_result": 15.2,
                    "expected_halfway_line": 6.75,
                    "pace_ratio": 2.25,
                    "halftime_margin_to_line": 8.45,
                    "on_track_probability": 0.95,
                    "recommended_side": "over",
                    "confidence": "high",
                    "model_version": "dfs-first-half-ridge-v2",
                    "model_rows": 1000,
                    "team_first_half_share": 0.5,
                    "estimated_first_half_minutes_share": 0.53,
                    "projected_first_half_total": 42.0,
                    "game_total": 84.0,
                    "start_time": "2026-08-07T19:00:00+00:00",
                }
            ],
        )
        result = dfs_model_module.settle_dfs_first_half_projection_snapshots(conn, selected_date="2026-08-07")
        settlement = conn.execute(
            """
            SELECT actual_first_half_result, winning_side, absolute_error, correct_side
            FROM dfs_first_half_projection_settlements
            """
        ).fetchone()
    assert result == {"settled": 1, "skipped": 0}
    assert settlement is not None
    assert float(settlement["actual_first_half_result"]) == pytest.approx(14.0)
    assert str(settlement["winning_side"]) == "over"
    assert float(settlement["absolute_error"]) == pytest.approx(1.2)
    assert int(settlement["correct_side"]) == 1


def test_training_start_date_defaults_to_previous_eastern_year(monkeypatch) -> None:
    monkeypatch.delenv("WNBA_TRAINING_START_DATE", raising=False)

    assert player_prop_model_module._training_start_date(date(2026, 7, 5)) == "2025-01-01"


def test_recent_h2h_market_history_returns_only_games_against_target_opponent() -> None:
    with connect() as conn:
        team_ids = {
            abbreviation: int(conn.execute("SELECT id FROM teams WHERE abbreviation = ?", (abbreviation,)).fetchone()["id"])
            for abbreviation in ("ATL", "NY", "CHI", "LA", "CON")
        }
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position) VALUES (90001, 'H2H Player', ?, 'G')",
            (team_ids["ATL"],),
        )
        games = [
            (90001, "2026-05-01", team_ids["ATL"], team_ids["NY"]),
            (90002, "2026-05-10", team_ids["CHI"], team_ids["NY"]),
            (90003, "2026-05-20", team_ids["ATL"], team_ids["CON"]),
            (90004, "2026-05-30", team_ids["NY"], team_ids["LA"]),
            (90005, "2026-08-03", team_ids["ATL"], team_ids["NY"]),
        ]
        conn.executemany(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status) VALUES (?, ?, ?, ?, ?, 'final')",
            [(game_id, game_date, f"{game_date}T19:00:00Z", home_id, away_id) for game_id, game_date, home_id, away_id in games],
        )
        history_teams = {
            90001: team_ids["ATL"],
            90002: team_ids["CHI"],
            90003: team_ids["ATL"],
            90004: team_ids["NY"],
        }
        conn.executemany(
            "INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at) VALUES (90001, ?, ?, 'test', 1.0, '2026-08-03T00:00:00Z')",
            [(team_id, game_id) for game_id, team_id in history_teams.items()],
        )
        stats = {
            90001: (18, 31.5),
            90002: (24, 33.0),
            90003: (40, 35.0),
            90004: (50, 36.0),
        }
        conn.executemany(
            """
            INSERT INTO player_game_stats (
                player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
            ) VALUES (90001, ?, ?, ?, 0, 0, 0, 0, 0, 0)
            """,
            [(game_id, minutes, points) for game_id, (points, minutes) in stats.items()],
        )

        history = main_module._recent_h2h_market_history(
            conn,
            player_id=90001,
            market="points",
            game_id=90005,
            resolved_team_id=team_ids["ATL"],
            limit=5,
        )

    assert history == {
        "h2h_opponent": "NY",
        "h2h_values": [24.0, 18.0],
        "h2h_minutes": [33.0, 31.5],
    }


def test_espn_roster_sync_moves_player_and_scheduled_projection_uses_new_team(monkeypatch) -> None:
    def fake_fetch(team_abbreviation: str) -> dict:
        athletes = []
        if team_abbreviation == "TOR":
            athletes = [
                {
                    "id": "4684384",
                    "fullName": "Aneesah Morrow",
                    "position": {"abbreviation": "F"},
                    "status": {"type": "active"},
                }
            ]
        return {"athletes": athletes}

    monkeypatch.setattr(espn_roster_module, "fetch_espn_team_roster", fake_fetch)
    with connect() as conn:
        team_ids = {
            str(row["abbreviation"]): int(row["id"])
            for row in conn.execute("SELECT id, abbreviation FROM teams").fetchall()
        }
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (4684384, "Aneesah Morrow", team_ids["CON"], "F", "starter"),
        )
        conn.execute(
            """
            INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status)
            VALUES (9001, '2026-07-30', '2026-07-30T23:00:00Z', ?, ?, 'final')
            """,
            (team_ids["CON"], team_ids["ATL"]),
        )
        conn.execute(
            """
            INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at)
            VALUES (4684384, ?, 9001, 'espn_boxscore', 0.95, '2026-07-31T00:00:00Z')
            """,
            (team_ids["CON"],),
        )
        conn.execute(
            """
            INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status)
            VALUES (9002, '2026-08-04', '2026-08-04T23:00:00Z', ?, ?, 'scheduled')
            """,
            (team_ids["TOR"], team_ids["NY"]),
        )
        conn.commit()

        result = espn_roster_module.sync_espn_rosters(conn)
        player = conn.execute("SELECT team_id, rotation_role FROM players WHERE id = 4684384").fetchone()
        current_history = conn.execute(
            """
            SELECT team_id FROM player_team_history
            WHERE player_id = 4684384 AND game_id IS NULL AND source = 'espn_roster_current'
            """
        ).fetchone()
        context = projections_module._game_context(conn, 4684384, 9002)

        assert int(player["team_id"]) == team_ids["TOR"]
        assert player["rotation_role"] == "starter"
        assert int(current_history["team_id"]) == team_ids["TOR"]
        assert result["moved_players"][0]["from_team_id"] == team_ids["CON"]
        assert context["team_id"] == team_ids["TOR"]
        assert context["opponent_id"] == team_ids["NY"]

        unchanged = espn_roster_module.sync_espn_rosters(conn)
        assert unchanged["changed_player_ids"] == []
        assert unchanged["moved_players"] == []


def test_training_start_date_uses_valid_override(monkeypatch) -> None:
    monkeypatch.setenv("WNBA_TRAINING_START_DATE", "2024-05-01")

    assert player_prop_model_module._training_start_date(date(2026, 7, 5)) == "2024-05-01"


def test_training_start_date_ignores_invalid_override(monkeypatch) -> None:
    monkeypatch.setenv("WNBA_TRAINING_START_DATE", "not-a-date")

    assert player_prop_model_module._training_start_date(date(2026, 7, 5)) == "2025-01-01"


def test_session_cookie_secure_follows_request_scheme(monkeypatch) -> None:
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("ENV", "prod")

    http_request = Request({"type": "http", "scheme": "http", "headers": [], "server": ("example.com", 80), "path": "/"})
    https_request = Request({"type": "http", "scheme": "https", "headers": [], "server": ("example.com", 443), "path": "/"})

    assert main_module._session_cookie_secure(http_request) is False
    assert main_module._session_cookie_secure(https_request) is True


def test_session_cookie_secure_prefers_forwarded_proto_and_override(monkeypatch) -> None:
    monkeypatch.delenv("SESSION_COOKIE_SECURE", raising=False)
    monkeypatch.setenv("ENV", "prod")

    proxied_https = Request(
        {
            "type": "http",
            "scheme": "http",
            "headers": [(b"x-forwarded-proto", b"https")],
            "server": ("example.com", 80),
            "path": "/",
        }
    )
    assert main_module._session_cookie_secure(proxied_https) is True

    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    assert main_module._session_cookie_secure(proxied_https) is False


def test_before_training_start_compares_iso_dates() -> None:
    assert player_prop_model_module._before_training_start("2024-12-31", "2025-01-01") is True
    assert player_prop_model_module._before_training_start("2025-01-01", "2025-01-01") is False
    assert player_prop_model_module._before_training_start("2025-06-01T00:00:00Z", "2025-01-01") is False


def test_model_cache_key_is_windows_safe() -> None:
    with connect() as conn:
        cache_key = player_prop_model_module._model_cache_key(
            conn,
            "C:\\Users\\me\\WNBAStats\\data\\wnba.sqlite",
            "residual:points",
            player_prop_model_module.DEFAULT_TUNING_CONFIG,
            kind="residual",
        )

    assert ":" not in cache_key
    assert "residual_points" in cache_key


def test_parallel_worker_count_defaults_to_two(monkeypatch) -> None:
    monkeypatch.delenv("WNBA_TRAINING_MAX_WORKERS", raising=False)

    assert training_module._parallel_worker_count(11) == 2


def test_parallel_worker_count_uses_env_override(monkeypatch) -> None:
    monkeypatch.setenv("WNBA_TRAINING_MAX_WORKERS", "4")

    assert training_module._parallel_worker_count(11) == 4


def test_parallel_worker_count_caps_env_override_to_task_count(monkeypatch) -> None:
    monkeypatch.setenv("WNBA_TRAINING_MAX_WORKERS", "4")

    assert training_module._parallel_worker_count(2) == 2


def test_game_training_db_ensure_serializes_concurrent_rebuilds(monkeypatch) -> None:
    load_test_history()
    rebuild_calls = 0
    rebuild_lock = threading.Lock()
    barrier = threading.Barrier(2)
    original_rebuild = game_training_db_module._rebuild_game_training_examples
    results: list[dict[str, object]] = []
    errors: list[Exception] = []

    def wrapped_rebuild(*args, **kwargs):
        nonlocal rebuild_calls
        with rebuild_lock:
            rebuild_calls += 1
        time.sleep(0.1)
        return original_rebuild(*args, **kwargs)

    monkeypatch.setattr(game_training_db_module, "_rebuild_game_training_examples", wrapped_rebuild)

    def worker() -> None:
        try:
            barrier.wait(timeout=2)
            with connect() as conn:
                results.append(game_training_db_module.ensure_game_training_db(conn))
        except Exception as exc:  # pragma: no cover - failure path surfaced by assertion
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=5)

    assert not errors
    assert len(results) == 2
    assert rebuild_calls == 1
    assert sorted(bool(result["rebuilt"]) for result in results) == [False, True]


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


def test_sqlite_connect_context_manager_closes_connection() -> None:
    with connect() as conn:
        conn.execute("SELECT 1").fetchone()

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        conn.execute("SELECT 1")


def test_cache_and_snapshot_dirs_follow_db_override(monkeypatch, tmp_path) -> None:
    db_path = tmp_path / "runtime" / "wnba.sqlite"
    monkeypatch.delenv("WNBA_CACHE_DIR", raising=False)
    monkeypatch.delenv("WNBA_SNAPSHOT_DIR", raising=False)
    monkeypatch.setenv("WNBA_DB_PATH", str(db_path))

    assert paths_module.get_cache_dir() == db_path.parent / "cache"
    assert paths_module.get_snapshot_dir() == db_path.parent / "snapshots"


def test_read_json_cache_returns_none_for_invalid_json(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: tmp_path)
    bad_cache = tmp_path / "broken.json"
    bad_cache.write_text("", encoding="utf-8")

    assert cache_module.read_json_cache("broken.json") is None


def test_read_through_cache_with_meta_serves_stale_payload_when_db_is_locked(monkeypatch) -> None:
    stale_payload = [{"player_name": "Cached Player"}]
    stale_cached_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    monkeypatch.setattr(
        main_module,
        "read_json_cache",
        lambda name: {
            "cache_key_version": main_module.READ_CACHE_VERSION,
            "cached_at": stale_cached_at,
            "cache_date": datetime.now(main_module.LOCAL_TZ).date().isoformat(),
            "ttl_seconds": 60,
            "payload": stale_payload,
        },
    )

    def locked_compute():
        raise sqlite3.OperationalError("database is locked")

    payload, status, compute_ms = main_module._read_through_cache_with_meta(
        "test-read-cache.json",
        60,
        locked_compute,
    )

    assert payload == stale_payload
    assert status == "STALE"
    assert compute_ms >= 0


def test_read_through_cache_with_meta_returns_stale_payload_while_rebuild_is_scheduled(monkeypatch) -> None:
    stale_payload = [{"player_name": "Cached Player"}]
    stale_cached_at = (datetime.now(timezone.utc) - timedelta(hours=2)).isoformat()
    monkeypatch.setattr(
        main_module,
        "read_json_cache",
        lambda name: {
            "cache_key_version": main_module.READ_CACHE_VERSION,
            "cached_at": stale_cached_at,
            "cache_date": datetime.now(main_module.LOCAL_TZ).date().isoformat(),
            "ttl_seconds": 60,
            "payload": stale_payload,
        },
    )
    scheduled: list[str] = []
    monkeypatch.setattr(
        main_module,
        "_start_read_cache_rebuild",
        lambda name, ttl_seconds, compute: scheduled.append(name) or True,
    )

    payload, status, compute_ms = main_module._read_through_cache_with_meta(
        "test-read-cache.json",
        60,
        lambda: (_ for _ in ()).throw(AssertionError("stale reads must not compute inline")),
    )

    assert payload == stale_payload
    assert status == "STALE"
    assert compute_ms == 0.0
    assert scheduled == ["test-read-cache.json"]


def test_stale_read_cache_never_crosses_local_date_rollover(monkeypatch) -> None:
    monkeypatch.setattr(
        main_module,
        "read_json_cache",
        lambda name: {
            "cache_key_version": main_module.READ_CACHE_VERSION,
            "cached_at": datetime.now(timezone.utc).isoformat(),
            "cache_date": "2000-01-01",
            "ttl_seconds": 3600,
            "payload": ["previous-day"],
        },
    )

    assert main_module._read_cached_payload("test-read-cache.json", allow_stale=True) is None


def test_publish_current_read_payloads_continues_after_single_failure(monkeypatch) -> None:
    published: list[str] = []

    monkeypatch.setattr(main_module, "_watchlist_payload", lambda conn: [3])
    monkeypatch.setattr(main_module, "line_discrepancies", lambda conn, game_id=None: [4])
    monkeypatch.setattr(main_module, "_roster_payload", lambda conn: [5])
    monkeypatch.setattr(main_module, "_model_runs_payload", lambda conn: {"latest": {}, "runs": [1, 2, 3]})
    monkeypatch.setattr(main_module, "_model_performance_payload", lambda conn: {"model": 1})
    monkeypatch.setattr(main_module, "_gem_performance_payload", lambda conn: {"gem": 1})
    monkeypatch.setattr(main_module, "_watchlist_performance_payload", lambda conn: {"watchlist": 1})
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: published.append(name))

    def failing_matchups(conn, game_ids=None, full_refresh=True):
        del conn, game_ids, full_refresh
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(main_module, "_publish_matchup_snapshot_aggregates", failing_matchups)

    result = main_module._publish_current_read_payloads(SimpleNamespace())

    assert main_module.MATCHUPS_CACHE_NAME not in published
    assert main_module.GEM_PERFORMANCE_CACHE_NAME in published
    assert main_module.WATCHLIST_PERFORMANCE_CACHE_NAME in published
    assert result[main_module.GEM_PERFORMANCE_CACHE_NAME] == 1
    assert result[main_module.WATCHLIST_PERFORMANCE_CACHE_NAME] == 1


def test_publish_current_read_payloads_can_skip_matchups(monkeypatch) -> None:
    published: list[str] = []

    monkeypatch.setattr(main_module, "_watchlist_payload", lambda conn: [3])
    monkeypatch.setattr(main_module, "line_discrepancies", lambda conn, game_id=None: [4])
    monkeypatch.setattr(main_module, "_roster_payload", lambda conn: [5])
    monkeypatch.setattr(main_module, "_model_runs_payload", lambda conn: {"latest": {}, "runs": [1, 2, 3]})
    monkeypatch.setattr(main_module, "_model_performance_payload", lambda conn: {"model": 1})
    monkeypatch.setattr(main_module, "_gem_performance_payload", lambda conn: {"gem": 1})
    monkeypatch.setattr(main_module, "_watchlist_performance_payload", lambda conn: {"watchlist": 1})
    monkeypatch.setattr(
        main_module,
        "_publish_matchup_snapshot_aggregates",
        lambda conn, game_ids=None, full_refresh=True: (_ for _ in ()).throw(AssertionError("matchups prewarm should be skipped")),
    )
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: published.append(name))

    result = main_module._publish_current_read_payloads(SimpleNamespace(), include_matchups=False)

    assert main_module.MATCHUPS_CACHE_NAME not in published
    assert main_module.MATCHUPS_CACHE_NAME not in result
    assert main_module.WATCHLIST_CACHE_NAME in published


def test_publish_current_read_payloads_uses_cached_rotowire_state(monkeypatch) -> None:
    published: list[str] = []
    roster_refresh_flags: list[bool] = []

    monkeypatch.setattr(main_module, "_watchlist_payload", lambda conn: [3])
    monkeypatch.setattr(main_module, "line_discrepancies", lambda conn, game_id=None: [4])
    monkeypatch.setattr(
        main_module,
        "_roster_payload",
        lambda conn, refresh_lineups=True: roster_refresh_flags.append(bool(refresh_lineups)) or [5],
    )
    monkeypatch.setattr(main_module, "_model_runs_payload", lambda conn: {"latest": {}, "runs": [1]})
    monkeypatch.setattr(main_module, "_model_performance_payload", lambda conn: {"model": 1})
    monkeypatch.setattr(main_module, "_gem_performance_payload", lambda conn: {"gem": 1})
    monkeypatch.setattr(main_module, "_watchlist_performance_payload", lambda conn: {"watchlist": 1})
    monkeypatch.setattr(
        main_module,
        "_cached_rotowire_refresh_metadata",
        lambda: {"source": "cache", "captured_at": "2026-07-11T10:00:00+00:00", "from_cache": True},
    )
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: published.append(name))

    captured_matchup_args: dict[str, object] = {}

    def fake_publish_matchups(conn, game_ids=None, full_refresh=True, injury_refresh=None):
        del conn
        captured_matchup_args["game_ids"] = game_ids
        captured_matchup_args["full_refresh"] = full_refresh
        captured_matchup_args["injury_refresh"] = injury_refresh
        return {main_module.MATCHUPS_CACHE_NAME: 1, main_module.VALUE_BOARD_CACHE_NAME: 2}

    monkeypatch.setattr(main_module, "_publish_matchup_snapshot_aggregates", fake_publish_matchups)

    result = main_module._publish_current_read_payloads(SimpleNamespace())

    assert roster_refresh_flags == [False]
    assert captured_matchup_args["injury_refresh"] == {
        "source": "cache",
        "captured_at": "2026-07-11T10:00:00+00:00",
        "from_cache": True,
    }
    assert result[main_module.MATCHUPS_CACHE_NAME] == 1
    assert main_module.ROSTER_CACHE_NAME in published


def test_refresh_roster_read_payloads_reuses_current_rotowire_state(monkeypatch) -> None:
    published: list[str] = []
    roster_refresh_flags: list[bool] = []

    monkeypatch.setattr(
        main_module,
        "_roster_payload",
        lambda conn, refresh_lineups=True: roster_refresh_flags.append(bool(refresh_lineups)) or [5],
    )
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: published.append(name))

    result = main_module._refresh_roster_read_payloads(SimpleNamespace())

    assert roster_refresh_flags == [False]
    assert result == {main_module.ROSTER_CACHE_NAME: 1}
    assert published == [main_module.ROSTER_CACHE_NAME]


def test_publish_post_mutation_read_payloads_includes_matchups(monkeypatch) -> None:
    published: list[str] = []

    monkeypatch.setattr(main_module, "_watchlist_payload", lambda conn: [3])
    monkeypatch.setattr(main_module, "line_discrepancies", lambda conn, game_id=None: [4])
    monkeypatch.setattr(main_module, "_roster_payload", lambda conn, refresh_lineups=True: [5])
    monkeypatch.setattr(main_module, "_model_runs_payload", lambda conn: {"latest": {}, "runs": [1]})
    monkeypatch.setattr(main_module, "_model_performance_payload", lambda conn: {"model": 1})
    monkeypatch.setattr(main_module, "_gem_performance_payload", lambda conn: {"gem": 1})
    monkeypatch.setattr(main_module, "_watchlist_performance_payload", lambda conn: {"watchlist": 1})
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: published.append(name))
    monkeypatch.setattr(
        main_module,
        "_publish_matchup_snapshot_aggregates",
        lambda conn, game_ids=None, full_refresh=True, injury_refresh=None: {main_module.MATCHUPS_CACHE_NAME: 1, main_module.VALUE_BOARD_CACHE_NAME: 2},
    )

    result = main_module._publish_post_mutation_read_payloads(SimpleNamespace())

    assert result[main_module.MATCHUPS_CACHE_NAME] == 1


def test_publish_post_mutation_read_payloads_can_skip_performance_and_keep_roster(monkeypatch) -> None:
    published: list[str] = []

    monkeypatch.setattr(main_module, "_watchlist_payload", lambda conn: [3])
    monkeypatch.setattr(main_module, "line_discrepancies", lambda conn, game_id=None: [4])
    monkeypatch.setattr(main_module, "_roster_payload", lambda conn, refresh_lineups=True: [5])
    monkeypatch.setattr(
        main_module,
        "_publish_matchup_snapshot_aggregates",
        lambda conn, game_ids=None, full_refresh=True, injury_refresh=None: {main_module.MATCHUPS_CACHE_NAME: 1, main_module.VALUE_BOARD_CACHE_NAME: 2},
    )
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: published.append(name))

    result = main_module._publish_post_mutation_read_payloads(SimpleNamespace(), include_performance=False)

    assert main_module.ROSTER_CACHE_NAME in published
    assert main_module.MODEL_RUNS_CACHE_NAME not in published
    assert main_module.MODEL_PERFORMANCE_CACHE_NAME not in published
    assert main_module.GEM_PERFORMANCE_CACHE_NAME not in published
    assert main_module.WATCHLIST_PERFORMANCE_CACHE_NAME not in published
    assert result[main_module.ROSTER_CACHE_NAME] == 1


def test_publish_matchup_snapshot_aggregates_uses_supplied_injury_refresh(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("WNBA_CACHE_DIR", str(tmp_path))

    game = {
        "id": 9910,
        "start_time": "2026-07-04T17:00:00+00:00",
        "home_team_id": 10,
        "away_team_id": 3,
    }

    def fake_groups(conn, game_ids=None):
        del conn, game_ids
        return [(game, [9910])]

    captured_injury_refresh: list[dict[str, object]] = []

    def fake_matchup(conn, game, game_ids, **kwargs):
        del conn, game, game_ids
        captured_injury_refresh.append(dict(kwargs["injury_refresh"]))
        return {
            "id": 9910,
            "start_time": "2026-07-04T17:00:00+00:00",
            "home_team_id": 10,
            "away_team_id": 3,
            "game_ids": [9910],
            "props": [],
        }

    monkeypatch.setattr(main_module, "_scheduled_matchup_game_groups", fake_groups)
    monkeypatch.setattr(main_module, "_build_matchup_payload_item", fake_matchup)
    monkeypatch.setattr(main_module, "_value_board_payload_for_games", lambda conn, game_ids, include_filtered_only=True: [])
    monkeypatch.setattr(main_module, "_prediction_state_by_game", lambda conn: {})
    monkeypatch.setattr(main_module, "_covers_records_by_game", lambda conn: {})
    monkeypatch.setattr(main_module, "_covers_market_odds_by_game", lambda: {})
    monkeypatch.setattr(main_module, "_GamePredictionCache", lambda *args: object())
    monkeypatch.setattr(
        main_module,
        "import_rotowire_lineups",
        lambda conn, force_refresh=False: (_ for _ in ()).throw(AssertionError("publish should use supplied injury refresh")),
    )

    supplied = {"source": "cache", "captured_at": "2026-07-11T10:00:00+00:00", "from_cache": True}
    result = main_module._publish_matchup_snapshot_aggregates(object(), injury_refresh=supplied)

    assert captured_injury_refresh == [supplied]
    assert result[main_module.MATCHUPS_CACHE_NAME] == 1


def test_value_board_rebuild_uses_cached_rotowire_refresh_metadata(monkeypatch) -> None:
    captured_injury_refresh: list[dict[str, object]] = []

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(main_module, "_aggregate_matchup_snapshot_payloads", lambda conn: None)
    monkeypatch.setattr(
        main_module,
        "_cached_rotowire_refresh_metadata",
        lambda: {"source": "cache", "captured_at": "2026-07-14T10:00:00+00:00", "from_cache": True},
    )
    monkeypatch.setattr(
        main_module,
        "_publish_matchup_snapshot_aggregates",
        lambda conn, game_ids=None, full_refresh=True, injury_refresh=None: captured_injury_refresh.append(dict(injury_refresh or {})) or {main_module.VALUE_BOARD_CACHE_NAME: 1},
    )
    monkeypatch.setattr(main_module, "_read_cached_payload", lambda name, allow_stale=False: [{"id": 1}] if name == main_module.VALUE_BOARD_CACHE_NAME else None)
    monkeypatch.setattr(
        main_module,
        "_read_through_cache_with_meta",
        lambda cache_name, ttl_seconds, compute: (compute(), "MISS", 0.0),
    )
    monkeypatch.setattr(main_module, "_set_observability_headers", lambda *args, **kwargs: None)

    result = main_module.value_board(Response())

    assert result == [{"id": 1}]
    assert captured_injury_refresh == [{"source": "cache", "captured_at": "2026-07-14T10:00:00+00:00", "from_cache": True}]


def test_matchups_rebuild_uses_cached_rotowire_refresh_metadata(monkeypatch) -> None:
    captured_injury_refresh: list[dict[str, object]] = []

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(main_module, "_aggregate_matchup_snapshot_payloads", lambda conn: None)
    monkeypatch.setattr(
        main_module,
        "_cached_rotowire_refresh_metadata",
        lambda: {"source": "cache", "captured_at": "2026-07-14T10:00:00+00:00", "from_cache": True},
    )
    monkeypatch.setattr(
        main_module,
        "_publish_matchup_snapshot_aggregates",
        lambda conn, game_ids=None, full_refresh=True, injury_refresh=None: captured_injury_refresh.append(dict(injury_refresh or {})) or {main_module.MATCHUPS_CACHE_NAME: 1},
    )
    monkeypatch.setattr(main_module, "_read_cached_payload", lambda name, allow_stale=False: [{"id": 1}] if name == main_module.MATCHUPS_CACHE_NAME else None)
    monkeypatch.setattr(
        main_module,
        "_read_through_cache_with_meta",
        lambda cache_name, ttl_seconds, compute: (compute(), "MISS", 0.0),
    )
    monkeypatch.setattr(main_module, "_set_observability_headers", lambda *args, **kwargs: None)

    result = main_module.matchups(Response())

    assert result == [{"id": 1}]
    assert captured_injury_refresh == [{"source": "cache", "captured_at": "2026-07-14T10:00:00+00:00", "from_cache": True}]


def test_watchlist_performance_uses_read_cache(monkeypatch) -> None:
    payload = {
        "qualified": 1,
        "wins": 1,
        "win_rate": 1.0,
        "message": "cached",
    }
    cached_at = datetime.now(timezone.utc).isoformat()
    cache_name = main_module.WATCHLIST_PERFORMANCE_CACHE_NAME
    monkeypatch.setattr(
        main_module,
        "read_json_cache",
        lambda name: {
            "cache_key_version": main_module.READ_CACHE_VERSION,
            "cached_at": cached_at,
            "cache_date": datetime.now(main_module.LOCAL_TZ).date().isoformat(),
            "ttl_seconds": 300,
            "payload": payload,
        } if name == cache_name else None,
    )

    def fail_connect():
        raise AssertionError("connect should not be called for a cached response")

    monkeypatch.setattr(main_module, "connect", fail_connect)

    response = Response()
    result = main_module.watchlist_performance(response)

    assert result == payload


@pytest.mark.anyio
async def test_app_response_cache_serves_fresh_payload_before_calling_backend(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: tmp_path)

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/api/model-performance",
            "raw_path": b"/api/model-performance",
            "query_string": b"",
            "headers": [],
            "client": ("testclient", 123),
            "server": ("testserver", 80),
            "scheme": "http",
        },
        _receive,
    )

    async def first_call(_request):
        return JSONResponse(content=[{"id": 1, "player_name": "Cached"}])

    async def locked_call(_request):
        raise sqlite3.OperationalError("database is locked")

    first = await main_module.cache_api_get_responses(request, first_call)
    assert first.status_code == 200
    assert first.headers.get("x-app-cache") == "STORE"

    second = await main_module.cache_api_get_responses(request, locked_call)
    assert second.status_code == 200
    assert second.headers.get("x-app-cache") == "HIT"
    assert json.loads(second.body.decode("utf-8")) == [{"id": 1, "player_name": "Cached"}]


@pytest.mark.parametrize("path", ["/api/roster", "/api/value-board", "/api/watchlist"])
def test_read_through_cached_endpoints_bypass_app_response_cache(path: str) -> None:
    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": path,
            "raw_path": path.encode("utf-8"),
            "query_string": b"",
            "headers": [],
        }
    )

    assert main_module._should_cache_app_response(request) is False


def test_audit_stale_payloads_flags_expired_and_old_cache_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "get_cache_dir", lambda: tmp_path)

    expired = {
        "cache_key_version": main_module.APP_RESPONSE_CACHE_VERSION,
        "cached_at": (datetime.now(timezone.utc) - timedelta(hours=3)).isoformat(),
        "cache_date": datetime.now(main_module.LOCAL_TZ).date().isoformat(),
        "ttl_seconds": 60,
        "status_code": 200,
        "payload": [],
    }
    fresh = {
        "cache_key_version": main_module.APP_RESPONSE_CACHE_VERSION,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "cache_date": datetime.now(main_module.LOCAL_TZ).date().isoformat(),
        "ttl_seconds": 3600,
        "status_code": 200,
        "payload": [],
    }
    old_raw = {
        "cache_date": "2026-01-01",
        "captured_at": "2026-01-01T12:00:00+00:00",
        "rows": [],
    }
    (tmp_path / "app_response_cache_test.json").write_text(json.dumps(expired), encoding="utf-8")
    (tmp_path / "current_value_board.json").write_text(json.dumps(fresh), encoding="utf-8")
    (tmp_path / "covers_props_raw.json").write_text(json.dumps(old_raw), encoding="utf-8")

    result = main_module._audit_stale_payloads()

    assert result["stale"] == 2
    assert "app_response_cache_test.json" in result["files"]
    assert "covers_props_raw.json" in result["files"]
    assert "current_value_board.json" not in result["files"]


def test_audit_stale_payloads_flags_previous_day_current_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "get_cache_dir", lambda: tmp_path)

    previous_day = {
        "cache_key_version": main_module.READ_CACHE_VERSION,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "cache_date": "2026-06-19",
        "ttl_seconds": 3600,
        "payload": [],
    }
    (tmp_path / main_module.VALUE_BOARD_CACHE_NAME).write_text(json.dumps(previous_day), encoding="utf-8")

    result = main_module._audit_stale_payloads()

    assert main_module.VALUE_BOARD_CACHE_NAME in result["files"]


def test_delete_stale_payloads_removes_only_audited_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "get_cache_dir", lambda: tmp_path)

    stale = {
        "cache_date": "2026-01-01",
        "captured_at": "2026-01-01T12:00:00+00:00",
        "rows": [],
    }
    fresh = {
        "cache_date": datetime.now(main_module.LOCAL_TZ).date().isoformat(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "rows": [],
    }
    stale_path = tmp_path / "sportsbook_props_raw.json"
    fresh_path = tmp_path / "covers_pages_raw.json"
    stale_path.write_text(json.dumps(stale), encoding="utf-8")
    fresh_path.write_text(json.dumps(fresh), encoding="utf-8")

    result = main_module._delete_stale_payloads()

    assert result["deleted"] == 1
    assert result["files"] == ["sportsbook_props_raw.json"]
    assert not stale_path.exists()
    assert fresh_path.exists()


def test_normalize_team_abbreviation_handles_covers_name_variants() -> None:
    assert normalize_team_abbreviation("No. 1 Phoenix Mercury") == "PHX"
    assert normalize_team_abbreviation("Washington Mystics (W)") == "WSH"
    assert normalize_team_abbreviation("Connecticut Sun (10-4)") == "CON"
    assert normalize_team_abbreviation("P.H.O.") == "PHX"
    assert normalize_team_abbreviation("LAS") == "LA"
    assert normalize_team_abbreviation("LVA") == "LV"


def test_history_import_normalize_team_key_resolves_las_and_lva_aliases() -> None:
    assert normalize_team_key("LAS") == "LA"
    assert normalize_team_key("LVA") == "LV"


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

    def fake_settle(conn, selected_dates=None):
        del selected_dates
        calls.append("settle")
        return {"settled": 1}

    monkeypatch.setattr("backend.app.main.import_espn_scoreboard", fake_scoreboard)
    monkeypatch.setattr("backend.app.main.settle_completed_props", fake_settle)
    monkeypatch.setattr("backend.app.main.settle_completed_game_predictions", lambda conn, selected_dates=None: {"settled": 0})

    result = import_espn_history_endpoint(season=2026, include_player_stats=False, include_previous_season=False)

    assert calls == ["settle"]
    assert result["settlements"] == {"settled": 1}
    assert result["predictions"] == 0
    assert result["selected_date"] is not None or result["selected_dates"]


def test_model_runs_endpoint_uses_read_cache(monkeypatch) -> None:
    cache_store: dict[str, object] = {}
    calls = {"latest": 0, "runs": 0}

    monkeypatch.setattr(main_module, "read_json_cache", lambda name: cache_store.get(name))
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: cache_store.__setitem__(name, payload))
    monkeypatch.setattr(main_module, "delete_json_cache", lambda name: bool(cache_store.pop(name, None)))

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())

    def fake_latest(_conn):
        calls["latest"] += 1
        return {"id": calls["latest"]}

    def fake_runs(_conn):
        calls["runs"] += 1
        return [{"id": calls["runs"]}]

    monkeypatch.setattr(main_module, "latest_model_run", fake_latest)
    monkeypatch.setattr(main_module, "list_model_runs", fake_runs)

    first = main_module.model_runs(Response())
    second = main_module.model_runs(Response())

    assert first == second
    assert calls == {"latest": 1, "runs": 1}


def test_startup_prewarms_models(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(main_module, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(main_module, "ensure_teams", lambda conn: calls.append("ensure_teams"))
    monkeypatch.setattr(main_module, "_start_model_prewarm", lambda: calls.append("prewarm"))
    monkeypatch.setattr(main_module, "_start_read_payload_prewarm", lambda: calls.append("publish"))
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: calls.append("invalidate"))
    monkeypatch.setattr(main_module, "_delete_stale_payloads", lambda: {"deleted": 0, "files": [], "checked": 0})
    monkeypatch.setattr(main_module, "_configured_api_key", lambda: None)
    monkeypatch.setattr(main_module, "_bootstrap_admin_configured", lambda: False)
    monkeypatch.setattr(main_module, "_is_dev_env", lambda: True)

    main_module.on_startup()

    assert calls == ["init_db", "ensure_teams", "prewarm", "publish"]


def test_startup_deletes_stale_payloads_before_prewarm(monkeypatch) -> None:
    calls: list[str] = []

    monkeypatch.setattr(main_module, "init_db", lambda: calls.append("init_db"))
    monkeypatch.setattr(main_module, "ensure_teams", lambda conn: calls.append("ensure_teams"))
    monkeypatch.setattr(main_module, "_start_model_prewarm", lambda: calls.append("prewarm"))
    monkeypatch.setattr(main_module, "_start_read_payload_prewarm", lambda: calls.append("publish"))
    monkeypatch.setattr(
        main_module,
        "_delete_stale_payloads",
        lambda: calls.append("delete_stale") or {"deleted": 2, "files": ["current_value_board.json", "app_response_cache_x.json"], "checked": 2},
    )
    monkeypatch.setattr(main_module, "_configured_api_key", lambda: None)
    monkeypatch.setattr(main_module, "_bootstrap_admin_configured", lambda: False)
    monkeypatch.setattr(main_module, "_is_dev_env", lambda: True)

    main_module.on_startup()

    assert calls == ["delete_stale", "init_db", "ensure_teams", "prewarm", "publish"]


def test_init_db_script_retries_locked_startup_then_succeeds(monkeypatch, capsys) -> None:
    attempts = {"init_db": 0, "ensure_teams": 0}
    sleeps: list[float] = []

    def flaky_init_db() -> None:
        attempts["init_db"] += 1
        if attempts["init_db"] < 3:
            raise sqlite3.OperationalError("database is locked")

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb) -> None:
            return None

    monkeypatch.setattr("backend.app.db.init_db", flaky_init_db)
    monkeypatch.setattr("backend.app.db.connect", lambda: DummyConn())
    monkeypatch.setattr(
        "backend.app.bootstrap.ensure_teams",
        lambda conn: attempts.__setitem__("ensure_teams", attempts["ensure_teams"] + 1),
    )
    monkeypatch.setattr("time.sleep", lambda seconds: sleeps.append(seconds))
    monkeypatch.setenv("WNBA_INIT_DB_LOCK_ATTEMPTS", "4")
    monkeypatch.setenv("WNBA_INIT_DB_LOCK_BASE_SLEEP", "0.25")

    runpy.run_path(INIT_DB_SCRIPT, run_name="__main__")

    output = capsys.readouterr().out
    assert attempts["init_db"] == 3
    assert attempts["ensure_teams"] == 1
    assert sleeps == [0.25, 0.5]
    assert "retrying in 0.2s" in output or "retrying in 0.3s" in output


def test_init_db_script_raises_after_retry_budget(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.db.init_db",
        lambda: (_ for _ in ()).throw(sqlite3.OperationalError("database is locked")),
    )
    monkeypatch.setattr("time.sleep", lambda seconds: None)
    monkeypatch.setenv("WNBA_INIT_DB_LOCK_ATTEMPTS", "2")
    monkeypatch.setenv("WNBA_INIT_DB_LOCK_BASE_SLEEP", "0.1")

    with pytest.raises(sqlite3.OperationalError, match="database is locked"):
        runpy.run_path(INIT_DB_SCRIPT, run_name="__main__")


def test_roster_endpoint_uses_read_cache(monkeypatch) -> None:
    cache_store: dict[str, object] = {}
    calls = {"import": 0}

    monkeypatch.setattr(main_module, "read_json_cache", lambda name: cache_store.get(name))
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: cache_store.__setitem__(name, payload))
    monkeypatch.setattr(main_module, "delete_json_cache", lambda name: bool(cache_store.pop(name, None)))

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())

    def fake_import(_conn, force_refresh=False):
        calls["import"] += 1
        cache_store[main_module.ROTOWIRE_RAW_CACHE_NAME] = {
            "captured_at": "2026-05-27T00:00:00+00:00",
            "rows": [{"team": "ny", "player_name": "A Player", "status": "gtd"}],
        }
        return {"from_cache": False}

    monkeypatch.setattr(main_module, "import_rotowire_lineups", fake_import)

    first = main_module.roster(Response())
    second = main_module.roster(Response())

    assert first == second
    assert calls["import"] == 1
    assert first[0]["team"] == "NY"
    assert first[0]["status"] == "GTD"


def test_resolve_roster_player_normalizes_team_aliases() -> None:
    load_test_history()
    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (777001, "Nyara Sabally", 3, "F", "starter"),
        )
        player = main_module._resolve_roster_player(conn, "NYL", "Nyara Sabally")

    assert player is not None
    assert int(player["player_id"]) == 777001
    assert player["rotation_role"] == "starter"


def test_resolve_roster_player_falls_back_to_unique_league_wide_name() -> None:
    load_test_history()
    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (777101, "Kelsey Plum", 7, "G", "starter"),
        )
        player = main_module._resolve_roster_player(conn, "LV", "Kelsey Plum")

    assert player is not None
    assert int(player["player_id"]) == 777101
    assert player["rotation_role"] == "starter"


def test_resolve_roster_player_display_fast_does_not_cross_team_match_by_name() -> None:
    load_test_history()
    with connect() as conn:
        la_team_id = int(
            conn.execute("SELECT id FROM teams WHERE abbreviation = 'LA'").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (777151, "Kelsey Plum", la_team_id, "G", "starter"),
        )
        player_rows = conn.execute(
            """
            SELECT
                p.id AS player_id,
                p.full_name,
                p.team_id,
                t.abbreviation AS team_abbreviation,
                p.rotation_role,
                p.position
            FROM players p
            LEFT JOIN teams t ON t.id = p.team_id
            """
        ).fetchall()
        player_index = main_module._build_roster_player_index(player_rows)
        player, display_fallback = main_module._resolve_roster_player_display(
            conn,
            "LV",
            "Kelsey Plum",
            player_rows=player_rows,
            player_index=player_index,
        )

    assert display_fallback is None
    assert player is None


def test_resolve_roster_player_display_fast_does_not_cross_team_match_by_initial_last() -> None:
    load_test_history()
    with connect() as conn:
        min_team_id = int(
            conn.execute("SELECT id FROM teams WHERE abbreviation = 'MIN'").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (777161, "DiJonai Carrington", min_team_id, "G", "rotation"),
        )
        player_rows = conn.execute(
            """
            SELECT
                p.id AS player_id,
                p.full_name,
                p.team_id,
                t.abbreviation AS team_abbreviation,
                p.rotation_role,
                p.position
            FROM players p
            LEFT JOIN teams t ON t.id = p.team_id
            """
        ).fetchall()
        player_index = main_module._build_roster_player_index(player_rows)
        player, display_fallback = main_module._resolve_roster_player_display(
            conn,
            "CHI",
            "D. Carrington",
            player_rows=player_rows,
            player_index=player_index,
        )

    assert display_fallback is None
    assert player is None


def test_resolve_roster_player_display_fast_falls_back_to_unique_league_wide_name() -> None:
    load_test_history()
    with connect() as conn:
        chi_team_id = int(
            conn.execute("SELECT id FROM teams WHERE abbreviation = 'CHI'").fetchone()[0]
        )
        wsh_team_id = int(
            conn.execute("SELECT id FROM teams WHERE abbreviation = 'WSH'").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (777171, "Ariel Atkins", wsh_team_id, "G", "starter"),
        )
        conn.execute(
            """
            INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (777171, chi_team_id, None, "test", 0.95, "2026-07-22T00:00:00Z"),
        )
        player_rows = conn.execute(
            """
            SELECT
                p.id AS player_id,
                p.full_name,
                p.team_id,
                t.abbreviation AS team_abbreviation,
                p.rotation_role,
                p.position
            FROM players p
            LEFT JOIN teams t ON t.id = p.team_id
            """
        ).fetchall()
        player_index = main_module._build_roster_player_index(player_rows)
        player, display_fallback = main_module._resolve_roster_player_display(
            conn,
            "CHI",
            "Ariel Atkins",
            player_rows=player_rows,
            player_index=player_index,
        )

    assert display_fallback is None
    assert player is not None
    assert int(player["player_id"]) == 777171


def test_resolve_roster_player_display_fast_falls_back_to_unique_league_wide_initial_last() -> None:
    load_test_history()
    with connect() as conn:
        chi_team_id = int(
            conn.execute("SELECT id FROM teams WHERE abbreviation = 'CHI'").fetchone()[0]
        )
        ny_team_id = int(
            conn.execute("SELECT id FROM teams WHERE abbreviation = 'NY'").fetchone()[0]
        )
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (777181, "Courtney Vandersloot", ny_team_id, "G", "starter"),
        )
        conn.execute(
            """
            INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (777181, chi_team_id, None, "test", 0.95, "2026-07-22T00:00:00Z"),
        )
        player_rows = conn.execute(
            """
            SELECT
                p.id AS player_id,
                p.full_name,
                p.team_id,
                t.abbreviation AS team_abbreviation,
                p.rotation_role,
                p.position
            FROM players p
            LEFT JOIN teams t ON t.id = p.team_id
            """
        ).fetchall()
        player_index = main_module._build_roster_player_index(player_rows)
        player, display_fallback = main_module._resolve_roster_player_display(
            conn,
            "CHI",
            "C. Vandersloot",
            player_rows=player_rows,
            player_index=player_index,
        )

    assert display_fallback is None
    assert player is not None
    assert int(player["player_id"]) == 777181


def test_resolve_roster_player_falls_back_to_unique_team_last_name() -> None:
    load_test_history()
    with connect() as conn:
        min_team_id = conn.execute(
            "SELECT id FROM teams WHERE abbreviation = 'MIN'"
        ).fetchone()[0]
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (777201, "Napheesa Collier", int(min_team_id), "F", "starter"),
        )
        player = main_module._resolve_roster_player(conn, "MIN", "Phee Collier")

    assert player is not None
    assert int(player["player_id"]) == 777201
    assert player["position"] == "F"


def test_rotowire_snapshot_cache_normalizes_team_aliases(monkeypatch) -> None:
    captured: dict[str, object] = {}
    monkeypatch.setattr(rotowire_import_module, "read_json_cache", lambda _name: None)
    monkeypatch.setattr(
        rotowire_import_module,
        "write_json_cache",
        lambda name, payload: captured.setdefault(name, payload),
    )

    rotowire_import_module._update_roster_snapshot_cache(
        [
            {"team": "NYL", "player_name": "Nyara Sabally", "status": "OUT"},
            {"team": "CONN", "player_name": "Aneesah Morrow", "status": "GTD"},
            {"team": "LVA", "player_name": "Chelsea Gray", "status": "OUT"},
        ],
        "2026-06-26T00:00:00+00:00",
        "rotowire",
    )

    payload = captured[rotowire_import_module.ROSTER_SNAPSHOT_CACHE_NAME]
    rows = payload["rows"]
    assert rows[0]["team"] == "CON"
    assert rows[1]["team"] == "LV"
    assert rows[2]["team"] == "NY"


def test_watchlist_endpoint_uses_read_cache(monkeypatch) -> None:
    cache_store: dict[str, object] = {}
    calls = {"watchlist": 0}

    monkeypatch.setattr(main_module, "read_json_cache", lambda name: cache_store.get(name))
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: cache_store.__setitem__(name, payload))
    monkeypatch.setattr(main_module, "delete_json_cache", lambda name: bool(cache_store.pop(name, None)))

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(main_module, "_watchlist_payload", lambda conn: calls.__setitem__("watchlist", calls["watchlist"] + 1) or [{"id": calls["watchlist"]}])

    first = main_module.watchlist(Response())
    second = main_module.watchlist(Response())

    assert first == second
    assert calls["watchlist"] == 1


def test_recalculate_endpoint_skips_model_refresh_and_marks_legacy(monkeypatch) -> None:
    connect_calls = 0
    publish_calls: list[bool] = []
    repair_calls = 0
    thread_starts = 0

    class DummyConn:
        def __enter__(self):
            nonlocal connect_calls
            connect_calls += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class ImmediateThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")

        def start(self):
            nonlocal thread_starts
            thread_starts += 1
            if self.target is not None:
                self.target()

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(main_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(main_module, "_create_prop_sync_job_record", lambda *args, **kwargs: None)

    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "running", False)

    def fake_repair_current_slate_props(conn, progress_callback=None):
        nonlocal repair_calls
        repair_calls += 1
        if progress_callback is not None:
            progress_callback("rebuilding_predictions", 1, 1, "Repair complete.")
        return {"rebuilt_predictions": 0}

    monkeypatch.setattr(main_module, "_repair_current_slate_props", fake_repair_current_slate_props)
    monkeypatch.setattr(main_module, "settle_completed_props", lambda conn: {"settled": 0})
    monkeypatch.setattr(main_module, "settle_completed_game_predictions", lambda conn: {"settled": 0})
    monkeypatch.setattr(
        main_module,
        "_publish_current_read_payloads",
        lambda conn, include_matchups=True, matchup_game_ids=None, full_matchup_refresh=True: publish_calls.append(bool(include_matchups)) or {},
    )

    response = Response()
    result = main_module.recalculate(response)

    assert result["status"] == "queued"
    assert result["scope"] == "legacy_recalculate"
    assert result["target_game_ids"] == []
    assert isinstance(result["started_at"], str)
    assert repair_calls == 1
    assert connect_calls >= 2
    assert thread_starts == 1
    assert response.headers["Deprecation"] == "true"
    assert response.headers["X-Legacy-Endpoint"] == "/api/recalculate"


def test_read_cache_invalidation_marks_new_cache_keys_stale(monkeypatch, tmp_path) -> None:
    cache_store = {
        name: main_module._cache_envelope([], 300)
        for name in main_module.READ_CACHE_FILES
    }
    written: dict[str, object] = {}
    monkeypatch.setattr(main_module, "get_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(main_module, "read_json_cache", lambda name: cache_store.get(name))
    monkeypatch.setattr(main_module, "write_json_cache", lambda name, payload: written.__setitem__(name, payload))
    monkeypatch.setattr(main_module, "_delete_app_response_caches", lambda: None)

    main_module._invalidate_read_caches()

    for name in (
        main_module.MODEL_PERFORMANCE_CACHE_NAME,
        main_module.MODEL_RUNS_CACHE_NAME,
        main_module.ROSTER_CACHE_NAME,
    ):
        assert name in written
        assert isinstance(written[name], dict)
        assert isinstance(written[name].get("invalidated_at"), str)


def test_read_cache_invalidation_clears_app_response_cache_files(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(main_module, "get_cache_dir", lambda: tmp_path)
    stale_app_cache = tmp_path / f"{main_module.APP_RESPONSE_CACHE_PREFIX}abc123.json"
    retained_cache = tmp_path / main_module.VALUE_BOARD_CACHE_NAME
    stale_app_cache.write_text("{}", encoding="utf-8")
    retained_cache.write_text("{}", encoding="utf-8")

    deleted: list[str] = []
    monkeypatch.setattr(main_module, "delete_json_cache", lambda name: deleted.append(name) or True)

    main_module._invalidate_read_caches()

    assert not stale_app_cache.exists()
    assert retained_cache.exists()
    assert main_module.VALUE_BOARD_CACHE_NAME not in deleted


def test_targeted_matchup_snapshot_publish_preserves_untouched_matchups(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WNBA_CACHE_DIR", str(tmp_path))

    game_a = {
        "id": 9910,
        "start_time": "2026-07-04T17:00:00+00:00",
        "home_team_id": 10,
        "away_team_id": 3,
    }
    game_b = {
        "id": 9920,
        "start_time": "2026-07-04T20:00:00+00:00",
        "home_team_id": 14,
        "away_team_id": 7,
    }

    def fake_groups(conn, game_ids=None):
        del conn
        groups = [(game_a, [9910]), (game_b, [9920])]
        if game_ids is None:
            return groups
        requested = {int(game_id) for game_id in game_ids}
        return [item for item in groups if requested.intersection(item[1])]

    def fake_matchup(conn, game, game_ids, **kwargs):
        del conn, kwargs
        return {
            "id": game["id"],
            "start_time": game["start_time"],
            "home_team_id": game["home_team_id"],
            "away_team_id": game["away_team_id"],
            "game_ids": list(game_ids),
            "props": [],
        }

    def fake_value_board(conn, game_ids, include_filtered_only=True):
        del conn, include_filtered_only
        game_id = int(game_ids[0])
        return [{"id": game_id, "expected_value": 0.1 if game_id == 9910 else 0.05, "edge": 0.1, "over_odds": -110, "under_odds": -110, "recommended_side": "over"}]

    monkeypatch.setattr(main_module, "_scheduled_matchup_game_groups", fake_groups)
    monkeypatch.setattr(main_module, "_build_matchup_payload_item", fake_matchup)
    monkeypatch.setattr(main_module, "_value_board_payload_for_games", fake_value_board)
    monkeypatch.setattr(main_module, "_prediction_state_by_game", lambda conn: {})
    monkeypatch.setattr(main_module, "_covers_records_by_game", lambda conn: {})
    monkeypatch.setattr(main_module, "_covers_market_odds_by_game", lambda: {})
    monkeypatch.setattr(main_module, "_GamePredictionCache", lambda *args: object())
    monkeypatch.setattr(main_module, "import_rotowire_lineups", lambda conn, force_refresh=False: {"source": "cache", "captured_at": None, "from_cache": True})

    existing_snapshot = main_module._matchup_snapshot_payload(
        fake_matchup(None, game_b, [9920]),
        snapshot_key=main_module._matchup_snapshot_key(game_b),
        game_ids=[9920],
        value_board_props=fake_value_board(None, [9920]),
    )
    cache_module.write_json_cache(
        main_module._matchup_snapshot_cache_name(main_module._matchup_snapshot_key(game_b)),
        main_module._cache_envelope(existing_snapshot, main_module.MATCHUPS_TTL_SECONDS),
    )

    main_module._publish_matchup_snapshot_aggregates(object(), game_ids=[9910], full_refresh=False)

    board_cache = main_module._read_cached_payload(main_module.VALUE_BOARD_CACHE_NAME)
    matchup_cache = main_module._read_cached_payload(main_module.MATCHUPS_CACHE_NAME)

    assert [item["id"] for item in board_cache] == [9910, 9920]
    assert [item["id"] for item in matchup_cache] == [9910, 9920]


def test_rotowire_refresh_route_skips_prediction_rebuild_and_only_republishes_roster(monkeypatch) -> None:
    connect_calls = 0
    deleted: list[str] = []
    roster_payload = [{"team": "ATL", "player_name": "Angel Reese", "status": "OUT"}]

    class DummyConn:
        def __enter__(self):
            nonlocal connect_calls
            connect_calls += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(
        main_module,
        "import_rotowire_lineups",
        lambda conn, force_refresh=False: {
            "source": "rotowire",
            "status": "imported",
            "roster_changed": True,
            "affected_team_ids": [3, 7],
        },
    )
    monkeypatch.setattr(main_module, "_scheduled_game_ids_for_teams", lambda conn, team_ids: [991, 992])
    monkeypatch.setattr(main_module, "delete_json_cache", lambda name: deleted.append(name) or True)

    rebuild_called = False

    def fail_rebuild(*args, **kwargs):
        nonlocal rebuild_called
        rebuild_called = True
        raise AssertionError("rebuild_predictions should not be called")

    monkeypatch.setattr(main_module, "rebuild_predictions", fail_rebuild)
    monkeypatch.setattr(
        main_module,
        "_publish_roster_cache_async",
        lambda: {"status": "queued", "started_at": "2026-05-08T00:00:00+00:00"},
    )
    monkeypatch.setattr(main_module, "_roster_payload", lambda conn, refresh_lineups=False: roster_payload)
    monkeypatch.setattr(
        main_module,
        "_queue_current_slate_repair_job",
        lambda game_ids=None, request=None: {"status": "queued", "scope": "injury_update", "target_game_ids": list(game_ids or [])},
    )
    monkeypatch.setattr(main_module, "queue_prepare_stocks_games", lambda game_ids=None: list(game_ids or []) == [991, 992])

    result = main_module._import_rotowire_injuries_impl(request=None, force_refresh=True)

    assert connect_calls == 1
    assert rebuild_called is False
    assert deleted == [main_module.ROSTER_CACHE_NAME, main_module.MATCHUPS_CACHE_NAME]
    assert result["predictions"] == 0
    assert result["affected_game_ids"] == [991, 992]
    assert result["published_payloads"] == {
        main_module.ROSTER_CACHE_NAME: 1,
    }
    assert result["roster"] == roster_payload
    assert result["repair"] == {"status": "queued", "scope": "injury_update", "target_game_ids": [991, 992]}
    assert result["specials"] == {"status": "queued", "target_game_ids": [991, 992]}


def test_rotowire_refresh_route_skips_repair_when_roster_snapshot_is_unchanged(monkeypatch) -> None:
    connect_calls = 0
    roster_payload = [{"team": "ATL", "player_name": "Angel Reese", "status": "OUT"}]

    class DummyConn:
        def __enter__(self):
            nonlocal connect_calls
            connect_calls += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(
        main_module,
        "import_rotowire_lineups",
        lambda conn, force_refresh=False: {
            "source": "rotowire",
            "status": "imported",
            "roster_changed": False,
            "affected_team_ids": [3, 7],
        },
    )
    monkeypatch.setattr(main_module, "_scheduled_game_ids_for_teams", lambda conn, team_ids: [991, 992])
    monkeypatch.setattr(main_module, "delete_json_cache", lambda name: True)
    monkeypatch.setattr(
        main_module,
        "_publish_roster_cache_async",
        lambda: {"status": "queued", "started_at": "2026-05-08T00:00:00+00:00"},
    )
    monkeypatch.setattr(main_module, "_roster_payload", lambda conn, refresh_lineups=False: roster_payload)

    repair_called = False

    def fail_queue(*args, **kwargs):
        nonlocal repair_called
        repair_called = True
        raise AssertionError("_queue_current_slate_repair_job should not be called")

    monkeypatch.setattr(main_module, "_queue_current_slate_repair_job", fail_queue)
    monkeypatch.setattr(
        main_module,
        "queue_prepare_stocks_games",
        lambda game_ids=None: (_ for _ in ()).throw(AssertionError("queue_prepare_stocks_games should not be called")),
    )

    result = main_module._import_rotowire_injuries_impl(request=None, force_refresh=True)

    assert connect_calls == 1
    assert repair_called is False
    assert result["affected_game_ids"] == [991, 992]
    assert result["roster"] == roster_payload
    assert result["repair"] == {"status": "not_needed", "scope": "injury_update", "target_game_ids": []}
    assert result["specials"] == {"status": "not_needed", "target_game_ids": []}


def test_covers_refresh_route_does_not_queue_prop_sync_after_failed_import(monkeypatch) -> None:
    connect_calls = 0

    class DummyConn:
        def __enter__(self):
            nonlocal connect_calls
            connect_calls += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(
        main_module,
        "_import_covers_provider_rows",
        lambda conn, selected_date=None, force_refresh=False, update_game_markets=True: {
            "status": "failed",
            "source": "covers",
            "message": "Fresh Covers scrape failed.",
            "prop_sync_eligible": False,
        },
    )
    monkeypatch.setattr(main_module, "_start_prop_sync_if_needed", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sync should not queue")))
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)
    monkeypatch.setattr(main_module, "_publish_post_mutation_read_payloads", lambda conn: {"matchups": 1})

    result = main_module._import_covers_impl(request=None, selected_date=None, force_refresh=True)

    assert connect_calls == 2
    assert result["sync_started"] is False
    assert result["published_payloads"] == {"matchups": 1}
    assert result["message"] == "Fresh Covers scrape failed."


def test_covers_refresh_route_does_not_queue_prop_sync_for_game_markets_only(monkeypatch) -> None:
    connect_calls = 0

    class DummyConn:
        def __enter__(self):
            nonlocal connect_calls
            connect_calls += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(
        main_module,
        "_import_covers_provider_rows",
        lambda conn, selected_date=None, force_refresh=False, update_game_markets=True: {
            "status": "imported_game_markets_only",
            "source": "covers",
            "message": "Imported Covers matchup lines without player prop rows.",
            "prop_sync_eligible": False,
        },
    )
    monkeypatch.setattr(main_module, "_start_prop_sync_if_needed", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("sync should not queue")))
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)
    monkeypatch.setattr(main_module, "_publish_post_mutation_read_payloads", lambda conn: {"matchups": 1})

    result = main_module._import_covers_impl(request=None, selected_date=None, force_refresh=True)

    assert connect_calls == 2
    assert result["sync_started"] is False
    assert result["published_payloads"] == {"matchups": 1}
    assert result["message"] == "Imported Covers matchup lines without player prop rows."


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
    monkeypatch.setattr("backend.app.main.settle_completed_props", lambda conn, selected_date=None, selected_dates=None: {"settled": 0})
    monkeypatch.setattr("backend.app.main.settle_completed_game_predictions", lambda conn, selected_date=None, selected_dates=None: {"settled": 0})
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


def test_espn_history_rejects_overlapping_imports(monkeypatch) -> None:
    class BusyLock:
        def acquire(self, blocking: bool = True) -> bool:
            return False

    monkeypatch.setattr(main_module, "_ESPN_HISTORY_IMPORT_LOCK", BusyLock())

    with pytest.raises(main_module.HTTPException) as exc_info:
        import_espn_history_endpoint(season=2026, include_player_stats=False, include_previous_season=False)

    assert exc_info.value.status_code == 409
    assert "already running" in str(exc_info.value.detail).lower()


def test_rest_days_uses_game_date_boundary_not_utc_rollover() -> None:
    with connect() as conn:
        team_id = int(conn.execute("SELECT id FROM teams WHERE abbreviation = 'PHX'").fetchone()["id"])
        opp_id = int(conn.execute("SELECT id FROM teams WHERE abbreviation = 'LV'").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, NULL, NULL)
            """,
            (900001, "2026-05-16", "2026-05-16T02:00Z", team_id, opp_id),
        )
        conn.execute(
            """
            INSERT INTO team_game_results (
                team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, ?, 1, 80, 78, 78.0, 0.0, 158.0, 'push', 'push')
            """,
            (team_id, 900001),
        )
        rest_days = main_module._rest_days_before_game(conn, team_id, "2026-05-24T19:00Z", "2026-05-24")
    assert rest_days == 8


def test_rest_days_counts_yesterday_as_one_day() -> None:
    with connect() as conn:
        team_id = int(conn.execute("SELECT id FROM teams WHERE abbreviation = 'NY'").fetchone()["id"])
        opp_id = int(conn.execute("SELECT id FROM teams WHERE abbreviation = 'LV'").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, NULL, NULL)
            """,
            (900002, "2026-05-26", "2026-05-26T23:00:00Z", team_id, opp_id),
        )
        conn.execute(
            """
            INSERT INTO team_game_results (
                team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, ?, 1, 82, 79, 79.0, 0.0, 161.0, 'push', 'push')
            """,
            (team_id, 900002),
        )
        rest_days = main_module._rest_days_before_game(conn, team_id, "2026-05-27T23:00:00Z", "2026-05-27")
    assert rest_days == 1


def test_missing_espn_scores_payload_returns_incomplete_final_game() -> None:
    with connect() as conn:
        team_home = int(conn.execute("SELECT id FROM teams WHERE abbreviation = 'PHX'").fetchone()["id"])
        team_away = int(conn.execute("SELECT id FROM teams WHERE abbreviation = 'LV'").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, NULL, NULL, ?)
            """,
            (990001, "2026-05-01", "2026-05-01T23:00:00Z", team_home, team_away, 990001),
        )
        conn.execute(
            """
            INSERT INTO team_game_results (
                team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, ?, 1, 82, 79, 79.0, 0.0, 161.0, 'push', 'push')
            """,
            (team_home, 990001),
        )
        payload = main_module._missing_espn_scores_payload(conn, limit=30)
    assert payload["count"] == 1
    assert payload["dates"] == ["2026-05-01"]
    assert payload["games"][0]["id"] == 990001
    assert payload["games"][0]["missing_reasons"] == ["team_results", "player_stats", "team_boxscores"]


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


def test_minutes_role_classification_identifies_core_starter_band() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=32.0,
        last_10_minutes_avg=31.0,
        ewma_minutes=31.5,
        minutes_trend=1.2,
        minute_volatility=3.8,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
    )

    projected, note = _project_minutes(
        None,
        player_id=1001,
        game_id=2010,
        rotation_role="starter",
        ewma_minutes=31.5,
        minutes_trend=1.2,
        recent_minutes_avg=32.0,
        last_10_minutes_avg=31.0,
        minute_volatility=3.8,
        context={"is_home": True},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=None,
        before_game_date=None,
    )

    assert role_state.bucket == "core_starter"
    assert 28.0 <= projected <= 37.0
    assert "core_starter" in note


def test_minutes_projection_allows_fringe_role_below_old_generic_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="bench",
        recent_minutes_avg=5.0,
        last_10_minutes_avg=6.0,
        ewma_minutes=6.5,
        minutes_trend=-1.5,
        minute_volatility=2.5,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
    )

    projected, note = _project_minutes(
        None,
        player_id=1002,
        game_id=2020,
        rotation_role="bench",
        ewma_minutes=6.5,
        minutes_trend=-1.5,
        recent_minutes_avg=5.0,
        last_10_minutes_avg=6.0,
        minute_volatility=2.5,
        context={"is_home": False},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=None,
        before_game_date=None,
    )

    assert role_state.bucket == "fringe"
    assert projected < 8.0
    assert "fringe" in note


def test_minutes_projection_caps_return_from_absence_downside_case() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=27.0,
        last_10_minutes_avg=31.0,
        ewma_minutes=30.0,
        minutes_trend=-4.5,
        minute_volatility=7.2,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=10.0,
    )

    projected, note = _project_minutes(
        None,
        player_id=1003,
        game_id=2030,
        rotation_role="starter",
        ewma_minutes=30.0,
        minutes_trend=-4.5,
        recent_minutes_avg=27.0,
        last_10_minutes_avg=31.0,
        minute_volatility=7.2,
        context={"is_home": False},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=10.0,
        before_game_date=None,
    )

    assert role_state.recent_absence_days == 10.0
    assert projected <= 31.0
    assert projected >= 18.0
    assert "rotation" in note or "starter_volatile" in note


def test_minutes_projection_expands_upside_for_injury_replacement_spike() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=24.0,
        last_10_minutes_avg=19.0,
        ewma_minutes=20.0,
        minutes_trend=4.5,
        minute_volatility=5.0,
        injury_status="available",
        injury_delta=2.0,
        recent_absence_days=None,
    )

    projected, note = _project_minutes(
        None,
        player_id=1004,
        game_id=2040,
        rotation_role="rotation",
        ewma_minutes=20.0,
        minutes_trend=4.5,
        recent_minutes_avg=24.0,
        last_10_minutes_avg=19.0,
        minute_volatility=5.0,
        context={"is_home": True},
        blowout_delta=0.0,
        injury_delta=2.0,
        injury_status="available",
        recent_absence_days=None,
        before_game_date=None,
    )

    assert role_state.recent_spike is True
    assert projected >= 23.0
    assert "starter_volatile" in note or "rotation" in note


def test_minutes_projection_stabilizes_against_overly_low_learned_output(monkeypatch) -> None:
    role_state = _classify_minutes_role(
        rotation_role="bench",
        recent_minutes_avg=19.0,
        last_10_minutes_avg=17.0,
        ewma_minutes=18.0,
        minutes_trend=1.0,
        minute_volatility=4.0,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
    )
    monkeypatch.setattr(
        "backend.app.player_prop_model.train_minutes_model",
        lambda *_args, **_kwargs: RidgeModel(
            market="minutes:bench",
            rows=500,
            intercept=0.0,
            coefficients=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_means=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_scales=[1.0] * len(MINUTES_FEATURE_NAMES),
        ),
    )
    monkeypatch.setattr("backend.app.player_prop_model._predict", lambda *_args, **_kwargs: -10.0)
    monkeypatch.setattr("backend.app.player_prop_model._venue_minutes_adjustment", lambda *_args, **_kwargs: 0.0)

    projected, note = _project_minutes(
        sqlite3.connect(":memory:"),
        player_id=1005,
        game_id=2050,
        rotation_role="bench",
        ewma_minutes=18.0,
        minutes_trend=1.0,
        recent_minutes_avg=19.0,
        last_10_minutes_avg=17.0,
        minute_volatility=4.0,
        context={"is_home": False, "rest_days": 2, "team_spread": 4.5},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=None,
        team_transition=[4.0, 1.0, 0.1, 0.6],
        before_game_date=None,
    )

    assert role_state.bucket in {"bench", "rotation"}
    assert projected >= 17.5
    assert "learned blend" in note
    assert "learned blend 9%/" in note
    assert "delta -10.0" in note
    assert "recency anchor" in note
    assert "baseline " in note


def test_minutes_projection_caps_stable_context_near_recent_blend(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.player_prop_model.train_minutes_model",
        lambda *_args, **_kwargs: RidgeModel(
            market="minutes:core_starter",
            rows=500,
            intercept=0.0,
            coefficients=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_means=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_scales=[1.0] * len(MINUTES_FEATURE_NAMES),
        ),
    )
    monkeypatch.setattr("backend.app.player_prop_model._predict", lambda *_args, **_kwargs: 18.0)
    monkeypatch.setattr("backend.app.player_prop_model._venue_minutes_adjustment", lambda *_args, **_kwargs: 0.0)

    recent_minutes_avg = 31.0
    last_10_minutes_avg = 30.0
    recent_blend = (0.65 * recent_minutes_avg) + (0.35 * last_10_minutes_avg)
    projected, note = _project_minutes(
        sqlite3.connect(":memory:"),
        player_id=1006,
        game_id=2060,
        rotation_role="starter",
        ewma_minutes=30.5,
        minutes_trend=0.6,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minute_volatility=3.4,
        context={"is_home": True, "rest_days": 2, "team_spread": 3.5},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=None,
        team_transition=[18.0, 0.2, 0.0, 0.78],
        before_game_date=None,
    )

    assert projected <= recent_blend + 2.2
    assert projected >= recent_blend - 2.2
    assert "stable-context recent blend cap" in note


def test_minutes_projection_tightens_stable_rotation_upside_without_vacancy(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.player_prop_model.train_minutes_model",
        lambda *_args, **_kwargs: RidgeModel(
            market="minutes:rotation",
            rows=500,
            intercept=0.0,
            coefficients=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_means=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_scales=[1.0] * len(MINUTES_FEATURE_NAMES),
        ),
    )
    monkeypatch.setattr("backend.app.player_prop_model._predict", lambda *_args, **_kwargs: 20.0)
    monkeypatch.setattr("backend.app.player_prop_model._venue_minutes_adjustment", lambda *_args, **_kwargs: 0.0)

    recent_minutes_avg = 23.0
    last_10_minutes_avg = 22.0
    recent_blend = (0.65 * recent_minutes_avg) + (0.35 * last_10_minutes_avg)
    projected, note = _project_minutes(
        sqlite3.connect(":memory:"),
        player_id=10061,
        game_id=2061,
        rotation_role="rotation",
        ewma_minutes=22.4,
        minutes_trend=0.7,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minute_volatility=3.6,
        context={"is_home": True, "rest_days": 2, "team_spread": 2.5},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=None,
        team_transition=[18.0, 0.1, 0.0, 0.84],
        lineup_context=[0.15, 0.79, 0.31],
        opportunity_context=[0.0, 0.0, 0.0, 18.0, 0.0, 0.0, 0.0],
        before_game_date=None,
    )

    assert projected <= max(recent_blend + 2.0, last_10_minutes_avg + 0.8)
    assert "learned blend 9%/33%" in note


def test_minutes_stable_context_cap_tightens_quiet_rotation_slice() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=22.0,
        last_10_minutes_avg=21.5,
        ewma_minutes=21.4,
        minutes_trend=0.6,
        minute_volatility=4.2,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        lineup_context=[0.15, 0.78, 0.31],
        opportunity_context=[0.0, 0.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=25.1,
        role_state=role_state,
        recent_blend=(0.65 * 22.0) + (0.35 * 21.5),
        recency_anchor=(0.65 * 22.0) + (0.25 * 21.5) + (0.10 * 21.4),
        last_10_minutes_avg=21.5,
        minutes_trend=0.6,
        minute_volatility=4.2,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "stable-context rotation rise cap" in notes
    assert capped == pytest.approx(22.625)


def test_minutes_projection_uses_more_conservative_blend_for_unsupported_rotation_upside(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.player_prop_model.train_minutes_model",
        lambda *_args, **_kwargs: RidgeModel(
            market="minutes:rotation",
            rows=500,
            intercept=0.0,
            coefficients=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_means=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_scales=[1.0] * len(MINUTES_FEATURE_NAMES),
        ),
    )
    monkeypatch.setattr("backend.app.player_prop_model._predict", lambda *_args, **_kwargs: 6.0)
    monkeypatch.setattr("backend.app.player_prop_model._venue_minutes_adjustment", lambda *_args, **_kwargs: 0.0)

    projected, note = _project_minutes(
        sqlite3.connect(":memory:"),
        player_id=10062,
        game_id=2062,
        rotation_role="rotation",
        ewma_minutes=22.0,
        minutes_trend=0.3,
        recent_minutes_avg=22.5,
        last_10_minutes_avg=22.0,
        minute_volatility=4.1,
        context={"is_home": True, "rest_days": 2, "team_spread": 2.0},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=None,
        team_transition=[20.0, 0.0, 0.0, 0.82],
        lineup_context=[0.15, 0.80, 0.30],
        opportunity_context=[0.0, 0.0, 0.0, 20.0, 0.0, 0.0, 0.0],
        before_game_date=None,
    )

    assert "learned blend 9%/33%" in note
    assert projected < 24.5


def test_minutes_projection_keeps_rotation_vacancy_floor_close_to_recent_blend() -> None:
    recent_minutes_avg = 21.0
    last_10_minutes_avg = 23.0
    recent_blend = (0.65 * recent_minutes_avg) + (0.35 * last_10_minutes_avg)

    projected, note = _project_minutes(
        None,
        player_id=10063,
        game_id=2063,
        rotation_role="rotation",
        ewma_minutes=20.5,
        minutes_trend=0.2,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minute_volatility=4.4,
        context={"is_home": True},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=None,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        lineup_context=[0.15, 0.79, 0.28],
        opportunity_context=[24.0, 1.0, 0.0, 20.0, 0.0, 0.0, 0.0],
        before_game_date=None,
    )

    assert projected <= recent_blend + 0.8
    assert "recency anchor" in note


def test_minutes_earned_blend_weight_cuts_stable_low_vol_rotation_weight() -> None:
    weight = player_prop_model_module._minutes_earned_blend_weight(
        base_weight=0.33,
        learned_minutes=24.8,
        recent_blend=22.2,
        recency_anchor=22.3,
        role_bucket="rotation",
        minute_volatility=4.2,
        recent_absence_days=None,
        recent_drop=False,
        recent_spike=False,
        injury_delta=0.0,
        opportunity_context=[0.0, 0.0],
    )

    assert weight == pytest.approx(0.1122)


def test_minutes_earned_blend_weight_preserves_more_weight_with_real_vacancy() -> None:
    quiet = player_prop_model_module._minutes_earned_blend_weight(
        base_weight=0.33,
        learned_minutes=24.8,
        recent_blend=22.2,
        recency_anchor=22.3,
        role_bucket="rotation",
        minute_volatility=4.2,
        recent_absence_days=None,
        recent_drop=False,
        recent_spike=False,
        injury_delta=0.0,
        opportunity_context=[0.0, 0.0],
    )
    vacancy = player_prop_model_module._minutes_earned_blend_weight(
        base_weight=0.33,
        learned_minutes=24.8,
        recent_blend=22.2,
        recency_anchor=22.3,
        role_bucket="rotation",
        minute_volatility=4.2,
        recent_absence_days=None,
        recent_drop=False,
        recent_spike=False,
        injury_delta=0.0,
        opportunity_context=[22.0, 1.0],
    )

    assert vacancy > quiet


def test_minutes_earned_blend_weight_cuts_medium_vol_rotation_weight_in_quiet_context() -> None:
    quiet = player_prop_model_module._minutes_earned_blend_weight(
        base_weight=0.33,
        learned_minutes=24.8,
        recent_blend=22.2,
        recency_anchor=22.3,
        role_bucket="rotation",
        minute_volatility=6.1,
        recent_absence_days=None,
        recent_drop=False,
        recent_spike=False,
        injury_delta=0.0,
        opportunity_context=[0.0, 0.0],
    )
    vacancy = player_prop_model_module._minutes_earned_blend_weight(
        base_weight=0.33,
        learned_minutes=24.8,
        recent_blend=22.2,
        recency_anchor=22.3,
        role_bucket="rotation",
        minute_volatility=6.1,
        recent_absence_days=None,
        recent_drop=False,
        recent_spike=False,
        injury_delta=0.0,
        opportunity_context=[18.0, 1.0],
    )

    assert quiet == pytest.approx(0.1254)
    assert vacancy > quiet


def test_minutes_context_bucket_distinguishes_soft_vacancy() -> None:
    assert main_module._minutes_context_bucket({
        "recent_transfer": 0,
        "recent_absence_days": 0.0,
        "same_position_unavailable_minutes": 12.0,
        "same_position_key_out_count": 0.0,
    }) == "soft_vacancy"
    assert main_module._minutes_context_bucket({
        "recent_transfer": 0,
        "recent_absence_days": 0.0,
        "same_position_unavailable_minutes": 20.0,
        "same_position_key_out_count": 1.0,
    }) == "vacancy"


def test_minutes_context_bucket_accepts_sqlite_row() -> None:
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                0 AS recent_transfer,
                0.0 AS recent_absence_days,
                12.0 AS same_position_unavailable_minutes,
                0.0 AS same_position_key_out_count
            """
        ).fetchone()

    assert row is not None
    assert main_module._minutes_context_bucket(row) == "soft_vacancy"


def test_minutes_projection_uses_lower_blend_for_unsupported_starter_volatile_spike(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.player_prop_model.train_minutes_model",
        lambda *_args, **_kwargs: RidgeModel(
            market="minutes:starter_volatile",
            rows=500,
            intercept=0.0,
            coefficients=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_means=[0.0] * len(MINUTES_FEATURE_NAMES),
            feature_scales=[1.0] * len(MINUTES_FEATURE_NAMES),
        ),
    )
    monkeypatch.setattr("backend.app.player_prop_model._predict", lambda *_args, **_kwargs: 6.0)
    monkeypatch.setattr("backend.app.player_prop_model._venue_minutes_adjustment", lambda *_args, **_kwargs: 0.0)

    projected, note = _project_minutes(
        sqlite3.connect(":memory:"),
        player_id=10064,
        game_id=2064,
        rotation_role="starter",
        ewma_minutes=25.0,
        minutes_trend=3.6,
        recent_minutes_avg=28.0,
        last_10_minutes_avg=24.0,
        minute_volatility=5.1,
        context={"is_home": True, "rest_days": 2, "team_spread": 2.0},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=None,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        lineup_context=[0.17, 0.82, 0.41],
        opportunity_context=[0.0, 0.0, 0.0, 20.0, 0.0, 0.0, 0.0],
        before_game_date=None,
    )

    assert "starter_volatile" in note
    assert "learned blend 9%/33%" in note
    assert projected < 29.5


def test_minutes_earned_blend_weight_keeps_soft_vacancy_above_quiet_spike() -> None:
    quiet = player_prop_model_module._minutes_earned_blend_weight(
        base_weight=0.33,
        learned_minutes=31.0,
        recent_blend=26.0,
        recency_anchor=26.5,
        role_bucket="starter_volatile",
        minute_volatility=5.2,
        recent_absence_days=None,
        recent_drop=False,
        recent_spike=True,
        injury_delta=0.0,
        opportunity_context=[0.0, 0.0],
    )
    soft = player_prop_model_module._minutes_earned_blend_weight(
        base_weight=0.33,
        learned_minutes=31.0,
        recent_blend=26.0,
        recency_anchor=26.5,
        role_bucket="starter_volatile",
        minute_volatility=5.2,
        recent_absence_days=None,
        recent_drop=False,
        recent_spike=True,
        injury_delta=0.0,
        opportunity_context=[12.0, 0.0],
    )
    hard = player_prop_model_module._minutes_earned_blend_weight(
        base_weight=0.33,
        learned_minutes=31.0,
        recent_blend=26.0,
        recency_anchor=26.5,
        role_bucket="starter_volatile",
        minute_volatility=5.2,
        recent_absence_days=None,
        recent_drop=False,
        recent_spike=True,
        injury_delta=0.0,
        opportunity_context=[22.0, 1.0],
    )

    assert quiet == soft < hard


def test_minutes_stable_context_cap_limits_soft_vacancy_starter_rise() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=25.5,
        last_10_minutes_avg=22.5,
        ewma_minutes=23.5,
        minutes_trend=3.4,
        minute_volatility=5.2,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        lineup_context=[0.17, 0.82, 0.41],
        opportunity_context=[12.0, 0.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=29.0,
        role_state=role_state,
        recent_blend=24.45,
        recency_anchor=24.55,
        last_10_minutes_avg=22.5,
        minutes_trend=3.4,
        minute_volatility=5.2,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[12.0, 0.0],
    )

    assert role_state.bucket == "starter_volatile"
    assert "soft-vacancy rise cap" in notes
    assert capped == pytest.approx(25.85)


def test_minutes_stable_context_cap_lifts_soft_vacancy_starter_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=24.8,
        last_10_minutes_avg=26.6,
        ewma_minutes=25.3,
        minutes_trend=-2.9,
        minute_volatility=4.4,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=6.0,
        lineup_context=[0.17, 0.82, 0.41],
        opportunity_context=[13.2, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=23.208,
        role_state=role_state,
        recent_blend=25.44,
        recency_anchor=25.147,
        last_10_minutes_avg=26.6,
        minutes_trend=-2.933,
        minute_volatility=4.422,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=6.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[13.2, 0.0],
    )

    assert role_state.bucket == "starter_volatile"
    assert "soft-vacancy starter floor" in notes
    assert floored == pytest.approx(24.54)


def test_minutes_stable_context_cap_limits_stable_starter_rise() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=28.0,
        last_10_minutes_avg=24.0,
        ewma_minutes=25.0,
        minutes_trend=3.6,
        minute_volatility=5.1,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        lineup_context=[0.17, 0.82, 0.41],
        opportunity_context=[0.0, 0.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=32.6,
        role_state=role_state,
        recent_blend=26.6,
        recency_anchor=26.7,
        last_10_minutes_avg=24.0,
        minutes_trend=3.6,
        minute_volatility=5.1,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "starter_volatile"
    assert "stable-context starter rise cap" in notes
    assert capped == pytest.approx(27.8)


def test_minutes_stable_context_cap_lifts_quiet_core_starter_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="star",
        recent_minutes_avg=31.0,
        last_10_minutes_avg=31.8,
        ewma_minutes=30.7,
        minutes_trend=-0.8,
        minute_volatility=5.2,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        lineup_context=[0.19, 0.86, 0.45],
        opportunity_context=[0.0, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=28.9,
        role_state=role_state,
        recent_blend=31.28,
        recency_anchor=30.96,
        last_10_minutes_avg=31.8,
        minutes_trend=-0.8,
        minute_volatility=5.2,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "core_starter"
    assert "stable-context core-starter floor" in notes
    assert floored == pytest.approx(29.98)


def test_minutes_stable_context_cap_limits_medium_vol_stable_rotation_rise() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=20.0,
        last_10_minutes_avg=18.7,
        ewma_minutes=19.4,
        minutes_trend=1.8,
        minute_volatility=7.1,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        lineup_context=[0.14, 0.76, 0.25],
        opportunity_context=[0.0, 0.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=21.8,
        role_state=role_state,
        recent_blend=19.545,
        recency_anchor=19.9,
        last_10_minutes_avg=18.7,
        minutes_trend=1.8,
        minute_volatility=7.1,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "stable-context rotation rise cap" in notes
    assert capped == pytest.approx(20.345)


def test_minutes_stable_context_cap_lifts_stable_starter_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=28.5,
        last_10_minutes_avg=31.0,
        ewma_minutes=29.8,
        minutes_trend=-4.8,
        minute_volatility=4.8,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        lineup_context=[0.17, 0.82, 0.41],
        opportunity_context=[8.4, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=26.563,
        role_state=role_state,
        recent_blend=30.115,
        recency_anchor=29.775,
        last_10_minutes_avg=31.0,
        minutes_trend=-4.8,
        minute_volatility=4.8,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[8.4, 0.0],
    )

    assert role_state.bucket == "starter_volatile"
    assert "stable-context starter floor" in notes
    assert floored == pytest.approx(28.075)


def test_minutes_projection_keeps_starter_vacancy_floor_close_to_recent_blend() -> None:
    recent_minutes_avg = 24.0
    last_10_minutes_avg = 26.0
    recent_blend = (0.65 * recent_minutes_avg) + (0.35 * last_10_minutes_avg)

    projected, note = _project_minutes(
        None,
        player_id=10066,
        game_id=2066,
        rotation_role="starter",
        ewma_minutes=23.0,
        minutes_trend=0.1,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minute_volatility=5.8,
        context={"is_home": True},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=None,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        lineup_context=[0.17, 0.82, 0.41],
        opportunity_context=[30.0, 1.0, 0.0, 20.0, 0.0, 0.0, 0.0],
        before_game_date=None,
    )

    assert projected <= recent_blend + 1.6
    assert "hard rule same-position vacancy floor" in note


def test_minutes_stable_context_cap_lifts_soft_vacancy_rotation_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=21.0,
        last_10_minutes_avg=23.0,
        ewma_minutes=20.5,
        minutes_trend=-0.4,
        minute_volatility=4.8,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        lineup_context=[0.15, 0.79, 0.28],
        opportunity_context=[13.2, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=20.2,
        role_state=role_state,
        recent_blend=21.7,
        recency_anchor=21.6,
        last_10_minutes_avg=23.0,
        minutes_trend=-0.4,
        minute_volatility=4.8,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[13.2, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "soft-vacancy rotation floor" in notes
    assert floored == pytest.approx(21.8)


def test_minutes_stable_context_cap_limits_soft_vacancy_rotation_rise() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=18.4,
        last_10_minutes_avg=15.9,
        ewma_minutes=17.3,
        minutes_trend=4.5,
        minute_volatility=5.8,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=4.0,
        lineup_context=[0.13, 0.73, 0.24],
        opportunity_context=[13.5, 0.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=21.2,
        role_state=role_state,
        recent_blend=17.525,
        recency_anchor=17.95,
        last_10_minutes_avg=15.9,
        minutes_trend=4.5,
        minute_volatility=5.8,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=4.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[13.5, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "soft-vacancy rotation rise cap" in notes
    assert capped == pytest.approx(18.925)


def test_minutes_stable_context_cap_lifts_stable_rotation_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=22.0,
        last_10_minutes_avg=22.7,
        ewma_minutes=21.5,
        minutes_trend=-1.2,
        minute_volatility=4.4,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        lineup_context=[0.14, 0.76, 0.25],
        opportunity_context=[0.0, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=20.4,
        role_state=role_state,
        recent_blend=22.245,
        recency_anchor=22.075,
        last_10_minutes_avg=22.7,
        minutes_trend=-1.2,
        minute_volatility=4.4,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "stable-context rotation floor" in notes
    assert floored == pytest.approx(21.175)


def test_minutes_stable_context_cap_lifts_quiet_medium_vol_rotation_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=22.6,
        last_10_minutes_avg=23.2,
        ewma_minutes=22.1,
        minutes_trend=-2.5,
        minute_volatility=6.3,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        lineup_context=[0.14, 0.76, 0.25],
        opportunity_context=[0.0, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=22.623,
        role_state=role_state,
        recent_blend=24.04,
        recency_anchor=21.429,
        last_10_minutes_avg=23.2,
        minutes_trend=-2.5,
        minute_volatility=6.3,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "stable-context rotation floor" in notes
    assert floored == pytest.approx(22.84)


def test_minutes_stable_context_cap_lifts_low_vol_stable_rotation_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=22.8,
        last_10_minutes_avg=23.0,
        ewma_minutes=22.4,
        minutes_trend=-1.2,
        minute_volatility=4.4,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=6.0,
        lineup_context=[0.14, 0.76, 0.25],
        opportunity_context=[0.0, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=20.627,
        role_state=role_state,
        recent_blend=22.87,
        recency_anchor=22.48,
        last_10_minutes_avg=23.0,
        minutes_trend=-1.2,
        minute_volatility=4.4,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=6.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "stable-context rotation floor" in notes
    assert floored == pytest.approx(21.77)


def test_minutes_stable_context_cap_lifts_near_stable_medium_vol_rotation_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=21.4,
        last_10_minutes_avg=20.7,
        ewma_minutes=20.9,
        minutes_trend=-3.5,
        minute_volatility=7.1,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=4.0,
        lineup_context=[0.14, 0.76, 0.25],
        opportunity_context=[6.5, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=18.252,
        role_state=role_state,
        recent_blend=21.165,
        recency_anchor=19.1,
        last_10_minutes_avg=20.7,
        minutes_trend=-3.5,
        minute_volatility=7.1,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=4.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[6.5, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "stable-context rotation floor" in notes
    assert floored == pytest.approx(19.865)


def test_minutes_stable_context_cap_limits_stable_rotation_rise() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=20.8,
        last_10_minutes_avg=19.6,
        ewma_minutes=20.0,
        minutes_trend=1.6,
        minute_volatility=5.0,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        lineup_context=[0.14, 0.76, 0.25],
        opportunity_context=[0.0, 0.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=22.387,
        role_state=role_state,
        recent_blend=20.38,
        recency_anchor=20.7,
        last_10_minutes_avg=19.6,
        minutes_trend=1.6,
        minute_volatility=5.0,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "stable-context rotation rise cap" in notes
    assert capped == pytest.approx(21.18)


def test_minutes_stable_context_cap_limits_vacancy_rotation_rise() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=16.8,
        last_10_minutes_avg=15.3,
        ewma_minutes=16.1,
        minutes_trend=4.1,
        minute_volatility=6.7,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=2.0,
        lineup_context=[0.12, 0.71, 0.24],
        opportunity_context=[48.0, 2.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=20.2,
        role_state=role_state,
        recent_blend=16.275,
        recency_anchor=16.85,
        last_10_minutes_avg=15.3,
        minutes_trend=4.1,
        minute_volatility=6.7,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=2.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[48.0, 2.0],
    )

    assert role_state.bucket == "rotation"
    assert "vacancy rotation rise cap" in notes
    assert capped == pytest.approx(17.775)


def test_minutes_stable_context_cap_limits_stable_vacancy_rotation_rise() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=17.8,
        last_10_minutes_avg=17.2,
        ewma_minutes=17.0,
        minutes_trend=2.4,
        minute_volatility=5.1,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=2.0,
        lineup_context=[0.13, 0.74, 0.24],
        opportunity_context=[53.0, 1.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=18.9,
        role_state=role_state,
        recent_blend=16.95,
        recency_anchor=17.35,
        last_10_minutes_avg=17.2,
        minutes_trend=2.4,
        minute_volatility=5.1,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=2.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[53.0, 1.0],
    )

    assert role_state.bucket == "rotation"
    assert "stable-vacancy rotation rise cap" in notes
    assert capped == pytest.approx(18.2)


def test_minutes_stable_context_cap_limits_vacancy_starter_rise() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=24.8,
        last_10_minutes_avg=20.9,
        ewma_minutes=23.1,
        minutes_trend=4.8,
        minute_volatility=6.7,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=2.0,
        lineup_context=[0.14, 0.72, 0.31],
        opportunity_context=[31.0, 1.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=31.2,
        role_state=role_state,
        recent_blend=23.435,
        recency_anchor=23.9,
        last_10_minutes_avg=20.9,
        minutes_trend=4.8,
        minute_volatility=6.7,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=2.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[31.0, 1.0],
    )

    assert role_state.bucket == "starter_volatile"
    assert "vacancy starter rise cap" in notes
    assert capped == pytest.approx(25.335)


def test_minutes_stable_context_lifts_low_vol_stable_starter_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=27.0,
        last_10_minutes_avg=28.2,
        ewma_minutes=27.4,
        minutes_trend=-1.1,
        minute_volatility=4.6,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=4.0,
        lineup_context=[0.15, 0.74, 0.32],
        opportunity_context=[0.0, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=24.7,
        role_state=role_state,
        recent_blend=27.42,
        recency_anchor=27.08,
        last_10_minutes_avg=28.2,
        minutes_trend=-1.1,
        minute_volatility=4.6,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=4.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "starter_volatile"
    assert "stable-context starter floor" in notes
    assert floored == pytest.approx(26.02)


def test_minutes_stable_context_lifts_quiet_medium_vol_stable_starter_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=24.8,
        last_10_minutes_avg=25.3,
        ewma_minutes=24.2,
        minutes_trend=0.2,
        minute_volatility=6.0,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=2.0,
        lineup_context=[0.15, 0.74, 0.32],
        opportunity_context=[6.7, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=23.387,
        role_state=role_state,
        recent_blend=25.29,
        recency_anchor=22.502,
        last_10_minutes_avg=25.3,
        minutes_trend=0.2,
        minute_volatility=6.0,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=2.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[6.7, 0.0],
    )

    assert role_state.bucket == "starter_volatile"
    assert "stable-context starter floor" in notes
    assert floored == pytest.approx(23.79)


def test_minutes_stable_context_cap_lifts_short_absence_return_floor() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=24.0,
        last_10_minutes_avg=23.0,
        ewma_minutes=22.5,
        minutes_trend=-2.0,
        minute_volatility=4.9,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=8.0,
        lineup_context=[0.17, 0.82, 0.41],
        opportunity_context=[0.0, 0.0],
    )

    floored, notes = player_prop_model_module._apply_minutes_stable_context_cap(
        projected=20.6,
        role_state=role_state,
        recent_blend=23.65,
        recency_anchor=23.475,
        last_10_minutes_avg=23.0,
        minutes_trend=-2.0,
        minute_volatility=4.9,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=8.0,
        team_transition=[18.0, 0.0, 0.0, 0.82],
        opportunity_context=[0.0, 0.0],
    )

    assert role_state.bucket == "rotation"
    assert "absence-return floor" in notes
    assert floored == pytest.approx(22.375)


def test_minutes_reference_baseline_stays_closer_to_recent_blend_for_low_vol_drop() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=28.5,
        last_10_minutes_avg=31.0,
        ewma_minutes=29.8,
        minutes_trend=-4.8,
        minute_volatility=4.8,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
    )

    baseline = player_prop_model_module._minutes_reference_baseline(
        role_state=role_state,
        base_heuristic=28.0,
        recent_blend=30.115,
        recent_minutes_avg=28.5,
        last_10_minutes_avg=31.0,
        minutes_trend=-4.8,
        minute_volatility=4.8,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=3.0,
    )

    assert baseline == pytest.approx(29.4382)


def test_minutes_reference_baseline_stays_closer_to_recent_blend_for_fringe_drop() -> None:
    role_state = _classify_minutes_role(
        rotation_role="bench",
        recent_minutes_avg=9.4,
        last_10_minutes_avg=10.9,
        ewma_minutes=8.3,
        minutes_trend=-6.9,
        minute_volatility=5.4,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=5.0,
    )

    baseline = player_prop_model_module._minutes_reference_baseline(
        role_state=role_state,
        base_heuristic=6.1,
        recent_blend=11.84,
        recent_minutes_avg=9.4,
        last_10_minutes_avg=10.9,
        minutes_trend=-6.9,
        minute_volatility=5.4,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=5.0,
    )

    assert role_state.bucket == "fringe"
    assert baseline == pytest.approx(10.5772)


def test_minutes_projection_rebound_guard_protects_strong_role_drop() -> None:
    recent_minutes_avg = 25.0
    last_10_minutes_avg = 29.0
    recent_blend = (0.65 * recent_minutes_avg) + (0.35 * last_10_minutes_avg)

    projected, note = _project_minutes(
        None,
        player_id=1007,
        game_id=2070,
        rotation_role="starter",
        ewma_minutes=23.0,
        minutes_trend=-8.0,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minute_volatility=6.1,
        context={"is_home": True},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=3.0,
        team_transition=[20.0, -6.2, -0.25, 0.46],
        lineup_context=[0.15, 0.84, 0.30],
        before_game_date=None,
    )

    assert projected >= recent_blend - 1.9
    assert "rebound guard strong-role drop" in note


def test_minutes_projection_rebound_guard_skips_weak_lineup_context() -> None:
    recent_minutes_avg = 25.0
    last_10_minutes_avg = 29.0
    recent_blend = (0.65 * recent_minutes_avg) + (0.35 * last_10_minutes_avg)

    projected, note = _project_minutes(
        None,
        player_id=1008,
        game_id=2080,
        rotation_role="starter",
        ewma_minutes=23.0,
        minutes_trend=-8.0,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minute_volatility=6.1,
        context={"is_home": True},
        blowout_delta=0.0,
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=3.0,
        team_transition=[20.0, -6.2, -0.25, 0.46],
        lineup_context=[0.09, 0.56, 0.18],
        before_game_date=None,
    )

    assert projected < recent_blend - 2.0
    assert "rebound guard strong-role drop" not in note


def test_minutes_hard_rules_soft_vacancy_starter_blowout_cap_is_less_aggressive() -> None:
    role_state = _classify_minutes_role(
        rotation_role="starter",
        recent_minutes_avg=31.2,
        last_10_minutes_avg=32.0,
        ewma_minutes=31.0,
        minutes_trend=2.4,
        minute_volatility=3.6,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=2.0,
        lineup_context=[0.18, 0.85, 0.33],
        opportunity_context=[15.1, 0.0],
    )

    capped, notes = player_prop_model_module._apply_minutes_hard_rules(
        projected=33.5,
        role_state=role_state,
        rotation_role="starter",
        injury_status="available",
        injury_delta=0.0,
        blowout_delta=-1.44,
        recent_blend=33.82,
        recent_minutes_avg=31.2,
        last_10_minutes_avg=32.0,
        opportunity_context=[15.1, 0.0],
    )

    assert role_state.bucket == "core_starter"
    assert "hard rule blowout star cap" in notes
    assert capped == pytest.approx(33.02)


def test_minutes_metric_summary_exposes_production_alias() -> None:
    summary = main_module._minutes_metric_summary(
        [
            {
                "learned_error": 2.0,
                "heuristic_error": 1.0,
                "recent5_error": 0.5,
                "recent10_error": -0.5,
                "recent_blend_error": 0.25,
            },
            {
                "learned_error": -1.0,
                "heuristic_error": -2.0,
                "recent5_error": -1.5,
                "recent10_error": 1.5,
                "recent_blend_error": -0.75,
            },
        ]
    )

    assert summary["production_mae"] == summary["learned_mae"] == 1.5
    assert summary["production_bias"] == summary["learned_bias"] == 0.5


def test_minutes_breakdown_entry_exposes_recent_blend_delta() -> None:
    rows = [
        {
            "learned_error": 3.0,
            "heuristic_error": 2.0,
            "recent5_error": 1.0,
            "recent10_error": 1.5,
            "recent_blend_error": 1.0,
            "learned_minutes": 27.0,
            "recent_blend_minutes": 24.0,
            "post_blend_projection": 26.5,
            "post_anchor_projection": 26.0,
            "post_baseline_projection": 25.5,
            "post_recency_floor_projection": 25.0,
            "post_bounds_projection": 25.0,
            "post_hard_rules_projection": 25.5,
            "post_rebound_guard_projection": 26.5,
            "final_projection": 27.0,
        },
        {
            "learned_error": -1.0,
            "heuristic_error": -0.5,
            "recent5_error": -1.5,
            "recent10_error": -1.0,
            "recent_blend_error": -0.5,
            "learned_minutes": 19.0,
            "recent_blend_minutes": 20.0,
            "post_blend_projection": 19.5,
            "post_anchor_projection": 19.0,
            "post_baseline_projection": 19.5,
            "post_recency_floor_projection": 19.0,
            "post_bounds_projection": 19.0,
            "post_hard_rules_projection": 19.0,
            "post_rebound_guard_projection": 19.0,
            "final_projection": 19.0,
        },
    ]

    entry = main_module._minutes_breakdown_entry(rows)

    assert entry["production_mae"] == pytest.approx(2.0)
    assert entry["recent_blend_mae"] == pytest.approx(0.75)
    assert entry["delta_vs_recent_blend"] == pytest.approx(1.25)
    assert entry["overpredict_rate"] == pytest.approx(0.5)
    assert entry["underpredict_rate"] == pytest.approx(0.5)
    assert entry["avg_learned_minus_recent_blend"] == pytest.approx(1.0)
    assert entry["avg_post_blend_minus_recent_blend"] == pytest.approx(1.0)
    assert entry["avg_post_anchor_minus_recent_blend"] == pytest.approx(0.5)
    assert entry["avg_post_baseline_minus_recent_blend"] == pytest.approx(0.5)
    assert entry["avg_post_recency_floor_minus_recent_blend"] == pytest.approx(0.0)
    assert entry["avg_post_bounds_minus_recent_blend"] == pytest.approx(0.0)
    assert entry["avg_post_hard_rules_minus_recent_blend"] == pytest.approx(0.25)
    assert entry["avg_post_rebound_guard_minus_recent_blend"] == pytest.approx(0.75)
    assert entry["avg_final_minus_recent_blend"] == pytest.approx(1.0)


def test_minutes_top_loss_rows_orders_by_regression_vs_recent_blend() -> None:
    rows = [
        {
            "player_id": 1,
            "game_id": 10,
            "game_date": "2026-07-01",
            "role_bucket": "rotation",
            "trend_bucket": "stable",
            "volatility_bucket": "low",
            "context_bucket": "stable_context",
            "actual_minutes": 20.0,
            "recent_blend_minutes": 21.0,
            "learned_minutes": 25.0,
            "recent_blend_error": 1.0,
            "learned_error": 5.0,
            "post_blend_projection": 24.0,
            "post_anchor_projection": 23.0,
            "post_baseline_projection": 22.5,
            "post_recency_floor_projection": 22.0,
            "post_bounds_projection": 22.0,
            "post_hard_rules_projection": 24.0,
            "post_rebound_guard_projection": 25.0,
            "final_projection": 25.0,
            "minutes_trend": 0.2,
            "minute_volatility": 4.0,
            "recent_absence_days": None,
            "same_position_unavailable_minutes": 0.0,
            "same_position_key_out_count": 0.0,
        },
        {
            "player_id": 2,
            "game_id": 11,
            "game_date": "2026-07-02",
            "role_bucket": "rotation",
            "trend_bucket": "stable",
            "volatility_bucket": "low",
            "context_bucket": "stable_context",
            "actual_minutes": 20.0,
            "recent_blend_minutes": 21.0,
            "learned_minutes": 22.0,
            "recent_blend_error": 1.0,
            "learned_error": 2.0,
            "post_blend_projection": 21.5,
            "post_anchor_projection": 21.4,
            "post_baseline_projection": 21.3,
            "post_recency_floor_projection": 21.2,
            "post_bounds_projection": 21.2,
            "post_hard_rules_projection": 21.8,
            "post_rebound_guard_projection": 22.0,
            "final_projection": 22.0,
            "minutes_trend": 0.1,
            "minute_volatility": 4.1,
            "recent_absence_days": None,
            "same_position_unavailable_minutes": 0.0,
            "same_position_key_out_count": 0.0,
        },
    ]

    top_rows = main_module._minutes_top_loss_rows(rows, role_bucket="rotation", limit=2)

    assert len(top_rows) == 2
    assert top_rows[0]["player_id"] == 1
    assert top_rows[0]["delta_vs_recent_blend_abs"] == pytest.approx(4.0)


def test_minutes_feature_values_include_role_shift_signals() -> None:
    role_state = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=24.0,
        last_10_minutes_avg=18.0,
        ewma_minutes=20.0,
        minutes_trend=4.0,
        minute_volatility=5.5,
        injury_status="available",
        injury_delta=1.5,
        recent_absence_days=8.0,
    )

    features = _minutes_feature_values(
        role_state=role_state,
        ewma_minutes=20.0,
        recent_minutes_avg=24.0,
        last_10_minutes_avg=18.0,
        minutes_trend=4.0,
        minute_volatility=5.5,
        rest_days=2,
        is_home=True,
        spread_abs=6.5,
        injury_delta=1.5,
        recent_absence_days=8.0,
        lineup_context=[0.19, 0.82, 0.61],
        opportunity_context=[22.0, 1.0, 11.5, 34.0, -2.0, 3.5, 5.5],
    )
    feature_map = dict(zip(MINUTES_FEATURE_NAMES, features))

    assert feature_map["recent_change_ratio"] == pytest.approx(24.0 / 18.0)
    assert feature_map["recent_vs_ewma_gap"] == pytest.approx(4.0)
    assert feature_map["absence_return_flag"] == pytest.approx(1.0)
    assert feature_map["recent_team_minute_share"] == pytest.approx(0.19)
    assert feature_map["recent_minute_rank"] == pytest.approx(0.82)
    assert feature_map["recent_position_minute_share"] == pytest.approx(0.61)
    assert feature_map["same_position_unavailable_minutes"] == pytest.approx(22.0)
    assert feature_map["same_position_key_out_count"] == pytest.approx(1.0)
    assert feature_map["same_position_opportunity_persistence"] == pytest.approx(11.5)
    assert feature_map["same_position_competition_minutes"] == pytest.approx(34.0)
    assert feature_map["same_position_opportunity_trend"] == pytest.approx(-2.0)
    assert feature_map["same_position_competition_trend"] == pytest.approx(3.5)
    assert feature_map["same_position_returner_pressure"] == pytest.approx(5.5)


def test_minutes_role_classification_uses_lineup_and_vacancy_context() -> None:
    baseline = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=21.0,
        last_10_minutes_avg=20.0,
        ewma_minutes=20.5,
        minutes_trend=1.0,
        minute_volatility=4.5,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
    )
    promoted = _classify_minutes_role(
        rotation_role="rotation",
        recent_minutes_avg=21.0,
        last_10_minutes_avg=20.0,
        ewma_minutes=20.5,
        minutes_trend=1.0,
        minute_volatility=4.5,
        injury_status="available",
        injury_delta=0.0,
        recent_absence_days=None,
        lineup_context=[0.18, 0.85, 0.60],
        opportunity_context=[22.0, 1.0, 11.5, 34.0, -2.0, 3.5, 5.5],
    )

    assert promoted.anchor_minutes > baseline.anchor_minutes
    assert promoted.bucket in {"starter_volatile", "core_starter"}


def test_historical_minutes_opportunity_context_uses_same_position_dnp_teammates() -> None:
    load_test_history()
    clear_model_cache()
    observed_at = datetime.now(timezone.utc).isoformat()

    with connect() as conn:
        conn.execute(
            "INSERT INTO teams (id, abbreviation, name) VALUES (?, ?, ?)",
            (99, "TMP", "Temp Team"),
        )
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (9101, "Target Guard", 99, "G", "rotation"),
        )
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (9102, "Unavailable Guard", 99, "G", "starter"),
        )
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (9103, "Unavailable Forward", 99, "F", "starter"),
        )
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9901, "2026-06-10", "2026-06-10T19:00:00+00:00", 99, 10, "final", 2, 2, -3.5, 164.5),
        )
        for game_id, game_date, minutes in (
            (9891, "2026-05-20", 30.0),
            (9892, "2026-05-24", 28.0),
            (9893, "2026-05-28", 26.0),
            (9894, "2026-06-01", 24.0),
        ):
            conn.execute(
                """
                INSERT INTO games (
                    id, game_date, start_time, home_team_id, away_team_id, status,
                    rest_days_home, rest_days_away, spread_home, game_total
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (game_id, game_date, f"{game_date}T19:00:00+00:00", 99, 10, "final", 2, 2, -2.5, 163.0),
            )
            conn.execute(
                """
                INSERT INTO player_game_stats (
                    player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (9102, game_id, minutes, 10, 3, 2, 1, 1, 0, 1),
            )
        conn.execute(
            """
            INSERT INTO player_game_availability (
                player_id, game_id, team_id, source, is_active, did_not_play, status_reason, minutes_text, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9102, 9901, 99, "espn_boxscore", 0, 1, "COACH'S DECISION", "DNP-CD", observed_at),
        )
        conn.execute(
            """
            INSERT INTO player_game_availability (
                player_id, game_id, team_id, source, is_active, did_not_play, status_reason, minutes_text, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9103, 9901, 99, "espn_boxscore", 0, 1, "COACH'S DECISION", "DNP-CD", observed_at),
        )
        conn.commit()

        result = _historical_minutes_opportunity_context(
            conn,
            player_id=9101,
            game_id=9901,
            team_id=99,
            position="G",
            before_game_date="2026-06-10",
        )

    assert result[0] == pytest.approx((30.0 + 28.0 + 26.0 + 24.0) / 4.0)
    assert result[1] == pytest.approx(1.0)
    assert result[2] == pytest.approx(0.0)
    assert result[3] == pytest.approx((30.0 + 28.0 + 26.0 + 24.0) / 4.0)
    assert result[4] == pytest.approx(0.0)
    assert result[5] == pytest.approx(((24.0 + 26.0) / 2.0) - ((28.0 + 30.0) / 2.0))
    assert result[6] == pytest.approx(0.0)


def test_train_minutes_model_returns_model_with_history() -> None:
    load_test_history()
    clear_model_cache()
    with connect() as conn:
        conn.commit()
        model = train_minutes_model(conn)

    assert model is not None
    assert model.rows > 0


def test_minutes_training_db_materializes_curated_examples() -> None:
    load_test_history()
    clear_model_cache()

    with connect() as conn:
        info = ensure_minutes_training_db(conn, force=True)
        samples, loaded = load_minutes_training_examples(conn, role_bucket=None, force_rebuild=False)

    assert Path(str(info["path"])).exists()
    assert info["rebuilt"] is True
    assert int(info["included_rows"]) > 0
    assert int(info["candidate_rows"]) >= int(info["included_rows"])
    assert loaded["source_signature"] == info["source_signature"]
    assert samples
    assert len(samples[0][0]) == len(MINUTES_FEATURE_NAMES)


def test_train_minutes_model_uses_materialized_training_db() -> None:
    load_test_history()
    clear_model_cache()

    with connect() as conn:
        ensure_minutes_training_db(conn, force=True)
        model = train_minutes_model(conn)

    assert model is not None
    assert model.market.startswith("minutes:")
    assert model.rows > 0


def test_minutes_training_db_excludes_dnp_and_persists_quality_flags() -> None:
    load_test_history()
    clear_model_cache()
    observed_at = datetime.now(timezone.utc).isoformat()

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO player_game_availability (
                player_id, game_id, team_id, source, is_active, did_not_play, status_reason, minutes_text, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (1001, 110, 10, "espn_boxscore", 0, 1, "COACH'S DECISION", "DNP-CD", observed_at),
        )
        info = ensure_minutes_training_db(conn, force=True)

    training_db_path = paths_module.get_training_db_path()
    with sqlite3.connect(training_db_path) as training_conn:
        training_conn.row_factory = sqlite3.Row
        row = training_conn.execute(
            """
            SELECT did_not_play, exclusion_reason, quality_flags_json, status_reason, minutes_text
            FROM minutes_training_examples
            WHERE source_player_id = ? AND source_game_id = ?
            """,
            (1001, 110),
        ).fetchone()
        metadata_rows = training_conn.execute(
            "SELECT key, value FROM minutes_training_metadata WHERE key = 'exclusion_counts'"
        ).fetchall()

    assert info["excluded_rows"] >= 1
    assert row is not None
    assert int(row["did_not_play"]) == 1
    assert row["exclusion_reason"] == "did_not_play"
    flags = json.loads(str(row["quality_flags_json"]))
    assert int(flags["has_availability_row"]) == 1
    assert int(flags["did_not_play"]) == 1
    assert int(flags["coach_decision_dnp"]) == 1
    assert str(row["status_reason"]) == "COACH'S DECISION"
    assert str(row["minutes_text"]) == "DNP-CD"
    assert metadata_rows
    exclusion_counts = json.loads(str(metadata_rows[0]["value"]))
    assert int(exclusion_counts["did_not_play"]) >= 1


def test_minutes_training_db_flags_injury_exit_low_minutes() -> None:
    availability = {
        "status_reason": "Left game with ankle injury",
        "minutes_text": "8",
        "did_not_play": 0,
    }

    assert minutes_training_db_module._injury_exit_risk_flag(
        availability=availability,  # type: ignore[arg-type]
        target_minutes=8.0,
        recent_blend=24.0,
    )
    assert (
        minutes_training_db_module._derive_cleanup_exclusion_reason(
            availability=availability,  # type: ignore[arg-type]
            target_minutes=8.0,
            recent_blend=24.0,
            recent_absence_days=None,
            team_margin=4.0,
            quality_flags={"injury_exit_risk_flag": 1},
            existing_reason=None,
        )
        == "injury_exit_low_minutes"
    )


def test_minutes_training_db_flags_overtime_like_spike() -> None:
    assert minutes_training_db_module._overtime_like_spike_flag(
        target_minutes=41.0,
        recent_blend=31.0,
        team_margin=3.0,
    )
    assert (
        minutes_training_db_module._derive_cleanup_exclusion_reason(
            availability=None,
            target_minutes=41.0,
            recent_blend=31.0,
            recent_absence_days=None,
            team_margin=3.0,
            quality_flags={"overtime_like_spike_flag": 1},
            existing_reason=None,
        )
        == "overtime_like_spike"
    )


def test_minutes_training_db_flags_low_minutes_role_collapse() -> None:
    assert minutes_training_db_module._low_minutes_collapse_flag(
        target_minutes=7.0,
        recent_blend=22.0,
        recent_absence_days=None,
        team_margin=6.0,
    )
    assert (
        minutes_training_db_module._derive_cleanup_exclusion_reason(
            availability=None,
            target_minutes=7.0,
            recent_blend=22.0,
            recent_absence_days=None,
            team_margin=6.0,
            quality_flags={"low_minutes_collapse_flag": 1},
            existing_reason=None,
        )
        == "low_minutes_role_collapse"
    )


def test_training_data_signature_includes_minutes_training_db_signature() -> None:
    load_test_history()
    clear_model_cache()

    with connect() as conn:
        ensure_minutes_training_db(conn, force=True)
        signature = training_module._training_data_signature(conn)

    assert isinstance(signature, str)
    assert len(signature) == 16


def test_player_archetype_profile_classifies_usage_scorer_and_assist_guard() -> None:
    load_test_history()
    with connect() as conn:
        usage_profile = _player_archetype_profile(
            conn,
            player_id=1001,
            reference_game_date="2026-05-30",
            exclude_game_id=2010,
            fallback_rotation_role="star",
        )
        assist_profile = _player_archetype_profile(
            conn,
            player_id=1003,
            reference_game_date="2026-05-30",
            exclude_game_id=2010,
            fallback_rotation_role="starter",
        )

    assert usage_profile.usage_scorer is True
    assert usage_profile.rebound_big is True
    assert assist_profile.assist_guard is True


def test_feature_snapshot_reason_includes_archetype_note() -> None:
    load_test_history()
    with connect() as conn:
        snapshot = feature_snapshot(conn, 1001, "points", 2010)

    assert "Archetype:" in snapshot.reason


def test_feature_snapshot_ignores_later_injury_rows() -> None:
    load_test_history()
    later_injury_time = datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc).isoformat()
    with connect() as conn:
        baseline = feature_snapshot(conn, 1002, "assists", 2020)
        conn.execute(
            "INSERT INTO injuries (player_id, status, note, captured_at) VALUES (?, ?, ?, ?)",
            (1002, "out", "later injury", later_injury_time),
        )
        adjusted = feature_snapshot(conn, 1002, "assists", 2020)

    assert baseline.injury_status == "available"
    assert adjusted.injury_status == "available"
    assert adjusted.hard_cap_zero is False
    assert adjusted.component_projection == baseline.component_projection


def test_injury_adjustment_ignores_teammate_games_after_as_of_date() -> None:
    with connect() as conn:
        team_id = int(conn.execute("SELECT id FROM teams ORDER BY id LIMIT 1").fetchone()["id"])
        opp_id = int(conn.execute("SELECT id FROM teams WHERE id != ? ORDER BY id LIMIT 1", (team_id,)).fetchone()["id"])
        conn.executemany(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            [
                (9101, "Target Guard", team_id, "G", "starter"),
                (9102, "Injured Teammate", team_id, "G", "starter"),
            ],
        )
        game_rows = [
            (9201, "2026-05-01", "2026-05-01T23:00:00Z"),
            (9202, "2026-05-02", "2026-05-02T23:00:00Z"),
            (9203, "2026-05-03", "2026-05-03T23:00:00Z"),
            (9204, "2026-05-04", "2026-05-04T23:00:00Z"),
            (9205, "2026-05-05", "2026-05-05T23:00:00Z"),
            (9206, "2026-05-06", "2026-05-06T23:00:00Z"),
            (9207, "2026-05-10", "2026-05-10T23:00:00Z"),
        ]
        for game_id, game_date, start_time in game_rows:
            conn.execute(
                """
                INSERT INTO games (
                    id, game_date, start_time, home_team_id, away_team_id, status,
                    rest_days_home, rest_days_away, spread_home, game_total
                ) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, -4.5, 162.0)
                """,
                (game_id, game_date, start_time, team_id, opp_id),
            )
            conn.executemany(
                """
                INSERT INTO player_game_stats (
                    player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (9101, game_id, 32, 14, 4, 5, 1, 1, 0, 2),
                    (9102, game_id, 28, 10 if game_id < 9207 else 40, 3, 2, 0, 0, 0, 1),
                ],
            )
        conn.execute(
            "INSERT INTO injuries (player_id, status, note, captured_at) VALUES (?, ?, ?, ?)",
            (9102, "out", "test injury", "2026-05-04T12:00:00Z"),
        )

        injury = _injury_adjustment_for_prop(
            conn,
            player_id=9101,
            team_id=team_id,
            rotation_role="starter",
            as_of_date="2026-05-06",
        )

    assert injury["status"] == "available"
    assert injury["minutes_delta"] == 0.8
    assert injury["usage_multiplier"] == pytest.approx(1.1385714286)


def test_injury_adjustment_reuses_cached_teammate_snapshot_for_same_team_and_date() -> None:
    load_test_history()
    with connect() as base_conn:
        team_id = 78
        base_conn.execute(
            "INSERT INTO teams (id, name, abbreviation) VALUES (?, ?, ?)",
            (team_id, "Cache Team", "CTM"),
        )
        base_conn.executemany(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            [
                (9201, "Cache One", team_id, "G", "starter"),
                (9202, "Cache Two", team_id, "F", "starter"),
            ],
        )
        for game_id, game_date in [(9301, "2026-05-01"), (9302, "2026-05-03"), (9303, "2026-05-05"), (9304, "2026-05-07"), (9305, "2026-05-09")]:
            base_conn.execute(
                """
                INSERT INTO games (
                    id, game_date, start_time, home_team_id, away_team_id, status,
                    rest_days_home, rest_days_away, spread_home, game_total
                ) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, -4.5, 162.0)
                """,
                (game_id, game_date, f"{game_date}T23:00:00Z", team_id, 2),
            )
            base_conn.executemany(
                """
                INSERT INTO player_game_stats (
                    player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (9201, game_id, 30, 15, 4, 5, 1, 1, 0, 2),
                    (9202, game_id, 29, 12, 8, 2, 0, 0, 1, 1),
                ],
            )
        base_conn.execute(
            "INSERT INTO injuries (player_id, status, note, captured_at) VALUES (?, ?, ?, ?)",
            (9202, "out", "cache injury", "2026-05-08T12:00:00Z"),
        )

        expensive_query_count = 0

        class CountingConn:
            def __init__(self, conn):
                self._conn = conn

            def execute(self, sql, params=()):
                nonlocal expensive_query_count
                normalized = " ".join(str(sql).split()).lower()
                if "from injuries i join players p on p.id = i.player_id" in normalized:
                    expensive_query_count += 1
                return self._conn.execute(sql, params)

        conn = CountingConn(base_conn)
        first = _injury_adjustment_for_prop(
            conn,
            player_id=9201,
            team_id=team_id,
            rotation_role="starter",
            as_of_date="2026-05-09",
        )
        second = _injury_adjustment_for_prop(
            conn,
            player_id=9201,
            team_id=team_id,
            rotation_role="starter",
            as_of_date="2026-05-09",
        )

    assert expensive_query_count == 1
    assert first == second


def test_walk_forward_training_saves_model_run() -> None:
    load_test_history()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (8801, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (8802, 8801, ?, ?, 21.7, 'over', 0.58, 0.52, 0.06, 0.03, 'medium', 'validation-test')
            """,
            (MODEL_VERSION, datetime.now(timezone.utc).isoformat()),
        )
        captured_at = datetime.now(timezone.utc).isoformat()
        for prediction_id in range(8900, 8916):
            conn.execute(
                """
                INSERT INTO game_predictions (
                    id, game_id, model_version, prediction_time, projected_margin, projected_total,
                    winner_pick, ats_pick, total_pick, confidence, reason,
                    spread_home, game_total, home_rest_days, away_rest_days
                ) VALUES (?, ?, ?, ?, 2.0, 154.0, 'NY', 'CON +5.5', 'Under', 'medium', 'training-eval', -5.5, 158.0, 2, 2)
                """,
                (prediction_id, 2010, f"training-test-{prediction_id}", captured_at),
            )
            conn.execute(
                """
                INSERT INTO settled_game_predictions (
                    game_prediction_id, game_id, home_score, away_score, actual_winner,
                    actual_margin, actual_total, actual_ats_pick, actual_total_result,
                    winner_correct, ats_correct, total_correct, settled_at
                ) VALUES (?, 2010, 88, 79, 'NY', 9.0, 166.0, 'NY', 'Over', 1, 1, 1, ?)
                """,
                (prediction_id, captured_at),
            )
        result = run_walk_forward_training(conn)
        rows = conn.execute("SELECT * FROM model_runs").fetchall()
    assert result["status"] == "completed"
    assert result["training_rows"] > 0
    assert len(rows) == 2
    assert {row["model_version"] for row in rows} == {MODEL_VERSION, "component-pregame-v2"}
    assert result["metrics"]["points"]["settled_rows"] >= 1
    assert "side_accuracy" in result["metrics"]["points"]
    assert "residual_rows" in result["metrics"]["points"]
    assert result["metrics"]["overall"]["settled_rows"] >= 1
    assert "residual_rows" in result["metrics"]["overall"]
    assert result["metrics"]["game_ats"]["rows"] >= 1
    assert result["metrics"]["game_total"]["rows"] >= 1
    assert "baseline_mae" in result["metrics"]["game_ats"]
    artifact_bundle = result.get("artifact_bundle")
    assert artifact_bundle is not None
    manifest_path = Path(str(artifact_bundle["manifest_path"]))
    assert manifest_path.exists()
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["model_version"] == MODEL_VERSION
    assert manifest["data_snapshot_hash"]
    assert Path(str(manifest["feature_schema_path"])).exists()


def test_market_evaluation_reports_training_sample_diagnostics() -> None:
    load_test_history()
    with connect() as conn:
        metrics = player_prop_model_module.evaluate_market_model(conn, "points")
        residual_metrics = player_prop_model_module.evaluate_market_residual_model(conn, "points")

    diagnostics = metrics["training_sample_diagnostics"]
    residual_diagnostics = residual_metrics["residual_training_sample_diagnostics"]

    assert diagnostics["candidate_rows"] >= diagnostics["included_rows"] >= 0
    assert residual_diagnostics["candidate_rows"] >= residual_diagnostics["included_rows"] >= 0
    assert "skipped_before_training_start" in diagnostics
    assert "skipped_missing_history_window" in diagnostics
    assert "skipped_incomplete_context" in diagnostics
    assert "skipped_ambiguous_team_identity" in diagnostics
    assert "skipped_incomplete_context" in residual_diagnostics
    assert "skipped_missing_history_window" in residual_diagnostics
    assert "skipped_ambiguous_team_identity" in residual_diagnostics
    assert "skipped_missing_snapshot" in residual_diagnostics


def test_training_samples_use_component_projection_as_baseline(monkeypatch) -> None:
    load_test_history()
    clear_model_cache()

    original = player_prop_model_module._historical_training_features

    def wrapped_features(*args, **kwargs):
        features = original(*args, **kwargs)
        features[FEATURE_INDEX["component_projection"]] = 77.0
        features[FEATURE_INDEX["last_10_avg"]] = 11.0
        return features

    monkeypatch.setattr(player_prop_model_module, "_historical_training_features", wrapped_features)

    with connect() as conn:
        samples, _diagnostics = player_prop_model_module._training_samples(conn, "points")

    assert samples
    assert samples[0].baseline == 77.0


def test_market_evaluation_reports_final_projection_metrics(monkeypatch) -> None:
    clear_model_cache()

    def make_features(component_projection: float) -> list[float]:
        values = [0.0 for _ in FEATURE_NAMES]
        values[FEATURE_INDEX["component_projection"]] = component_projection
        values[FEATURE_INDEX["weighted_recent"]] = component_projection
        values[FEATURE_INDEX["ewma_value"]] = component_projection
        values[FEATURE_INDEX["last_10_avg"]] = component_projection
        values[FEATURE_INDEX["rate_projection"]] = component_projection
        values[FEATURE_INDEX["ewma_minutes"]] = 30.0
        values[FEATURE_INDEX["minutes_trend"]] = 0.0
        return values

    raw_samples = [
        player_prop_model_module.TrainingSample(
            features=make_features(10.0),
            target=11.6,
            game_date=f"2026-01-{day:02d}",
            season="2026",
            segment="2026-01",
            baseline=10.0,
        )
        for day in range(1, 26)
    ]
    residual_samples = [
        player_prop_model_module.TrainingSample(
            features=make_features(10.0),
            target=0.0,
            game_date=f"2026-01-{day:02d}",
            season="2026",
            segment="2026-01",
            baseline=0.0,
        )
        for day in range(1, 26)
    ]
    final_samples = [
        player_prop_model_module.FinalProjectionSample(
            features=make_features(10.0),
            target=11.6,
            game_date=f"2026-02-{day:02d}",
            season="2026",
            segment="2026-02",
            component_projection=10.0,
            line=12.0,
            over_odds=-110,
            under_odds=-110,
            sample_count=20,
            avg_minutes=30.0,
        )
        for day in range(1, 21)
    ]

    monkeypatch.setattr(player_prop_model_module, "_training_samples", lambda conn, market: (raw_samples, player_prop_model_module.TrainingSampleDiagnostics(included_rows=len(raw_samples), candidate_rows=len(raw_samples))))
    monkeypatch.setattr(player_prop_model_module, "_residual_training_samples", lambda conn, market: (residual_samples, player_prop_model_module.TrainingSampleDiagnostics(included_rows=len(residual_samples), candidate_rows=len(residual_samples))))
    monkeypatch.setattr(player_prop_model_module, "_final_projection_samples", lambda conn, market: (final_samples, player_prop_model_module.TrainingSampleDiagnostics(included_rows=len(final_samples), candidate_rows=len(final_samples))))

    def fake_fit_model(market: str, samples: list[player_prop_model_module.TrainingSample], config=None):
        intercept = 11.5 if not market.startswith("residual:") else 0.0
        return RidgeModel(
            market=market,
            rows=len(samples),
            intercept=intercept,
            coefficients=[0.0 for _ in FEATURE_NAMES],
            feature_means=[0.0 for _ in FEATURE_NAMES],
            feature_scales=[1.0 for _ in FEATURE_NAMES],
        )

    monkeypatch.setattr(player_prop_model_module, "_fit_model_from_samples", fake_fit_model)

    with connect() as conn:
        metrics = player_prop_model_module.evaluate_market_model(conn, "points_assists")

    assert metrics["final_rows"] == 20
    assert metrics["final_mae"] is not None
    assert metrics["final_baseline_mae"] is not None
    assert metrics["final_mae_improvement"] is not None
    assert float(metrics["final_mae_improvement"]) > 0


def test_training_samples_skip_ambiguous_historical_team_identity() -> None:
    load_test_history()
    with connect() as conn:
        conn.execute("INSERT INTO teams (id, name, abbreviation) VALUES (999, 'Ambiguous Team', 'AMB')")
        conn.execute("UPDATE players SET team_id = 999 WHERE id = 1001")
        metrics = player_prop_model_module.evaluate_market_model(conn, "points")

    diagnostics = metrics["training_sample_diagnostics"]
    assert diagnostics["skipped_ambiguous_team_identity"] >= 0
    assert diagnostics["included_rows"] > 0


def test_audit_settled_prop_history_gaps_reports_missing_dates() -> None:
    load_test_history()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9801, 100, 1001, 'DraftKings', 'points', 21.5, -110, -110, '2026-06-01T00:00:00Z')
            """
        )
        audit = audit_settled_prop_history_gaps(conn, start_date="2026-04-01", end_date="2026-06-30")

    assert audit["missing_dates_count"] >= 1
    assert "2026-04-01" in audit["missing_dates"]
    assert audit["strict_ok"] is False


def test_expand_settled_prop_history_targets_missing_dates(monkeypatch) -> None:
    load_test_history()
    scoreboard_calls = []
    boxscore_calls = []
    settlement_calls = []

    def fake_scoreboard(conn, season, force_refresh=False, selected_date=None):
        scoreboard_calls.append((season, force_refresh, selected_date))
        return {"season": season, "selected_date": selected_date}

    def fake_boxscores(conn, season, force_refresh=False, missing_only=False, selected_date=None):
        boxscore_calls.append((season, force_refresh, missing_only, selected_date))
        return {"season": season, "selected_date": selected_date, "missing_only": missing_only}

    def fake_settle(conn, *, selected_date=None, selected_dates=None):
        settlement_calls.append((selected_date, selected_dates))
        return {"settled": 3, "selected_dates": selected_dates or []}

    monkeypatch.setattr("backend.app.history_expansion.import_espn_scoreboard", fake_scoreboard)
    monkeypatch.setattr("backend.app.history_expansion.import_espn_player_boxscores", fake_boxscores)
    monkeypatch.setattr("backend.app.history_expansion.settle_completed_props", fake_settle)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9802, 100, 1001, 'DraftKings', 'points', 22.5, -110, -110, '2026-06-01T00:00:00Z')
            """
        )
        result = expand_settled_prop_history(
            conn,
            start_date="2026-04-01",
            end_date="2026-06-30",
            force_refresh=False,
        )

    assert result["attempted_dates"] == ["2026-04-01"]
    assert scoreboard_calls == [(2026, False, "2026-04-01")]
    assert boxscore_calls == [(2026, False, True, "2026-04-01")]
    assert settlement_calls == [(None, ["2026-04-01"])]


def test_training_sample_weights_bias_fit_toward_recent_results() -> None:
    samples = []
    for idx in range(20):
        target = 10.0 if idx < 10 else 30.0
        samples.append(
            player_prop_model_module.TrainingSample(
                features=[1.0 for _ in FEATURE_NAMES],
                target=target,
                game_date=f"2025-06-{idx + 1:02d}",
                season="2025",
                segment=f"2025-W{idx // 5}",
                baseline=20.0,
            )
        )

    weighted_model = player_prop_model_module._fit_model_from_samples(
        "points",
        samples,
        config=player_prop_model_module.ModelTuningConfig(recency_weight_scale=1.0),
    )
    unweighted_model = player_prop_model_module._fit_model_from_rows(
        "points",
        [(sample.features, sample.target) for sample in samples],
    )

    assert weighted_model is not None
    assert unweighted_model is not None
    assert weighted_model.intercept > unweighted_model.intercept
    assert weighted_model.intercept > 20.0


def test_training_samples_mark_recent_verified_team_changes() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        initial_rows = player_prop_model_module._player_training_rows(conn, 1001)
        conn.execute(
            "INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1001, 3, int(initial_rows[0]["game_id"]), "transfer-test", 1.0, captured_at),
        )
        conn.execute(
            "INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at) VALUES (?, ?, ?, ?, ?, ?)",
            (1001, 10, int(initial_rows[6]["game_id"]), "transfer-test", 1.0, captured_at),
        )
        player_prop_model_module.clear_model_cache()
        rows = player_prop_model_module._player_training_rows(conn, 1001)

    assert player_prop_model_module._is_recent_transfer_training_row(rows, 7) is True


def test_walk_forward_training_reuses_cached_run_when_data_unchanged(monkeypatch) -> None:
    load_test_history()
    with connect() as conn:
        first = run_walk_forward_training(conn)
        first_row_count = conn.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]

        def fail_component(_conn):
            raise AssertionError("component benchmark should not rerun")

        monkeypatch.setattr("backend.app.training._run_component_benchmark", fail_component)
        second = run_walk_forward_training(conn)
        second_row_count = conn.execute("SELECT COUNT(*) FROM model_runs").fetchone()[0]

    assert first["run_type"] == "walk_forward_segments"
    assert second["run_type"] == "walk_forward_segments"
    assert first["started_at"] == second["started_at"]
    assert first_row_count == second_row_count


def test_run_parameter_tuning_returns_ranked_candidates() -> None:
    load_test_history()
    with connect() as conn:
        result = run_parameter_tuning(
            conn,
            ridge_penalties=[0.75, 1.25],
            market_weight_scales=[1.0],
            player_weight_scales=[1.0],
            stabilization_scales=[0.9],
            recency_weight_scales=[1.0],
        )

    assert result["run_type"] == "parameter_tuning"
    assert result["candidate_count"] == 2
    assert result["best_candidate"] is not None
    assert len(result["candidates"]) == 2
    assert result["candidates"][0]["rank"] == 1
    assert result["candidates"][1]["rank"] == 2
    assert result["best_candidate"]["summary"]["avg_mae"] is not None
    assert result["best_candidate"]["summary"]["total_rows"] > 0


def test_market_specific_stabilization_is_stricter_for_noisy_markets() -> None:
    values = [0.0 for _ in FEATURE_NAMES]
    values[FEATURE_NAMES.index("weighted_recent")] = 20.0
    values[FEATURE_NAMES.index("last_10_avg")] = 20.0
    values[FEATURE_NAMES.index("rate_projection")] = 20.0
    values[FEATURE_NAMES.index("component_projection")] = 20.0
    values[FEATURE_NAMES.index("ewma_minutes")] = 30.0
    values[FEATURE_NAMES.index("minutes_trend")] = 0.0
    snapshot = FeatureSnapshot(values=values, component_projection=20.0, reason="test")

    points_projection, points_note = _stabilize_learned_projection(5.0, snapshot, "points")
    blocks_projection, blocks_note = _stabilize_learned_projection(5.0, snapshot, "blocks")

    assert points_note == "stabilized up (0.25x anchor)"
    assert blocks_note == "stabilized up (0.25x anchor)"
    assert blocks_projection > points_projection


def test_market_blend_weights_stay_moderate_for_learned_projection() -> None:
    assert _market_weight(600) == pytest.approx(0.18)
    assert _market_weight(200) == pytest.approx(0.13)
    assert _market_weight(50) == pytest.approx(0.09)

    assert _player_market_weight(3, 28.0) == pytest.approx(0.55)
    assert _player_market_weight(6, 28.0) == pytest.approx(0.42)
    assert _player_market_weight(12, 12.0) == pytest.approx(0.34)
    assert _player_market_weight(8, 24.0) == pytest.approx(0.26)
    assert _player_market_weight(12, 24.0) == pytest.approx(0.18)

    assert _residual_market_weight("points", 260, 20, 28.0) == 0.0
    assert _residual_market_weight("points_assists", 199, 20, 28.0) == 0.0
    assert _residual_market_weight("points_assists", 260, 20, 28.0) > 0.0


def test_sparse_combo_markets_use_more_conservative_blend_weights() -> None:
    assert _market_depth_scale("points") == pytest.approx(1.0)
    assert _market_depth_scale("points_rebounds_assists") < _market_depth_scale("points_rebounds")
    assert _market_depth_scale("blocks_steals") < _market_depth_scale("assists")

    points_weight = _market_line_weight("points", 220, 12, 28.0)
    pra_weight = _market_line_weight("points_rebounds_assists", 220, 12, 28.0)
    blocks_steals_weight = _market_line_weight("blocks_steals", 220, 12, 28.0)

    assert pra_weight < points_weight
    assert blocks_steals_weight < points_weight
    assert _residual_market_weight("points_rebounds_assists", 260, 20, 28.0) == 0.0


def test_component_market_line_weight_stays_conservative_relative_to_learned_weight() -> None:
    learned_weight = _market_line_weight("points", 220, 12, 28.0)
    component_weight = _component_market_line_weight("points", 12, 28.0)
    pra_component_weight = _component_market_line_weight("points_rebounds_assists", 12, 28.0)

    assert component_weight < learned_weight
    assert pra_component_weight < component_weight


def test_market_specific_component_projection_regresses_volatile_combo_markets() -> None:
    adjusted = _market_specific_component_projection(
        market="points_rebounds_assists",
        component_base=24.0,
        rate_projection=26.0,
        last_10_avg=20.0,
        consistency_score=0.25,
        value_volatility=11.0,
    )

    assert adjusted < 24.0


def test_train_market_model_uses_active_non_sqlite_connection(monkeypatch) -> None:
    class FakeRemoteConnection:
        pass

    calls = []

    def fake_uncached(conn, market, config=None):
        calls.append((conn, market, config))
        return None

    monkeypatch.setattr("backend.app.player_prop_model._train_market_model_uncached", fake_uncached)

    conn = FakeRemoteConnection()
    result = train_market_model(conn, "points")

    assert result is None
    assert len(calls) == 1
    assert calls[0][0] is conn
    assert calls[0][1] == "points"
    assert calls[0][2] is not None


def test_train_market_model_uses_persistent_cache(tmp_path, monkeypatch) -> None:
    monkeypatch.setenv("WNBA_CACHE_DIR", str(tmp_path / "cache"))
    load_test_history()
    clear_model_cache()

    with connect() as conn:
        first = train_market_model(conn, "points")
        assert first is not None

    clear_model_cache()

    def fail_uncached(*args, **kwargs):
        raise AssertionError("should have loaded from persistent cache")

    monkeypatch.setattr("backend.app.player_prop_model._train_market_model_uncached", fail_uncached)

    with connect() as conn:
        second = train_market_model(conn, "points")

    assert second is not None
    assert second.rows == first.rows


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


def test_model_diagnostics_defaults_to_current_model_version(monkeypatch) -> None:
    captured_versions: list[str] = []

    class DummyReport:
        total_predictions = 0
        mae = None
        rmse = None
        bias = None
        directional_accuracy = None
        market_breakdown = {}

    def fake_report(conn, model_version):
        captured_versions.append(model_version)
        return DummyReport()

    monkeypatch.setattr(main_module, "build_accuracy_report", fake_report)
    monkeypatch.setattr(main_module, "build_accuracy_report_for_days", lambda conn, days, model_version: fake_report(conn, model_version))

    result = main_module.model_diagnostics()

    assert result["model_version"] == MODEL_VERSION
    assert captured_versions
    assert all(version == MODEL_VERSION for version in captured_versions)


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


def test_settle_completed_props_repairs_incomplete_rows() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    stale_settled_at = "2026-01-01T00:00:00+00:00"
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9102, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        conn.execute(
            """
            INSERT INTO settled_props (
                prop_line_id, actual_result, winning_side, margin, player_minutes, game_margin, team_margin,
                team_spread, blowout_result, blowout_threshold, settled_at
            ) VALUES (9102, 19.0, 'under', -1.5, 32.0, NULL, NULL, NULL, 'unknown', NULL, ?)
            """,
            (stale_settled_at,),
        )

        result = settle_completed_props(conn)
        row = conn.execute("SELECT * FROM settled_props WHERE prop_line_id = 9102").fetchone()

    assert result["settled"] == 0
    assert result["repaired"] == 1
    assert row["actual_result"] == 19.0
    assert row["winning_side"] == "under"
    assert row["margin"] == -1.5
    assert row["player_minutes"] == 32.0
    assert row["game_margin"] == 6.0
    assert row["team_margin"] == 6.0
    assert row["team_spread"] == -4.5
    assert row["blowout_result"] == "no"
    assert row["blowout_threshold"] == 15.0
    assert row["settled_at"] != stale_settled_at


def test_settle_completed_props_uses_team_history_for_game_context() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute("UPDATE players SET team_id = 3 WHERE id = 1001")
        conn.execute(
            """
            INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at)
            VALUES (1001, 10, 100, 'test', 1.0, ?)
            """,
            (captured_at,),
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9103, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (captured_at,),
        )

        result = settle_completed_props(conn)
        row = conn.execute("SELECT * FROM settled_props WHERE prop_line_id = 9103").fetchone()

    assert result["settled"] == 1
    assert row["game_margin"] == 6.0
    assert row["team_margin"] == 6.0
    assert row["team_spread"] == -4.5
    assert row["blowout_result"] == "no"


def test_settle_completed_props_hydrates_team_stats_for_unsettled_final_dates(monkeypatch) -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    hydrated: list[tuple[int, str]] = []

    def fake_import_espn_player_boxscores(
        conn,
        season,
        force_refresh=False,
        missing_only=False,
        selected_date=None,
        max_workers=8,
    ):
        hydrated.append((season, selected_date))
        return {
            "season": season,
            "selected_date": selected_date,
            "updated_team_game_results": 2,
            "updated_team_game_boxscores": 2,
        }

    monkeypatch.setattr(espn_history_module, "import_espn_player_boxscores", fake_import_espn_player_boxscores)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9104, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        result = settlement_module.settle_completed_props(conn)

    assert hydrated == []
    assert result["hydrated_dates"] == []
    assert result["settled"] == 1


def test_historical_training_features_use_live_context_factors(monkeypatch) -> None:
    load_test_history()
    with connect() as conn:
        row = conn.execute(
            """
            SELECT
                s.*,
                g.game_date,
                g.home_team_id,
                g.away_team_id,
                g.rest_days_home,
                g.rest_days_away,
                g.spread_home,
                g.game_total,
                p.team_id,
                p.rotation_role
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            JOIN players p ON p.id = s.player_id
            WHERE s.player_id = 1001
            ORDER BY g.game_date ASC, s.game_id ASC
            LIMIT 1
            """
        ).fetchone()

        monkeypatch.setattr("backend.app.player_prop_model._pace_factor", lambda *_args, **_kwargs: 1.05)
        monkeypatch.setattr("backend.app.player_prop_model._opponent_factor", lambda *_args, **_kwargs: 0.94)
        monkeypatch.setattr("backend.app.player_prop_model._common_opponent_factor", lambda *_args, **_kwargs: 1.03)
        monkeypatch.setattr("backend.app.player_prop_model._h2h_factor", lambda *_args, **_kwargs: 0.97)
        monkeypatch.setattr(
            "backend.app.player_prop_model._blowout_adjustment",
            lambda *_args, **_kwargs: {"risk": "medium", "minutes_delta": 2.5},
        )
        monkeypatch.setattr(
            "backend.app.player_prop_model._project_minutes",
            lambda *_args, **_kwargs: (30.0, "test minutes"),
        )

        features = _historical_training_features(
            conn,
            row,
            history=[18.0, 19.0, 21.0, 20.0, 17.0],
            minutes=[31.0, 32.0, 33.0, 31.0, 30.0],
            market="points",
            previous_game_date="2026-03-31",
        )

    assert features[FEATURE_INDEX["pace_factor"]] > 0
    assert features[FEATURE_INDEX["opponent_factor"]] > 0
    assert features[FEATURE_INDEX["common_opponent_factor"]] > 0
    assert features[FEATURE_INDEX["h2h_factor"]] > 0
    assert features[FEATURE_INDEX["blowout_minutes_delta"]] == 2.5


def test_historical_training_features_include_team_transition_context() -> None:
    load_test_history()
    with connect() as conn:
        rows = player_prop_model_module._player_training_rows(conn, 1001)
        row_index = 7
        row = rows[row_index]
        history_rows = rows[max(0, row_index - 7):row_index]
        features = _historical_training_features(
            conn,
            row,
            history=[float(item["points"]) for item in history_rows],
            minutes=[float(item["minutes"]) for item in history_rows],
            market="points",
            previous_game_date=str(rows[row_index - 1]["game_date"]),
            recent_rows=history_rows,
            player_rows=rows,
            row_index=row_index,
        )

    assert len(features) == len(FEATURE_NAMES)
    assert features[FEATURE_INDEX["games_since_joining_team"]] == 7.0
    assert -1.0 <= features[FEATURE_INDEX["teammate_minutes_redistribution"]] <= 1.0
    assert 0.0 <= features[FEATURE_INDEX["rotation_stability"]] <= 1.0


def test_calibrated_probability_uses_settled_history_support(monkeypatch) -> None:
    load_test_history()
    monkeypatch.setattr(projections_module, "CALIBRATION_MIN_SAMPLES", 4)
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        line_rows = [
            (9111, 100, 1001, 24.5, 0.58),
            (9112, 101, 1001, 24.5, 0.67),
            (9113, 102, 1001, 24.5, 0.57),
            (9114, 103, 1001, 24.5, 0.66),
        ]
        conn.executemany(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, 'DraftKings', 'points', ?, -110, -110, ?)
            """,
            [(prop_id, game_id, player_id, line, captured_at) for prop_id, game_id, player_id, line, _prob in line_rows],
        )
        conn.executemany(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, 'adaptive-context-v1', ?, 18.5, 'under', ?, 0.52, 0.06, 0.03, 'medium', 'calibration-test')
            """,
            [(prop_id, captured_at, prob) for prop_id, _game_id, _player_id, _line, prob in line_rows],
        )
        settle_completed_props(conn)
        calibrated = projections_module._calibrated_probability(
            conn,
            raw_probability=0.62,
            market="points",
            model_version="adaptive-context-v1",
            side="under",
        )

    assert calibrated > 0.62
    assert calibrated <= 0.95


def test_build_prop_projection_applies_historical_market_bias_adjustment(monkeypatch) -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        projections_module,
        "predict_player_prop",
        lambda *_args, **_kwargs: (20.0, "base reason", "adaptive-context-v1"),
    )
    monkeypatch.setattr(projections_module, "_estimated_sigma", lambda *_args, **_kwargs: 3.0)
    monkeypatch.setattr(projections_module, "PROJECTION_BIAS_MIN_SAMPLES", 4)

    with connect() as conn:
        line_rows = [
            (9121, 100, 1001),
            (9122, 101, 1001),
            (9123, 102, 1001),
            (9124, 103, 1001),
            (9125, 104, 1001),
        ]
        conn.executemany(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            [(prop_id, game_id, player_id, captured_at) for prop_id, game_id, player_id in line_rows],
        )
        conn.executemany(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, 'adaptive-context-v1', ?, 20.0, 'over', 0.56, 0.52, 0.04, 0.02, 'medium', 'bias-test')
            """,
            [(prop_id, captured_at) for prop_id, _game_id, _player_id in line_rows[:-1]],
        )
        settle_completed_props(conn)

        projection = projections_module.build_prop_projection(conn, 9125)

    assert projection.projection > 20.0
    assert "Historical market bias adjustment" in projection.reason


def test_build_prop_projection_prefers_over_when_projection_exceeds_line(monkeypatch) -> None:
    captured_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        projections_module,
        "predict_player_prop",
        lambda *_args, **_kwargs: (22.8, "base reason", "adaptive-context-v1"),
    )
    monkeypatch.setattr(projections_module, "_estimated_sigma", lambda *_args, **_kwargs: 3.0)
    monkeypatch.setattr(
        projections_module,
        "_calibrated_probability",
        lambda _conn, raw_probability, _market, _model_version, side: 0.40 if side == "over" else 0.65,
    )

    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (9911, "Olivia Miles", 10, "G", "starter"),
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)",
            (9911, "2026-06-20", "2026-06-20T19:00:00Z", 10, 3, -2.5, 159.5),
        )
        conn.execute(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (9911, 9911, 9911, "DraftKings", "points_rebounds", 21.5, -110, -110, captured_at),
        )

        projection = projections_module.build_prop_projection(conn, 9911)

    assert projection.projection == 22.8
    assert projection.recommended_side == "under"


def test_predict_player_prop_blends_market_residual_model(monkeypatch) -> None:
    feature_values = [0.0 for _ in FEATURE_NAMES]
    snapshot = FeatureSnapshot(values=feature_values, component_projection=18.0, reason="snapshot reason")
    learned_model = RidgeModel("points", 200, 0.0, [], [0.0 for _ in FEATURE_NAMES], [1.0 for _ in FEATURE_NAMES])
    residual_model = RidgeModel("residual:points", 120, 0.0, [], [0.0 for _ in FEATURE_NAMES], [1.0 for _ in FEATURE_NAMES])

    monkeypatch.setattr("backend.app.player_prop_model.feature_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr("backend.app.player_prop_model._player_sample_quality", lambda *_args, **_kwargs: (20, 30.0))
    monkeypatch.setattr("backend.app.player_prop_model.train_market_model", lambda *_args, **_kwargs: learned_model)
    monkeypatch.setattr("backend.app.player_prop_model.train_market_residual_model", lambda *_args, **_kwargs: residual_model)
    monkeypatch.setattr(
        "backend.app.player_prop_model._market_overlay_decision",
        lambda *_args, **_kwargs: {"mode": "full_learned", "note": "walk-forward gate passed"},
    )
    monkeypatch.setattr(
        "backend.app.player_prop_model._predict",
        lambda model, _features: -1.0 if str(model.market).startswith("residual:") else 24.0,
    )
    monkeypatch.setattr("backend.app.player_prop_model._stabilize_combo_market_projection", lambda learned, *_args, **_kwargs: learned)
    monkeypatch.setattr("backend.app.player_prop_model._stabilize_learned_projection", lambda learned, *_args, **_kwargs: (learned, "no stabilization"))
    monkeypatch.setattr("backend.app.player_prop_model._market_weight", lambda *_args, **_kwargs: 0.2)
    monkeypatch.setattr("backend.app.player_prop_model._player_market_weight", lambda *_args, **_kwargs: 0.2)
    monkeypatch.setattr("backend.app.player_prop_model._residual_market_weight", lambda *_args, **_kwargs: 0.3)
    monkeypatch.setattr("backend.app.player_prop_model._market_price_nudge", lambda *_args, **_kwargs: 0.0)

    with connect() as conn:
        projection, reason, model_version = predict_player_prop(
            conn,
            player_id=1001,
            market="points",
            game_id=100,
            line=20.0,
            over_odds=-110,
            under_odds=-110,
        )

    assert projection == pytest.approx(21.94, abs=0.01)
    assert "residual blend 30%" in reason
    assert model_version == MODEL_VERSION


def test_predict_player_prop_uses_component_only_when_market_policy_rejects_learned(monkeypatch) -> None:
    feature_values = [0.0 for _ in FEATURE_NAMES]
    snapshot = FeatureSnapshot(values=feature_values, component_projection=18.0, reason="snapshot reason")
    learned_model = RidgeModel("points", 200, 0.0, [], [0.0 for _ in FEATURE_NAMES], [1.0 for _ in FEATURE_NAMES])

    monkeypatch.setattr("backend.app.player_prop_model.feature_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr("backend.app.player_prop_model._player_sample_quality", lambda *_args, **_kwargs: (20, 30.0))
    monkeypatch.setattr("backend.app.player_prop_model.train_market_model", lambda *_args, **_kwargs: learned_model)
    monkeypatch.setattr(
        "backend.app.player_prop_model._market_overlay_decision",
        lambda *_args, **_kwargs: {"mode": "component_only", "note": "walk-forward gate failed"},
    )
    monkeypatch.setattr("backend.app.player_prop_model._component_market_line_weight", lambda *_args, **_kwargs: 0.20)
    monkeypatch.setattr("backend.app.player_prop_model._predict", lambda *_args, **_kwargs: 24.0)
    monkeypatch.setattr("backend.app.player_prop_model._stabilize_combo_market_projection", lambda learned, *_args, **_kwargs: learned)
    monkeypatch.setattr("backend.app.player_prop_model._stabilize_learned_projection", lambda learned, *_args, **_kwargs: (learned, "no stabilization"))

    with connect() as conn:
        projection, reason, model_version = predict_player_prop(
            conn,
            player_id=1001,
            market="points",
            game_id=100,
            line=20.0,
            over_odds=-110,
            under_odds=-110,
        )

    assert projection == pytest.approx(18.4)
    assert "walk-forward gate failed" in reason
    assert "component line anchor 20% at 20.0" in reason
    assert model_version == COMPONENT_MODEL_VERSION


def test_market_overlay_decision_honors_component_only_policy_even_when_gate_passes(monkeypatch) -> None:
    monkeypatch.setattr(
        player_prop_model_module,
        "_market_overlay_metrics",
        lambda _conn: {
            "points_rebounds": {
                "rows": 2400,
                "mae_improvement": 0.18,
                "rmse_improvement": 0.09,
                "bias": 0.02,
            }
        },
    )

    with connect() as conn:
        decision = player_prop_model_module._market_overlay_decision(conn, "points_rebounds")

    assert decision["mode"] == "component_only"
    assert "market policy holds component baseline live" in str(decision["note"])


def test_predict_player_prop_blends_component_and_learned_when_market_policy_passes(monkeypatch) -> None:
    feature_values = [0.0 for _ in FEATURE_NAMES]
    snapshot = FeatureSnapshot(values=feature_values, component_projection=18.0, reason="snapshot reason")
    learned_model = RidgeModel("points", 200, 0.0, [], [0.0 for _ in FEATURE_NAMES], [1.0 for _ in FEATURE_NAMES])

    monkeypatch.setattr("backend.app.player_prop_model.feature_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr("backend.app.player_prop_model._player_sample_quality", lambda *_args, **_kwargs: (20, 30.0))
    monkeypatch.setattr("backend.app.player_prop_model.train_market_model", lambda *_args, **_kwargs: learned_model)
    monkeypatch.setattr(
        "backend.app.player_prop_model._market_overlay_decision",
        lambda *_args, **_kwargs: {"mode": "blend", "blend_weight": 0.25, "note": "walk-forward gate passed"},
    )
    monkeypatch.setattr("backend.app.player_prop_model.train_market_residual_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("backend.app.player_prop_model._predict", lambda *_args, **_kwargs: 24.0)
    monkeypatch.setattr("backend.app.player_prop_model._stabilize_combo_market_projection", lambda learned, *_args, **_kwargs: learned)
    monkeypatch.setattr("backend.app.player_prop_model._stabilize_learned_projection", lambda learned, *_args, **_kwargs: (learned, "no stabilization"))

    with connect() as conn:
        projection, reason, model_version = predict_player_prop(
            conn,
            player_id=1001,
            market="points",
            game_id=100,
            line=None,
            over_odds=None,
            under_odds=None,
        )

    assert projection == pytest.approx(19.5)
    assert "learned/component blend 25%" in reason
    assert model_version == MODEL_VERSION


def test_build_prop_projection_penalizes_thin_combo_under_recommendations(monkeypatch) -> None:
    captured_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        projections_module,
        "predict_player_prop",
        lambda *_args, **_kwargs: (24.2, "base reason", "adaptive-context-v1"),
    )
    monkeypatch.setattr(projections_module, "_estimated_sigma", lambda *_args, **_kwargs: 3.0)
    monkeypatch.setattr(
        projections_module,
        "_calibrated_probability",
        lambda _conn, raw_probability, _market, _model_version, side: 0.59 if side == "over" else 0.57,
    )

    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (9921, "Combo Guard", 10, "G", "starter"),
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)",
            (9921, "2026-06-21", "2026-06-21T19:00:00Z", 10, 3, -2.5, 159.5),
        )
        conn.execute(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (9921, 9921, 9921, "DraftKings", "points_rebounds", 25.0, -110, -110, captured_at),
        )

        projection = projections_module.build_prop_projection(conn, 9921)

    assert projection.projection == 24.2
    assert projection.recommended_side == "over"


def test_build_prop_projection_penalizes_thin_pra_under_recommendations(monkeypatch) -> None:
    captured_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        projections_module,
        "predict_player_prop",
        lambda *_args, **_kwargs: (29.1, "base reason", "adaptive-context-v1"),
    )
    monkeypatch.setattr(projections_module, "_estimated_sigma", lambda *_args, **_kwargs: 3.0)
    monkeypatch.setattr(
        projections_module,
        "_calibrated_probability",
        lambda _conn, raw_probability, _market, _model_version, side: 0.59 if side == "over" else 0.57,
    )

    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (9922, "PRA Wing", 10, "F", "starter"),
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)",
            (9922, "2026-06-22", "2026-06-22T19:00:00Z", 10, 3, -2.5, 159.5),
        )
        conn.execute(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (9922, 9922, 9922, "DraftKings", "points_rebounds_assists", 30.0, -110, -110, captured_at),
        )

        projection = projections_module.build_prop_projection(conn, 9922)

    assert projection.projection == 29.1
    assert projection.recommended_side == "over"


def test_build_prop_projection_penalizes_thin_threes_over_recommendations(monkeypatch) -> None:
    captured_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        projections_module,
        "predict_player_prop",
        lambda *_args, **_kwargs: (2.7, "base reason", "adaptive-context-v1"),
    )
    monkeypatch.setattr(projections_module, "_estimated_sigma", lambda *_args, **_kwargs: 1.0)
    monkeypatch.setattr(
        projections_module,
        "_calibrated_probability",
        lambda _conn, raw_probability, _market, _model_version, side: 0.59 if side == "over" else 0.57,
    )

    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (9923, "Shooter Guard", 10, "G", "starter"),
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)",
            (9923, "2026-06-23", "2026-06-23T19:00:00Z", 10, 3, -2.5, 159.5),
        )
        conn.execute(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (9923, 9923, 9923, "DraftKings", "threes", 2.5, -110, -110, captured_at),
        )

        projection = projections_module.build_prop_projection(conn, 9923)

    assert projection.projection == 2.7
    assert projection.recommended_side == "under"


def test_build_prop_projection_penalizes_thin_rebounds_over_recommendations(monkeypatch) -> None:
    captured_at = datetime.now(timezone.utc).isoformat()
    monkeypatch.setattr(
        projections_module,
        "predict_player_prop",
        lambda *_args, **_kwargs: (8.8, "base reason", "adaptive-context-v1"),
    )
    monkeypatch.setattr(projections_module, "_estimated_sigma", lambda *_args, **_kwargs: 2.0)
    monkeypatch.setattr(
        projections_module,
        "_calibrated_probability",
        lambda _conn, raw_probability, _market, _model_version, side: 0.58 if side == "over" else 0.57,
    )

    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (9924, "Board Forward", 10, "F", "starter"),
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)",
            (9924, "2026-06-24", "2026-06-24T19:00:00Z", 10, 3, -2.5, 159.5),
        )
        conn.execute(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (9924, 9924, 9924, "DraftKings", "rebounds", 8.5, -110, -110, captured_at),
        )

        projection = projections_module.build_prop_projection(conn, 9924)

    assert projection.projection == 8.8
    assert projection.recommended_side == "under"


def test_predict_player_prop_uses_reduced_transfer_blend_when_transfer_slice_underperforms(monkeypatch) -> None:
    feature_values = [0.0 for _ in FEATURE_NAMES]
    snapshot = FeatureSnapshot(values=feature_values, component_projection=18.0, reason="snapshot reason")
    learned_model = RidgeModel("points", 200, 0.0, [], [0.0 for _ in FEATURE_NAMES], [1.0 for _ in FEATURE_NAMES])

    monkeypatch.setattr("backend.app.player_prop_model.feature_snapshot", lambda *_args, **_kwargs: snapshot)
    monkeypatch.setattr("backend.app.player_prop_model._player_sample_quality", lambda *_args, **_kwargs: (20, 30.0))
    monkeypatch.setattr("backend.app.player_prop_model.train_market_model", lambda *_args, **_kwargs: learned_model)
    monkeypatch.setattr(
        "backend.app.player_prop_model._market_overlay_decision",
        lambda *_args, **_kwargs: {
            "mode": "transfer_blend",
            "blend_weight": 0.10,
            "note": "recent-transfer MAE improvement below gate",
        },
    )
    monkeypatch.setattr("backend.app.player_prop_model.train_market_residual_model", lambda *_args, **_kwargs: None)
    monkeypatch.setattr("backend.app.player_prop_model._predict", lambda *_args, **_kwargs: 24.0)
    monkeypatch.setattr("backend.app.player_prop_model._stabilize_combo_market_projection", lambda learned, *_args, **_kwargs: learned)
    monkeypatch.setattr("backend.app.player_prop_model._stabilize_learned_projection", lambda learned, *_args, **_kwargs: (learned, "no stabilization"))

    with connect() as conn:
        projection, reason, model_version = predict_player_prop(
            conn,
            player_id=1001,
            market="points",
            game_id=100,
            line=None,
            over_odds=None,
            under_odds=None,
        )

    assert projection == pytest.approx(18.6)
    assert "reduced learned/component blend 10%" in reason
    assert model_version == MODEL_VERSION


def test_residual_model_promotion_gate_requires_positive_walk_forward_improvement() -> None:
    assert _residual_model_passes_promotion_gate(
        {
            "rows": RESIDUAL_PROMOTION_MIN_ROWS,
            "mae_improvement": 0.12,
            "rmse_improvement": 0.01,
        }
    )
    assert not _residual_model_passes_promotion_gate(
        {
            "rows": RESIDUAL_PROMOTION_MIN_ROWS,
            "mae_improvement": -0.05,
            "rmse_improvement": 0.08,
        }
    )
    assert not _residual_model_passes_promotion_gate(
        {
            "rows": RESIDUAL_PROMOTION_MIN_ROWS,
            "mae_improvement": 0.08,
            "rmse_improvement": -0.01,
        }
    )
    assert not _residual_model_passes_promotion_gate(
        {
            "rows": RESIDUAL_PROMOTION_MIN_ROWS - 1,
            "mae_improvement": 0.08,
            "rmse_improvement": 0.02,
        }
    )


def test_train_market_residual_model_skips_regressive_model(monkeypatch) -> None:
    samples = [
        player_prop_model_module.TrainingSample(
            features=[0.0 for _ in FEATURE_NAMES],
            target=1.0,
            game_date="2026-06-01",
            season="2026",
            segment="2026-06",
            baseline=1.0,
        )
        for _ in range(RESIDUAL_PROMOTION_MIN_ROWS)
    ]
    residual_model = RidgeModel("residual:points", len(samples), 0.0, [0.0 for _ in FEATURE_NAMES], [0.0 for _ in FEATURE_NAMES], [1.0 for _ in FEATURE_NAMES])

    monkeypatch.setattr(
        player_prop_model_module,
        "_residual_training_samples",
        lambda *_args, **_kwargs: (samples, player_prop_model_module.TrainingSampleDiagnostics()),
    )
    monkeypatch.setattr(player_prop_model_module, "_fit_model_from_samples", lambda *_args, **_kwargs: residual_model)
    monkeypatch.setattr(
        player_prop_model_module,
        "_evaluate_walk_forward_samples",
        lambda *_args, **_kwargs: {
            "rows": len(samples),
            "mae_improvement": -0.2,
            "rmse_improvement": -0.1,
        },
    )

    with connect() as conn:
        trained = player_prop_model_module._train_market_residual_model_uncached(conn, "points")

    assert trained is None


def test_predict_player_prop_uses_local_combo_estimator_for_pra(monkeypatch) -> None:
    component_calls: list[str] = []

    def fake_predict(conn, player_id, market, game_id, line=None, over_odds=None, under_odds=None, config=None, runtime_cache=None, allow_training=True):
        component_calls.append(market)
        outputs = {
            "points": (18.4, "points reason", "adaptive-context-v1"),
            "rebounds": (7.1, "rebounds reason", "adaptive-context-v1"),
            "assists": (5.6, "assists reason", "adaptive-context-v1"),
        }
        if market in outputs:
            return outputs[market]
        raise AssertionError(f"unexpected market {market}")

    monkeypatch.setattr("backend.app.player_prop_model.predict_player_prop", fake_predict)

    from backend.app.player_prop_model import _predict_local_combo_market

    with connect() as conn:
        projection, reason, model_version = _predict_local_combo_market(
            conn,
            player_id=1001,
            market="points_rebounds_assists",
            game_id=100,
        )

    assert component_calls == ["points", "rebounds", "assists"]
    assert projection == pytest.approx(31.1)
    assert "Local combo estimator for points_rebounds_assists" in reason
    assert "points 18.4, rebounds 7.1, assists 5.6" in reason
    assert model_version == "adaptive-context-v1"


def test_repair_current_slate_props_targets_only_active_games() -> None:
    now_local = datetime.now(main_module.LOCAL_TZ)
    active_start = (now_local + timedelta(hours=1)).replace(minute=0, second=0, microsecond=0)
    stale_start = (now_local - timedelta(days=2)).replace(hour=19, minute=0, second=0, microsecond=0)
    with connect() as conn:
        conn.executemany(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            [
                (9101, "Active Guard", 10, "G", "starter"),
                (9102, "Stale Guard", 13, "G", "starter"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)
            """,
            [
                (
                    9910,
                    active_start.date().isoformat(),
                    active_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    10,
                    3,
                    -4.5,
                    160.5,
                ),
                (
                    9920,
                    stale_start.date().isoformat(),
                    stale_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    13,
                    14,
                    -2.5,
                    158.5,
                ),
            ],
        )
        conn.executemany(
            """
            INSERT INTO player_game_stats (
                player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (9101, 9910, 31.0, 17.0, 4.0, 5.0, 2.0, 1.0, 0.0, 2.0),
                (9102, 9920, 30.0, 15.0, 3.0, 4.0, 1.0, 1.0, 0.0, 2.0),
            ],
        )
        captured_at = datetime.now(timezone.utc).isoformat()
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    "the_odds_api",
                    "active-event",
                    9910,
                    active_start.date().isoformat(),
                    active_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "New York Liberty",
                    "Connecticut Sun",
                    "draftkings",
                    "DraftKings",
                    "player_points",
                    "points",
                    "Active Guard",
                    "over",
                    16.5,
                    -120,
                    captured_at,
                ),
                (
                    "the_odds_api",
                    "active-event",
                    9910,
                    active_start.date().isoformat(),
                    active_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "New York Liberty",
                    "Connecticut Sun",
                    "draftkings",
                    "DraftKings",
                    "player_points",
                    "points",
                    "Active Guard",
                    "under",
                    16.5,
                    100,
                    captured_at,
                ),
                (
                    "the_odds_api",
                    "stale-event",
                    9920,
                    stale_start.date().isoformat(),
                    stale_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "Washington Mystics",
                    "Toronto Tempo",
                    "draftkings",
                    "DraftKings",
                    "player_points",
                    "points",
                    "Stale Guard",
                    "over",
                    14.5,
                    -115,
                    captured_at,
                ),
                (
                    "the_odds_api",
                    "stale-event",
                    9920,
                    stale_start.date().isoformat(),
                    stale_start.astimezone(timezone.utc).isoformat().replace("+00:00", "Z"),
                    "Washington Mystics",
                    "Toronto Tempo",
                    "draftkings",
                    "DraftKings",
                    "player_points",
                    "points",
                    "Stale Guard",
                    "under",
                    14.5,
                    -105,
                    captured_at,
                ),
            ],
        )

        result = main_module._repair_current_slate_props(conn)
        active_prop_lines = int(conn.execute("SELECT COUNT(*) FROM prop_lines WHERE game_id = 9910").fetchone()[0] or 0)
        stale_prop_lines = int(conn.execute("SELECT COUNT(*) FROM prop_lines WHERE game_id = 9920").fetchone()[0] or 0)

    assert result["scope"] == "current_slate"
    assert result["target_game_ids"] == [9910]
    assert result["scanned_props"] >= 1
    assert result["synced_props"] >= 1
    assert active_prop_lines >= 1
    assert stale_prop_lines == 0


def test_repair_current_slate_props_rebuilds_only_changed_prop_lines(monkeypatch) -> None:
    rebuild_calls: list[tuple[list[int] | None, list[int] | None]] = []
    game_rebuild_calls: list[list[int] | None] = []

    def fake_sync(conn, **kwargs):
        assert kwargs["game_ids"] == [9910]
        assert kwargs["include_change_details"] is True
        assert kwargs["rebuild_predictions_after"] is False
        return SyncPropLinesResult(
            synced_props=2,
            changed_props=2,
            changed_prop_line_ids=[501, 502],
            touched_game_ids=[9910],
        )

    def fake_rebuild(conn, game_ids=None, prop_line_ids=None, chunk_size=30, progress_callback=None):
        rebuild_calls.append(
            (
                list(game_ids) if game_ids is not None else None,
                list(prop_line_ids) if prop_line_ids is not None else None,
            )
        )
        return projections_module.LiveRebuildResult(
            projections=[],
            attempted=2,
            written=2,
            skipped=0,
            errors=[],
        )

    monkeypatch.setattr(main_module, "_active_slate_game_ids", lambda conn: [9910])
    monkeypatch.setattr(main_module, "sync_prop_lines_from_sportsbook", fake_sync)
    monkeypatch.setattr(main_module, "rebuild_predictions_live", fake_rebuild)
    monkeypatch.setattr(
        main_module,
        "rebuild_game_predictions_live",
        lambda conn, game_ids=None, progress_callback=None: (
            game_rebuild_calls.append(list(game_ids) if game_ids is not None else None) or {"attempted": 1, "written": 1}
        ),
    )
    monkeypatch.setattr(main_module, "_snapshot_watchlist", lambda conn, slate_date: {"tracked": 0})
    monkeypatch.setattr(main_module, "_snapshot_current_dfs_first_half", lambda conn, game_ids=None: {"inserted": 2, "updated": 0, "skipped": 0})

    with connect() as conn:
        result = main_module._repair_current_slate_props(conn)

    assert rebuild_calls == [(None, [501, 502])]
    assert game_rebuild_calls == [[9910]]
    assert result["scope"] == "current_slate"
    assert result["scanned_props"] == 2
    assert result["synced_props"] == 2
    assert result["changed_prop_line_ids"] == [501, 502]
    assert result["attempted_predictions"] == 2
    assert result["rebuilt_predictions"] == 2
    assert result["skipped_predictions"] == 0
    assert result["attempted_game_predictions"] == 1
    assert result["rebuilt_game_predictions"] == 1
    assert result["dfs_snapshot"] == {"inserted": 2, "updated": 0, "skipped": 0}


def test_repair_current_slate_props_injury_update_rebuilds_full_games_in_batches(monkeypatch) -> None:
    rebuild_calls: list[tuple[list[int] | None, list[int] | None, int]] = []
    game_rebuild_calls: list[list[int] | None] = []

    def fake_rebuild(conn, game_ids=None, prop_line_ids=None, chunk_size=20, progress_callback=None):
        rebuild_calls.append(
            (
                list(game_ids) if game_ids is not None else None,
                list(prop_line_ids) if prop_line_ids is not None else None,
                int(chunk_size),
            )
        )
        return projections_module.LiveRebuildResult(
            projections=[],
            attempted=20,
            written=20,
            skipped=0,
            errors=[],
        )

    def fail_sync(*_args, **_kwargs):
        raise AssertionError("injury_update should not sync sportsbook prop lines")

    monkeypatch.setattr(main_module, "sync_prop_lines_from_sportsbook", fail_sync)
    monkeypatch.setattr(main_module, "rebuild_predictions_live", fake_rebuild)
    monkeypatch.setattr(
        main_module,
        "rebuild_game_predictions_live",
        lambda conn, game_ids=None, progress_callback=None: (
            game_rebuild_calls.append(list(game_ids) if game_ids is not None else None) or {"attempted": 1, "written": 1}
        ),
    )
    monkeypatch.setattr(main_module, "_snapshot_watchlist", lambda conn, slate_date: {"tracked": 0})
    monkeypatch.setattr(main_module, "_snapshot_current_dfs_first_half", lambda conn, game_ids=None: {"inserted": 5, "updated": 0, "skipped": 0})

    with connect() as conn:
        result = main_module._repair_current_slate_props(conn, target_game_ids=[9910])

    assert rebuild_calls == [([9910], None, 30)]
    assert game_rebuild_calls == [[9910]]
    assert result["scope"] == "injury_update"
    assert result["scanned_props"] == 0
    assert result["synced_props"] == 0
    assert result["changed_prop_line_ids"] == []
    assert result["attempted_predictions"] == 20
    assert result["rebuilt_predictions"] == 20
    assert result["dfs_snapshot"] == {"inserted": 5, "updated": 0, "skipped": 0}


def test_rebuild_game_predictions_live_batches_games(monkeypatch) -> None:
    batch_sizes: list[int] = []
    progress_updates: list[tuple[int, int, str | None]] = []

    class DummyResult:
        def __init__(self, rows):
            self._rows = rows

        def fetchall(self):
            return self._rows

    class DummyConn:
        def execute(self, sql, params=()):
            if "FROM games g" in sql:
                rows = [
                    {
                        "id": game_id,
                        "game_date": "2026-07-12",
                        "start_time": f"2026-07-12T{i:02d}:00:00Z",
                        "home_team_id": 1,
                        "away_team_id": 2,
                        "home_team": "NY",
                        "away_team": "CON",
                        "rest_days_home": 2,
                        "rest_days_away": 2,
                        "spread_home": -5.5,
                        "game_total": 158.0,
                        "home_moneyline": None,
                        "away_moneyline": None,
                        "home_spread_price": None,
                        "away_spread_price": None,
                        "over_price": None,
                        "under_price": None,
                    }
                    for i, game_id in enumerate(range(2000, 2045), start=1)
                ]
                return DummyResult(rows)
            raise AssertionError(f"Unexpected SQL: {sql}")

    monkeypatch.setattr(main_module, "project_game", lambda conn, game: {"game_id": int(game["id"])})
    monkeypatch.setattr(
        main_module,
        "save_game_predictions",
        lambda conn, games, predictions: batch_sizes.append(len(games)) or len(games),
    )

    result = main_module.rebuild_game_predictions_live(
        DummyConn(),
        chunk_size=20,
        progress_callback=lambda current, total, message: progress_updates.append((current, total, message)),
    )

    assert batch_sizes == [20, 20, 5]
    assert result == {"attempted": 45, "written": 45}
    assert progress_updates[-1] == (45, 45, "Built 45 of 45 game predictions.")


def test_rebuild_predictions_skips_model_prewarm_when_refresh_disabled(monkeypatch) -> None:
    load_test_history()
    prewarm_calls = 0
    build_flags: list[bool] = []

    def fake_prewarm(conn):
        nonlocal prewarm_calls
        prewarm_calls += 1
        return {}

    def fake_build(conn, prop_line_id, *, runtime_cache=None, allow_training=True):
        build_flags.append(bool(allow_training))
        return projections_module.PropProjection(
            prop_line_id=int(prop_line_id),
            model_version="component",
            prediction_time="2026-06-25T00:00:00+00:00",
            projection=10.0,
            recommended_side="over",
            model_probability=0.55,
            implied_probability=0.50,
            edge=0.05,
            expected_value=0.02,
            confidence="medium",
            reason="test",
        )

    monkeypatch.setattr(projections_module, "prewarm_model_cache", fake_prewarm)
    monkeypatch.setattr(projections_module, "build_prop_projection", fake_build)

    with connect() as conn:
        scheduled_game = conn.execute(
            """
            SELECT g.id
            FROM games g
            JOIN prop_lines pl ON pl.game_id = g.id
            WHERE g.status = 'scheduled'
            ORDER BY g.id
            LIMIT 1
            """
        ).fetchone()
        assert scheduled_game is not None
        rows = projections_module.rebuild_predictions(conn, game_ids=[int(scheduled_game["id"])], refresh_models=False)

    assert prewarm_calls == 0
    assert rows
    assert build_flags
    assert all(flag is False for flag in build_flags)


def test_rebuild_predictions_live_prewarms_and_writes_incrementally_with_training(monkeypatch) -> None:
    load_test_history()
    build_flags: list[bool] = []
    prewarm_calls = 0

    def fake_prewarm(conn):
        nonlocal prewarm_calls
        prewarm_calls += 1
        return {}

    def fake_build(conn, prop_line_id, *, runtime_cache=None, allow_training=True):
        build_flags.append(bool(allow_training))
        return projections_module.PropProjection(
            prop_line_id=int(prop_line_id),
            model_version="component",
            prediction_time="2026-06-25T00:00:00+00:00",
            projection=10.0,
            recommended_side="over",
            model_probability=0.55,
            implied_probability=0.50,
            edge=0.05,
            expected_value=0.02,
            confidence="medium",
            reason="test",
        )

    monkeypatch.setattr(projections_module, "prewarm_model_cache", fake_prewarm)
    monkeypatch.setattr(projections_module, "build_prop_projection", fake_build)

    with connect() as conn:
        scheduled_game = conn.execute(
            """
            SELECT g.id
            FROM games g
            JOIN prop_lines pl ON pl.game_id = g.id
            WHERE g.status = 'scheduled'
            ORDER BY g.id
            LIMIT 1
            """
        ).fetchone()
        assert scheduled_game is not None
        game_id = int(scheduled_game["id"])
        result = projections_module.rebuild_predictions_live(conn, game_ids=[game_id], chunk_size=2)
        count = int(
            conn.execute(
                """
                SELECT COUNT(*)
                FROM prop_predictions pp
                JOIN prop_lines pl ON pl.id = pp.prop_line_id
                WHERE pl.game_id = ?
                """,
                (game_id,),
            ).fetchone()[0]
            or 0
        )

    assert result.attempted > 0
    assert result.written == result.attempted
    assert result.skipped == 0
    assert result.errors == []
    assert count == result.written
    assert prewarm_calls == 1
    assert build_flags
    assert all(flag is True for flag in build_flags)


def test_rebuild_predictions_live_keeps_existing_predictions_when_a_rebuild_fails(monkeypatch) -> None:
    load_test_history()

    def fake_build(conn, prop_line_id, *, runtime_cache=None, allow_training=True):
        if int(prop_line_id) == 101:
            raise RuntimeError("boom")
        return projections_module.PropProjection(
            prop_line_id=int(prop_line_id),
            model_version="component",
            prediction_time="2026-06-25T00:00:00+00:00",
            projection=12.0,
            recommended_side="over",
            model_probability=0.57,
            implied_probability=0.50,
            edge=0.07,
            expected_value=0.03,
            confidence="medium",
            reason="rebuilt",
        )

    monkeypatch.setattr(projections_module, "build_prop_projection", fake_build)

    with connect() as conn:
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines WHERE id IN (101, 102)")
        conn.executemany(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (101, 2010, 1001, "DraftKings", "points", 21.5, -110, -110, "2026-06-24T00:00:00+00:00"),
                (102, 2010, 1001, "FanDuel", "rebounds", 8.5, -115, -105, "2026-06-24T00:00:00+00:00"),
            ],
        )
        conn.executemany(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (101, "stale", "2026-06-24T00:00:00+00:00", 9.0, "under", 0.52, 0.50, 0.02, 0.01, "low", "stale"),
                (102, "stale", "2026-06-24T00:00:00+00:00", 9.5, "under", 0.52, 0.50, 0.02, 0.01, "low", "stale"),
            ],
        )
        conn.commit()

        result = projections_module.rebuild_predictions_live(conn, prop_line_ids=[101, 102], chunk_size=2)
        rows = conn.execute(
            """
            SELECT prop_line_id, model_version, projection, reason
            FROM prop_predictions
            WHERE prop_line_id IN (101, 102)
            ORDER BY prop_line_id
            """
        ).fetchall()

    assert result.attempted == 2
    assert result.written == 1
    assert result.skipped == 1
    assert result.errors == ["101: boom"]
    assert [tuple(row) for row in rows] == [
        (101, "stale", 9.0, "stale"),
        (102, "component", 12.0, "rebuilt"),
    ]


def test_rebuild_predictions_live_does_not_auto_generate_special_snapshots(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    def fake_build(conn, prop_line_id, *, runtime_cache=None, allow_training=True):
        return projections_module.PropProjection(
            prop_line_id=int(prop_line_id),
            model_version="component",
            prediction_time="2026-06-25T00:00:00+00:00",
            projection=10.0,
            recommended_side="over",
            model_probability=0.55,
            implied_probability=0.50,
            edge=0.05,
            expected_value=0.02,
            confidence="medium",
            reason="test",
        )

    monkeypatch.setattr(projections_module, "build_prop_projection", fake_build)

    with connect() as conn:
        scheduled_game = conn.execute(
            """
            SELECT g.id
            FROM games g
            JOIN prop_lines pl ON pl.game_id = g.id
            WHERE g.status = 'scheduled'
            ORDER BY g.id
            LIMIT 1
            """
        ).fetchone()
        assert scheduled_game is not None
        projections_module.rebuild_predictions_live(conn, game_ids=[int(scheduled_game["id"])], chunk_size=2)

    assert tracking_path.exists() is False


def test_special_stocks_only_returns_scheduled_games(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        scheduled_row = conn.execute(
            """
            SELECT g.id AS game_id, p.id AS player_id, p.full_name AS player_name, g.game_date
            FROM games g
            JOIN prop_lines pl ON pl.game_id = g.id
            JOIN players p ON p.id = pl.player_id
            WHERE g.status = 'scheduled'
            ORDER BY g.id, p.id
            LIMIT 1
            """
        ).fetchone()
        assert scheduled_row is not None
        player_row = conn.execute(
            """
            SELECT p.id AS player_id, p.full_name AS player_name, p.team_id
            FROM players p
            WHERE p.id = ?
            """,
            (int(scheduled_row["player_id"]),),
        ).fetchone()
        assert player_row is not None
        home_team_id = int(player_row["team_id"])
        away_team_id = int(
            conn.execute(
                "SELECT id FROM teams WHERE id <> ? ORDER BY id LIMIT 1",
                (home_team_id,),
            ).fetchone()[0]
        )
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, ?, ?)
            """,
            (909001, "2026-05-29", "2026-05-29T23:00:00+00:00", home_team_id, away_team_id, -3.5, 164.5),
        )
        conn.commit()
        scheduled_game_date = str(scheduled_row["game_date"])
        final_row = {
            "game_id": 909001,
            "player_id": int(player_row["player_id"]),
            "player_name": str(player_row["player_name"]),
            "game_date": "2026-05-29",
        }

    stocks_tracking_module.ensure_tracking_schema()
    with sqlite3.connect(tracking_path) as tracking:
        tracking.executemany(
            """
            INSERT INTO projection_snapshots (
                game_id,
                player_id,
                player_name,
                game_date,
                captured_at,
                model_version,
                projected_steals,
                projected_blocks,
                projected_stocks,
                steal_prob_1_plus,
                steal_prob_2_plus,
                block_prob_1_plus,
                block_prob_2_plus,
                stocks_prob_2_plus,
                data_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    int(scheduled_row["game_id"]),
                    int(scheduled_row["player_id"]),
                    str(scheduled_row["player_name"]),
                    str(scheduled_row["game_date"]),
                    "2026-07-11T10:00:00+00:00",
                    MODEL_VERSION,
                    1.2,
                    0.8,
                    2.0,
                    0.7,
                    0.3,
                    0.5,
                    0.2,
                    0.6,
                    "model_only",
                ),
                (
                    int(final_row["game_id"]),
                    int(final_row["player_id"]),
                    str(final_row["player_name"]),
                    str(final_row["game_date"]),
                    "2026-05-29T10:00:00+00:00",
                    MODEL_VERSION,
                    1.0,
                    0.5,
                    1.5,
                    0.6,
                    0.2,
                    0.4,
                    0.1,
                    0.4,
                    "model_only",
                ),
            ],
        )
        tracking.commit()

    monkeypatch.setattr(main_module, "_local_today_iso", lambda: scheduled_game_date)
    rows = main_module.special_stocks()

    assert len(rows) == 1
    assert int(rows[0]["game_id"]) == int(scheduled_row["game_id"])


def test_special_stocks_batches_context_without_losing_recent_history(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        scheduled_row = conn.execute(
            """
            SELECT
                g.id AS game_id,
                g.game_date,
                g.home_team_id,
                g.away_team_id,
                p.id AS player_id,
                p.full_name AS player_name
            FROM games g
            JOIN prop_lines pl ON pl.game_id = g.id
            JOIN players p ON p.id = pl.player_id
            WHERE g.status = 'scheduled'
            ORDER BY g.game_date, g.id, p.id
            LIMIT 1
            """
        ).fetchone()
        assert scheduled_row is not None
        scheduled_game_id = int(scheduled_row["game_id"])
        scheduled_game_date = str(scheduled_row["game_date"])
        second_player = conn.execute(
            """
            SELECT p.id AS player_id, p.full_name AS player_name
            FROM players p
            JOIN player_game_stats s ON s.player_id = p.id
            WHERE p.team_id IN (?, ?)
              AND p.id <> ?
            GROUP BY p.id, p.full_name
            ORDER BY p.id
            LIMIT 1
            """,
            (
                int(scheduled_row["home_team_id"]),
                int(scheduled_row["away_team_id"]),
                int(scheduled_row["player_id"]),
            ),
        ).fetchone()
        assert second_player is not None
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                990001,
                scheduled_game_id,
                int(second_player["player_id"]),
                "test-book",
                "points",
                8.5,
                -110,
                -110,
                "2026-07-11T09:00:00+00:00",
            ),
        )
        conn.commit()
        scheduled_rows = [
            {
                "game_id": scheduled_game_id,
                "player_id": int(scheduled_row["player_id"]),
                "player_name": str(scheduled_row["player_name"]),
                "game_date": scheduled_game_date,
            },
            {
                "game_id": scheduled_game_id,
                "player_id": int(second_player["player_id"]),
                "player_name": str(second_player["player_name"]),
                "game_date": scheduled_game_date,
            },
        ]

    stocks_tracking_module.ensure_tracking_schema()
    with sqlite3.connect(tracking_path) as tracking:
        tracking.executemany(
            """
            INSERT INTO projection_snapshots (
                game_id,
                player_id,
                player_name,
                game_date,
                captured_at,
                model_version,
                projected_steals,
                projected_blocks,
                projected_stocks,
                steal_prob_1_plus,
                steal_prob_2_plus,
                block_prob_1_plus,
                block_prob_2_plus,
                stocks_prob_2_plus,
                data_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
                [
                    (
                        int(row["game_id"]),
                        int(row["player_id"]),
                        str(row["player_name"]),
                        str(row["game_date"]),
                        f"2026-07-11T1{index}:00:00+00:00",
                        MODEL_VERSION,
                        1.0 + index,
                    0.5,
                    1.5 + index,
                    0.6,
                    0.2,
                    0.4,
                    0.1,
                    0.55 + (0.05 * index),
                    "model_only",
                )
                for index, row in enumerate(scheduled_rows)
            ],
        )
        tracking.commit()
    stocks_tracking_module.rebuild_game_board_summaries(game_ids=[scheduled_game_id])

    monkeypatch.setattr(main_module, "_local_today_iso", lambda: scheduled_game_date)
    rows = main_module.special_stocks()

    assert len(rows) == 2
    for row in rows:
        assert int(row["board_player_count"]) == 2
        assert int(row["board_candidate_count_50_plus"]) >= 1
        assert float(row["board_candidate_threshold"]) >= 0.5
        assert int(row["board_candidate_count_threshold"]) >= 1
        assert float(row["board_avg_prob_2_plus"]) > 0.0
        assert row["board_top_player_name"]
        assert row["recent_values"]
        assert row["recent_minutes"]
        assert row["team"] is not None
        assert row["home_team"] is not None
        assert row["away_team"] is not None


def test_rebuild_candidate_players_populates_tracking_pool(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        built = stocks_tracking_module.rebuild_candidate_players(conn)

    assert built > 0
    with sqlite3.connect(tracking_path) as tracking:
        tracking.row_factory = sqlite3.Row
        row = tracking.execute(
            """
            SELECT game_id, player_id, recent_minutes_avg, recent_stocks_avg, candidate_reason
            FROM candidate_players
            ORDER BY recent_stocks_avg DESC, recent_minutes_avg DESC
            LIMIT 1
            """
        ).fetchone()

    assert row is not None
    assert int(row["game_id"]) > 0
    assert int(row["player_id"]) > 0
    assert float(row["recent_minutes_avg"]) >= 10.0
    assert str(row["candidate_reason"])


def test_rebuild_candidate_players_expands_for_specialists_promotions_and_injury_replacements(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    captured_at = datetime.now(timezone.utc).isoformat()

    with connect() as conn:
        conn.executemany(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            [
                (1010, "Bench Promotion", 10, "G", "bench"),
                (1011, "Defensive Specialist", 10, "G", "rotation"),
                (1012, "Injury Replacement", 10, "F", "bench"),
                (1013, "Unavailable Guard One", 10, "G", "starter"),
                (1014, "Unavailable Guard Two", 10, "G", "rotation"),
            ],
        )
        promotion_rows = [
            (1010, 110, 8.0, 6, 2, 1, 0, 0, 0, 1),
            (1010, 111, 8.0, 7, 2, 1, 0, 0, 0, 1),
            (1010, 112, 10.0, 8, 3, 1, 0, 1, 0, 1),
            (1010, 113, 18.0, 9, 3, 2, 0, 0, 1, 1),
            (1010, 114, 19.0, 11, 3, 2, 0, 1, 0, 1),
            (1010, 115, 20.0, 10, 4, 2, 0, 0, 1, 1),
        ]
        specialist_rows = [
            (1011, 110, 9.0, 5, 2, 1, 0, 1, 0, 1),
            (1011, 111, 9.0, 6, 2, 1, 0, 0, 1, 1),
            (1011, 112, 9.0, 5, 2, 1, 0, 1, 0, 1),
            (1011, 113, 9.0, 7, 2, 1, 0, 1, 1, 1),
            (1011, 114, 10.0, 6, 2, 1, 0, 1, 0, 1),
            (1011, 115, 9.0, 6, 2, 1, 0, 0, 1, 1),
        ]
        replacement_rows = [
            (1012, 110, 9.0, 5, 4, 1, 0, 0, 0, 1),
            (1012, 111, 9.0, 4, 4, 1, 0, 0, 1, 1),
            (1012, 112, 8.0, 5, 5, 1, 0, 0, 0, 1),
            (1012, 113, 10.0, 6, 4, 1, 0, 1, 0, 1),
            (1012, 114, 9.0, 5, 5, 1, 0, 0, 0, 1),
        ]
        conn.executemany(
            """
            INSERT INTO player_game_stats (
                player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            promotion_rows + specialist_rows + replacement_rows,
        )
        conn.executemany(
            "INSERT INTO injuries (player_id, status, note, captured_at) VALUES (?, ?, ?, ?)",
            [
                (1013, "out", "test", captured_at),
                (1014, "inactive", "test", captured_at),
            ],
        )
        conn.commit()
        built = stocks_tracking_module.rebuild_candidate_players(conn, game_ids=[2010])

    assert built > 0
    with sqlite3.connect(tracking_path) as tracking:
        tracking.row_factory = sqlite3.Row
        rows = tracking.execute(
            """
            SELECT player_name, candidate_reason
            FROM candidate_players
            WHERE game_id = 2010
              AND player_id IN (1010, 1011, 1012)
            ORDER BY player_id
            """
        ).fetchall()

    reasons = {str(row["player_name"]): str(row["candidate_reason"]) for row in rows}
    assert reasons["Bench Promotion"] == "recent lineup promotion"
    assert reasons["Defensive Specialist"] == "defensive specialist profile"
    assert reasons["Injury Replacement"] == "injury opportunity replacement"


def test_rebuild_prepared_games_copies_scheduled_slate(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        copied = stocks_tracking_module.rebuild_prepared_games(conn)
        expected = int(
            conn.execute("SELECT COUNT(*) FROM games WHERE status = 'scheduled'").fetchone()[0]
        )

    assert copied == expected
    with sqlite3.connect(tracking_path) as tracking:
        row = tracking.execute("SELECT COUNT(*) FROM prepared_games").fetchone()
    assert row is not None
    assert int(row[0]) == expected


def test_snapshot_stocks_can_use_candidate_pool_without_prop_lines(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    runtime_cache_ids: list[int] = []

    with connect() as conn:
        candidate = conn.execute(
            """
            SELECT
                g.id AS game_id,
                g.game_date,
                p.id AS player_id,
                p.full_name
            FROM games g
            JOIN players p ON p.team_id IN (g.home_team_id, g.away_team_id)
            WHERE g.status = 'scheduled'
            ORDER BY g.id, p.id
            LIMIT 1
            """
        ).fetchone()
        assert candidate is not None
        conn.execute(
            "DELETE FROM prop_predictions WHERE prop_line_id IN (SELECT id FROM prop_lines WHERE game_id = ? AND player_id = ?)",
            (int(candidate["game_id"]), int(candidate["player_id"])),
        )
        conn.execute(
            "DELETE FROM prop_lines WHERE game_id = ? AND player_id = ?",
            (int(candidate["game_id"]), int(candidate["player_id"])),
        )
        conn.commit()

    stocks_tracking_module.ensure_tracking_schema()
    with sqlite3.connect(tracking_path) as tracking:
        tracking.execute(
            """
            INSERT INTO candidate_players (
                game_id,
                player_id,
                player_name,
                game_date,
                team_id,
                team_abbr,
                rotation_role,
                recent_minutes_avg,
                recent_stocks_avg,
                recent_games,
                candidate_reason,
                built_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(candidate["game_id"]),
                int(candidate["player_id"]),
                str(candidate["full_name"]),
                str(candidate["game_date"]),
                10,
                "NY",
                "starter",
                28.0,
                1.7,
                8,
                "test candidate",
                datetime.now(timezone.utc).isoformat(),
            ),
        )
        tracking.commit()

    class _FakeSnapshot:
        def __init__(self, projection: float) -> None:
            self.component_projection = projection

    def fake_feature_snapshot(
        conn,
        player_id,
        market,
        game_id,
        before_game_date=None,
        allow_training=True,
        use_injury_context=True,
        use_live_minutes_context=True,
        runtime_cache=None,
    ):
        assert runtime_cache is not None
        runtime_cache_ids.append(id(runtime_cache))
        assert int(player_id) == int(candidate["player_id"])
        assert int(game_id) == int(candidate["game_id"])
        assert use_live_minutes_context is False
        return _FakeSnapshot(1.0 if market == "steals" else 0.5)

    monkeypatch.setattr(stocks_tracking_module, "feature_snapshot", fake_feature_snapshot)

    with connect() as conn:
        written = stocks_tracking_module.snapshot_stocks(conn, game_ids=[int(candidate["game_id"])])

    assert written > 0
    assert runtime_cache_ids
    assert len(set(runtime_cache_ids)) == 1


def test_rebuild_player_prep_features_populates_tracking_cache(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        candidate_count = stocks_tracking_module.rebuild_candidate_players(conn)
        built = stocks_tracking_module.rebuild_player_prep_features(conn)

    assert candidate_count > 0
    assert built >= candidate_count * 3
    with sqlite3.connect(tracking_path) as tracking:
        tracking.row_factory = sqlite3.Row
        row = tracking.execute(
            """
            SELECT
                game_id,
                player_id,
                market,
                contextual_projection,
                rest_days,
                projected_minutes,
                minute_volatility,
                injury_status,
                injury_availability_factor,
                injury_usage_multiplier,
                injury_minutes_delta,
                opportunity_unavailable,
                opportunity_key_outs,
                opportunity_persistence,
                opportunity_competition
            FROM player_prep_features
            ORDER BY game_id, player_id, market
            LIMIT 1
            """
        ).fetchone()

    assert row is not None
    assert int(row["game_id"]) > 0
    assert int(row["player_id"]) > 0
    assert str(row["market"]) in {"steals", "blocks", "blocks_steals"}
    assert float(row["contextual_projection"]) >= 0.0
    assert int(row["rest_days"]) >= 0
    assert float(row["projected_minutes"]) >= 0.0
    assert float(row["minute_volatility"]) >= 0.0
    assert str(row["injury_status"])
    assert float(row["injury_availability_factor"]) > 0.0
    assert float(row["injury_usage_multiplier"]) > 0.0
    assert abs(float(row["injury_minutes_delta"])) < 20.0
    assert float(row["opportunity_unavailable"]) >= 0.0
    assert float(row["opportunity_key_outs"]) >= 0.0
    assert float(row["opportunity_persistence"]) >= 0.0
    assert float(row["opportunity_competition"]) >= 0.0


def test_rebuild_team_prep_context_populates_tracking_cache(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        stocks_tracking_module.rebuild_prepared_games(conn)
        built = stocks_tracking_module.rebuild_team_prep_context(conn)

    assert built > 0
    with sqlite3.connect(tracking_path) as tracking:
        tracking.row_factory = sqlite3.Row
        row = tracking.execute(
            """
            SELECT
                game_id,
                team_id,
                opponent_id,
                pace_factor,
                steals_allowed_factor,
                blocks_allowed_factor,
                stocks_allowed_factor,
                team_turnover_rate_factor,
                forced_turnover_rate_factor,
                turnover_pressure_factor
            FROM team_prep_context
            ORDER BY game_id, team_id
            LIMIT 1
            """
        ).fetchone()

    assert row is not None
    assert int(row["game_id"]) > 0
    assert int(row["team_id"]) > 0
    assert int(row["opponent_id"]) > 0
    assert float(row["pace_factor"]) > 0.0
    assert float(row["steals_allowed_factor"]) > 0.0
    assert float(row["blocks_allowed_factor"]) > 0.0
    assert float(row["stocks_allowed_factor"]) > 0.0
    assert float(row["team_turnover_rate_factor"]) > 0.0
    assert float(row["forced_turnover_rate_factor"]) > 0.0
    assert float(row["turnover_pressure_factor"]) > 0.0


def test_prepare_stocks_data_targets_selected_dates(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        scheduled_dates = [
            str(row["game_date"])
            for row in conn.execute(
                """
                SELECT DISTINCT game_date
                FROM games
                WHERE status = 'scheduled'
                ORDER BY game_date
                LIMIT 2
                """
            ).fetchall()
        ]
        assert scheduled_dates
        result = stocks_tracking_module.prepare_stocks_data(conn, target_dates=[scheduled_dates[0]])

    assert result["selected_dates"] == [scheduled_dates[0]]
    assert result["prepared_games"] > 0
    assert result["candidate_players"] > 0
    assert result["team_prep_context"] >= result["prepared_games"] * 2
    assert result["player_prep_features"] >= result["candidate_players"] * 3
    assert result["game_board_summaries"] == result["prepared_games"]
    assert result["snapshots_written"] > 0
    with sqlite3.connect(tracking_path) as tracking:
        prepared_dates = [
            str(row[0])
            for row in tracking.execute(
                "SELECT DISTINCT game_date FROM prepared_games ORDER BY game_date"
            ).fetchall()
        ]
        tracked_dates = [
            str(row[0])
            for row in tracking.execute(
                "SELECT DISTINCT game_date FROM candidate_players ORDER BY game_date"
            ).fetchall()
        ]
        prep_feature_dates = [
            str(row[0])
            for row in tracking.execute(
                "SELECT DISTINCT game_date FROM player_prep_features ORDER BY game_date"
            ).fetchall()
        ]
        team_context_dates = [
            str(row[0])
            for row in tracking.execute(
                "SELECT DISTINCT game_date FROM team_prep_context ORDER BY game_date"
            ).fetchall()
        ]
        board_summary_dates = [
            str(row[0])
            for row in tracking.execute(
                "SELECT DISTINCT game_date FROM game_board_summaries ORDER BY game_date"
            ).fetchall()
        ]
        prep_run = tracking.execute(
            """
            SELECT status, scope, prepared_games, candidate_players, game_board_summaries, snapshots_written
            FROM prep_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert prepared_dates == [scheduled_dates[0]]
    assert tracked_dates == [scheduled_dates[0]]
    assert team_context_dates == [scheduled_dates[0]]
    assert prep_feature_dates == [scheduled_dates[0]]
    assert board_summary_dates == [scheduled_dates[0]]
    assert prep_run is not None
    assert str(prep_run[0]) == "completed"
    assert str(prep_run[1]) == "dates"
    assert int(prep_run[2]) == int(result["prepared_games"])
    assert int(prep_run[3]) == int(result["candidate_players"])
    assert int(prep_run[4]) == int(result["game_board_summaries"])
    assert int(prep_run[5]) == int(result["snapshots_written"])


def test_prepare_stocks_data_targets_selected_games(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        scheduled_game_ids = [
            int(row["id"])
            for row in conn.execute(
                """
                SELECT id
                FROM games
                WHERE status = 'scheduled'
                ORDER BY id
                LIMIT 1
                """
            ).fetchall()
        ]
        assert scheduled_game_ids
        result = stocks_tracking_module.prepare_stocks_data(conn, game_ids=scheduled_game_ids)

    assert result["game_ids"] == scheduled_game_ids
    assert len(result["selected_dates"]) == 1
    assert result["prepared_games"] == 1
    assert result["candidate_players"] > 0
    assert result["team_prep_context"] == 2
    assert result["player_prep_features"] >= result["candidate_players"] * 3
    assert result["game_board_summaries"] == 1
    with sqlite3.connect(tracking_path) as tracking:
        tracked_game_ids = [
            int(row[0])
            for row in tracking.execute(
                "SELECT DISTINCT game_id FROM prepared_games ORDER BY game_id"
            ).fetchall()
        ]
        summary_row = tracking.execute(
            """
            SELECT player_count, candidate_count_50_plus, candidate_threshold, candidate_count_threshold, top_player_name
            FROM game_board_summaries
            WHERE game_id = ?
            """,
            (scheduled_game_ids[0],),
        ).fetchone()
        prep_run = tracking.execute(
            """
            SELECT status, scope, prepared_games, team_prep_context, game_board_summaries
            FROM prep_runs
            ORDER BY id DESC
            LIMIT 1
            """
        ).fetchone()

    assert tracked_game_ids == scheduled_game_ids
    assert summary_row is not None
    assert int(summary_row[0]) > 0
    assert int(summary_row[1]) >= 0
    assert float(summary_row[2]) >= 0.5
    assert int(summary_row[3]) >= 0
    assert str(summary_row[4])
    assert prep_run is not None
    assert str(prep_run[0]) == "completed"
    assert str(prep_run[1]) == "game_ids"
    assert int(prep_run[2]) == 1
    assert int(prep_run[3]) == 2
    assert int(prep_run[4]) == 1


def test_snapshot_stocks_uses_shared_runtime_cache_by_default(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    runtime_cache_ids: list[int] = []

    class _FakeSnapshot:
        def __init__(self, projection: float) -> None:
            self.component_projection = projection

    def fake_feature_snapshot(
        conn,
        player_id,
        market,
        game_id,
        before_game_date=None,
        allow_training=True,
        use_injury_context=True,
        use_live_minutes_context=True,
        runtime_cache=None,
    ):
        assert runtime_cache is not None
        runtime_cache_ids.append(id(runtime_cache))
        assert use_live_minutes_context is False
        return _FakeSnapshot(1.0 if market == "steals" else 0.5)

    monkeypatch.setattr(stocks_tracking_module, "feature_snapshot", fake_feature_snapshot)

    with connect() as conn:
        written = stocks_tracking_module.snapshot_stocks(conn)

    assert written > 0
    assert runtime_cache_ids
    assert len(set(runtime_cache_ids)) == 1


def test_snapshot_stocks_prefers_precomputed_player_prep_features(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        stocks_tracking_module.rebuild_candidate_players(conn)
        built = stocks_tracking_module.rebuild_player_prep_features(conn)

    def fail_feature_snapshot(*args, **kwargs):
        raise AssertionError("snapshot_stocks should use precomputed player prep features")

    monkeypatch.setattr(stocks_tracking_module, "feature_snapshot", fail_feature_snapshot)

    with connect() as conn:
        written = stocks_tracking_module.snapshot_stocks(conn)

    assert built > 0
    assert written > 0


def test_snapshot_stocks_skips_unchanged_rows(monkeypatch, tmp_path) -> None:
    load_test_history()
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with connect() as conn:
        stocks_tracking_module.rebuild_candidate_players(conn)
        stocks_tracking_module.rebuild_team_prep_context(conn)
        stocks_tracking_module.rebuild_player_prep_features(conn)
        first_written = stocks_tracking_module.snapshot_stocks(conn)
        second_written = stocks_tracking_module.snapshot_stocks(conn)

    assert first_written > 0
    assert second_written == 0
    with sqlite3.connect(tracking_path) as tracking:
        snapshot_count = tracking.execute("SELECT COUNT(*) FROM projection_snapshots").fetchone()[0]

    assert int(snapshot_count) == int(first_written)


def test_history_winsorization_clips_market_outliers() -> None:
    clipped = player_prop_model_module._winsorize_history_values([1.0, 1.0, 2.0, 2.0, 9.0], "blocks_steals")

    assert len(clipped) == 5
    assert clipped[-1] < 9.0
    assert clipped[-1] > 2.0


def test_blowout_history_weight_downweights_starter_rows() -> None:
    starter_weight = player_prop_model_module._historical_blowout_weight({"team_margin": 19.0}, "starter")
    bench_weight = player_prop_model_module._historical_blowout_weight({"team_margin": 19.0}, "bench")
    close_weight = player_prop_model_module._historical_blowout_weight({"team_margin": 8.0}, "starter")

    assert starter_weight < bench_weight
    assert starter_weight < 1.0
    assert close_weight == 1.0


def test_extreme_blowout_history_prune_keeps_competitive_starter_sample() -> None:
    rows = [
        {"team_margin": 8.0, "rotation_role": "starter"},
        {"team_margin": 11.0, "rotation_role": "starter"},
        {"team_margin": 7.0, "rotation_role": "starter"},
        {"team_margin": 12.0, "rotation_role": "starter"},
        {"team_margin": 9.0, "rotation_role": "starter"},
        {"team_margin": 22.0, "rotation_role": "starter"},
        {"team_margin": 26.0, "rotation_role": "starter"},
    ]

    filtered = player_prop_model_module._prune_extreme_blowout_history_rows(rows, "starter")
    bench_filtered = player_prop_model_module._prune_extreme_blowout_history_rows(rows, "bench")

    assert len(filtered) == 5
    assert all(float(row["team_margin"]) < 20.0 for row in filtered)
    assert len(bench_filtered) == len(rows)


def test_stocks_feature_bundle_prunes_extreme_blowout_rows(monkeypatch) -> None:
    monkeypatch.setattr(stocks_tracking_module, "_player_stocks_target_context", lambda *args, **kwargs: (1, 2))
    monkeypatch.setattr(
        stocks_tracking_module,
        "_player_stocks_history_rows",
        lambda *args, **kwargs: [
            {"steals": 1.0, "blocks": 1.0, "is_home": 1, "rest_days": 2, "rotation_role": "starter", "team_margin": 8.0},
            {"steals": 1.0, "blocks": 1.0, "is_home": 1, "rest_days": 2, "rotation_role": "starter", "team_margin": 9.0},
            {"steals": 1.0, "blocks": 1.0, "is_home": 1, "rest_days": 2, "rotation_role": "starter", "team_margin": 11.0},
            {"steals": 1.0, "blocks": 1.0, "is_home": 1, "rest_days": 2, "rotation_role": "starter", "team_margin": 12.0},
            {"steals": 1.0, "blocks": 1.0, "is_home": 1, "rest_days": 2, "rotation_role": "starter", "team_margin": 7.0},
            {"steals": 1.0, "blocks": 1.0, "is_home": 1, "rest_days": 2, "rotation_role": "starter", "team_margin": 10.0},
            {"steals": 4.0, "blocks": 2.0, "is_home": 1, "rest_days": 2, "rotation_role": "starter", "team_margin": 23.0},
            {"steals": 3.0, "blocks": 2.0, "is_home": 1, "rest_days": 2, "rotation_role": "starter", "team_margin": 25.0},
        ],
    )

    bundle = stocks_tracking_module._build_player_stocks_feature_bundle(
        sqlite3.connect(":memory:"),
        player_id=1,
        game_id=10,
        game_date="2026-07-13",
        market="blocks_steals",
        base_projection=2.0,
    )

    assert bundle["same_venue_games"] == 6
    assert float(bundle["recent_avg"]) == pytest.approx(2.0)
    assert float(bundle["stability_avg"]) == pytest.approx(2.0)


def test_stocks_role_bucket_maps_common_positions() -> None:
    assert stocks_tracking_module._stocks_role_bucket("G") == "guard"
    assert stocks_tracking_module._stocks_role_bucket("PG") == "guard"
    assert stocks_tracking_module._stocks_role_bucket("C") == "big"
    assert stocks_tracking_module._stocks_role_bucket("FC") == "big"
    assert stocks_tracking_module._stocks_role_bucket("F") == "wing"
    assert stocks_tracking_module._stocks_role_bucket("") == "wing"


def test_stocks_matchup_multiplier_uses_role_specific_allowed_factors() -> None:
    context = {
        "pace_factor": 1.00,
        "steals_allowed_factor": 0.98,
        "steals_allowed_guard_factor": 1.10,
        "steals_allowed_wing_factor": 0.97,
        "steals_allowed_big_factor": 0.93,
        "blocks_allowed_factor": 1.01,
        "blocks_allowed_guard_factor": 0.95,
        "blocks_allowed_wing_factor": 1.02,
        "blocks_allowed_big_factor": 1.08,
        "stocks_allowed_factor": 1.00,
        "stocks_allowed_guard_factor": 1.09,
        "stocks_allowed_wing_factor": 0.99,
        "stocks_allowed_big_factor": 1.04,
        "team_turnover_rate_factor": 1.08,
        "forced_turnover_rate_factor": 1.06,
        "turnover_pressure_factor": 1.04,
    }

    guard_steals = stocks_tracking_module._stocks_matchup_context_multiplier(
        context,
        "steals",
        role_bucket="guard",
    )
    big_blocks = stocks_tracking_module._stocks_matchup_context_multiplier(
        context,
        "blocks",
        role_bucket="big",
    )
    wing_stocks = stocks_tracking_module._stocks_matchup_context_multiplier(
        context,
        "blocks_steals",
        role_bucket="wing",
    )

    assert guard_steals == pytest.approx(
        (0.50 * 1.10) + (0.20 * 1.04) + (0.15 * 1.08) + (0.10 * 1.06) + (0.05 * 1.00)
    )
    assert big_blocks == pytest.approx((0.65 * 1.08) + (0.35 * 1.00))
    assert wing_stocks == pytest.approx(
        (0.35 * 0.99)
        + (0.18 * 0.97)
        + (0.17 * 1.02)
        + (0.10 * 1.04)
        + (0.10 * 1.08)
        + (0.05 * 1.06)
        + (0.05 * 1.00)
    )


def test_stocks_tracking_schema_adds_role_aware_context_columns(monkeypatch, tmp_path) -> None:
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with sqlite3.connect(tracking_path) as conn:
        conn.execute(
            """
            CREATE TABLE team_prep_context (
                game_id INTEGER NOT NULL,
                game_date TEXT NOT NULL,
                team_id INTEGER NOT NULL,
                opponent_id INTEGER NOT NULL,
                is_home INTEGER NOT NULL,
                pace_factor REAL NOT NULL DEFAULT 1,
                steals_allowed_factor REAL NOT NULL DEFAULT 1,
                blocks_allowed_factor REAL NOT NULL DEFAULT 1,
                stocks_allowed_factor REAL NOT NULL DEFAULT 1,
                turnover_pressure_factor REAL NOT NULL DEFAULT 1,
                built_at TEXT NOT NULL,
                PRIMARY KEY (game_id, team_id)
            )
            """
        )
        conn.commit()

    stocks_tracking_module.ensure_tracking_schema()

    with sqlite3.connect(tracking_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(team_prep_context)").fetchall()}

    assert "steals_allowed_guard_factor" in columns
    assert "steals_allowed_wing_factor" in columns
    assert "steals_allowed_big_factor" in columns
    assert "blocks_allowed_guard_factor" in columns
    assert "blocks_allowed_wing_factor" in columns
    assert "blocks_allowed_big_factor" in columns
    assert "stocks_allowed_guard_factor" in columns
    assert "stocks_allowed_wing_factor" in columns
    assert "stocks_allowed_big_factor" in columns
    assert "team_turnover_rate_factor" in columns
    assert "forced_turnover_rate_factor" in columns


def test_stocks_tracking_schema_adds_player_prep_context_columns(monkeypatch, tmp_path) -> None:
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))

    with sqlite3.connect(tracking_path) as conn:
        conn.execute(
            """
            CREATE TABLE player_prep_features (
                game_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                game_date TEXT NOT NULL,
                market TEXT NOT NULL,
                base_projection REAL NOT NULL DEFAULT 0,
                contextual_projection REAL NOT NULL DEFAULT 0,
                recent_avg REAL NOT NULL DEFAULT 0,
                stability_avg REAL NOT NULL DEFAULT 0,
                same_venue_avg REAL NOT NULL DEFAULT 0,
                same_venue_games INTEGER NOT NULL DEFAULT 0,
                recent_hit_rate_2_plus REAL,
                rest_days INTEGER NOT NULL DEFAULT 2,
                is_home INTEGER,
                built_at TEXT NOT NULL,
                PRIMARY KEY (game_id, player_id, market)
            )
            """
        )
        conn.commit()

    stocks_tracking_module.ensure_tracking_schema()

    with sqlite3.connect(tracking_path) as conn:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(player_prep_features)").fetchall()}

    assert "projected_minutes" in columns
    assert "minute_volatility" in columns
    assert "injury_status" in columns
    assert "injury_availability_factor" in columns
    assert "injury_usage_multiplier" in columns
    assert "injury_minutes_delta" in columns
    assert "opportunity_unavailable" in columns
    assert "opportunity_key_outs" in columns
    assert "opportunity_persistence" in columns
    assert "opportunity_competition" in columns


def test_delete_unavailable_special_snapshots_from_espn_removes_missing_and_dnp(monkeypatch, tmp_path) -> None:
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    stocks_tracking_module.ensure_tracking_schema()

    def fake_fetch_summary(game_id: int, force_refresh: bool = False) -> dict:
        assert game_id == 401857065
        return {
            "boxscore": {
                "players": [
                    {
                        "team": {"abbreviation": "PHX"},
                        "statistics": [
                            {
                                "athletes": [
                                    {
                                        "athlete": {"id": "1001", "displayName": "Keep Player"},
                                        "didNotPlay": False,
                                        "active": True,
                                        "stats": ["21"],
                                    },
                                    {
                                        "athlete": {"id": "1002", "displayName": "DNP Player"},
                                        "didNotPlay": True,
                                        "active": False,
                                        "reason": "COACH'S DECISION",
                                        "stats": [],
                                    },
                                ]
                            }
                        ],
                    }
                ]
            }
        }

    monkeypatch.setattr("backend.app.stocks_tracking.fetch_summary", fake_fetch_summary)

    with connect() as conn:
        conn.executemany(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            [
                (1001, "Keep Player", 10, "G", "starter"),
                (1002, "DNP Player", 10, "G", "rotation"),
                (1003, "Missing Player", 10, "F", "rotation"),
            ],
        )
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (9001, '2026-07-13', '2026-07-13T23:30:00Z', 10, 3, 'final', 2, 2, -2.5, 162.5, 401857065)
            """
        )
        with sqlite3.connect(tracking_path) as tracking:
            tracking.execute(
                """
                INSERT INTO projection_snapshots (
                    id, game_id, player_id, player_name, game_date, captured_at, model_version,
                    projected_steals, projected_blocks, projected_stocks,
                    steal_prob_1_plus, steal_prob_2_plus, block_prob_1_plus, block_prob_2_plus,
                    stocks_prob_2_plus, stocks_prob_3_plus, data_quality
                ) VALUES
                    (1, 9001, 1001, 'Keep Player', '2026-07-14', '2026-07-14T01:00:00+00:00', 'model', 1, 0, 1, 0.5, 0.2, 0.3, 0.1, 0.4, 0.1, 'model_only'),
                    (2, 9001, 1002, 'DNP Player', '2026-07-14', '2026-07-14T01:00:00+00:00', 'model', 1, 0, 1, 0.5, 0.2, 0.3, 0.1, 0.4, 0.1, 'model_only'),
                    (3, 9001, 1003, 'Missing Player', '2026-07-14', '2026-07-14T01:00:00+00:00', 'model', 1, 0, 1, 0.5, 0.2, 0.3, 0.1, 0.4, 0.1, 'model_only')
                """
            )
            tracking.commit()

        result = stocks_tracking_module.delete_unavailable_special_snapshots_from_espn(conn, game_ids=[9001])

    assert result["eligible_snapshots"] == 3
    assert result["deleted"] == 2
    assert result["deleted_did_not_play"] == 1
    assert result["deleted_missing_boxscore"] == 1
    with sqlite3.connect(tracking_path) as tracking:
        remaining = tracking.execute(
            "SELECT player_id FROM projection_snapshots ORDER BY player_id"
        ).fetchall()
    assert [int(row[0]) for row in remaining] == [1001]


def test_delete_unavailable_special_snapshots_from_espn_supports_dry_run(monkeypatch, tmp_path) -> None:
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    stocks_tracking_module.ensure_tracking_schema()

    monkeypatch.setattr(
        "backend.app.stocks_tracking.fetch_summary",
        lambda game_id, force_refresh=False: {
            "boxscore": {
                "players": [
                    {
                        "team": {"abbreviation": "PHX"},
                        "statistics": [
                            {
                                "athletes": [
                                    {
                                        "athlete": {"id": "9999", "displayName": "Other Player"},
                                        "didNotPlay": False,
                                        "active": True,
                                        "stats": ["18"],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        },
    )

    with connect() as conn:
        conn.execute("INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (1004, 'Dry Run Player', 10, 'G', 'starter')")
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (9002, '2026-07-13', '2026-07-13T23:30:00Z', 10, 3, 'final', 2, 2, -2.5, 162.5, 401857066)
            """
        )
        with sqlite3.connect(tracking_path) as tracking:
            tracking.execute(
                """
                INSERT INTO projection_snapshots (
                    id, game_id, player_id, player_name, game_date, captured_at, model_version,
                    projected_steals, projected_blocks, projected_stocks,
                    steal_prob_1_plus, steal_prob_2_plus, block_prob_1_plus, block_prob_2_plus,
                    stocks_prob_2_plus, stocks_prob_3_plus, data_quality
                ) VALUES
                    (4, 9002, 1004, 'Dry Run Player', '2026-07-14', '2026-07-14T01:00:00+00:00', 'model', 1, 0, 1, 0.5, 0.2, 0.3, 0.1, 0.4, 0.1, 'model_only')
                """
            )
            tracking.commit()

        result = stocks_tracking_module.delete_unavailable_special_snapshots_from_espn(
            conn,
            game_ids=[9002],
            dry_run=True,
        )

    assert result["deleted"] == 1
    assert result["dry_run"] is True
    with sqlite3.connect(tracking_path) as tracking:
        remaining_count = tracking.execute("SELECT COUNT(*) FROM projection_snapshots").fetchone()[0]
    assert int(remaining_count) == 1


def test_delete_unavailable_special_snapshots_uses_eastern_local_game_date_filter(monkeypatch, tmp_path) -> None:
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    stocks_tracking_module.ensure_tracking_schema()

    monkeypatch.setattr(
        "backend.app.stocks_tracking.fetch_summary",
        lambda game_id, force_refresh=False: {
            "boxscore": {
                "players": [
                    {
                        "team": {"abbreviation": "PHX"},
                        "statistics": [
                            {
                                "athletes": [
                                    {
                                        "athlete": {"id": "9999", "displayName": "Other Player"},
                                        "didNotPlay": False,
                                        "active": True,
                                        "stats": ["18"],
                                    }
                                ]
                            }
                        ],
                    }
                ]
            }
        },
    )

    with connect() as conn:
        conn.execute("INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (1005, 'Local Date Player', 10, 'G', 'starter')")
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (9003, '2026-07-14', '2026-07-14T02:30:00Z', 10, 3, 'final', 2, 2, -2.5, 162.5, 401857067)
            """
        )
        with sqlite3.connect(tracking_path) as tracking:
            tracking.execute(
                """
                INSERT INTO projection_snapshots (
                    id, game_id, player_id, player_name, game_date, captured_at, model_version,
                    projected_steals, projected_blocks, projected_stocks,
                    steal_prob_1_plus, steal_prob_2_plus, block_prob_1_plus, block_prob_2_plus,
                    stocks_prob_2_plus, stocks_prob_3_plus, data_quality
                ) VALUES
                    (5, 9003, 1005, 'Local Date Player', '2026-07-14', '2026-07-14T01:00:00+00:00', 'model', 1, 0, 1, 0.5, 0.2, 0.3, 0.1, 0.4, 0.1, 'model_only')
                """
            )
            tracking.commit()

        result = stocks_tracking_module.delete_unavailable_special_snapshots_from_espn(
            conn,
            target_dates=["2026-07-13"],
        )

    assert result["eligible_snapshots"] == 1
    assert result["deleted"] == 1


def test_prune_special_snapshots_uses_eastern_today_boundary(monkeypatch, tmp_path) -> None:
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    stocks_tracking_module.ensure_tracking_schema()
    monkeypatch.setattr(stocks_tracking_module, "local_today_iso", lambda: "2026-07-14")

    with connect() as conn:
        conn.execute("INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (1006, 'Boundary Player', 10, 'G', 'starter')")
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (9004, '2026-07-15', '2026-07-15T02:30:00Z', 10, 3, 'scheduled', 2, 2, -2.5, 162.5, 401857068)
            """
        )
        with sqlite3.connect(tracking_path) as tracking:
            tracking.execute(
                """
                INSERT INTO projection_snapshots (
                    id, game_id, player_id, player_name, game_date, captured_at, model_version,
                    projected_steals, projected_blocks, projected_stocks,
                    steal_prob_1_plus, steal_prob_2_plus, block_prob_1_plus, block_prob_2_plus,
                    stocks_prob_2_plus, stocks_prob_3_plus, data_quality
                ) VALUES
                    (6, 9004, 1006, 'Boundary Player', '2026-07-15', '2026-07-14T23:00:00+00:00', 'model', 1, 0, 1, 0.5, 0.2, 0.3, 0.1, 0.4, 0.1, 'model_only')
                """
            )
            tracking.commit()

        result = stocks_tracking_module.prune_special_snapshots(conn)

    assert result["deleted_stale"] == 0


def test_special_probability_calibration_is_market_specific(monkeypatch, tmp_path) -> None:
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    stocks_tracking_module.ensure_tracking_schema()

    with sqlite3.connect(tracking_path) as tracking:
        tracking.row_factory = sqlite3.Row
        rows = []
        settlements = []
        for idx in range(30):
            snapshot_id = idx + 1
            game_id = 5000 + idx
            rows.append(
                (
                    snapshot_id,
                    game_id,
                    7000 + idx,
                    f"Calibrated Player {idx}",
                    "2026-05-01",
                    f"2026-05-01T0{idx % 10}:00:00+00:00",
                    COMPONENT_MODEL_VERSION,
                    1.0,
                    0.6,
                    1.6,
                    0.63,
                    0.26,
                    0.45,
                    0.12,
                    0.35,
                    0.08,
                    "model_only",
                )
            )
            settlements.append((snapshot_id, 1, 0, "2026-05-02T00:00:00+00:00"))
        for idx in range(30, 60):
            snapshot_id = idx + 1
            game_id = 5000 + idx
            rows.append(
                (
                    snapshot_id,
                    game_id,
                    7000 + idx,
                    f"Calibrated Player {idx}",
                    "2026-05-03",
                    f"2026-05-03T0{idx % 10}:00:00+00:00",
                    COMPONENT_MODEL_VERSION,
                    0.8,
                    0.7,
                    2.2,
                    0.55,
                    0.19,
                    0.50,
                    0.16,
                    0.42,
                    0.19,
                    "model_only",
                )
            )
            settlements.append((snapshot_id, 1, 1, "2026-05-04T00:00:00+00:00"))
        tracking.executemany(
            """
            INSERT INTO projection_snapshots (
                id,
                game_id,
                player_id,
                player_name,
                game_date,
                captured_at,
                model_version,
                projected_steals,
                projected_blocks,
                projected_stocks,
                steal_prob_1_plus,
                steal_prob_2_plus,
                block_prob_1_plus,
                block_prob_2_plus,
                stocks_prob_2_plus,
                stocks_prob_3_plus,
                data_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        tracking.executemany(
            """
            INSERT INTO settlements (
                snapshot_id,
                actual_steals,
                actual_blocks,
                settled_at
            ) VALUES (?, ?, ?, ?)
            """,
            settlements,
        )
        tracking.commit()

        steals_raw = 1 - math.exp(-1.0)
        steals_calibrated = stocks_tracking_module._calibrated_special_probability(
            tracking,
            market="steals",
            threshold=1,
            projected_value=1.0,
            raw_probability=steals_raw,
            game_date="2026-05-10",
        )
        stocks_raw = stocks_tracking_module._poisson_at_least(2.2, 3)
        stocks_calibrated = stocks_tracking_module._calibrated_special_probability(
            tracking,
            market="blocks_steals",
            threshold=3,
            projected_value=2.2,
            raw_probability=stocks_raw,
            game_date="2026-05-10",
        )

    assert steals_calibrated > steals_raw
    assert stocks_calibrated < stocks_raw


def test_special_probability_calibration_requires_minimum_history_support(monkeypatch, tmp_path) -> None:
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    stocks_tracking_module.ensure_tracking_schema()

    with sqlite3.connect(tracking_path) as tracking:
        tracking.row_factory = sqlite3.Row
        rows = []
        settlements = []
        for idx in range(20):
            snapshot_id = idx + 1
            rows.append(
                (
                    snapshot_id,
                    6100 + idx,
                    7100 + idx,
                    f"Sparse History Player {idx}",
                    "2026-05-01",
                    f"2026-05-01T0{idx % 10}:00:00+00:00",
                    COMPONENT_MODEL_VERSION,
                    1.1,
                    0.4,
                    1.5,
                    0.67,
                    0.30,
                    0.33,
                    0.10,
                    0.44,
                    0.14,
                    "model_only",
                )
            )
            settlements.append((snapshot_id, 1, 0, "2026-05-02T00:00:00+00:00"))
        tracking.executemany(
            """
            INSERT INTO projection_snapshots (
                id,
                game_id,
                player_id,
                player_name,
                game_date,
                captured_at,
                model_version,
                projected_steals,
                projected_blocks,
                projected_stocks,
                steal_prob_1_plus,
                steal_prob_2_plus,
                block_prob_1_plus,
                block_prob_2_plus,
                stocks_prob_2_plus,
                stocks_prob_3_plus,
                data_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        tracking.executemany(
            """
            INSERT INTO settlements (
                snapshot_id,
                actual_steals,
                actual_blocks,
                settled_at
            ) VALUES (?, ?, ?, ?)
            """,
            settlements,
        )
        tracking.commit()

        raw_probability = 1 - math.exp(-1.1)
        calibrated = stocks_tracking_module._calibrated_special_probability(
            tracking,
            market="steals",
            threshold=1,
            projected_value=1.1,
            raw_probability=raw_probability,
            game_date="2026-05-10",
        )

    assert calibrated == raw_probability


def test_special_threshold_recommendations_fallback_when_history_is_sparse() -> None:
    rows = [
        {"stocks_prob_2_plus": 0.62, "actual_stocks": 2.0},
        {"stocks_prob_2_plus": 0.54, "actual_stocks": 1.0},
        {"stocks_prob_2_plus": 0.47, "actual_stocks": 2.0},
    ]

    result = stocks_tracking_module.fit_special_threshold_recommendations(rows)

    assert result["status"] == "insufficient_history"
    assert result["high_confidence_threshold"] == 0.5
    assert result["watch_threshold"] == 0.4
    assert result["settled_rows"] == 3


def test_special_threshold_recommendations_fit_high_confidence_cutoff() -> None:
    rows: list[dict[str, float]] = []
    rows.extend({"stocks_prob_2_plus": 0.60, "actual_stocks": 2.0} for _ in range(12))
    rows.extend({"stocks_prob_2_plus": 0.55, "actual_stocks": 2.0} for _ in range(10))
    rows.extend({"stocks_prob_2_plus": 0.48, "actual_stocks": 2.0} for _ in range(10))
    rows.extend({"stocks_prob_2_plus": 0.42, "actual_stocks": 1.0} for _ in range(10))

    result = stocks_tracking_module.fit_special_threshold_recommendations(rows)

    assert result["status"] == "fit"
    assert result["settled_rows"] == 42
    assert float(result["high_confidence_threshold"]) >= 0.55
    assert float(result["watch_threshold"]) < float(result["high_confidence_threshold"])
    assert result["evaluated_thresholds"]


def test_special_stocks_stats_exposes_recommended_threshold_fields(monkeypatch, tmp_path) -> None:
    tracking_path = tmp_path / "stocks-tracking.sqlite"
    monkeypatch.setenv("WNBA_STOCKS_TRACKING_DB", str(tracking_path))
    stocks_tracking_module.ensure_tracking_schema()

    with sqlite3.connect(tracking_path) as tracking:
        rows = []
        settlements = []
        for idx in range(42):
            snapshot_id = idx + 1
            probability = 0.6 if idx < 12 else 0.55 if idx < 22 else 0.48 if idx < 32 else 0.42
            actual_stocks = 2 if idx < 32 else 1
            rows.append(
                (
                    snapshot_id,
                    9000 + idx,
                    10000 + idx,
                    f"Threshold Player {idx}",
                    "2026-05-01",
                    f"2026-05-01T0{idx % 10}:00:00+00:00",
                    COMPONENT_MODEL_VERSION,
                    0.8,
                    0.6,
                    1.8,
                    0.55,
                    0.22,
                    0.48,
                    0.18,
                    probability,
                    0.12,
                    "model_only",
                )
            )
            settlements.append((snapshot_id, actual_stocks - 1, 1, "2026-05-02T00:00:00+00:00"))
        tracking.executemany(
            """
            INSERT INTO projection_snapshots (
                id,
                game_id,
                player_id,
                player_name,
                game_date,
                captured_at,
                model_version,
                projected_steals,
                projected_blocks,
                projected_stocks,
                steal_prob_1_plus,
                steal_prob_2_plus,
                block_prob_1_plus,
                block_prob_2_plus,
                stocks_prob_2_plus,
                stocks_prob_3_plus,
                data_quality
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        tracking.executemany(
            """
            INSERT INTO settlements (
                snapshot_id,
                actual_steals,
                actual_blocks,
                settled_at
            ) VALUES (?, ?, ?, ?)
            """,
            settlements,
        )
        tracking.commit()

    result = main_module.special_stocks_stats()

    assert float(result["recommended_candidate_threshold"]) >= 0.55
    assert int(result["recommended_candidate_count"]) in {12, 22}
    assert int(result["recommended_candidate_hits_2_plus"]) == int(result["recommended_candidate_count"])
    assert float(result["recommended_candidate_hit_rate_2_plus"]) == 1.0

def test_feature_snapshot_reuses_shared_player_context_with_runtime_cache(monkeypatch) -> None:
    load_test_history()
    call_count = 0
    original = player_prop_model_module._player_recent_feature_rows

    def counting_rows(conn, player_id, before_game_date, exclude_game_id):
        nonlocal call_count
        call_count += 1
        return original(conn, player_id, before_game_date, exclude_game_id)

    monkeypatch.setattr(player_prop_model_module, "_player_recent_feature_rows", counting_rows)

    with connect() as conn:
        runtime_cache: dict[str, dict[tuple, object]] = {}
        first = feature_snapshot(conn, 1001, "points", 2010, runtime_cache=runtime_cache)
        second = feature_snapshot(conn, 1001, "rebounds", 2010, runtime_cache=runtime_cache)

    assert first.component_projection >= 0.0
    assert second.component_projection >= 0.0
    assert call_count == 1


def test_repair_current_slate_props_falls_back_to_scheduled_games(monkeypatch) -> None:
    def fake_sync(conn, **kwargs):
        assert kwargs["game_ids"] == [9910, 9920]
        return SyncPropLinesResult(
            synced_props=4,
            changed_props=1,
            changed_prop_line_ids=[501],
            touched_game_ids=[9910, 9920],
        )

    monkeypatch.setattr(main_module, "_active_slate_game_ids", lambda conn: [])
    monkeypatch.setattr(main_module, "_scheduled_game_ids", lambda conn: [9910, 9920])
    monkeypatch.setattr(main_module, "sync_prop_lines_from_sportsbook", fake_sync)
    monkeypatch.setattr(
        main_module,
        "rebuild_predictions_live",
        lambda conn, game_ids=None, prop_line_ids=None, chunk_size=20, progress_callback=None: projections_module.LiveRebuildResult(
            projections=[],
            attempted=1,
            written=1,
            skipped=0,
            errors=[],
        ),
    )
    monkeypatch.setattr(
        main_module,
        "rebuild_game_predictions_live",
        lambda conn, game_ids=None, progress_callback=None: {"attempted": len(game_ids or []), "written": len(game_ids or [])},
    )
    monkeypatch.setattr(main_module, "_snapshot_watchlist", lambda conn, slate_date: {"tracked": 0})
    monkeypatch.setattr(main_module, "_snapshot_current_dfs_first_half", lambda conn, game_ids=None: {"inserted": 1, "updated": 1, "skipped": 0})

    with connect() as conn:
        result = main_module._repair_current_slate_props(conn)

    assert result["scope"] == "scheduled"
    assert result["target_game_ids"] == [9910, 9920]
    assert result["scanned_props"] == 4
    assert result["synced_props"] == 1
    assert result["attempted_predictions"] == 1
    assert result["rebuilt_predictions"] == 1
    assert result["skipped_predictions"] == 0
    assert result["attempted_game_predictions"] == 2
    assert result["rebuilt_game_predictions"] == 2
    assert result["dfs_snapshot"] == {"inserted": 1, "updated": 1, "skipped": 0}


def test_scheduled_game_ids_limits_fallback_to_next_scheduled_slate(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_local_today_iso", lambda: "2026-07-14")

    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)
            """,
            [
                (9910, "2026-07-12", "2026-07-12T17:00:00Z", 10, 3, -4.5, 160.5),
                (9920, "2026-07-14", "2026-07-14T17:00:00Z", 6, 8, -1.5, 162.5),
                (9930, "2026-07-14", "2026-07-14T20:00:00Z", 4, 11, -2.5, 158.5),
                (9940, "2026-07-16", "2026-07-16T18:00:00Z", 9, 12, -3.5, 159.5),
            ],
        )

        scheduled_game_ids = main_module._scheduled_game_ids(conn)

    assert scheduled_game_ids == [9920, 9930]


def test_scheduled_game_ids_for_teams_limits_to_next_team_slate(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_local_today_iso", lambda: "2026-07-14")
    monkeypatch.setattr(main_module, "_active_slate_game_ids", lambda conn: [])

    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)
            """,
            [
                (9950, "2026-07-14", "2026-07-14T17:00:00Z", 6, 8, -1.5, 162.5),
                (9960, "2026-07-14", "2026-07-14T20:00:00Z", 4, 11, -2.5, 158.5),
                (9970, "2026-07-16", "2026-07-16T18:00:00Z", 6, 9, -3.5, 159.5),
                (9980, "2026-07-18", "2026-07-18T18:00:00Z", 8, 10, -3.5, 159.5),
            ],
        )

        scheduled_game_ids = main_module._scheduled_game_ids_for_teams(conn, [6, 8])

    assert scheduled_game_ids == [9950]


def test_scheduled_game_ids_for_teams_prefers_filtered_active_slate(monkeypatch) -> None:
    monkeypatch.setattr(main_module, "_active_slate_game_ids", lambda conn: [9990, 9991, 9992])

    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)
            """,
            [
                (9990, "2026-07-16", "2026-07-16T17:00:00Z", 6, 8, -1.5, 162.5),
                (9991, "2026-07-16", "2026-07-16T20:00:00Z", 4, 11, -2.5, 158.5),
                (9992, "2026-07-16", "2026-07-16T22:00:00Z", 9, 10, -3.5, 159.5),
                (9993, "2026-07-18", "2026-07-18T18:00:00Z", 6, 10, -3.5, 159.5),
            ],
        )

        scheduled_game_ids = main_module._scheduled_game_ids_for_teams(conn, [6, 8])

    assert scheduled_game_ids == [9990]


def test_settle_auto_repairs_missing_current_slate_predictions(monkeypatch) -> None:
    repair_calls = 0

    def fake_repair(conn, progress_callback=None):
        nonlocal repair_calls
        repair_calls += 1
        return {
            "scope": "current_slate",
            "target_game_ids": [9910],
            "scanned_props": 4,
            "synced_props": 4,
            "rebuilt_predictions": 4,
        }

    monkeypatch.setattr(main_module, "settle_completed_props", lambda conn, selected_date=None, selected_dates=None: {"settled": 1})
    monkeypatch.setattr(main_module, "settle_completed_game_predictions", lambda conn, selected_date=None, selected_dates=None: {"settled": 1})
    monkeypatch.setattr(main_module, "_sync_gem_snapshot_settlements", lambda conn: {"settled": 0})
    monkeypatch.setattr(main_module, "_sync_watchlist_snapshot_settlements", lambda conn: {"settled": 0})
    monkeypatch.setattr(main_module, "_repair_current_slate_props", fake_repair)
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)
    monkeypatch.setattr(
        main_module,
        "_publish_post_mutation_read_payloads",
        lambda conn, matchup_game_ids=None, full_matchup_refresh=True: {"current_value_board.json": 1},
    )
    monkeypatch.setattr(main_module, "_repair_current_slate_target", lambda conn: ("current_slate", [9910]))

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)
            """,
            (9910, "2026-07-04", datetime.now(timezone.utc).isoformat(), 10, 3, -3.5, 162.5),
        )
        conn.commit()

    result = main_module.settle_props()

    assert repair_calls == 1
    assert result["scheduled_repair"] == {
        "scope": "current_slate",
        "target_game_ids": [9910],
        "scanned_props": 4,
        "synced_props": 4,
        "rebuilt_predictions": 4,
        "repair_reason": "scheduled_predictions_missing_after_settlement",
        "missing_game_ids": [9910],
    }


def test_settle_skips_auto_repair_when_current_slate_predictions_exist(monkeypatch) -> None:
    repair_calls = 0

    def fake_repair(conn, progress_callback=None):
        nonlocal repair_calls
        repair_calls += 1
        return {"scope": "current_slate", "target_game_ids": [9910]}

    monkeypatch.setattr(main_module, "settle_completed_props", lambda conn, selected_date=None, selected_dates=None: {"settled": 1})
    monkeypatch.setattr(main_module, "settle_completed_game_predictions", lambda conn, selected_date=None, selected_dates=None: {"settled": 1})
    monkeypatch.setattr(main_module, "_sync_gem_snapshot_settlements", lambda conn: {"settled": 0})
    monkeypatch.setattr(main_module, "_sync_watchlist_snapshot_settlements", lambda conn: {"settled": 0})
    monkeypatch.setattr(main_module, "_repair_current_slate_props", fake_repair)
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)
    monkeypatch.setattr(
        main_module,
        "_publish_post_mutation_read_payloads",
        lambda conn, matchup_game_ids=None, full_matchup_refresh=True: {"current_value_board.json": 1},
    )
    monkeypatch.setattr(main_module, "_repair_current_slate_target", lambda conn: ("current_slate", [9910]))

    with connect() as conn:
        now = datetime.now(timezone.utc).isoformat()
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (99101, "Current Slate Player", 10, "G", "starter"),
        )
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)
            """,
            (9910, "2026-07-04", now, 10, 3, -3.5, 162.5),
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (991011, 9910, 99101, "DraftKings", "points", 15.5, -110, -110, now),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (991012, 991011, MODEL_VERSION, now, 17.0, "over", 0.58, 0.52, 0.06, 0.03, "medium", "ready"),
        )
        conn.commit()

    result = main_module.settle_props()

    assert repair_calls == 0
    assert result["scheduled_repair"] is None


def test_repair_current_slate_endpoint_queues_background_job(monkeypatch) -> None:
    connect_calls = 0
    thread_starts = 0

    class DummyConn:
        def __enter__(self):
            nonlocal connect_calls
            connect_calls += 1
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class ImmediateThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")

        def start(self):
            nonlocal thread_starts
            thread_starts += 1
            if self.target is not None:
                self.target()

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(main_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(main_module, "_create_prop_sync_job_record", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        main_module,
        "_run_current_slate_repair_job",
        lambda: (
            main_module._set_prop_sync_progress(
                stage="publishing_payloads",
                stage_index=3,
                stage_total=3,
                current=1,
                total=1,
                message="Current slate repair finished.",
            )
            or {
                "scope": "current_slate",
                "target_game_ids": [9910],
                "changed_prop_line_ids": [501, 502],
                "rebuilt_predictions": 2,
                "published_payloads": {"watchlist.json": 1},
            }
        ),
    )
    monkeypatch.setattr(main_module, "_publish_post_mutation_read_payloads", lambda conn, **kwargs: {"watchlist.json": 1})
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "running", False)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "started_at", None)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "finished_at", None)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "last_error", None)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "last_result", None)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "scope", None)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "target_game_ids", [])
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "stage", None)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "stage_index", 0)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "stage_total", 1)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "current", 0)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "total", 0)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "percent", 0.0)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "message", None)
    monkeypatch.setitem(main_module._PROP_SYNC_STATE, "updated_at", None)

    result = main_module.repair_current_slate_props()

    assert connect_calls == 0
    assert thread_starts == 1
    assert result["status"] == "queued"
    assert result["scope"] == "current_slate"
    assert result["target_game_ids"] == []
    assert main_module._PROP_SYNC_STATE["last_result"] == {
        "status": "completed",
        "scope": "current_slate",
        "target_game_ids": [9910],
        "changed_prop_line_ids": [501, 502],
        "rebuilt_predictions": 2,
        "published_payloads": {"watchlist.json": 1},
    }
    assert main_module._PROP_SYNC_STATE["target_game_ids"] == [9910]
    assert main_module._PROP_SYNC_STATE["stage"] == "publishing_payloads"
    assert main_module._PROP_SYNC_STATE["percent"] == 1.0


def test_start_prop_sync_if_needed_tracks_progress(monkeypatch) -> None:
    thread_starts = 0

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class ImmediateThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")

        def start(self):
            nonlocal thread_starts
            thread_starts += 1
            if self.target is not None:
                self.target()

    def fake_sync(conn, **kwargs):
        callback = kwargs.get("progress_callback")
        if callback is not None:
            callback(3, 6, "Matched 3 props.")
        return SyncPropLinesResult(
            synced_props=6,
            changed_props=3,
            changed_prop_line_ids=[901, 902, 903],
            touched_game_ids=[9910],
        )

    def fake_rebuild(conn, game_ids=None, prop_line_ids=None, chunk_size=20, progress_callback=None):
        assert game_ids is None
        assert prop_line_ids == [901, 902, 903]
        if progress_callback is not None:
            progress_callback(3, 3, "Built 3 of 3 projections.")
        return projections_module.LiveRebuildResult(
            projections=[],
            attempted=3,
            written=3,
            skipped=0,
            errors=[],
        )

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(main_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(main_module, "sync_prop_lines_from_sportsbook", fake_sync)
    monkeypatch.setattr(main_module, "rebuild_predictions_live", fake_rebuild)
    monkeypatch.setattr(main_module, "_snapshot_current_dfs_first_half", lambda conn, game_ids=None: {"inserted": 3, "updated": 0, "skipped": 0})
    monkeypatch.setattr(main_module, "_publish_post_mutation_read_payloads", lambda conn, **kwargs: {"watchlist.json": 1})
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)

    started = main_module._start_prop_sync_if_needed("odds_import")

    assert started is True
    assert thread_starts == 1
    assert main_module._PROP_SYNC_STATE["target_game_ids"] == [9910]
    assert main_module._PROP_SYNC_STATE["stage"] == "publishing_payloads"
    assert main_module._PROP_SYNC_STATE["percent"] == 1.0
    assert main_module._PROP_SYNC_STATE["last_result"] == {
        "source": "odds_import",
        "synced_props": 6,
        "changed_props": 3,
        "rebuilt_predictions": 3,
        "attempted_predictions": 3,
        "skipped_predictions": 0,
        "dfs_snapshot": {"inserted": 3, "updated": 0, "skipped": 0},
        "target_game_ids": [9910],
        "published_payloads": {"watchlist.json": 1},
    }


def test_start_prop_sync_if_needed_retries_sqlite_lock(monkeypatch) -> None:
    thread_starts = 0
    attempts = {"count": 0}
    job_events: list[tuple[str, str, str, dict | None]] = []
    finished: list[tuple[str, str | None]] = []

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    class ImmediateThread:
        def __init__(self, *args, **kwargs):
            self.target = kwargs.get("target")

        def start(self):
            nonlocal thread_starts
            thread_starts += 1
            if self.target is not None:
                self.target()

    def fake_pipeline(*args, **kwargs):
        attempts["count"] += 1
        if attempts["count"] == 1:
            raise sqlite3.OperationalError("database is locked")
        return SimpleNamespace(
            target_game_ids=[9910],
            scanned_props=6,
            synced_props=3,
            rebuilt_predictions=3,
            attempted_predictions=3,
            skipped_predictions=0,
            dfs_snapshot={"inserted": 3, "updated": 0, "skipped": 0},
        )

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(main_module.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(main_module, "run_prop_sync_pipeline", fake_pipeline)
    monkeypatch.setattr(main_module, "run_post_pipeline_steps", lambda **kwargs: SimpleNamespace(published_payloads={"watchlist.json": 1}))
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)
    monkeypatch.setattr(main_module, "_begin_prop_sync_job", lambda source: "job-1")
    monkeypatch.setattr(main_module, "_create_job_run", lambda *args, **kwargs: 91)
    monkeypatch.setattr(
        main_module,
        "_append_job_run_event",
        lambda job_run_id, event_type, message, level="info", details=None: job_events.append(
            (str(job_run_id), event_type, message, details)
        ),
    )
    monkeypatch.setattr(
        main_module,
        "_finish_job_run",
        lambda job_run_id, status, result=None, error_text=None: finished.append((status, error_text)),
    )
    monkeypatch.setattr(main_module.time, "sleep", lambda _seconds: None)

    started = main_module._start_prop_sync_if_needed("covers_import")

    assert started is True
    assert thread_starts == 1
    assert attempts["count"] == 2
    assert any(event_type == "job.retry" for _, event_type, _, _ in job_events)
    assert finished[-1] == ("completed", None)
    assert main_module._PROP_SYNC_STATE["last_result"] == {
        "source": "covers_import",
        "synced_props": 6,
        "changed_props": 3,
        "rebuilt_predictions": 3,
        "attempted_predictions": 3,
        "skipped_predictions": 0,
        "dfs_snapshot": {"inserted": 3, "updated": 0, "skipped": 0},
        "target_game_ids": [9910],
        "published_payloads": {"watchlist.json": 1},
    }


def test_run_legacy_recalculate_job_tracks_repair_substages_separately(monkeypatch) -> None:
    progress_updates: list[tuple[str | None, int | None, int | None, int | None, str | None]] = []

    def fake_repair(conn, progress_callback=None):
        if progress_callback is not None:
            progress_callback("syncing_props", 40, 176, "Matched props.")
            progress_callback("rebuilding_predictions", 20, 146, "Built 20 projections.")
            progress_callback("rebuilding_games", 3, 6, "Built 3 game projections.")
        return {"rebuilt_predictions": 20}

    monkeypatch.setattr(main_module, "_repair_current_slate_props", fake_repair)
    monkeypatch.setattr(main_module, "settle_completed_props", lambda conn: {"settled": 4})
    monkeypatch.setattr(main_module, "settle_stocks", lambda conn: {"settled": 1})
    monkeypatch.setattr(main_module, "settle_dfs_first_half_projection_snapshots", lambda conn: {"settled": 3, "skipped": 0})
    monkeypatch.setattr(main_module, "settle_completed_game_predictions", lambda conn: {"settled": 2})
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)
    monkeypatch.setattr(main_module, "_publish_current_read_payloads", lambda conn: None)
    monkeypatch.setattr(
        main_module,
        "_set_prop_sync_progress",
        lambda *args, **kwargs: progress_updates.append(
            (
                kwargs.get("stage"),
                kwargs.get("stage_index"),
                kwargs.get("stage_total"),
                kwargs.get("current"),
                kwargs.get("message"),
            )
        ),
    )

    result = main_module._run_legacy_recalculate_job()

    assert result == {
        "predictions": 20,
        "settled": 4,
        "special_settled": 1,
        "dfs_settled": 3,
        "game_settled": 2,
    }
    assert ("syncing_props", 1, 6, 40, "Matched props.") in progress_updates
    assert ("rebuilding_predictions", 2, 6, 20, "Built 20 projections.") in progress_updates
    assert ("rebuilding_games", 3, 6, 3, "Built 3 game projections.") in progress_updates
    assert ("settling_props", 4, 6, 0, "Settling completed player props.") in progress_updates
    assert ("settling_games", 5, 6, 0, "Settling completed game predictions.") in progress_updates
    assert ("publishing_payloads", 6, 6, 1, "Legacy recalculate finished.") in progress_updates


def test_run_current_slate_repair_job_reports_progress_without_worker_connection(monkeypatch) -> None:
    progress_updates: list[dict[str, object]] = []

    def fake_repair(conn, target_game_ids=None, progress_callback=None):
        if progress_callback is not None:
            progress_callback("syncing_props", 1, 1, "Skipped sportsbook sync for injury update.")
            progress_callback("rebuilding_predictions", 20, 146, "Built 20 projections.")
            progress_callback("rebuilding_games", 1, 3, "Built 1 game prediction.")
        return {
            "scope": "injury_update",
            "target_game_ids": [9910],
            "rebuilt_predictions": 20,
            "attempted_game_predictions": 3,
            "rebuilt_game_predictions": 1,
        }

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(main_module, "_repair_current_slate_props", fake_repair)
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)
    monkeypatch.setattr(
        main_module,
        "run_post_pipeline_steps",
        lambda **kwargs: SimpleNamespace(
            recent_finals_settlement={"settled_games": 1},
            published_payloads={"watchlist.json": 1},
        ),
    )
    monkeypatch.setattr(
        main_module,
        "_set_prop_sync_progress",
        lambda *args, **kwargs: progress_updates.append(dict(kwargs)),
    )

    result = main_module._run_current_slate_repair_job([9910])

    assert result["target_game_ids"] == [9910]
    assert result["published_payloads"] == {"watchlist.json": 1}
    assert any(update.get("stage") == "rebuilding_predictions" for update in progress_updates)
    assert all("conn" not in update for update in progress_updates)


def test_row_to_prop_sync_state_marks_stale_jobs() -> None:
    stale_started_at = "2026-07-01T00:00:00+00:00"
    stale_updated_at = "2026-07-01T00:05:00+00:00"

    row = {
        "id": 11,
        "status": "queued",
        "started_at": stale_started_at,
        "finished_at": None,
        "last_error": None,
        "last_result_json": None,
        "scope": "legacy_recalculate",
        "target_game_ids_json": "[]",
        "stage": "queued",
        "stage_index": 0,
        "stage_total": 1,
        "current_count": 0,
        "total_count": 0,
        "percent": 0.0,
        "message": "Queued for background processing.",
        "updated_at": stale_updated_at,
    }

    state = main_module._row_to_prop_sync_state(row)

    assert state is not None
    assert state["running"] is False
    assert state["status"] == "stale"
    assert state["finished_at"] == stale_updated_at
    assert "stale" in str(state["last_error"]).lower()


def test_settle_recent_completed_games_repairs_recent_final_props(monkeypatch) -> None:
    load_test_history()
    monkeypatch.setattr(main_module, "RECENT_FINALS_SETTLEMENT_LOOKBACK_DAYS", 1000)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (9901, 100, 1001, "DraftKings", "points", 20.5, -110, -110, "2026-05-08T18:00:00+00:00"),
        )
        conn.execute(
            """
            INSERT INTO game_predictions (
                game_id, model_version, prediction_time, home_projected_points, away_projected_points,
                projected_margin, projected_total, winner_pick, ats_pick, ats_edge, total_pick, total_edge,
                confidence, reason, spread_home, game_total, home_rest_days, away_rest_days
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                100,
                "component-game-v2",
                "2026-05-08T18:00:00+00:00",
                80.0,
                75.0,
                5.0,
                155.0,
                "LV",
                "LV -5.5",
                1.0,
                "Under",
                1.0,
                "medium",
                "test",
                -5.5,
                155.0,
                2,
                2,
            ),
        )
        result = main_module._settle_recent_completed_games(conn)
        settled_prop = conn.execute("SELECT * FROM settled_props WHERE prop_line_id = 9901").fetchone()
        settled_game = conn.execute("SELECT COUNT(*) FROM settled_game_predictions WHERE game_id = 100").fetchone()[0]

    assert result["selected_dates"]
    assert result["prop_settlements"]["settled"] == 1
    assert result["game_settlements"]["settled"] == 1
    assert settled_prop is not None
    assert settled_game == 1


def test_matchups_payload_survives_rotowire_failure(monkeypatch) -> None:
    load_test_history()

    monkeypatch.setattr(main_module, "import_rotowire_lineups", lambda conn, force_refresh=False: (_ for _ in ()).throw(RuntimeError("rotowire unavailable")))
    monkeypatch.setattr(main_module, "_covers_records_by_game", lambda conn: {})
    monkeypatch.setattr(main_module, "_covers_market_odds_by_game", lambda: {})
    monkeypatch.setattr(main_module, "_team_last_10_summary", lambda conn, team_id: {})
    monkeypatch.setattr(main_module, "_is_today_active_game_time", lambda start_time: True)
    monkeypatch.setattr(main_module, "_value_board_payload_for_games", lambda conn, game_ids, include_filtered_only=False: [])
    monkeypatch.setattr(main_module, "_sportsbook_props_for_games", lambda conn, game_ids: [])
    monkeypatch.setattr(main_module, "_line_discrepancies_for_games", lambda conn, game_ids: [])
    monkeypatch.setattr(
        main_module,
        "project_game",
        lambda conn, game, **kwargs: {
            "home_projected_points": 80.0,
            "away_projected_points": 75.0,
            "projected_margin": 5.0,
            "projected_total": 155.0,
            "winner_pick": "NY",
            "ats_pick": "NY",
            "ats_edge": 0.04,
            "total_pick": "Under",
            "total_edge": 0.03,
            "game_confidence": "medium",
            "game_reason": "test",
        },
    )

    with connect() as conn:
        payload = main_module._matchups_payload(conn)

    assert payload
    assert payload[0]["props"] == []
    assert payload[0]["injury_source"] == "unavailable"
    assert payload[0]["injury_captured_at"] is None
    assert payload[0]["injury_from_cache"] is False


def test_matchups_payload_does_not_write_game_predictions(monkeypatch) -> None:
    load_test_history()

    monkeypatch.setattr(main_module, "import_rotowire_lineups", lambda conn, force_refresh=False: {"source": "cache", "captured_at": None, "from_cache": True})
    monkeypatch.setattr(main_module, "_covers_records_by_game", lambda conn: {})
    monkeypatch.setattr(main_module, "_covers_market_odds_by_game", lambda: {})
    monkeypatch.setattr(main_module, "_team_last_10_summary", lambda conn, team_id: {})
    monkeypatch.setattr(main_module, "_is_today_active_game_time", lambda start_time: True)
    monkeypatch.setattr(main_module, "_value_board_payload_for_games", lambda conn, game_ids, include_filtered_only=False: [])
    monkeypatch.setattr(main_module, "_sportsbook_props_for_games", lambda conn, game_ids: [])
    monkeypatch.setattr(main_module, "_line_discrepancies_for_games", lambda conn, game_ids: [])
    monkeypatch.setattr(
        main_module,
        "project_game",
        lambda conn, game, **kwargs: {
            "home_projected_points": 80.0,
            "away_projected_points": 75.0,
            "projected_margin": 5.0,
            "projected_total": 155.0,
            "winner_pick": "NY",
            "ats_pick": "NY",
            "ats_edge": 0.04,
            "total_pick": "Under",
            "total_edge": 0.03,
            "game_confidence": "medium",
            "game_reason": "test",
        },
    )

    with connect() as conn:
        conn.execute("DELETE FROM game_predictions")
        conn.commit()
        payload = main_module._matchups_payload(conn)
        saved = conn.execute("SELECT COUNT(*) AS count FROM game_predictions").fetchone()

    assert payload
    assert payload[0]["game_prediction_id"] is None
    assert int(saved["count"]) == 0


def test_matchups_payload_prefers_game_markets_over_covers_override(monkeypatch) -> None:
    load_test_history()

    monkeypatch.setattr(main_module, "import_rotowire_lineups", lambda conn, force_refresh=False: {"source": "cache", "captured_at": None, "from_cache": True})
    monkeypatch.setattr(main_module, "_covers_records_by_game", lambda conn: {})
    monkeypatch.setattr(
        main_module,
        "_covers_market_odds_by_game",
        lambda: {
            2010: {
                "spread_home": 4.5,
                "game_total": 149.5,
                "home_moneyline": 140.0,
                "away_moneyline": -160.0,
                "home_spread_price": -102.0,
                "away_spread_price": -118.0,
                "over_price": -108.0,
                "under_price": -112.0,
                "spread_market": {"away_line": -4.5, "away_price": -118.0, "home_line": 4.5, "home_price": -102.0},
                "total_market": {"over_line": 149.5, "over_price": -108.0, "under_line": 149.5, "under_price": -112.0},
                "moneyline_market": {"away_price": -160.0, "home_price": 140.0},
            }
        },
    )
    monkeypatch.setattr(main_module, "_team_last_10_summary", lambda conn, team_id: {})
    monkeypatch.setattr(main_module, "_is_today_active_game_time", lambda start_time: True)
    monkeypatch.setattr(main_module, "_value_board_payload_for_games", lambda conn, game_ids, include_filtered_only=False: [])
    monkeypatch.setattr(main_module, "_sportsbook_props_for_games", lambda conn, game_ids: [])
    monkeypatch.setattr(main_module, "_line_discrepancies_for_games", lambda conn, game_ids: [])
    monkeypatch.setattr(
        main_module,
        "project_game",
        lambda conn, game, **kwargs: {
            "home_projected_points": 80.0,
            "away_projected_points": 75.0,
            "projected_margin": 5.0,
            "projected_total": 155.0,
            "winner_pick": "NY",
            "ats_pick": "NY",
            "ats_edge": 0.04,
            "total_pick": "Under",
            "total_edge": 0.03,
            "game_confidence": "medium",
            "game_reason": "test",
        },
    )

    with connect() as conn:
        conn.execute(
            """
            UPDATE games
            SET spread_home = ?, game_total = ?, home_moneyline = ?, away_moneyline = ?,
                home_spread_price = ?, away_spread_price = ?, over_price = ?, under_price = ?
            WHERE id = 2010
            """,
            (-6.5, 166.5, -250.0, 215.0, -110.0, -110.0, -105.0, -115.0),
        )
        conn.commit()
        payload = main_module._matchups_payload(conn)

    assert payload
    assert payload[0]["spread_home"] == -6.5
    assert payload[0]["game_total"] == 166.5
    assert payload[0]["home_moneyline"] == -250.0
    assert payload[0]["away_moneyline"] == 215.0
    assert payload[0]["spread_market"]["home_line"] == -6.5
    assert payload[0]["total_market"]["over_line"] == 166.5
    assert payload[0]["moneyline_market"]["home_price"] == -250.0


def test_matchups_payload_uses_local_calendar_for_rest_days(monkeypatch) -> None:
    with connect() as conn:
        atl = conn.execute("SELECT id FROM teams WHERE abbreviation = 'ATL'").fetchone()["id"]
        sea = conn.execute("SELECT id FROM teams WHERE abbreviation = 'SEA'").fetchone()["id"]
        gs = conn.execute("SELECT id FROM teams WHERE abbreviation = 'GS'").fetchone()["id"]
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, ?, ?)
            """,
            (910001, "2026-06-26", "2026-06-27T02:00Z", gs, atl, 4.5, 165.5),
        )
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)
            """,
            (910002, "2026-06-28", "2026-06-28T01:00:00+00:00", sea, atl, 8.5, 166.5),
        )
        conn.commit()

        monkeypatch.setattr(main_module, "import_rotowire_lineups", lambda conn, force_refresh=False: {"source": "cache", "captured_at": None, "from_cache": True})
        monkeypatch.setattr(main_module, "_covers_records_by_game", lambda conn: {})
        monkeypatch.setattr(main_module, "_covers_market_odds_by_game", lambda: {})
        monkeypatch.setattr(main_module, "_team_last_10_summary", lambda conn, team_id: {})
        monkeypatch.setattr(main_module, "_value_board_payload_for_games", lambda conn, game_ids, include_filtered_only=False: [])
        monkeypatch.setattr(main_module, "_sportsbook_props_for_games", lambda conn, game_ids: [])
        monkeypatch.setattr(main_module, "_line_discrepancies_for_games", lambda conn, game_ids: [])
        monkeypatch.setattr(main_module, "_is_today_active_game_time", lambda start_time: "2026-06-28T01:00:00+00:00" in str(start_time))
        monkeypatch.setattr(
            main_module,
            "project_game",
            lambda conn, game, **kwargs: {
                "home_projected_points": 81.0,
                "away_projected_points": 74.0,
                "projected_margin": 7.0,
                "projected_total": 155.0,
                "winner_pick": "SEA",
                "ats_pick": "SEA",
                "ats_edge": 0.05,
                "total_pick": "Under",
                "total_edge": 0.02,
                "game_confidence": "medium",
                "game_reason": "test",
            },
        )

        payload = main_module._matchups_payload(conn)

    matchup = next(item for item in payload if item["id"] == 910002)
    assert matchup["away_team"] == "ATL"
    assert matchup["away_rest_days"] == 2


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


def test_game_projection_applies_injury_penalty_for_key_starter() -> None:
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
        baseline = project_game(conn, game)
        conn.execute(
            "INSERT INTO injuries (player_id, status, note, captured_at) VALUES (?, ?, ?, ?)",
            (1001, "out", "test", datetime.now(timezone.utc).isoformat()),
        )
        adjusted = project_game(conn, game)

    assert adjusted["home_projected_points"] < baseline["home_projected_points"]
    assert adjusted["projected_margin"] < baseline["projected_margin"]
    assert "injury adjustment" in adjusted["game_reason"].lower()


def test_increased_role_flag_turns_on_for_teammate_replacement() -> None:
    assert main_module._has_increased_role(
        reason="injury available (avail 1.00, team usage 1.08, min +1.2); manual adjustment applied."
    ) is True


def test_increased_role_flag_stays_off_for_mild_bump() -> None:
    assert main_module._has_increased_role(
        reason="injury available (avail 1.00, team usage 1.03, min +0.5); manual adjustment applied."
    ) is False


def test_increased_role_flag_stays_off_for_neutral_injury_context() -> None:
    assert main_module._has_increased_role(
        reason="injury available (avail 1.00, team usage 1.00, min +0.0); no manual adjustment."
    ) is False


def test_roster_out_since_tracks_current_unavailable_streak() -> None:
    load_test_history()
    with connect() as conn:
        conn.executemany(
            "INSERT INTO injuries (player_id, status, note, captured_at) VALUES (?, ?, ?, ?)",
            [
                (1001, "out", "older absence", "2026-05-01T12:00:00Z"),
                (1001, "available", "cleared", "2026-05-03T12:00:00Z"),
                (1001, "out", "new absence", "2026-05-06T12:00:00Z"),
                (1001, "inactive", "still out", "2026-05-07T12:00:00Z"),
            ],
        )
        out_since = main_module._roster_out_since(conn, 1001, "OUT")

    assert out_since == "2026-05-06T12:00:00Z"


def test_roster_out_since_map_tracks_current_unavailable_streaks() -> None:
    load_test_history()
    with connect() as conn:
        conn.executemany(
            "INSERT INTO injuries (player_id, status, note, captured_at) VALUES (?, ?, ?, ?)",
            [
                (1001, "out", "older absence", "2026-05-01T12:00:00Z"),
                (1001, "available", "cleared", "2026-05-03T12:00:00Z"),
                (1001, "out", "new absence", "2026-05-06T12:00:00Z"),
                (1001, "inactive", "still out", "2026-05-07T12:00:00Z"),
                (1002, "out", "first absence", "2026-05-02T12:00:00Z"),
                (1002, "inactive", "still out", "2026-05-04T12:00:00Z"),
                (1003, "available", "healthy", "2026-05-05T12:00:00Z"),
            ],
        )
        out_since = main_module._roster_out_since_map(conn, {1001, 1002, 1003})

    assert out_since == {
        1001: "2026-05-06T12:00:00Z",
        1002: "2026-05-02T12:00:00Z",
        1003: None,
    }


def test_game_projection_calibrates_low_totals_upward() -> None:
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
        baseline = project_game(conn, game)
        conn.execute("DELETE FROM game_predictions")
        conn.execute("DELETE FROM settled_game_predictions")
        captured_at = datetime.now(timezone.utc).isoformat()
        for prediction_id in range(1, 13):
            conn.execute(
                """
                INSERT INTO game_predictions (
                    id, game_id, model_version, prediction_time, projected_total,
                    winner_pick, ats_pick, total_pick, confidence, reason
                ) VALUES (?, ?, ?, ?, 150.0, 'NY', 'NY -5.5', 'Under', 'medium', 'test calibration')
                """,
                (prediction_id, 2010, f"test-calibration-{prediction_id}", captured_at),
            )
            conn.execute(
                """
                INSERT INTO settled_game_predictions (
                    game_prediction_id, game_id, home_score, away_score, actual_winner,
                    actual_margin, actual_total, actual_ats_pick, actual_total_result,
                    winner_correct, ats_correct, total_correct, settled_at
                ) VALUES (?, 2010, 82, 80, 'NY', 2.0, 162.0, 'NY', 'Under', 1, 1, 1, ?)
                """,
                (prediction_id, captured_at),
            )
        calibrated = project_game(conn, game)

    assert calibrated["projected_total"] > baseline["projected_total"]
    assert calibrated["total_edge"] > baseline["total_edge"]


def test_game_projection_applies_margin_residual_adjustment() -> None:
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
        baseline = project_game(conn, game)
        conn.execute("DELETE FROM game_predictions")
        conn.execute("DELETE FROM settled_game_predictions")
        captured_at = datetime.now(timezone.utc).isoformat()
        for prediction_id in range(200, 220):
            conn.execute(
                """
                INSERT INTO game_predictions (
                    id, game_id, model_version, prediction_time, projected_margin,
                    winner_pick, ats_pick, total_pick, confidence, reason,
                    spread_home, home_rest_days, away_rest_days
                ) VALUES (?, ?, ?, ?, 2.0, 'NY', 'CON +5.5', 'N/A', 'medium', 'test margin residual', -5.5, 2, 2)
                """,
                (prediction_id, 2010, f"test-margin-{prediction_id}", captured_at),
            )
            conn.execute(
                """
                INSERT INTO settled_game_predictions (
                    game_prediction_id, game_id, home_score, away_score, actual_winner,
                    actual_margin, actual_total, actual_ats_pick, actual_total_result,
                    winner_correct, ats_correct, total_correct, settled_at
                ) VALUES (?, 2010, 88, 79, 'NY', 9.0, 167.0, 'NY', 'Over', 1, 1, 1, ?)
                """,
                (prediction_id, captured_at),
            )
        adjusted = project_game(conn, game)

    assert adjusted["projected_margin"] > baseline["projected_margin"]
    assert adjusted["ats_edge"] > baseline["ats_edge"]
    assert "residual blend" in adjusted["game_reason"].lower()


def test_game_projection_applies_total_residual_adjustment() -> None:
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
        baseline = project_game(conn, game)
        conn.execute("DELETE FROM game_predictions")
        conn.execute("DELETE FROM settled_game_predictions")
        captured_at = datetime.now(timezone.utc).isoformat()
        for prediction_id in range(300, 320):
            conn.execute(
                """
                INSERT INTO game_predictions (
                    id, game_id, model_version, prediction_time, projected_total,
                    winner_pick, ats_pick, total_pick, confidence, reason,
                    game_total, home_rest_days, away_rest_days
                ) VALUES (?, ?, ?, ?, 154.0, 'NY', 'N/A', 'Under', 'medium', 'test total residual', 158.0, 2, 2)
                """,
                (prediction_id, 2010, f"test-total-{prediction_id}", captured_at),
            )
            conn.execute(
                """
                INSERT INTO settled_game_predictions (
                    game_prediction_id, game_id, home_score, away_score, actual_winner,
                    actual_margin, actual_total, actual_ats_pick, actual_total_result,
                    winner_correct, ats_correct, total_correct, settled_at
                ) VALUES (?, 2010, 85, 81, 'NY', 4.0, 166.0, 'NY', 'Over', 1, 1, 1, ?)
                """,
                (prediction_id, captured_at),
            )
        adjusted = project_game(conn, game)

    assert adjusted["projected_total"] > baseline["projected_total"]
    assert adjusted["total_edge"] > baseline["total_edge"]
    assert "residual blend" in adjusted["game_reason"].lower()


def test_game_projection_total_pick_matches_displayed_projection(monkeypatch) -> None:
    load_test_history()

    def fake_project_team_points(*args, **kwargs) -> float:
        return 85.0 if kwargs.get("is_home") else 80.0

    def fake_calibrate_total_projection(conn, projected_total: float, game_total: float | None) -> float:
        return projected_total + 5.0

    def fake_apply_game_total_residual(conn, projected_total: float, game_total: float, rest_days_home: int, rest_days_away: int):
        return projected_total + 7.0, "test total residual"

    def fake_predict_market_total_edge(*args, **kwargs):
        return None

    monkeypatch.setattr("backend.app.game_predictions._project_team_points", fake_project_team_points)
    monkeypatch.setattr("backend.app.game_predictions._calibrate_total_projection", fake_calibrate_total_projection)
    monkeypatch.setattr("backend.app.game_predictions._apply_game_total_residual", fake_apply_game_total_residual)
    monkeypatch.setattr("backend.app.game_predictions._predict_market_total_edge", fake_predict_market_total_edge)

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
        result = project_game(conn, game)

    assert result["projected_total"] == 176.4
    assert result["total_edge"] == 11.9
    assert result["total_pick"] == "Over"


def test_game_projection_total_pick_uses_market_decision_layer(monkeypatch) -> None:
    load_test_history()

    def fake_predict_market_total_edge(*args, **kwargs):
        return game_predictions_module._MarketTotalDecision(rows=180, edge=-1.7, weight=0.52)

    monkeypatch.setattr("backend.app.game_predictions._predict_market_total_edge", fake_predict_market_total_edge)

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
        result = project_game(conn, game)

    assert result["projected_total"] is not None
    assert result["total_edge"] == -1.7
    assert result["total_pick"] == "Under"
    assert result["total_reason"] is not None
    assert "total-market decision blend" in result["total_reason"]


def test_evaluate_game_residual_models_reports_baseline_and_blended_metrics() -> None:
    load_test_history()
    with connect() as conn:
        conn.execute("DELETE FROM game_predictions")
        conn.execute("DELETE FROM settled_game_predictions")
        captured_at = datetime.now(timezone.utc).isoformat()
        for prediction_id in range(400, 420):
            conn.execute(
                """
                INSERT INTO game_predictions (
                    id, game_id, model_version, prediction_time, projected_margin, projected_total,
                    winner_pick, ats_pick, total_pick, confidence, reason,
                    spread_home, game_total, home_rest_days, away_rest_days
                ) VALUES (?, ?, ?, ?, 2.0, 154.0, 'NY', 'CON +5.5', 'Under', 'medium', 'test eval', -5.5, 158.0, 2, 2)
                """,
                (prediction_id, 2010, f"test-eval-{prediction_id}", captured_at),
            )
            conn.execute(
                """
                INSERT INTO settled_game_predictions (
                    game_prediction_id, game_id, home_score, away_score, actual_winner,
                    actual_margin, actual_total, actual_ats_pick, actual_total_result,
                    winner_correct, ats_correct, total_correct, settled_at
                ) VALUES (?, 2010, 88, 79, 'NY', 9.0, 166.0, 'NY', 'Over', 1, 1, 1, ?)
                """,
                (prediction_id, captured_at),
            )
        metrics = evaluate_game_residual_models(conn)

    assert metrics["game_ats"]["rows"] >= 20
    assert metrics["game_total"]["rows"] >= 20
    assert metrics["game_ats"]["baseline_mae"] is not None
    assert metrics["game_total"]["baseline_mae"] is not None
    assert metrics["game_ats"]["mae"] <= metrics["game_ats"]["baseline_mae"]
    assert metrics["game_total"]["mae"] <= metrics["game_total"]["baseline_mae"]
    assert metrics["game_overall"]["rows"] >= 40


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
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-05-08")
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


def test_player_half_training_db_builds_curated_prop_examples() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            "INSERT INTO game_segment_results (game_id, home_q1_points, away_q1_points, home_q2_points, away_q2_points, home_1h_points, away_1h_points, source, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (100, 21, 18, 20, 17, 41, 35, "test", captured_at),
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (900001, 100, 1001, "DraftKings", "points", 18.5, -110, -110, captured_at),
        )
        conn.execute(
            """
            INSERT INTO settled_props (
                prop_line_id, actual_result, winning_side, margin, player_minutes, game_margin, team_margin, team_spread,
                blowout_result, blowout_threshold, team_points, opponent_points, team_possessions, opponent_possessions,
                pace, team_off_rating, opponent_off_rating, net_rating, team_possessions_source, opponent_possessions_source, settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                900001, 19.0, "over", 0.5, 32.0, 6.0, 6.0, -4.5,
                "no_blowout", 15.0, 82.0, 76.0, 78.0, 78.0,
                78.0, 105.1, 97.4, 7.7, "test", "test", captured_at,
            ),
        )
        conn.commit()

        info = ensure_player_half_training_db(conn, force=True)
        rows, _ = load_player_half_prop_examples(conn, market="points")

    assert info["player_rows"] > 0
    assert info["prop_rows"] == 1
    assert len(rows) == 1
    row = rows[0]
    assert row["market"] == "points"
    assert float(row["estimated_first_half_result"]) > 0.0
    assert row["line_progress_ratio"] is not None


def test_odds_import_backfills_provider_player_ids(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    load_test_history()
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-05-08")
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
        player_ids = [
            int(row["provider_player_id"])
            for row in conn.execute(
                "SELECT provider_player_id FROM sportsbook_prop_lines ORDER BY id"
            ).fetchall()
        ]

    assert result["provider_player_ids_filled"] == 2
    assert player_ids == [1001, 1001]


def test_fuzzy_player_name_match_accepts_token_superset() -> None:
    assert _fuzzy_player_name_match("Skylar Diggins", "Skylar Diggins-Smith") is True
    assert _fuzzy_player_name_match("A'ja Wilson", "A'ja Wilson") is True
    assert _fuzzy_player_name_match("Breanna Stewart", "Sabrina Ionescu") is False


def test_odds_import_ignores_cached_future_events(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    load_test_history()
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-05-08")
    monkeypatch.setattr(
        "backend.app.odds_import.read_json_cache",
        lambda name: [
            {
                "id": "tomorrow-event",
                "commence_time": "2026-05-09T23:30:00Z",
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
    assert result["imported"] == 0
    assert len(rows) == 0


def test_odds_import_syncs_model_prop_lines_from_sportsbook(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    load_test_history()
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-05-08")
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


def test_odds_import_reports_rebuild_progress_as_separate_stage(monkeypatch) -> None:
    progress_updates: list[tuple[str, int, int, str | None]] = []

    def fake_sync(conn, **kwargs):
        callback = kwargs.get("progress_callback")
        rebuild_callback = kwargs.get("rebuild_progress_callback")
        if callback is not None:
            callback(40, 176, "Matched props.")
        if rebuild_callback is not None:
            rebuild_callback(20, 146, "Built 20 projections.")
        return 3

    monkeypatch.setattr(odds_import_module, "sync_prop_lines_from_sportsbook", fake_sync)

    with connect() as conn:
        result = odds_import_module._sync_props_after_import(
            conn,
            progress_callback=lambda stage, current, total, message: progress_updates.append((stage, current, total, message)),
        )

    assert result == 3
    assert any(stage == "syncing_props" for stage, *_ in progress_updates)
    assert any(stage == "rebuilding_predictions" for stage, *_ in progress_updates)
    assert progress_updates.index(next(update for update in progress_updates if update[0] == "syncing_props")) < progress_updates.index(
        next(update for update in progress_updates if update[0] == "rebuilding_predictions")
    )


def test_odds_import_updates_game_markets_from_saved_payload(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    load_test_history()
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-05-08")
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
                        "key": "fanduel",
                        "title": "FanDuel",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Connecticut Sun", "price": -105},
                                    {"name": "New York Liberty", "price": -115},
                                ],
                            },
                            {
                                "key": "spreads",
                                "outcomes": [
                                    {"name": "Connecticut Sun", "price": -114, "point": 1.5},
                                    {"name": "New York Liberty", "price": -106, "point": -1.5},
                                ],
                            },
                            {
                                "key": "totals",
                                "outcomes": [
                                    {"name": "Over", "price": -110, "point": 161.5},
                                    {"name": "Under", "price": -108, "point": 161.5},
                                ],
                            },
                        ],
                    }
                ],
            }
        ] if name == RAW_CACHE_NAME else None,
    )
    with connect() as conn:
        result = import_the_odds_api_props(conn)
        game = conn.execute(
            """
            SELECT spread_home, game_total, home_moneyline, away_moneyline,
                   home_spread_price, away_spread_price, over_price, under_price
            FROM games
            WHERE id = 2010
            """
        ).fetchone()

    assert result["status"] == "loaded_from_cache"
    assert game["spread_home"] == -1.5
    assert game["game_total"] == 161.5
    assert game["home_moneyline"] == -115
    assert game["away_moneyline"] == -105
    assert game["home_spread_price"] == -106
    assert game["away_spread_price"] == -114
    assert game["over_price"] == -110
    assert game["under_price"] == -108


def test_odds_event_game_market_extracts_game_lines() -> None:
    market = odds_import_module._event_game_market(
        {
            "home_team": "Toronto Tempo",
            "away_team": "Los Angeles Sparks",
            "bookmakers": [
                {
                    "key": "fanduel",
                    "title": "FanDuel",
                    "markets": [
                        {
                            "key": "h2h",
                            "outcomes": [
                                {"name": "Los Angeles Sparks", "price": -105},
                                {"name": "Toronto Tempo", "price": -115},
                            ],
                        },
                        {
                            "key": "spreads",
                            "outcomes": [
                                {"name": "Los Angeles Sparks", "price": -114, "point": 1.5},
                                {"name": "Toronto Tempo", "price": -106, "point": -1.5},
                            ],
                        },
                        {
                            "key": "totals",
                            "outcomes": [
                                {"name": "Over", "price": -110, "point": 179.5},
                                {"name": "Under", "price": -110, "point": 179.5},
                            ],
                        },
                    ],
                }
            ],
        }
    )

    assert market == {
        "spread_home": -1.5,
        "game_total": 179.5,
        "home_moneyline": -115.0,
        "away_moneyline": -105.0,
        "home_spread_price": -106.0,
        "away_spread_price": -114.0,
        "over_price": -110.0,
        "under_price": -110.0,
    }


def test_odds_import_fetches_only_todays_events(monkeypatch) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    monkeypatch.setattr("backend.app.odds_import.load_dotenv", lambda: None)
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-05-08")
    monkeypatch.setattr("backend.app.odds_import.read_json_cache", lambda _: None)
    monkeypatch.setattr("backend.app.odds_import.write_json_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr("backend.app.odds_import.sync_prop_lines_from_sportsbook", lambda conn: 0)

    fetched_urls: list[str] = []

    def fake_fetch(url: str):
        fetched_urls.append(url)
        if url.endswith("/events?apiKey=test-key"):
            return [
                {"id": "today-event", "commence_time": "2026-05-08T23:30:00Z"},
                {"id": "tomorrow-event", "commence_time": "2026-05-09T23:30:00Z"},
            ]
        if "/events/today-event/odds?" in url:
            return {
                "id": "today-event",
                "commence_time": "2026-05-08T23:30:00Z",
                "home_team": "New York Liberty",
                "away_team": "Connecticut Sun",
                "bookmakers": [],
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("backend.app.odds_import._fetch_json", fake_fetch)
    monkeypatch.setattr(
        "backend.app.odds_import._replace_sportsbook_rows",
        lambda conn, payload, captured_at: {"events": len(payload), "imported": 0, "captured_at": captured_at},
    )

    with connect() as conn:
        result = import_the_odds_api_props(conn, force_refresh=True)

    assert result["status"] == "imported"
    assert result["fetched_events"] == 1
    assert fetched_urls[0].endswith("/events?apiKey=test-key")
    assert any("/events/today-event/odds?" in url for url in fetched_urls)
    assert not any("/events/tomorrow-event/odds?" in url for url in fetched_urls)


def test_odds_import_batches_player_markets_and_merges_successful_chunks(monkeypatch) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.setattr("backend.app.odds_import.load_dotenv", lambda: None)
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-05-08")
    monkeypatch.setattr("backend.app.odds_import.read_json_cache", lambda _: None)
    monkeypatch.setattr("backend.app.odds_import.write_json_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr("backend.app.odds_import.sync_prop_lines_from_sportsbook", lambda conn: 0)

    fetched_urls: list[str] = []

    def fake_fetch(url: str):
        fetched_urls.append(url)
        if url.endswith("/events?apiKey=test-key"):
            return [
                {
                    "id": "today-event",
                    "commence_time": "2026-05-08T23:30:00Z",
                }
            ]
        if "markets=h2h%2Cspreads%2Ctotals" in url:
            return {
                "id": "today-event",
                "commence_time": "2026-05-08T23:30:00Z",
                "home_team": "New York Liberty",
                "away_team": "Connecticut Sun",
                "bookmakers": [
                    {
                        "key": "fanduel",
                        "title": "FanDuel",
                        "markets": [
                            {
                                "key": "h2h",
                                "outcomes": [
                                    {"name": "Connecticut Sun", "price": -105},
                                    {"name": "New York Liberty", "price": -115},
                                ],
                            }
                        ],
                    }
                ],
            }
        if "markets=player_points%2Cplayer_rebounds%2Cplayer_assists" in url:
            return {
                "id": "today-event",
                "commence_time": "2026-05-08T23:30:00Z",
                "home_team": "New York Liberty",
                "away_team": "Connecticut Sun",
                "bookmakers": [
                    {
                        "key": "fanduel",
                        "title": "FanDuel",
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
        if "markets=player_threes%2Cplayer_points_rebounds%2Cplayer_points_assists" in url:
            raise RuntimeError("Odds API HTTP 422: invalid markets")
        if "markets=player_rebounds_assists%2Cplayer_points_rebounds_assists%2Cplayer_steals" in url:
            return {
                "id": "today-event",
                "commence_time": "2026-05-08T23:30:00Z",
                "home_team": "New York Liberty",
                "away_team": "Connecticut Sun",
                "bookmakers": [
                    {
                        "key": "draftkings",
                        "title": "DraftKings",
                        "markets": [
                            {
                                "key": "player_steals",
                                "outcomes": [
                                    {"name": "Over", "description": "Breanna Stewart", "price": 120, "point": 1.5},
                                    {"name": "Under", "description": "Breanna Stewart", "price": -150, "point": 1.5},
                                ],
                            }
                        ],
                    }
                ],
            }
        if "markets=player_blocks%2Cplayer_blocks_steals" in url:
            return {
                "id": "today-event",
                "commence_time": "2026-05-08T23:30:00Z",
                "home_team": "New York Liberty",
                "away_team": "Connecticut Sun",
                "bookmakers": [],
            }
        raise AssertionError(f"unexpected url: {url}")

    monkeypatch.setattr("backend.app.odds_import._fetch_json", fake_fetch)

    with connect() as conn:
        result = import_the_odds_api_props(conn, force_refresh=True)

    assert result["status"] == "partial_import"
    assert result["fetched_events"] == 1
    assert result["imported"] == 4
    assert len(result["errors"]) == 1
    assert "invalid markets" in result["errors"][0]["error"]
    assert any("markets=h2h%2Cspreads%2Ctotals" in url for url in fetched_urls)
    assert any("markets=player_blocks%2Cplayer_blocks_steals" in url for url in fetched_urls)


def test_odds_import_returns_provider_error_when_all_event_requests_fail(monkeypatch) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.setattr("backend.app.odds_import.load_dotenv", lambda: None)
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-05-08")
    monkeypatch.setattr("backend.app.odds_import.read_json_cache", lambda _: None)
    monkeypatch.setattr("backend.app.odds_import.write_json_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr("backend.app.odds_import.sync_prop_lines_from_sportsbook", lambda conn: 0)

    def fake_fetch(url: str):
        if url.endswith("/events?apiKey=test-key"):
            return [
                {
                    "id": "today-event",
                    "commence_time": "2026-05-08T23:30:00Z",
                }
            ]
        raise RuntimeError("Odds API HTTP 401: invalid api key")

    monkeypatch.setattr("backend.app.odds_import._fetch_json", fake_fetch)

    with connect() as conn:
        result = import_the_odds_api_props(conn, force_refresh=True)
        rows = conn.execute("SELECT * FROM sportsbook_prop_lines").fetchall()

    assert result["status"] == "provider_error"
    assert result["imported"] == 0
    assert len(rows) == 0
    assert len(result["errors"]) == 5
    assert "invalid api key" in str(result["message"]).lower()


def test_odds_import_preserves_cached_payload_when_refresh_fails_before_any_event_payloads(monkeypatch) -> None:
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.setattr("backend.app.odds_import.load_dotenv", lambda: None)
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-07-15")
    cached_payload = [
        {
            "id": "cached-event",
            "commence_time": "2026-07-15T23:30:00Z",
            "home_team": "Washington Mystics",
            "away_team": "Portland Fire",
            "bookmakers": [],
        }
    ]
    writes: list[object] = []
    monkeypatch.setattr("backend.app.odds_import.read_json_cache", lambda _: cached_payload)
    monkeypatch.setattr("backend.app.odds_import.write_json_cache", lambda *args, **kwargs: writes.append(args[1]))
    monkeypatch.setattr("backend.app.odds_import.sync_prop_lines_from_sportsbook", lambda conn: 0)

    def fake_fetch(url: str):
        if url.endswith("/events?apiKey=test-key"):
            return [
                {
                    "id": "today-event",
                    "commence_time": "2026-07-15T23:30:00Z",
                }
            ]
        raise RuntimeError("Odds API HTTP 401: Usage quota has been reached. See usage plans at https://the-odds-api.com")

    monkeypatch.setattr("backend.app.odds_import._fetch_json", fake_fetch)

    with connect() as conn:
        result = import_the_odds_api_props(conn, force_refresh=True)

    assert result["status"] == "provider_error"
    assert result["cached_events"] == 1
    assert result["events"] == 1
    assert writes == []


def test_odds_import_load_saved_returns_missing_cache_without_provider_request(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    monkeypatch.setattr("backend.app.odds_import.read_json_cache", lambda _: None)

    def fail_fetch(url: str):
        raise AssertionError(f"provider request should not run for saved-cache import: {url}")

    monkeypatch.setattr("backend.app.odds_import._fetch_json", fail_fetch)

    with connect() as conn:
        result = import_the_odds_api_props(conn, force_refresh=False)

    assert result["status"] == "missing_cache"
    assert result["source"] == "cache"
    assert result["imported"] == 0
    assert result["prop_sync_eligible"] is False
    assert "refresh odds" in str(result["message"]).lower()


def test_odds_import_load_saved_returns_stale_cache_for_old_payload(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    monkeypatch.setattr("backend.app.odds_import.local_today_iso", lambda: "2026-07-15")
    monkeypatch.setattr(
        "backend.app.odds_import.read_json_cache",
        lambda _: [
            {
                "id": "cached-event",
                "commence_time": "2026-06-25T23:00:00Z",
                "home_team": "Toronto Tempo",
                "away_team": "Los Angeles Sparks",
                "bookmakers": [],
            }
        ],
    )

    def fail_fetch(url: str):
        raise AssertionError(f"provider request should not run for stale saved-cache import: {url}")

    monkeypatch.setattr("backend.app.odds_import._fetch_json", fail_fetch)

    with connect() as conn:
        result = import_the_odds_api_props(conn, force_refresh=False)

    assert result["status"] == "stale_cache"
    assert result["source"] == "cache"
    assert result["imported"] == 0
    assert result["cached_events"] == 1
    assert result["prop_sync_eligible"] is False
    assert "latest cached commence_time" in str(result["message"]).lower()
    assert "2026-06-25t23:00:00z" in str(result["message"]).lower()


def test_odds_cache_summary_reports_active_cache_path(monkeypatch, tmp_path) -> None:
    class FixedDateTime(datetime):
        @classmethod
        def now(cls, tz=None):
            return cls(2026, 5, 8, 12, 0, tzinfo=timezone.utc)

    monkeypatch.setattr(odds_import_module, "get_cache_dir", lambda: tmp_path)
    monkeypatch.setattr(
        odds_import_module,
        "read_json_cache",
        lambda _: [{"id": "event-1", "commence_time": "2026-05-08T23:30:00Z"}],
    )
    monkeypatch.setattr(odds_import_module, "datetime", FixedDateTime)

    summary = odds_import_module.odds_cache_summary()

    assert summary["exists"] is True
    assert summary["path"] == str(tmp_path / RAW_CACHE_NAME)
    assert summary["events"] == 1
    assert summary["future_events"] == 1


def test_covers_historical_start_from_page_accepts_abbreviated_month_names() -> None:
    page = """
    <div>
      12:00 PM ET · Jul 15, 2026
    </div>
    """

    parsed = covers_import_module._historical_start_from_page(page)

    assert parsed == "2026-07-15T16:00:00+00:00"


def test_run_odds_import_job_refreshes_covers_without_overwriting_game_markets(monkeypatch) -> None:
    captured: dict[str, object] = {}
    progress_updates: list[tuple[str | None, int | None, int | None, int | None, str | None]] = []

    monkeypatch.setattr(
        main_module,
        "_import_the_odds_api_provider_rows",
        lambda conn, force_refresh=False, progress_callback=None: (
            progress_callback("requesting_provider", 1, 1, "Fetched provider payload.")
            if progress_callback is not None
            else None
        ) or {"status": "imported", "message": "ok", "prop_sync_eligible": True},
    )
    monkeypatch.setattr(
        main_module,
        "sync_prop_lines_from_sportsbook",
        lambda conn, **kwargs: (
            kwargs["progress_callback"](40, 176, "Matched props.") if kwargs.get("progress_callback") is not None else None
        ) or SyncPropLinesResult(
            synced_props=176,
            changed_props=20,
            changed_prop_line_ids=list(range(20)),
            touched_game_ids=[9910],
        ),
    )
    monkeypatch.setattr(
        main_module,
        "rebuild_predictions_live",
        lambda conn, game_ids=None, prop_line_ids=None, chunk_size=20, progress_callback=None: (
            progress_callback(20, 146, "Built 20 projections.") if progress_callback is not None else None
        ) or projections_module.LiveRebuildResult(
            projections=[],
            attempted=146,
            written=20,
            skipped=126,
            errors=[],
        ),
    )
    monkeypatch.setattr(main_module, "_snapshot_current_dfs_first_half", lambda conn, game_ids=None: {"inserted": 8, "updated": 0, "skipped": 0})
    monkeypatch.setattr(main_module, "_settle_recent_completed_games", lambda conn: {"selected_dates": []})

    def fake_import_covers_props(conn, selected_date=None, force_refresh=False, sync_props=True, update_game_markets=True):
        captured["selected_date"] = selected_date
        captured["force_refresh"] = force_refresh
        captured["sync_props"] = sync_props
        captured["update_game_markets"] = update_game_markets
        return {"status": "imported", "message": "covers ok"}

    monkeypatch.setattr(main_module, "import_covers_props", fake_import_covers_props)
    monkeypatch.setattr(main_module, "_publish_post_mutation_read_payloads", lambda conn: {"matchups": 1})
    monkeypatch.setattr(main_module, "_invalidate_read_caches", lambda: None)
    monkeypatch.setattr(
        main_module,
        "_set_prop_sync_progress",
        lambda *args, **kwargs: progress_updates.append(
            (
                kwargs.get("stage"),
                kwargs.get("stage_index"),
                kwargs.get("stage_total"),
                kwargs.get("current"),
                kwargs.get("message"),
            )
        ),
    )

    result = main_module._run_odds_import_job(True)

    assert captured == {
        "selected_date": main_module._local_today_iso(),
        "force_refresh": True,
        "sync_props": False,
        "update_game_markets": False,
    }
    assert result["covers_context"]["status"] == "imported"
    assert ("requesting_provider", 1, 6, 1, "Fetched provider payload.") in progress_updates
    assert ("syncing_props", 2, 6, 40, "Matched props.") in progress_updates
    assert ("rebuilding_predictions", 3, 6, 20, "Built 20 projections.") in progress_updates


def test_run_odds_import_job_refreshes_covers_after_provider_failure(monkeypatch) -> None:
    progress_updates: list[tuple[str | None, int | None, int | None, int | None, str | None]] = []
    state_updates: list[dict[str, object]] = []

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(
        main_module,
        "_import_the_odds_api_provider_rows",
        lambda conn, force_refresh=False, progress_callback=None: {
            "status": "provider_error",
            "message": "Odds API import failed for 1 request.",
            "prop_sync_eligible": False,
        },
    )
    monkeypatch.setattr(
        main_module,
        "import_covers_props",
        lambda conn, selected_date=None, force_refresh=False, sync_props=True, update_game_markets=True: {
            "status": "imported",
            "message": "covers ok",
            "imported": 12,
        },
    )
    monkeypatch.setattr(main_module, "_publish_post_mutation_read_payloads", lambda conn: {"matchups": 1})
    monkeypatch.setattr(main_module, "_mutate_prop_sync_state", lambda **kwargs: state_updates.append(kwargs))
    monkeypatch.setattr(
        main_module,
        "_set_prop_sync_progress",
        lambda *args, **kwargs: progress_updates.append(
            (
                kwargs.get("stage"),
                kwargs.get("stage_index"),
                kwargs.get("stage_total"),
                kwargs.get("current"),
                kwargs.get("message"),
            )
        ),
    )

    result = main_module._run_odds_import_job(True)

    assert result["status"] == "provider_error"
    assert result["covers_context"] == {"status": "imported", "message": "covers ok", "imported": 12}
    assert result["published_payloads"] == {"matchups": 1}
    assert ("refreshing_covers_context", 5, 6, 0, "Refreshing Covers matchup context for H2H and team history.") in progress_updates
    assert ("publishing_payloads", 6, 6, 0, "Publishing refreshed odds payloads.") in progress_updates
    assert state_updates and state_updates[-1]["status"] == "failed"
    assert state_updates[-1]["last_error"] == "Odds API import failed for 1 request."


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


def test_odds_sync_reingest_preserves_unchanged_prop_lines() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines")
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9601, 2010, 1001, 'DraftKings', 'points', 21.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9601, 'adaptive-context-v1', 'pregame', 22.0, 'over', 0.56, 0.52, 0.04, 0.07, 'medium', 'stale')
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
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 21.5, -110, captured_at),
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 21.5, -110, captured_at),
            ],
        )

        result = sync_prop_lines_from_sportsbook(
            conn,
            rebuild_predictions_after=False,
            include_change_details=True,
        )
        line = conn.execute("SELECT * FROM prop_lines WHERE id = 9601").fetchone()
        prediction = conn.execute("SELECT * FROM prop_predictions WHERE prop_line_id = 9601").fetchone()

    assert isinstance(result, SyncPropLinesResult)
    assert result.changed_props == 0
    assert result.changed_prop_line_ids == []
    assert line is not None
    assert prediction is not None
    assert prediction["reason"] == "stale"


def test_odds_sync_removes_dfs_snapshot_before_replacing_scheduled_prop_line() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines")
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9651, 2010, 1001, 'DraftKings', 'points', 21.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        snapshot_result = dfs_model_module.snapshot_current_dfs_first_half_estimates(
            conn,
            [
                {
                    "prop_line_id": 9651,
                    "game_id": 2010,
                    "player_id": 1001,
                    "sportsbook": "DraftKings",
                    "market": "points",
                    "line": 21.5,
                    "over_odds": -110,
                    "under_odds": -110,
                    "full_game_projection": 22.0,
                    "estimated_first_half_result": 11.0,
                    "expected_halfway_line": 10.75,
                    "pace_ratio": 1.0,
                    "halftime_margin_to_line": 0.25,
                    "on_track_probability": 0.55,
                    "recommended_side": "over",
                    "confidence": "medium",
                    "model_version": "dfs-first-half-ridge-v2",
                    "model_rows": 100,
                    "team_first_half_share": 0.5,
                    "estimated_first_half_minutes_share": 0.5,
                    "projected_first_half_total": 40.0,
                    "game_total": 80.0,
                    "start_time": "2026-05-08T23:30:00Z",
                }
            ],
        )
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 22.5, -105, captured_at),
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 22.5, -115, captured_at),
            ],
        )

        result = sync_prop_lines_from_sportsbook(
            conn,
            rebuild_predictions_after=False,
            include_change_details=True,
        )
        old_line = conn.execute("SELECT id FROM prop_lines WHERE id = 9651").fetchone()
        old_snapshot = conn.execute(
            "SELECT id FROM dfs_first_half_projection_snapshots WHERE prop_line_id = 9651"
        ).fetchone()
        replacement = conn.execute(
            "SELECT line FROM prop_lines WHERE game_id = 2010 AND player_id = 1001 AND market = 'points'"
        ).fetchone()

    assert snapshot_result == {"inserted": 1, "updated": 0, "skipped": 0}
    assert isinstance(result, SyncPropLinesResult)
    assert old_line is None
    assert old_snapshot is None
    assert replacement is not None
    assert float(replacement["line"]) == pytest.approx(22.5)


def test_odds_sync_reingest_updates_only_changed_prop_line_predictions(monkeypatch) -> None:
    load_test_history()
    first_captured_at = datetime.now(timezone.utc).isoformat()
    second_captured_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    rebuild_calls: list[list[int] | None] = []

    def fake_rebuild_predictions(conn, game_ids=None, prop_line_ids=None, chunk_size=20, progress_callback=None):
        rebuild_calls.append(list(prop_line_ids) if prop_line_ids is not None else None)
        return projections_module.LiveRebuildResult(
            projections=[],
            attempted=len(prop_line_ids or []),
            written=len(prop_line_ids or []),
            skipped=0,
            errors=[],
        )

    monkeypatch.setattr("backend.app.odds_import.rebuild_predictions_live", fake_rebuild_predictions)

    with connect() as conn:
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines")
        conn.executemany(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (9701, 2010, 1001, "DraftKings", "points", 21.5, -110, -110, first_captured_at),
                (9702, 2010, 1001, "DraftKings", "rebounds", 8.5, -108, -112, first_captured_at),
            ],
        )
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 21.5, -110, second_captured_at),
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 21.5, -110, second_captured_at),
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_rebounds", "rebounds", "Breanna Stewart", "over", 8.5, -102, second_captured_at),
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_rebounds", "rebounds", "Breanna Stewart", "under", 8.5, -118, second_captured_at),
            ],
        )

        result = sync_prop_lines_from_sportsbook(conn, include_change_details=True)
        point_row = conn.execute("SELECT * FROM prop_lines WHERE id = 9701").fetchone()
        rebound_row = conn.execute("SELECT * FROM prop_lines WHERE id = 9702").fetchone()

    assert isinstance(result, SyncPropLinesResult)
    assert result.changed_prop_line_ids == [9702]
    assert rebuild_calls == [[9702]]
    assert point_row is not None
    assert rebound_row is not None
    assert point_row["captured_at"] == second_captured_at
    assert rebound_row["over_odds"] == -102
    assert rebound_row["under_odds"] == -118


def test_odds_sync_rebuilds_touched_line_when_prediction_is_missing(monkeypatch) -> None:
    load_test_history()
    first_captured_at = datetime.now(timezone.utc).isoformat()
    second_captured_at = (datetime.now(timezone.utc) + timedelta(minutes=5)).isoformat()
    rebuild_calls: list[list[int] | None] = []

    def fake_rebuild_predictions(conn, game_ids=None, prop_line_ids=None, chunk_size=20, progress_callback=None):
        rebuild_calls.append(list(prop_line_ids) if prop_line_ids is not None else None)
        return projections_module.LiveRebuildResult(
            projections=[],
            attempted=len(prop_line_ids or []),
            written=len(prop_line_ids or []),
            skipped=0,
            errors=[],
        )

    monkeypatch.setattr("backend.app.odds_import.rebuild_predictions_live", fake_rebuild_predictions)

    with connect() as conn:
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines")
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9751, 2010, 1001, 'DraftKings', 'points', 21.5, -110, -110, ?)
            """,
            (first_captured_at,),
        )
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 21.5, -110, second_captured_at),
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 21.5, -110, second_captured_at),
            ],
        )

        result = sync_prop_lines_from_sportsbook(conn, include_change_details=True)
        row = conn.execute("SELECT * FROM prop_lines WHERE id = 9751").fetchone()

    assert isinstance(result, SyncPropLinesResult)
    assert result.changed_prop_line_ids == [9751]
    assert rebuild_calls == [[9751]]
    assert row is not None
    assert row["captured_at"] == second_captured_at


def test_odds_sync_matches_player_name_without_punctuation() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines")
        conn.execute(
            """
            INSERT INTO players (id, full_name, team_id, position, rotation_role)
            VALUES (2001, 'A''ja Wilson', 10, 'F', 'star')
            """
        )
        conn.execute(
            """
            INSERT INTO player_game_stats (
                player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
            ) VALUES (2001, 100, 30, 24, 10, 3, 1, 1, 2, 2)
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
                ("covers", "covers-event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "draftkings", "DraftKings", "player_points", "points", "Aja Wilson", "over", 20.5, -110, captured_at),
                ("covers", "covers-event", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "draftkings", "DraftKings", "player_points", "points", "Aja Wilson", "under", 20.5, -110, captured_at),
            ],
        )

        synced = sync_prop_lines_from_sportsbook(conn)
        row = conn.execute(
            """
            SELECT pl.*
            FROM prop_lines pl
            WHERE pl.player_id = 2001
              AND pl.market = 'points'
            """
        ).fetchone()

    assert synced == 1
    assert row is not None
    assert row["line"] == 20.5


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


def test_list_sportsbook_props_excludes_settled_or_completed_rows() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9251, 100, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("test", "evt-final", 100, "2026-04-01", "2026-04-01T19:00:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 20.5, -110, captured_at),
                ("test", "evt-final", 100, "2026-04-01", "2026-04-01T19:00:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 20.5, -110, captured_at),
            ],
        )
        settled = settle_completed_props(conn)
        payload = list_sportsbook_props(conn, 100)

    assert settled["settled"] >= 1
    assert payload == []


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


def test_odds_sync_deletes_snapshot_dependents_before_scheduled_line_cleanup() -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9501, 2010, 1001, 'DraftKings', 'points', 21.5, -110, -110, ?)
            """,
            (captured_at,),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9502, 9501, 'adaptive-context-v1', 'pregame', 22.0, 'over', 0.56, 0.52, 0.04, 0.07, 'medium', 'test')
            """
        )
        conn.execute(
            """
            INSERT INTO gem_snapshots (
                id, snapshot_date, preset, created_at, updated_at, min_ev, min_edge, allow_low, min_score
            ) VALUES (9503, '2026-05-08', 'default', ?, ?, 0.0, 0.0, 0, 0.0)
            """,
            (captured_at, captured_at),
        )
        conn.execute(
            """
            INSERT INTO gem_snapshot_items (
                snapshot_id, prop_line_id, game_id, player_id, market, side, line, edge, expected_value, confidence
            ) VALUES (9503, 9501, 2010, 1001, 'points', 'over', 21.5, 0.04, 0.07, 'medium')
            """
        )
        conn.execute(
            """
            INSERT INTO watchlist_snapshots (
                id, snapshot_date, created_at, updated_at, min_ev, min_edge, max_edge, item_count
            ) VALUES (9504, '2026-05-08', ?, ?, 0.0, 0.0, 1.0, 1)
            """,
            (captured_at, captured_at),
        )
        conn.execute(
            """
            INSERT INTO watchlist_snapshot_items (
                snapshot_id, prop_line_id, prediction_id, game_id, player_id, market, side, line, edge, expected_value, confidence
            ) VALUES (9504, 9501, 9502, 2010, 1001, 'points', 'over', 21.5, 0.04, 0.07, 'medium')
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
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 22.5, -110, captured_at),
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 22.5, -110, captured_at),
            ],
        )

        sync_prop_lines_from_sportsbook(conn)

        assert conn.execute("SELECT 1 FROM prop_lines WHERE id = 9501").fetchone() is None
        assert conn.execute("SELECT 1 FROM prop_predictions WHERE id = 9502").fetchone() is None
        assert conn.execute("SELECT 1 FROM gem_snapshot_items WHERE prop_line_id = 9501").fetchone() is None
        assert conn.execute("SELECT 1 FROM watchlist_snapshot_items WHERE prop_line_id = 9501").fetchone() is None


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


def test_rebuild_predictions_clears_watchlist_rows_for_replaced_predictions() -> None:
    load_test_history()
    now = datetime.now(timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (9940, '2026-05-25', ?, 10, 3, 'scheduled', 2, 2, -2.5, 161.5)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9941, 9940, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (now,),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9942, 9941, ?, ?, 19.0, 'under', 0.57, 0.50, 0.06, 0.03, 'low', 'watch-fk-test')
            """,
            (MODEL_VERSION, now),
        )
        conn.execute(
            """
            INSERT INTO watchlist_snapshots (
                id, snapshot_date, created_at, updated_at, min_ev, min_edge, max_edge, item_count
            ) VALUES (9943, '2026-05-25', ?, ?, 0.0, 0.0, 1.0, 1)
            """,
            (now, now),
        )
        conn.execute(
            """
            INSERT INTO watchlist_snapshot_items (
                snapshot_id, prop_line_id, prediction_id, game_id, player_id, market, side, line, edge, expected_value, confidence
            ) VALUES (9943, 9941, 9942, 9940, 1001, 'points', 'under', 20.5, 0.06, 0.03, 'low')
            """
        )

        projections = rebuild_predictions(conn)
        replacement = conn.execute(
            "SELECT * FROM prop_predictions WHERE prop_line_id = 9941 ORDER BY id DESC LIMIT 1"
        ).fetchone()
        old_watch_row = conn.execute(
            "SELECT 1 FROM watchlist_snapshot_items WHERE prediction_id = 9942",
        ).fetchone()

    assert projections
    assert replacement is not None
    assert replacement["id"] != 9942
    assert old_watch_row is None


def test_rebuild_predictions_uses_sqlite_write_lock(monkeypatch) -> None:
    load_test_history()
    entered: list[str] = []

    @contextmanager
    def fake_lock():
        entered.append("lock")
        yield

    monkeypatch.setattr(projections_module, "sqlite_write_lock", fake_lock)

    with connect() as conn:
        rebuild_predictions(conn)

    assert entered == ["lock"]


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


def test_model_performance_uses_latest_value_board_pick_per_prop_line() -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (2002, "Latest Pick Player", 10, "G", "starter"),
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, ?, ?)",
            (20002, "2026-05-02", "2026-05-02T19:00:00Z", 10, 3, -2.5, 151.5),
        )
        conn.execute(
            "INSERT INTO team_game_results (team_id, game_id, is_home, points, opponent_points, possessions, closing_spread, closing_total, ats_result, total_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (10, 20002, 1, 81, 75, 79.0, -2.5, 151.5, "cover", "over"),
        )
        conn.execute(
            "INSERT INTO player_game_stats (player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2002, 20002, 33, 19, 4, 6, 2, 1, 0, 2),
        )
        conn.execute(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (20002, 20002, 2002, "DraftKings", "points", 20.5, -110, -110, datetime.now(timezone.utc).isoformat()),
        )
        conn.executemany(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (200021, 20002, MODEL_VERSION, "2026-05-02T15:00:00Z", 21.6, "over", 0.62, 0.50, 0.12, 0.05, "medium", "older losing pick"),
                (200022, 20002, MODEL_VERSION, "2026-05-02T16:00:00Z", 19.4, "under", 0.57, 0.50, 0.06, 0.03, "low", "latest winning pick"),
            ],
        )
        settled = settle_completed_props(conn)

    assert settled["settled"] == 1
    performance = model_performance()
    assert performance["settled"] == 1
    assert performance["wins"] == 1
    assert performance["win_rate"] == 1.0
    assert performance["average_ev"] == 0.03
    assert "value-board" in performance["message"].lower()
    assert performance["game_totals"]["settled"] == 0


def test_model_performance_includes_live_game_totals_tracking() -> None:
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, ?, ?)
            """,
            (20010, "2026-05-03", "2026-05-03T19:00:00Z", 10, 3, -2.5, 158.5),
        )
        conn.execute(
            """
            INSERT INTO team_game_results (
                team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (10, 20010, 1, 82, 76, 80.0, -2.5, 158.5, "cover", "under"),
        )
        conn.execute(
            """
            INSERT INTO team_game_results (
                team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (3, 20010, 0, 76, 82, 80.0, 2.5, 158.5, "no_cover", "under"),
        )
        conn.execute(
            """
            INSERT INTO game_predictions (
                id, game_id, model_version, prediction_time,
                home_projected_points, away_projected_points, projected_margin, projected_total,
                projected_q1_total, projected_first_half_total,
                winner_pick, ats_pick, ats_edge, total_pick, total_edge,
                confidence, total_confidence, reason, total_reason,
                spread_home, game_total, home_rest_days, away_rest_days
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                200101,
                20010,
                GAME_MODEL_VERSION,
                "2026-05-03T15:00:00Z",
                80.0,
                75.0,
                5.0,
                155.0,
                39.0,
                77.0,
                "HOME",
                "HOME",
                2.0,
                "Under",
                -3.5,
                "medium",
                "high",
                "game reason",
                "total reason",
                -2.5,
                158.5,
                2,
                2,
            ),
        )
        settled = settle_completed_game_predictions(conn)

    assert settled["settled"] == 1
    performance = model_performance()
    totals = performance["game_totals"]
    assert totals["total_settled"] == 1
    assert totals["settled"] == 1
    assert totals["wins"] == 1
    assert totals["win_rate"] == 1.0
    assert totals["average_absolute_error"] == 3.0
    assert totals["average_absolute_edge"] == 3.5
    assert totals["by_edge"] == [{"label": "2.5-5", "settled": 1, "wins": 1, "win_rate": 1.0}]
    assert totals["by_confidence"] == [{"label": "high", "settled": 1, "wins": 1, "win_rate": 1.0}]
    assert GAME_MODEL_VERSION in totals["message"]


def test_assemble_direct_game_features_includes_market_prices_and_pregame_aggregates() -> None:
    features = game_predictions_module._assemble_direct_game_features(
        home_recent_points=81.0,
        away_recent_points=78.0,
        home_recent_allowed=76.0,
        away_recent_allowed=80.0,
        home_average_points=82.0,
        away_average_points=77.0,
        home_average_allowed=75.0,
        away_average_allowed=79.0,
        home_average_possessions=79.0,
        away_average_possessions=77.0,
        pace_factor=1.01,
        rest_days_home=2,
        rest_days_away=1,
        home_game_count=15,
        away_game_count=14,
        home_recent_possessions=80.0,
        away_recent_possessions=76.0,
        home_season_off_rating=103.0,
        away_season_off_rating=98.0,
        home_season_def_rating=97.0,
        away_season_def_rating=101.0,
        home_recent_off_rating=104.0,
        away_recent_off_rating=99.0,
        home_recent_def_rating=95.0,
        away_recent_def_rating=102.0,
        spread_home=-3.5,
        game_total=158.5,
        home_moneyline=-140.0,
        away_moneyline=120.0,
        home_spread_price=-108.0,
        away_spread_price=-112.0,
        over_price=-105.0,
        under_price=-115.0,
        matchup_pregame={
            "home_projected_minutes_total": 198.0,
            "away_projected_minutes_total": 194.0,
            "home_projected_points_total": 80.5,
            "away_projected_points_total": 77.0,
            "home_top3_minutes_share": 0.51,
            "away_top3_minutes_share": 0.49,
            "home_top5_points_share": 0.72,
            "away_top5_points_share": 0.69,
            "home_rotation_count": 8.0,
            "away_rotation_count": 7.0,
            "home_creator_count": 3.0,
            "away_creator_count": 2.0,
        },
    )

    feature_map = dict(zip(game_predictions_module.GAME_DIRECT_FEATURE_NAMES, features, strict=True))
    assert len(features) == len(game_predictions_module.GAME_DIRECT_FEATURE_NAMES)
    assert feature_map["home_spread_price"] == -108.0
    assert feature_map["under_price"] == -115.0
    assert feature_map["home_projected_points_total"] == 80.5
    assert feature_map["projected_points_delta"] == 3.5
    assert feature_map["creator_count_diff"] == 1.0


def test_watchlist_payload_applies_market_specific_low_confidence_filters(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (2003, "Watchlist Filter Player", 10, "G", "starter"),
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)",
            (20003, "2026-06-05", (now + timedelta(hours=4)).isoformat(), 10, 3, -1.5, 158.5),
        )
        conn.executemany(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (200031, 20003, 2003, "DraftKings", "assists", 5.5, -110, -110, now.isoformat()),
                (200032, 20003, 2003, "DraftKings", "rebounds", 6.5, -110, -110, now.isoformat()),
                (200033, 20003, 2003, "DraftKings", "points_assists", 23.5, -110, -110, now.isoformat()),
            ],
        )
        conn.executemany(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (2000311, 200031, MODEL_VERSION, now.isoformat(), 5.0, "under", 0.56, 0.50, 0.055, 0.03, "low", "eligible assists"),
                (2000321, 200032, MODEL_VERSION, now.isoformat(), 6.0, "under", 0.555, 0.50, 0.055, 0.03, "low", "filtered rebounds"),
                (2000331, 200033, MODEL_VERSION, now.isoformat(), 22.6, "under", 0.56, 0.50, 0.055, 0.03, "low", "filtered points assists"),
            ],
        )

        monkeypatch.setattr(main_module, "_is_active_game_time", lambda _start_time: True)
        payload = main_module._watchlist_payload(conn)

    assert [item["market"] for item in payload] == ["assists"]


def test_watchlist_hides_props_for_unavailable_players(monkeypatch) -> None:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.executemany(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            [
                (2005, "Unavailable Watchlist Player", 10, "G", "starter"),
                (2006, "Available Watchlist Player", 3, "G", "starter"),
            ],
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)",
            (20005, "2026-06-06", (now + timedelta(hours=4)).isoformat(), 10, 3, -1.5, 158.5),
        )
        conn.executemany(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (200051, 20005, 2005, "DraftKings", "assists", 5.5, -110, -110, now.isoformat()),
                (200052, 20005, 2006, "DraftKings", "assists", 4.5, -110, -110, now.isoformat()),
            ],
        )
        conn.executemany(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (2000511, 200051, MODEL_VERSION, now.isoformat(), 5.0, "under", 0.56, 0.50, 0.055, 0.03, "low", "should hide"),
                (2000521, 200052, MODEL_VERSION, now.isoformat(), 4.0, "under", 0.56, 0.50, 0.055, 0.03, "low", "should show"),
            ],
        )
        conn.execute(
            "INSERT INTO injuries (player_id, status, note, captured_at) VALUES (?, ?, ?, ?)",
            (2005, "inactive", "rest", now.isoformat()),
        )

        monkeypatch.setattr(main_module, "_is_active_game_time", lambda _start_time: True)
        payload = main_module._watchlist_payload(conn)

    assert [item["player"] for item in payload] == ["Available Watchlist Player"]


def test_watchlist_performance_uses_market_specific_low_confidence_filters() -> None:
    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (2004, "Settled Watchlist Player", 10, "G", "starter"),
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, ?, ?)",
            (20004, "2026-05-04", "2026-05-04T19:00:00Z", 10, 3, -2.0, 156.0),
        )
        conn.execute(
            "INSERT INTO team_game_results (team_id, game_id, is_home, points, opponent_points, possessions, closing_spread, closing_total, ats_result, total_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (10, 20004, 1, 80, 77, 78.0, -2.0, 156.0, "cover", "over"),
        )
        conn.execute(
            "INSERT INTO player_game_stats (player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2004, 20004, 32, 17, 6, 5, 1, 1, 0, 2),
        )
        conn.executemany(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (200041, 20004, 2004, "DraftKings", "assists", 5.5, -110, -110, datetime.now(timezone.utc).isoformat()),
                (200042, 20004, 2004, "DraftKings", "rebounds", 6.5, -110, -110, datetime.now(timezone.utc).isoformat()),
                (200043, 20004, 2004, "DraftKings", "points_assists", 23.5, -110, -110, datetime.now(timezone.utc).isoformat()),
            ],
        )
        conn.executemany(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (2000411, 200041, MODEL_VERSION, "2026-05-04T15:00:00Z", 5.0, "under", 0.56, 0.50, 0.055, 0.03, "low", "eligible assists"),
                (2000421, 200042, MODEL_VERSION, "2026-05-04T15:05:00Z", 6.0, "under", 0.555, 0.50, 0.055, 0.03, "low", "filtered rebounds"),
                (2000431, 200043, MODEL_VERSION, "2026-05-04T15:10:00Z", 22.6, "under", 0.56, 0.50, 0.055, 0.03, "low", "filtered points assists"),
            ],
        )
        settled = settle_completed_props(conn)

    assert settled["settled"] == 3
    performance = main_module.watchlist_performance(Response())
    assert performance["qualified"] == 1
    assert performance["wins"] == 1
    assert performance["win_rate"] == 1.0


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


def test_covers_parser_extracts_player_prop_rows_from_current_table_markup() -> None:
    html = """
    <script type="application/ld+json">
    {"startDate": "07/28/2026 23:30:00 &#x2B;00:00"}
    </script>
    <article id="908" class="card bg-white rounded rounded-2 shadow-sm p-3">
      <h2 class="fs-9">Total Points and Rebounds</h2>
      <a href="https://www.covers.com/sport/basketball/wnba/players/248305/olivia-nelson-ododa">
        <div class="playerContainer d-flex flex-row align-items-center gap-3 mb-3 mb-lg-0">
          <span class="category fw-bold text-nowrap">Olivia Nelson-Ododa</span>
        </div>
      </a>
      <table class="w-100 m-0 bg-white">
        <caption class="visually-hidden">Game Odds Connecticut Sun vs. Washington Mystics</caption>
        <thead>
          <tr>
            <th scope="col" class="oddsHeader">OVER</th>
            <th scope="col" class="oddsHeader">UNDER</th>
          </tr>
        </thead>
        <tbody>
          <tr scope="row">
            <td class="ps-lg-2 pe-1 oddsCell">
              <a>
                <span class="fw-bold fs-12">o17.5</span>
                <span class="fw-bold americanOdds fs-13">-112</span>
              </a>
              <img alt="Caesars logo" />
            </td>
            <td class="ps-1 oddsCell">
              <a>
                <span class="fw-bold fs-12">u17.5</span>
                <span class="fw-bold americanOdds fs-13">-105</span>
              </a>
              <img alt="Fanatics Sportsbook logo" />
            </td>
          </tr>
        </tbody>
      </table>
      <button id="compare-odds-btn"></button>
      <div class="compareOddsTable w-100 m-0">
        <table class="w-100 m-0">
          <tbody>
            <tr>
              <th scope="row" class="sportsbookLogoContainer">
                <img alt="DraftKings logo" />
              </th>
              <td class="compareOddsSide">
                <a>
                  <span class="fw-bold fs-12">o17.5</span>
                  <span class="fw-bold americanOdds fs-13">+100</span>
                </a>
              </td>
              <td class="compareOddsSide ps-1">
                <a>
                  <span class="fw-bold fs-12">u17.5</span>
                  <span class="fw-bold americanOdds fs-13">-130</span>
                </a>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </article>
    """

    with connect() as conn:
        metadata = _metadata_from_page(conn, CoversGame("374045", "https://example.test/odds"), html)
        rows = _event_rows(metadata, html, "2026-07-28T16:56:41+00:00")

    assert len(rows) == 4
    assert rows[0][10] == "points_rebounds"
    assert rows[0][11] == "Olivia Nelson-Ododa"
    assert {row[8] for row in rows} == {"Caesars", "Fanatics Sportsbook", "DraftKings"}
    assert {row[12] for row in rows} == {"over", "under"}
    assert {
        (row[12], row[13], row[14], row[8])
        for row in rows
    } == {
        ("over", 17.5, -112, "Caesars"),
        ("under", 17.5, -105, "Fanatics Sportsbook"),
        ("over", 17.5, 100, "DraftKings"),
        ("under", 17.5, -130, "DraftKings"),
    }


def test_covers_historical_replace_preserves_other_dates() -> None:
    def row_for(game_date: str, event_id: str) -> dict:
        return dict(
            zip(
                covers_import_module.ROW_COLUMNS,
                (
                    "covers", event_id, None, game_date, f"{game_date}T19:00:00+00:00",
                    "New York Liberty", "Connecticut Sun", "draftkings", "DraftKings",
                    "player_points", "points", "Breanna Stewart", "over", 20.5, -110,
                    f"{game_date}T12:00:00+00:00",
                ),
            )
        )

    with connect() as conn:
        covers_import_module._replace_covers_rows(conn, [row_for("2025-05-17", "historic")])
        covers_import_module._replace_covers_rows(conn, [row_for("2026-05-08", "current")])
        stored_dates = [
            row[0]
            for row in conn.execute(
                "SELECT game_date FROM sportsbook_prop_lines WHERE provider = 'covers' ORDER BY game_date"
            ).fetchall()
        ]

    assert stored_dates == ["2025-05-17", "2026-05-08"]


def test_covers_matchup_links_accepts_sport_and_sports_paths(monkeypatch) -> None:
    html = """
    <a href="/sport/basketball/wnba/matchup/373849/odds">A</a>
    <a href='/sports/basketball/wnba/matchup/373850'>B</a>
    """
    monkeypatch.setattr(covers_import_module, "_fetch_text", lambda url: html)
    games = covers_matchup_links()

    assert [game.event_id for game in games] == ["373849", "373850"]
    assert games[0].odds_url.endswith("/sport/basketball/wnba/matchup/373849/odds")
    assert games[1].odds_url.endswith("/sports/basketball/wnba/matchup/373850/odds")


def test_covers_matchup_href_parser_reads_single_and_double_quotes() -> None:
    html = """
    <a href="/sport/basketball/wnba/matchup/1/odds"></a>
    <a href='/sports/basketball/wnba/matchup/2'></a>
    """
    assert _covers_matchup_hrefs(html) == [
        ("/sport/basketball/wnba/matchup/1/odds", "1"),
        ("/sports/basketball/wnba/matchup/2", "2"),
    ]


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


def test_covers_parser_handles_portland_fire_pdx_game_lines() -> None:
    html = """
    <script type="application/ld+json">
    {"startDate": "05/19/2026 02:00:00 &#x2B;00:00", "name": "Connecticut Sun vs Portland Fire"}
    </script>
    <section>
      <h2>CON vs PDX Game Odds</h2>
      <div>Team Spread Total ML</div>
      <div>CON +4.5 -115 o173.5 -108 +155</div>
      <div>PDX -4.5 -105 u174.5 -120 -175</div>
    </section>
    """
    with connect() as conn:
        metadata = _metadata_from_page(conn, CoversGame("373872", "https://example.test/odds"), html)
        game = conn.execute("SELECT * FROM games WHERE id = ?", (metadata.game_id,)).fetchone()

    assert metadata.spread_home == -4.5
    assert metadata.game_total == 173.5
    assert game["spread_home"] == -4.5
    assert game["game_total"] == 173.5


def test_covers_parser_reads_historical_betting_information_block() -> None:
    html = """
    <script type="application/ld+json">
    {"startDate": "05/10/2026 20:30:00 &#x2B;00:00", "name": "Phoenix Mercury vs Golden State Valkyries"}
    </script>
    <section>
      <div>Betting Information Team ATS (Margin) O/U (Margin)</div>
      <div>PHO Phoenix +2.5 159.5o (14.5) GS Golden State -2.5 (13.5) 159.5u Line Movement</div>
    </section>
    """
    with connect() as conn:
        metadata = _metadata_from_page(conn, CoversGame("373853", "https://example.test/odds"), html)

    assert metadata.away_team == "Phoenix Mercury"
    assert metadata.home_team == "Golden State Valkyries"
    assert metadata.spread_home == -2.5
    assert metadata.game_total == 159.5


def test_covers_parser_reads_historical_teams_without_structured_name() -> None:
    html = """
    <script type="application/ld+json">
    {"startDate": "05/08/2026 23:30:00 &#x2B;00:00"}
    </script>
    <section>
      <div>Connecticut vs New York Results, Match Player Stats &amp; Records</div>
      <div>Betting Information Team ATS (Margin) O/U (Margin)</div>
      <div>CON Connecticut +19.5 169.5o (11.5) NY New York -19.5 (11.5) 169.5u Line Movement</div>
    </section>
    """
    with connect() as conn:
        metadata = _metadata_from_page(conn, CoversGame("373844", "https://example.test/odds"), html)

    assert metadata.away_team == "CON"
    assert metadata.home_team == "NY"
    assert metadata.spread_home == -19.5
    assert metadata.game_total == 169.5


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


def test_covers_odds_page_parser_extracts_spread_total_and_moneyline() -> None:
    html = """
    <section class="covers-CoversGame-Tickers">
      <div class="covers-CoversGame-Card game-Scheduled">
        <div class="game-info">
          <div class="team-row away-row">
            <span class="team-name">
              <span class="team-ShortName">CHI</span>
            </span>
            <span class="team-odds spread">-1.5</span>
            <span class="team-odds totals">o161.0</span>
          </div>
          <div class="team-row home-row">
            <span class="team-name">
              <span class="team-ShortName">WAS</span>
            </span>
            <span class="team-odds spread">1.5</span>
            <span class="team-odds totals">u161.0</span>
          </div>
        </div>
      </div>
    </section>
    <article class="card bg-white rounded rounded-2 shadow-sm p-3">
      <header class="card-header d-flex flex-row justify-content-between align-items-center pb-2">
        <h2 class="fs-9">Moneyline</h2>
      </header>
      <div class="card-body">
        <table class="w-100 m-0 bg-white">
          <caption class="visually-hidden">Game Odds Chicago Sky vs. Washington Mystics</caption>
          <thead>
            <tr>
              <th scope="col" class="emptyCellHeader"></th>
              <th scope="col" class="oddsHeader w-auto mw-50 text-center fw-normal fs-13 pb-1 pe-1">CHI</th>
              <th scope="col" class="oddsHeader w-auto mw-50 text-center fw-normal fs-13 pb-1 ps-1">WAS</th>
            </tr>
          </thead>
          <tbody>
            <tr scope="row">
              <td class="emptyCell"></td>
              <td class="ps-lg-2 pe-1 oddsCell">
                <div class="oddsSide">
                  <a data-linkcont="matchup-odds-best_odds-click-wnba-moneyline-chi-draftkings">
                    <span class="fw-bold fs-12">-130</span>
                  </a>
                </div>
              </td>
              <td class="ps-1 oddsCell">
                <div class="oddsSide">
                  <a data-linkcont="matchup-odds-best_odds-click-wnba-moneyline-was-fanduel">
                    <span class="fw-bold fs-12">&#x2B;110</span>
                  </a>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </article>
    <article class="card bg-white rounded rounded-2 shadow-sm p-3">
      <header class="card-header d-flex flex-row justify-content-between align-items-center pb-2">
        <h2 class="fs-9">Spread</h2>
      </header>
      <div class="card-body">
        <table class="w-100 m-0 bg-white">
          <thead>
            <tr>
              <th scope="col" class="emptyCellHeader"></th>
              <th scope="col" class="oddsHeader w-auto mw-50 text-center fw-normal fs-13 pb-1 pe-1">CHI</th>
              <th scope="col" class="oddsHeader w-auto mw-50 text-center fw-normal fs-13 pb-1 ps-1">WAS</th>
            </tr>
          </thead>
          <tbody>
            <tr scope="row">
              <td class="emptyCell"></td>
              <td class="ps-lg-2 pe-1 oddsCell">
                <div class="oddsSide">
                  <a data-linkcont="matchup-odds-best_odds-click-wnba-spread-chi-fanduel">
                    <span class="fw-bold fs-12">-1.5</span>
                    <span class="fw-bold americanOdds fs-13">-114</span>
                  </a>
                </div>
              </td>
              <td class="ps-1 oddsCell">
                <div class="oddsSide">
                  <a data-linkcont="matchup-odds-best_odds-click-wnba-spread-was-caesars">
                    <span class="fw-bold fs-12">&#x2B;1.5</span>
                    <span class="fw-bold americanOdds fs-13">&#x2B;100</span>
                  </a>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </article>
    <article class="card bg-white rounded rounded-2 shadow-sm p-3">
      <header class="card-header d-flex flex-row justify-content-between align-items-center pb-2">
        <h2 class="fs-9">Total</h2>
      </header>
      <div class="card-body">
        <table class="w-100 m-0 bg-white">
          <thead>
            <tr>
              <th scope="col" class="emptyCellHeader"></th>
              <th scope="col" class="oddsHeader w-auto mw-50 text-center fw-normal fs-13 pb-1 pe-1">OVER</th>
              <th scope="col" class="oddsHeader w-auto mw-50 text-center fw-normal fs-13 pb-1 ps-1">UNDER</th>
            </tr>
          </thead>
          <tbody>
            <tr scope="row">
              <td class="emptyCell"></td>
              <td class="ps-lg-2 pe-1 oddsCell">
                <div class="oddsSide">
                  <a data-linkcont="matchup-odds-best_odds-click-wnba-total-over-caesars">
                    <span class="fw-bold fs-12">o161.0</span>
                    <span class="fw-bold americanOdds fs-13">-110</span>
                  </a>
                </div>
              </td>
              <td class="ps-1 oddsCell">
                <div class="oddsSide">
                  <a data-linkcont="matchup-odds-best_odds-click-wnba-total-under-betmgm">
                    <span class="fw-bold fs-12">u160.5</span>
                    <span class="fw-bold americanOdds fs-13">-110</span>
                  </a>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </div>
    </article>
    """

    market = covers_import_module._game_market_from_page(html, "Washington Mystics", "Chicago Sky")

    assert market == {
        "spread_home": 1.5,
        "game_total": 161.0,
        "home_moneyline": 110.0,
        "away_moneyline": -130.0,
        "away_spread": -1.5,
        "away_spread_price": -114.0,
        "home_spread_price": 100.0,
        "over_total": 161.0,
        "over_price": -110.0,
        "under_total": 160.5,
        "under_price": -110.0,
    }


def test_covers_odds_board_parser_extracts_moneylines() -> None:
    html = """
    <section>
      <div>Moneyline Odds Table</div>
      <div>Today, 19:30</div>
      <div>CON</div>
      <div>ATL</div>
      <div>+809 9.09 81/10</div>
      <div>-1282 1.08 1/12</div>
      <div>Odds &amp; Props</div>
      <div>Today, 22:00</div>
      <div>PDX</div>
      <div>GS</div>
      <div>+289 3.89 26/9</div>
      <div>-377 1.27 3/11</div>
      <div>Odds &amp; Props</div>
    </section>
    """

    result = covers_import_module._moneylines_from_odds_board(html)

    assert result[("CON", "ATL")] == {"away_moneyline": 809.0, "home_moneyline": -1282.0}
    assert result[("PDX", "GS")] == {"away_moneyline": 289.0, "home_moneyline": -377.0}


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


def test_covers_records_by_game_falls_back_to_saved_pages(monkeypatch) -> None:
    pages_payload = {
        "cache_date": datetime.now(main_module.LOCAL_TZ).date().isoformat(),
        "games": [
            {
                "event_id": "373916",
                "odds_url": "https://example.test/odds",
                "matchup_url": "https://example.test/matchup",
                "matchup_page": "<html>saved matchup page</html>",
                "odds_page": "<html>saved odds page</html>",
            }
        ],
    }

    def fake_read_json_cache(name: str):
        if name == "covers_props_raw.json":
            return None
        if name == "covers_pages_raw.json":
            return pages_payload
        return None

    monkeypatch.setattr(main_module, "read_json_cache", fake_read_json_cache)
    monkeypatch.setattr(main_module, "delete_json_cache", lambda name: False)
    monkeypatch.setattr(
        main_module,
        "_metadata_from_page",
        lambda conn, game, page, fallback_page=None: SimpleNamespace(
            game_id=401856966,
            records={"head_to_head": [{"date": "Jun 1, '26"}], "away_last_10": [], "home_last_10": [], "team_table": []},
        ),
    )

    with connect() as conn:
        records = main_module._covers_records_by_game(conn)

    assert records == {
        401856966: {"head_to_head": [{"date": "Jun 1, '26"}], "away_last_10": [], "home_last_10": [], "team_table": []}
    }


def test_covers_market_title_aliases() -> None:
    assert _market_from_title("3 Pointers Made") == "3-pointers_made"
    assert _market_from_title("Total Steals") == "total_steals"
    assert _market_from_title("Total Blocks") == "total_blocks"
    assert _market_from_title("Total Steals + Blocks") == "total_steals_and_blocks"
    assert _market_from_title("Total Points + Rebounds") == "total_points_and_rebounds"
    assert _market_from_title("Total Points + Rebounds + Assists") == "total_points_rebounds_and_assists"


def test_covers_cache_is_current_requires_recent_capture_time() -> None:
    fresh_payload = {
        "provider": "covers",
        "rows": [{"provider_event_id": "fresh"}],
        "captured_at": (datetime.now(timezone.utc) - timedelta(seconds=COVERS_CACHE_TTL_SECONDS - 30)).isoformat(),
    }
    stale_payload = {
        "provider": "covers",
        "rows": [{"provider_event_id": "stale"}],
        "captured_at": (datetime.now(timezone.utc) - timedelta(seconds=COVERS_CACHE_TTL_SECONDS + 30)).isoformat(),
    }

    assert _covers_cache_is_current(fresh_payload) is True
    assert _covers_cache_is_current(stale_payload) is False


def test_covers_import_force_refresh_uses_cache_when_fresh_scrape_returns_no_rows(monkeypatch) -> None:
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
        lambda conn, rows, games=None, update_game_markets=True: {
            "events": 1,
            "imported": 1,
            "captured_at": "2026-05-22T20:00:00+00:00",
        },
    )
    monkeypatch.setattr(covers_import_module, "sync_prop_lines_from_sportsbook", lambda conn: 0)
    with connect() as conn:
        result = covers_import_module.import_covers_props(conn, force_refresh=True)

    assert result["status"] == "loaded_from_cache"
    assert result["source"] == "cache"
    assert result["imported"] == 1


def test_covers_fetch_text_uses_requests_fallback(monkeypatch) -> None:
    class FakeResponse:
        encoding = "utf-8"
        text = "fallback body"

        def raise_for_status(self) -> None:
            return None

    def raise_urlopen(*args, **kwargs):
        raise URLError("temporary failure")

    monkeypatch.setattr(covers_import_module, "urlopen", raise_urlopen)
    monkeypatch.setattr(covers_import_module.requests, "get", lambda *args, **kwargs: FakeResponse())

    assert covers_import_module._fetch_text("https://example.com/test") == "fallback body"


def test_rotowire_lineup_parser_extracts_may_not_play_by_team() -> None:
    html = """
    <section>
      <div>7:30 PM ET</div>
      <div><a>GSV</a> <a>IND</a></div>
      <ul>
        <li>Confirmed Lineup</li>
        <li>MAY NOT PLAY</li>
        <li>Juste Jocyte GTD</li>
        <li>C. Zandalasini OUT</li>
      </ul>
      <ul>
        <li>Confirmed Lineup</li>
        <li>MAY NOT PLAY</li>
        <li>Caitlin Clark GTD</li>
      </ul>
    </section>
    """

    rows = _parse_lineup_injuries(html)

    assert rows == [
        {"team": "GSV", "player_name": "Juste Jocyte", "status": "GTD"},
        {"team": "GSV", "player_name": "C. Zandalasini", "status": "OUT"},
        {"team": "IND", "player_name": "Caitlin Clark", "status": "GTD"},
    ]


def test_rotowire_lineup_parser_extracts_split_position_player_status_rows() -> None:
    html = """
    <section>
      <div>1:00 PM ET</div>
      <div><a>MIN</a></div>
      <div><a>CHI</a></div>
      <ul>
        <li>Expected Lineup</li>
        <li>MAY NOT PLAY</li>
        <li>F</li>
        <li>N. Collier</li>
        <li>OUT</li>
        <li>F</li>
        <li>Dorka Juhasz</li>
        <li>OUT</li>
      </ul>
      <ul>
        <li>Expected Lineup</li>
        <li>MAY NOT PLAY</li>
        <li>F</li>
        <li>Azura Stevens</li>
        <li>GTD</li>
      </ul>
    </section>
    """

    rows = _parse_lineup_injuries(html)

    assert rows == [
        {"team": "MIN", "player_name": "N. Collier", "status": "OUT"},
        {"team": "MIN", "player_name": "Dorka Juhasz", "status": "OUT"},
        {"team": "CHI", "player_name": "Azura Stevens", "status": "GTD"},
    ]


def test_rotowire_lineup_parser_stops_may_not_play_at_projected_minutes_break() -> None:
    html = """
    <section>
      <div>8:00 PM ET</div>
      <div><a>NYL</a></div>
      <div><a>DAL</a></div>
      <ul>
        <li>Expected Lineup</li>
        <li>G M. Johannes GTD</li>
        <li>G S. Ionescu</li>
        <li>G P. Astier</li>
        <li>F B. Stewart</li>
        <li>C Jonquel Jones</li>
        <li>Projected Minutes</li>
        <li>MAY NOT PLAY</li>
        <li>G M. Johannes GTD</li>
        <li>F L. Fiebich OUT</li>
        <li>F Satou Sabally OUT</li>
        <li>Expected Lineup</li>
        <li>G A. Ogunbowale</li>
        <li>G P. Bueckers</li>
        <li>G Azzi Fudd</li>
        <li>C Awak Kuier</li>
        <li>C J. Shepard</li>
        <li>Projected Minutes</li>
        <li>MAY NOT PLAY</li>
        <li>F Alanna Smith GTD</li>
      </ul>
    </section>
    """

    rows = _parse_lineup_injuries(html)

    assert rows == [
        {"team": "NYL", "player_name": "M. Johannes", "status": "GTD"},
        {"team": "NYL", "player_name": "L. Fiebich", "status": "OUT"},
        {"team": "NYL", "player_name": "Satou Sabally", "status": "OUT"},
        {"team": "DAL", "player_name": "Alanna Smith", "status": "GTD"},
    ]


def test_rotowire_import_uses_cache_when_fresh(monkeypatch) -> None:
    cached_payload = {
        "captured_at": "2026-05-22T20:00:00+00:00",
        "rows": [
            {"team": "IND", "player_name": "Caitlin Clark", "status": "GTD"},
        ],
    }
    monkeypatch.setattr(rotowire_import_module, "read_json_cache", lambda _: cached_payload)
    monkeypatch.setattr(rotowire_import_module, "_cache_is_current", lambda conn, payload: True)
    monkeypatch.setattr(rotowire_import_module, "_fetch_text", lambda _: (_ for _ in ()).throw(AssertionError("should not fetch")))

    with connect() as conn:
        result = rotowire_import_module.import_rotowire_lineups(conn, force_refresh=False)

    assert result["from_cache"] is True
    assert result["source"] == "cache"


def test_rotowire_import_force_refresh_fetches(monkeypatch) -> None:
    monkeypatch.setattr(rotowire_import_module, "read_json_cache", lambda _: {"captured_at": "2026-05-22T20:00:00+00:00", "rows": []})
    monkeypatch.setattr(
        rotowire_import_module,
        "_fetch_text",
        lambda _: "<section><div>GSV IND</div><li>Confirmed Lineup</li><li>MAY NOT PLAY</li><li>Caitlin Clark GTD</li></section>",
    )
    monkeypatch.setattr(rotowire_import_module, "write_json_cache", lambda *args, **kwargs: None)

    with connect() as conn:
        result = rotowire_import_module.import_rotowire_lineups(conn, force_refresh=True)

    assert result["from_cache"] is False
    assert result["source"] == "rotowire"


def test_rotowire_import_clears_resolved_injuries(monkeypatch) -> None:
    load_test_history()
    monkeypatch.setattr(rotowire_import_module, "write_json_cache", lambda *args, **kwargs: None)

    first_html = """
    <section>
            <div>TOR CON</div>
      <ul>
        <li>Expected Lineup</li>
        <li>MAY NOT PLAY</li>
        <li>G</li>
        <li>Sonia Citron</li>
        <li>OUT</li>
      </ul>
    </section>
    """
    second_html = """
    <section>
            <div>TOR CON</div>
      <ul>
        <li>Confirmed Lineup</li>
      </ul>
    </section>
    """

    monkeypatch.setattr(rotowire_import_module, "_fetch_text", lambda _: first_html)
    with connect() as conn:
        result = rotowire_import_module.import_rotowire_lineups(conn, force_refresh=True)
        row = conn.execute(
            "SELECT status FROM injuries WHERE player_id = ? ORDER BY captured_at DESC LIMIT 1",
            (1002,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "out"
    assert result["source"] == "rotowire"

    monkeypatch.setattr(rotowire_import_module, "_fetch_text", lambda _: second_html)
    with connect() as conn:
        result = rotowire_import_module.import_rotowire_lineups(conn, force_refresh=True)
        row = conn.execute(
            "SELECT status, note FROM injuries WHERE player_id = ? ORDER BY captured_at DESC LIMIT 1",
            (1002,),
        ).fetchone()
    assert row is not None
    assert row["status"] == "available"
    assert row["note"] == "rotowire lineup cleared"
    assert "Cleared" in str(result["message"])


def test_rotowire_import_force_refresh_falls_back_to_cached_rows_on_fetch_failure(monkeypatch) -> None:
    cached_payload = {
        "captured_at": "2026-05-22T20:00:00+00:00",
        "rows": [
            {"team": "IND", "player_name": "Caitlin Clark", "status": "GTD"},
        ],
    }
    monkeypatch.setattr(rotowire_import_module, "read_json_cache", lambda _: cached_payload)
    monkeypatch.setattr(
        rotowire_import_module,
        "_fetch_text",
        lambda _: (_ for _ in ()).throw(TimeoutError("network timeout")),
    )
    monkeypatch.setattr(rotowire_import_module, "write_json_cache", lambda *args, **kwargs: None)

    with connect() as conn:
        result = rotowire_import_module.import_rotowire_lineups(conn, force_refresh=True)

    assert result["from_cache"] is True
    assert result["used_fallback_cache"] is True
    assert result["source"] == "cache"
    assert "timeout" in str(result["fetch_error"]).lower()


def test_odds_sync_can_skip_rebuild_inside_sync_transaction(monkeypatch) -> None:
    load_test_history()
    captured_at = datetime.now(timezone.utc).isoformat()
    rebuild_calls: list[list[int] | None] = []

    def fake_rebuild_predictions(conn, game_ids=None, prop_line_ids=None, chunk_size=20):
        rebuild_calls.append(list(game_ids) if game_ids is not None else None)
        return projections_module.LiveRebuildResult(
            projections=[],
            attempted=0,
            written=0,
            skipped=0,
            errors=[],
        )

    monkeypatch.setattr("backend.app.odds_import.rebuild_predictions_live", fake_rebuild_predictions)

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
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 21.5, -110, captured_at),
                ("covers", "evt", 2010, "2026-05-08", "2026-05-08T23:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "under", 21.5, -110, captured_at),
            ],
        )

        synced = sync_prop_lines_from_sportsbook(conn, rebuild_predictions_after=False)

    assert synced == 1
    assert rebuild_calls == []


def test_rotowire_import_returns_db_locked_status_when_injury_write_is_busy(monkeypatch) -> None:
    monkeypatch.setattr(rotowire_import_module, "read_json_cache", lambda _: {"captured_at": "2026-05-22T20:00:00+00:00", "rows": []})
    monkeypatch.setattr(
        rotowire_import_module,
        "_fetch_text",
        lambda _: "<section><div>GSV IND</div><li>Confirmed Lineup</li><li>MAY NOT PLAY</li><li>Caitlin Clark GTD</li></section>",
    )
    monkeypatch.setattr(rotowire_import_module, "write_json_cache", lambda *args, **kwargs: None)
    monkeypatch.setattr(rotowire_import_module, "_resolve_player_id", lambda conn, team_abbreviation, player_name: 1)

    class DummyRow(dict):
        pass

    class DummyCursor:
        def __init__(self, row=None, rows=None):
            self._row = row
            self._rows = rows or []

        def fetchone(self):
            return self._row

        def fetchall(self):
            return self._rows

    class DummyConn:
        def __init__(self) -> None:
            self.rolled_back = False

        def execute(self, sql, params=()):
            normalized = " ".join(str(sql).split()).lower()
            if "from games" in normalized:
                return DummyCursor(rows=[])
            if normalized.startswith("select captured_at from injuries"):
                return DummyCursor(row=None)
            if normalized.startswith("insert into injuries"):
                raise sqlite3.OperationalError("database is locked")
            return DummyCursor()

        def commit(self):
            return None

        def rollback(self):
            self.rolled_back = True

    conn = DummyConn()
    result = rotowire_import_module.import_rotowire_lineups(conn, force_refresh=True)

    assert result["status"] == "db_locked"
    assert "database is busy" in str(result["message"]).lower()
    assert conn.rolled_back is True


def test_recover_sqlite_lock_returns_db_locked_when_checkpoint_is_blocked(monkeypatch) -> None:
    class DummyConn:
        def execute(self, sql, params=()):
            normalized = " ".join(str(sql).split()).lower()
            if normalized == "pragma busy_timeout = 1000":
                return None
            if normalized == "begin immediate":
                raise sqlite3.OperationalError("database table is locked")
            if normalized == "rollback":
                return None
            raise AssertionError(f"Unexpected SQL: {sql}")

        def close(self):
            return None

    monkeypatch.setattr(main_module, "_audit_sqlite_lock", lambda: {"engine": "sqlite", "status": "ok", "locked": False})
    monkeypatch.setattr(main_module, "get_db_path", lambda: "/tmp/test.sqlite")
    monkeypatch.setattr(main_module.sqlite3, "connect", lambda *args, **kwargs: DummyConn())

    result = main_module._recover_sqlite_lock()

    assert result["status"] == "db_locked"
    assert result["recovered"] is False
    assert "still locked" in str(result["message"]).lower()
    assert "database table is locked" in str(result["recovery_error"]).lower()


def test_odds_sync_treats_database_table_locked_as_busy(monkeypatch) -> None:
    class DummyConn:
        def __init__(self) -> None:
            self.rolled_back = False

        def execute(self, sql, params=()):
            raise sqlite3.OperationalError("database table is locked")

        def rollback(self):
            self.rolled_back = True

    conn = DummyConn()

    result = odds_import_module.sync_prop_lines_from_sportsbook(conn)

    assert result == 0
    assert conn.rolled_back is True


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


def test_espn_boxscore_records_dnp_and_deletes_open_props(monkeypatch) -> None:
    def fake_fetch_summary(game_id: int, force_refresh: bool = False) -> dict:
        return {
            "boxscore": {
                "players": [
                    {
                        "team": {"abbreviation": "WSH"},
                        "statistics": [
                            {
                                "labels": ["MIN", "PTS", "REB", "AST", "3PT", "STL", "BLK", "TO"],
                                "athletes": [
                                    {
                                        "athlete": {
                                            "id": "4433524",
                                            "displayName": "Sonia Citron",
                                            "position": {"abbreviation": "G"},
                                        },
                                        "didNotPlay": True,
                                        "active": False,
                                        "reason": "COACH'S DECISION",
                                        "starter": False,
                                        "stats": [],
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
            INSERT INTO players (id, full_name, team_id, position, rotation_role)
            VALUES (4433524, 'Sonia Citron', 11, 'G', 'starter')
            """
        )
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (401857034, '2026-07-02', '2026-07-02T23:30:00Z', 11, 8, 'final', 2, 2, -2.5, 162.5, 401857034)
            """
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9001, 401857034, 4433524, 'DraftKings', 'points', 17.5, -110, -110, '2026-07-02T20:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9001, 'adaptive-context-v1', '2026-07-02T20:00:00+00:00', 18.2, 'over', 0.55, 0.52, 0.03, 0.02, 'medium', 'test')
            """
        )

        result = import_espn_player_boxscores(conn, 2026, selected_date="2026-07-02", missing_only=True)
        availability = conn.execute(
            """
            SELECT *
            FROM player_game_availability
            WHERE player_id = 4433524 AND game_id = 401857034 AND source = 'espn_boxscore'
            """
        ).fetchone()
        remaining_prop = conn.execute("SELECT COUNT(*) FROM prop_lines WHERE id = 9001").fetchone()[0]
        remaining_prediction = conn.execute("SELECT COUNT(*) FROM prop_predictions WHERE prop_line_id = 9001").fetchone()[0]

    assert result["inserted_player_game_stats"] == 0
    assert result["recorded_player_game_availability"] == 1
    assert result["deleted_dnp_prop_lines"] == 1
    assert availability["did_not_play"] == 1
    assert availability["status_reason"] == "COACH'S DECISION"
    assert remaining_prop == 0
    assert remaining_prediction == 0


def test_espn_boxscore_backfills_team_possessions_from_summary(monkeypatch) -> None:
    def fake_fetch_summary(game_id: int, force_refresh: bool = False) -> dict:
        assert game_id == 401857068
        return {
            "boxscore": {
                "teams": [
                    {
                        "team": {"abbreviation": "SEA"},
                        "statistics": [
                            {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "36-78", "label": "FG"},
                            {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "14-23", "label": "FT"},
                            {"name": "offensiveRebounds", "displayValue": "16", "abbreviation": "OR"},
                            {"name": "totalTurnovers", "displayValue": "16", "abbreviation": "ToTO"},
                        ],
                    },
                    {
                        "team": {"abbreviation": "CHI"},
                        "statistics": [
                            {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "35-74", "label": "FG"},
                            {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "14-19", "label": "FT"},
                            {"name": "offensiveRebounds", "displayValue": "6", "abbreviation": "OR"},
                            {"name": "turnovers", "displayValue": "13", "abbreviation": "TO"},
                            {"name": "teamTurnovers", "displayValue": "1", "abbreviation": "TTO"},
                        ],
                    },
                ],
                "players": [],
            }
        }

    monkeypatch.setattr("backend.app.espn_history.fetch_summary", fake_fetch_summary)

    with connect() as conn:
        home_team_id = ensure_team(conn, "CHI")
        away_team_id = ensure_team(conn, "SEA")
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (?, '2026-07-15', '2026-07-15T16:00:00Z', ?, ?, 'final', 2, 2, 0.0, 185.0, 401857068)
            """
            ,
            (401857068, home_team_id, away_team_id),
        )
        conn.executemany(
            """
            INSERT INTO team_game_results (
                team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (home_team_id, 401857068, 1, 95, 90, 78.0, 0.0, 185.0, "push", "push"),
                (away_team_id, 401857068, 0, 90, 95, 78.0, 0.0, 185.0, "push", "push"),
            ],
        )

        result = import_espn_player_boxscores(conn, 2026, selected_date="2026-07-15", missing_only=True)
        rows = conn.execute(
            "SELECT team_id, possessions FROM team_game_results WHERE game_id = 401857068 ORDER BY team_id"
        ).fetchall()
        boxscore_rows = conn.execute(
            """
            SELECT
                team_id,
                offensive_rebounds,
                turnovers,
                team_turnovers,
                total_turnovers,
                field_goals_attempted,
                free_throws_attempted,
                possessions
            FROM team_game_boxscores
            WHERE game_id = 401857068
            ORDER BY team_id
            """
        ).fetchall()

    assert result["updated_team_game_results"] == 2
    assert result["updated_team_game_boxscores"] == 2
    assert {int(row["team_id"]): float(row["possessions"]) for row in rows} == {
        int(away_team_id): 88.12,
        int(home_team_id): 90.36,
    }
    assert {
        int(row["team_id"]): (
            int(row["offensive_rebounds"] or 0),
            int(row["turnovers"] or 0),
            int(row["team_turnovers"] or 0),
            int(row["total_turnovers"] or 0),
            int(row["field_goals_attempted"] or 0),
            int(row["free_throws_attempted"] or 0),
            float(row["possessions"] or 0.0),
        )
        for row in boxscore_rows
    } == {
        int(away_team_id): (16, 0, 0, 16, 78, 23, 88.12),
        int(home_team_id): (6, 13, 1, 0, 74, 19, 90.36),
    }


def test_backfill_espn_team_possessions_repairs_placeholder_rows(monkeypatch) -> None:
    def fake_fetch_summary(game_id: int, force_refresh: bool = False) -> dict:
        assert game_id == 401857068
        return {
            "boxscore": {
                "teams": [
                    {
                        "team": {"abbreviation": "SEA"},
                        "statistics": [
                            {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "36-78", "label": "FG"},
                            {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "14-23", "label": "FT"},
                            {"name": "offensiveRebounds", "displayValue": "16", "abbreviation": "OR"},
                            {"name": "totalTurnovers", "displayValue": "16", "abbreviation": "ToTO"},
                        ],
                    },
                    {
                        "team": {"abbreviation": "CHI"},
                        "statistics": [
                            {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "35-74", "label": "FG"},
                            {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "14-19", "label": "FT"},
                            {"name": "offensiveRebounds", "displayValue": "6", "abbreviation": "OR"},
                            {"name": "turnovers", "displayValue": "13", "abbreviation": "TO"},
                            {"name": "teamTurnovers", "displayValue": "1", "abbreviation": "TTO"},
                        ],
                    },
                ]
            }
        }

    monkeypatch.setattr("backend.app.espn_history.fetch_summary", fake_fetch_summary)

    with connect() as conn:
        home_team_id = ensure_team(conn, "CHI")
        away_team_id = ensure_team(conn, "SEA")
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (?, '2026-07-15', '2026-07-15T16:00:00Z', ?, ?, 'final', 2, 2, 0.0, 185.0, 401857068)
            """,
            (401857068, home_team_id, away_team_id),
        )
        conn.executemany(
            """
            INSERT INTO team_game_results (
                team_id, game_id, is_home, points, opponent_points, possessions,
                closing_spread, closing_total, ats_result, total_result
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (home_team_id, 401857068, 1, 95, 90, 78.0, 0.0, 185.0, "push", "push"),
                (away_team_id, 401857068, 0, 90, 95, 78.0, 0.0, 185.0, "push", "push"),
            ],
        )

        result = backfill_espn_team_possessions(conn, 2026, selected_date="2026-07-15")
        rows = conn.execute(
            "SELECT team_id, possessions FROM team_game_results WHERE game_id = 401857068 ORDER BY team_id"
        ).fetchall()
        boxscore_rows = conn.execute(
            """
            SELECT team_id, offensive_rebounds, total_turnovers, field_goals_attempted, free_throws_attempted, possessions
            FROM team_game_boxscores
            WHERE game_id = 401857068
            ORDER BY team_id
            """
        ).fetchall()

    assert result["games_checked"] == 1
    assert result["updated_team_game_results"] == 2
    assert result["updated_team_game_boxscores"] == 2
    assert result["games_with_updates"] == 1
    assert {int(row["team_id"]): float(row["possessions"]) for row in rows} == {
        int(away_team_id): 88.12,
        int(home_team_id): 90.36,
    }
    assert len(boxscore_rows) == 2


def test_espn_summary_injury_without_boxscore_entry_deletes_open_props(monkeypatch) -> None:
    def fake_fetch_summary(game_id: int, force_refresh: bool = False) -> dict:
        return {
            "boxscore": {
                "players": [
                    {
                        "team": {"abbreviation": "LV"},
                        "statistics": [
                            {
                                "labels": ["MIN", "PTS", "REB", "AST", "3PT", "STL", "BLK", "TO"],
                                "athletes": [
                                    {
                                        "athlete": {
                                            "id": "4281190",
                                            "displayName": "Dana Evans",
                                            "position": {"abbreviation": "G"},
                                        },
                                        "starter": False,
                                        "stats": ["12", "4", "1", "2", "0-2", "0", "0", "1"],
                                    }
                                ],
                            }
                        ],
                    }
                ]
            },
            "injuries": [
                {
                    "team": {"abbreviation": "LV", "displayName": "Las Vegas Aces"},
                    "injuries": [
                        {
                            "status": "Day-To-Day",
                            "athlete": {
                                "id": "3149391",
                                "displayName": "A'ja Wilson",
                                "position": {"abbreviation": "C"},
                            },
                            "details": {"type": "Leg"},
                        }
                    ],
                }
            ],
        }

    monkeypatch.setattr("backend.app.espn_history.fetch_summary", fake_fetch_summary)

    with connect() as conn:
        conn.execute(
            """
            INSERT INTO players (id, full_name, team_id, position, rotation_role)
            VALUES (3149391, 'A''ja Wilson', 1, 'C', 'star')
            """
        )
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (401857321, '2026-06-30', '2026-06-30T23:00:00Z', 3, 1, 'final', 2, 2, 1.5, 167.5, 401857321)
            """
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9011, 401857321, 3149391, 'DraftKings', 'points_rebounds', 34.5, -110, -110, '2026-06-30T20:00:00+00:00')
            """
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9011, 'adaptive-context-v1', '2026-06-30T20:00:00+00:00', 35.2, 'over', 0.54, 0.52, 0.02, 0.01, 'medium', 'test')
            """
        )

        result = import_espn_player_boxscores(conn, 2026, selected_date="2026-06-30", missing_only=True)
        availability = conn.execute(
            """
            SELECT *
            FROM player_game_availability
            WHERE player_id = 3149391 AND game_id = 401857321 AND source = 'espn_summary_injury'
            """
        ).fetchone()
        remaining_prop = conn.execute("SELECT COUNT(*) FROM prop_lines WHERE id = 9011").fetchone()[0]

    assert result["deleted_dnp_prop_lines"] == 1
    assert availability["did_not_play"] == 1
    assert availability["is_active"] == 0
    assert "day-to-day" in str(availability["status_reason"]).lower()
    assert remaining_prop == 0


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


def test_espn_scoreboard_uses_local_game_date_for_late_utc_tip(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.espn_history.fetch_scoreboard",
        lambda season, force_refresh=False, selected_date=None: {
            "events": [
                {
                    "id": "777003",
                    "date": "2026-06-03T02:00:00Z",
                    "competitions": [
                        {
                            "status": {"type": {"name": "STATUS_FINAL", "state": "post", "completed": True}},
                            "competitors": [
                                {"homeAway": "home", "score": "95", "team": {"abbreviation": "GS", "displayName": "Golden State Valkyries"}},
                                {"homeAway": "away", "score": "77", "team": {"abbreviation": "POR", "displayName": "Portland Fire"}},
                            ],
                        }
                    ],
                }
            ]
        },
    )

    with connect() as conn:
        result = import_espn_scoreboard(conn, 2026, selected_date="2026-06-03")
        game = conn.execute("SELECT * FROM games WHERE id = 777003").fetchone()

    assert result["inserted_games"] == 1
    assert game["game_date"] == "2026-06-02"
    assert game["status"] == "final"


def test_espn_scoreboard_uses_cached_summary_for_final_possessions(monkeypatch) -> None:
    monkeypatch.setattr(
        "backend.app.espn_history.fetch_scoreboard",
        lambda season, force_refresh=False, selected_date=None: {
            "events": [
                {
                    "id": "777004",
                    "date": "2026-06-03T23:00:00Z",
                    "competitions": [
                        {
                            "status": {"type": {"name": "STATUS_FINAL", "state": "post", "completed": True}},
                            "competitors": [
                                {"homeAway": "home", "score": "95", "team": {"abbreviation": "CHI", "displayName": "Chicago Sky"}},
                                {"homeAway": "away", "score": "90", "team": {"abbreviation": "SEA", "displayName": "Seattle Storm"}},
                            ],
                        }
                    ],
                }
            ]
        },
    )
    cache_module.write_json_cache(
        "espn_wnba_summary_777004.json",
        {
            "boxscore": {
                "teams": [
                    {
                        "team": {"abbreviation": "SEA"},
                        "statistics": [
                            {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "36-78", "label": "FG"},
                            {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "14-23", "label": "FT"},
                            {"name": "offensiveRebounds", "displayValue": "16", "abbreviation": "OR"},
                            {"name": "totalTurnovers", "displayValue": "16", "abbreviation": "ToTO"},
                        ],
                    },
                    {
                        "team": {"abbreviation": "CHI"},
                        "statistics": [
                            {"name": "fieldGoalsMade-fieldGoalsAttempted", "displayValue": "35-74", "label": "FG"},
                            {"name": "freeThrowsMade-freeThrowsAttempted", "displayValue": "14-19", "label": "FT"},
                            {"name": "offensiveRebounds", "displayValue": "6", "abbreviation": "OR"},
                            {"name": "turnovers", "displayValue": "13", "abbreviation": "TO"},
                            {"name": "teamTurnovers", "displayValue": "1", "abbreviation": "TTO"},
                        ],
                    },
                ]
            }
        },
    )

    with connect() as conn:
        home_team_id = ensure_team(conn, "CHI")
        away_team_id = ensure_team(conn, "SEA")
        result = import_espn_scoreboard(conn, 2026, selected_date="2026-06-03")
        rows = conn.execute(
            "SELECT team_id, possessions FROM team_game_results WHERE game_id = 777004 ORDER BY team_id"
        ).fetchall()

    assert result["inserted_team_game_results"] == 2
    assert {int(row["team_id"]): float(row["possessions"]) for row in rows} == {
        int(away_team_id): 88.12,
        int(home_team_id): 90.36,
    }


def test_default_espn_daily_dates_include_previous_local_day() -> None:
    dates = main_module._default_espn_daily_dates(today_local=datetime(2026, 6, 4).date())

    assert dates == ["2026-06-03", "2026-06-04"]


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


def test_game_date_from_start_time_uses_local_timezone() -> None:
    assert espn_history_module._game_date_from_start_time("2026-06-03T02:00:00Z") == "2026-06-02"
    assert espn_history_module._game_date_from_start_time("2026-06-03T23:30:00Z") == "2026-06-03"


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


def test_line_discrepancies_use_canonical_game_matchup_labels() -> None:
    load_test_history()
    with connect() as conn:
        start_time = "2026-05-08T19:30:00Z"
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (2099, '2026-05-08', ?, 10, 3, 'scheduled', 1, 1, -3.5, 163.5)
            """,
            (start_time,),
        )
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("test", "evt-stale", 2099, "2026-05-08", start_time, "Phoenix Mercury", "Seattle Storm", "dk", "DraftKings", "player_points", "points", "Test Player", "over", 18.5, -110, "now"),
                ("test", "evt-stale", 2099, "2026-05-08", start_time, "Phoenix Mercury", "Seattle Storm", "fd", "FanDuel", "player_points", "points", "Test Player", "over", 19.5, 105, "now"),
            ],
        )

        rows = line_discrepancies(conn, 2099)

    assert rows[0]["matchup"] == "CON at NY"


def test_line_discrepancies_payload_includes_recent_and_h2h_lines() -> None:
    load_test_history()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at, provider_player_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("test", "evt-history", 2010, "2026-05-08", "2026-05-08T19:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Breanna Stewart", "over", 18.5, -110, "now", 1001),
                ("test", "evt-history", 2010, "2026-05-08", "2026-05-08T19:30:00Z", "New York Liberty", "Connecticut Sun", "fd", "FanDuel", "player_points", "points", "Breanna Stewart", "over", 19.5, 105, "now", 1001),
            ],
        )

        rows = main_module._line_discrepancies_payload(conn, 2010)

    assert rows
    assert rows[0]["recent_values"]
    assert isinstance(rows[0]["recent_values"][0], float)
    assert "h2h_values" in rows[0]
    assert "h2h_opponent" in rows[0]


def test_value_board_filters_low_confidence_unless_edge_is_high() -> None:
    load_test_history()
    with connect() as conn:
        start_time = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (9910, '2026-05-23', ?, 10, 3, 'scheduled', 2, 2, -2.5, 161.5)
            """,
            (start_time,),
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9911, 9910, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9912, 9910, 1002, 'DraftKings', 'assists', 3.5, -110, -110, ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9911, 'adaptive-context-v1', ?, 23.0, 'over', 0.55, 0.52, 0.08, 0.03, 'low', 'test')
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9912, 'adaptive-context-v1', ?, 5.0, 'over', 0.61, 0.52, 0.14, 0.07, 'low', 'test')
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        rows = main_module._value_board_payload(conn, 9910)

    player_market = {(str(row["player"]), str(row["market"])) for row in rows}
    assert ("Breanna Stewart", "points") in player_market
    assert ("Sonia Citron", "assists") in player_market


def test_value_board_includes_strong_positive_ev_low_confidence_combo_props() -> None:
    load_test_history()
    with connect() as conn:
        start_time = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (9915, '2026-05-23', ?, 10, 3, 'scheduled', 2, 2, -2.5, 161.5)
            """,
            (start_time,),
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9916, 9915, 1001, 'DraftKings', 'points_assists', 22.5, -110, -110, ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9916, 'adaptive-context-v1', ?, 20.8, 'under', 0.57, 0.52, 0.042, 0.078, 'low', 'test')
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )

        rows = main_module._value_board_payload(conn, 9915)

    player_market = {(str(row["player"]), str(row["market"])) for row in rows}
    assert ("Breanna Stewart", "points_assists") in player_market


def test_value_board_preserves_prop_line_id() -> None:
    load_test_history()
    with connect() as conn:
        start_time = (datetime.now(timezone.utc) + timedelta(hours=3)).isoformat()
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (9913, '2026-05-23', ?, 10, 3, 'scheduled', 2, 2, -2.5, 161.5)
            """,
            (start_time,),
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9914, 9913, 1001, 'DraftKings', 'points', 20.5, -110, -110, ?)
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9914, 'adaptive-context-v1', ?, 23.0, 'over', 0.55, 0.52, 0.08, 0.03, 'medium', 'test')
            """,
            (datetime.now(timezone.utc).isoformat(),),
        )

        rows = main_module._value_board_payload(conn, 9913, include_filtered_only=False)

    assert len(rows) == 1
    assert rows[0]["prop_line_id"] == 9914
    assert rows[0]["id"] != rows[0]["prop_line_id"]


def test_value_board_excludes_settled_props() -> None:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.execute(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            (2101, "Settled Value Board Player", 10, "G", "starter"),
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'final', 2, 2, ?, ?)",
            (21001, "2026-06-04", "2026-06-04T19:00:00Z", 10, 3, -2.5, 158.5),
        )
        conn.execute(
            "INSERT INTO team_game_results (team_id, game_id, is_home, points, opponent_points, possessions, closing_spread, closing_total, ats_result, total_result) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (10, 21001, 1, 84, 79, 79.0, -2.5, 158.5, "cover", "over"),
        )
        conn.execute(
            "INSERT INTO player_game_stats (player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (2101, 21001, 35, 27, 5, 6, 2, 1, 0, 2),
        )
        conn.execute(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (210011, 21001, 2101, "DraftKings", "points", 24.5, -110, -110, now.isoformat()),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (210012, 210011, MODEL_VERSION, now.isoformat(), 26.1, "over", 0.58, 0.52, 0.06, 0.03, "medium", "settled-row-test"),
        )

        settled = settle_completed_props(conn)
        rows = main_module._value_board_payload(conn, game_id=21001, include_filtered_only=False)

    assert settled["settled"] == 1
    assert rows == []


def test_value_board_hides_props_for_unavailable_players() -> None:
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.executemany(
            "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
            [
                (2201, "Unavailable Value Board Player", 10, "G", "starter"),
                (2202, "Available Value Board Player", 3, "G", "starter"),
            ],
        )
        conn.execute(
            "INSERT INTO games (id, game_date, start_time, home_team_id, away_team_id, status, rest_days_home, rest_days_away, spread_home, game_total) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, ?, ?)",
            (22001, "2026-06-06", (now + timedelta(hours=4)).isoformat(), 10, 3, -2.5, 160.5),
        )
        conn.executemany(
            "INSERT INTO prop_lines (id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                (220011, 22001, 2201, "DraftKings", "points", 18.5, -110, -110, now.isoformat()),
                (220012, 22001, 2202, "DraftKings", "points", 14.5, -110, -110, now.isoformat()),
            ],
        )
        conn.executemany(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (2200111, 220011, MODEL_VERSION, now.isoformat(), 20.0, "over", 0.58, 0.52, 0.06, 0.03, "medium", "should hide"),
                (2200121, 220012, MODEL_VERSION, now.isoformat(), 16.0, "over", 0.58, 0.52, 0.06, 0.03, "medium", "should show"),
            ],
        )
        conn.execute(
            "INSERT INTO injuries (player_id, status, note, captured_at) VALUES (?, ?, ?, ?)",
            (2201, "OUT", "rest", now.isoformat()),
        )

        rows = main_module._value_board_payload(conn, game_id=22001, include_filtered_only=False)

    assert [row["player"] for row in rows] == ["Available Value Board Player"]


def test_gems_keep_one_direction_per_player_market_game(monkeypatch) -> None:
    with connect() as conn:
        monkeypatch.setattr(
            main_module,
            "_value_board_payload",
            lambda _conn: [
                {
                    "id": 1,
                    "prop_line_id": 1,
                    "game_id": 401856946,
                    "player_id": 2566106,
                    "player": "Dearica Hamby",
                    "market": "threes",
                    "line": 0.5,
                    "recommended_side": "under",
                    "edge": 0.11,
                    "expected_value": 0.26,
                    "confidence": "medium",
                },
                {
                    "id": 2,
                    "prop_line_id": 2,
                    "game_id": 401856946,
                    "player_id": 2566106,
                    "player": "Dearica Hamby",
                    "market": "threes",
                    "line": 1.5,
                    "recommended_side": "over",
                    "edge": 0.10,
                    "expected_value": 0.24,
                    "confidence": "medium",
                },
                {
                    "id": 3,
                    "prop_line_id": 3,
                    "game_id": 401856946,
                    "player_id": 2566106,
                    "player": "Dearica Hamby",
                    "market": "threes",
                    "line": 2.5,
                    "recommended_side": "under",
                    "edge": 0.08,
                    "expected_value": 0.10,
                    "confidence": "medium",
                },
            ],
        )
        monkeypatch.setattr(
            main_module,
            "line_discrepancies",
            lambda _conn: [
                {"game_id": 401856946, "player_name": "Dearica Hamby", "market": "threes", "side": "under", "line_gap": 1.0, "price_gap": 20},
                {"game_id": 401856946, "player_name": "Dearica Hamby", "market": "threes", "side": "over", "line_gap": 1.0, "price_gap": 20},
            ],
        )
        gems = main_module._build_current_gems(conn, "balanced")

    assert gems
    assert gems[0]["side"] == "under"
    assert all(item["side"] == "under" for item in gems)


def test_gems_preserve_prop_line_id(monkeypatch) -> None:
    with connect() as conn:
        monkeypatch.setattr(
            main_module,
            "_value_board_payload",
            lambda _conn: [
                {
                    "id": 7,
                    "prop_line_id": 9007,
                    "game_id": 401856946,
                    "player_id": 2566106,
                    "player": "Dearica Hamby",
                    "market": "threes",
                    "line": 2.5,
                    "recommended_side": "under",
                    "edge": 0.11,
                    "expected_value": 0.26,
                    "confidence": "medium",
                },
            ],
        )
        monkeypatch.setattr(
            main_module,
            "line_discrepancies",
            lambda _conn: [
                {
                    "game_id": 401856946,
                    "player_name": "Dearica Hamby",
                    "market": "threes",
                    "side": "under",
                    "line_gap": 1.0,
                    "price_gap": 20,
                },
            ],
        )
        gems = main_module._build_current_gems(conn, "balanced")

    assert gems
    assert gems[0]["prop_line_id"] == 9007


def test_coalesce_matchup_games_handles_sqlite_rows_with_moneylines() -> None:
    start_time = datetime(2026, 6, 2, 23, 0, tzinfo=timezone.utc).isoformat()
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, home_moneyline, away_moneyline
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?)
            """,
            (9001, "2026-06-02", start_time, 10, 3, 2, 2, -4.5, 161.5, None, None),
        )
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, home_moneyline, away_moneyline
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', ?, ?, ?, ?, ?, ?)
            """,
            (9002, "2026-06-02", start_time, 10, 3, 2, 2, -4.5, 161.5, -175, 145),
        )
        games = conn.execute(
            """
            SELECT
                id,
                game_date,
                start_time,
                home_team_id,
                away_team_id,
                rest_days_home,
                rest_days_away,
                spread_home,
                game_total,
                home_moneyline,
                away_moneyline
            FROM games
            WHERE id IN (9001, 9002)
            ORDER BY id
            """
        ).fetchall()

    groups = main_module._coalesce_matchup_games(games)

    assert len(groups) == 1
    game, game_ids = groups[0]
    assert game["id"] == 9002
    assert game["home_moneyline"] == -175
    assert game["away_moneyline"] == 145
    assert game_ids == [9001, 9002]


def test_watchlist_snapshot_and_settlement_sync(monkeypatch) -> None:
    load_test_history()
    now = datetime.now(timezone.utc)
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (9920, '2026-05-23', ?, 10, 3, 'scheduled', 2, 2, -2.5, 161.5)
            """,
            ((now + timedelta(hours=3)).isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO prop_lines (
                id, game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
            ) VALUES (9921, 9920, 1001, 'DraftKings', 'assists', 6.5, -110, -110, ?)
            """,
            (now.isoformat(),),
        )
        conn.execute(
            """
            INSERT INTO prop_predictions (
                id, prop_line_id, model_version, prediction_time, projection, recommended_side,
                model_probability, implied_probability, edge, expected_value, confidence, reason
            ) VALUES (9922, 9921, 'adaptive-context-v1', ?, 6.0, 'under', 0.57, 0.50, 0.06, 0.03, 'low', 'watch-test')
            """,
            (now.isoformat(),),
        )

        monkeypatch.setattr(main_module, "_is_active_game_time", lambda _start_time: True)
        snap = main_module._snapshot_watchlist(conn, "2026-05-23")

        assert snap["tracked"] >= 1
        snapshot_id = int(snap["snapshot_id"])
        item = conn.execute(
            "SELECT * FROM watchlist_snapshot_items WHERE snapshot_id = ? AND prop_line_id = 9921",
            (snapshot_id,),
        ).fetchone()
        assert item is not None
        assert item["is_settled"] == 0

        conn.execute(
            """
            INSERT INTO settled_props (
                prop_line_id, actual_result, winning_side, margin, player_minutes, game_margin, team_margin,
                team_spread, blowout_result, blowout_threshold, settled_at
            ) VALUES (9921, 18.0, 'under', 2.5, 31.0, 4.0, 4.0, -2.5, 'no', 15.0, ?)
            """,
            (now.isoformat(),),
        )
        settled = main_module._sync_watchlist_snapshot_settlements(conn, snapshot_id=snapshot_id)
        refreshed = conn.execute("SELECT * FROM watchlist_snapshots WHERE id = ?", (snapshot_id,)).fetchone()

    assert settled["settled_count"] >= 1
    assert settled["wins_count"] >= 1
    assert refreshed["settled_count"] >= 1
    assert refreshed["wins_count"] >= 1
