from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone
import json
from types import SimpleNamespace

import pytest
from fastapi import Response
from fastapi.responses import JSONResponse
from starlette.requests import Request

from backend.app import covers_import as covers_import_module
from backend.app import cache as cache_module
from backend.app import espn_history as espn_history_module
from backend.app import projections as projections_module
from backend.app import rotowire_import as rotowire_import_module
from backend.app.bootstrap import ensure_teams, normalize_team_abbreviation
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
from backend.app.espn_history import import_espn_player_boxscores, import_espn_scoreboard
from backend.app.game_prediction_tracking import save_game_prediction, settle_completed_game_predictions
from backend.app.game_predictions import evaluate_game_residual_models, project_game
from backend.app.history_import import determine_ats_result
from backend.app.main import app, import_espn_history as import_espn_history_endpoint, model_performance
from backend.app import main as main_module
from backend.app.odds import american_to_implied_probability, expected_value
from backend.app.odds_import import (
    RAW_CACHE_NAME,
    SyncPropLinesResult,
    _merge_event_cache,
    import_the_odds_api_props,
    line_discrepancies,
    sync_prop_lines_from_sportsbook,
)
from backend.app.player_prop_model import (
    FEATURE_NAMES,
    FEATURE_INDEX,
    FeatureSnapshot,
    RidgeModel,
    _classify_minutes_role,
    _historical_training_features,
    _injury_adjustment_for_prop,
    _player_archetype_profile,
    _project_minutes,
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


@pytest.mark.anyio
async def test_app_response_cache_serves_stale_payload_when_db_is_locked(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(cache_module, "get_cache_dir", lambda: tmp_path)

    async def _receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "GET",
            "path": "/api/watchlist",
            "raw_path": b"/api/watchlist",
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
    assert second.headers.get("x-app-cache") == "STALE"
    assert json.loads(second.body.decode("utf-8")) == [{"id": 1, "player_name": "Cached"}]


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
    monkeypatch.setattr(main_module, "_configured_api_key", lambda: None)
    monkeypatch.setattr(main_module, "_bootstrap_admin_configured", lambda: False)
    monkeypatch.setattr(main_module, "_is_dev_env", lambda: True)

    main_module.on_startup()

    assert calls == ["init_db", "ensure_teams", "prewarm", "invalidate", "publish"]


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
    calls: list[bool] = []

    class DummyConn:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return None

    monkeypatch.setattr(main_module, "connect", lambda: DummyConn())
    monkeypatch.setattr(
        main_module,
        "rebuild_predictions",
        lambda conn, game_ids=None, refresh_models=True: calls.append(refresh_models) or [],
    )
    monkeypatch.setattr(main_module, "settle_completed_props", lambda conn: {"settled": 0})
    monkeypatch.setattr(main_module, "settle_completed_game_predictions", lambda conn: {"settled": 0})
    monkeypatch.setattr(main_module, "_snapshot_watchlist", lambda conn, snapshot_date: None)

    response = Response()
    result = main_module.recalculate(response)

    assert result == {"predictions": 0, "settled": 0, "game_settled": 0}
    assert calls == [False]
    assert response.headers["Deprecation"] == "true"
    assert response.headers["X-Legacy-Endpoint"] == "/api/recalculate"


def test_read_cache_invalidation_clears_new_cache_keys(monkeypatch) -> None:
    deleted: list[str] = []
    monkeypatch.setattr(main_module, "delete_json_cache", lambda name: deleted.append(name) or True)

    main_module._invalidate_read_caches()

    assert main_module.MODEL_PERFORMANCE_CACHE_NAME in deleted
    assert main_module.MODEL_RUNS_CACHE_NAME in deleted
    assert main_module.ROSTER_CACHE_NAME in deleted


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
    assert main_module.VALUE_BOARD_CACHE_NAME in deleted


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


def test_missing_espn_scores_payload_returns_past_scheduled_game() -> None:
    with connect() as conn:
        team_home = int(conn.execute("SELECT id FROM teams WHERE abbreviation = 'PHX'").fetchone()["id"])
        team_away = int(conn.execute("SELECT id FROM teams WHERE abbreviation = 'LV'").fetchone()["id"])
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (?, ?, ?, ?, ?, 'scheduled', 2, 2, NULL, NULL, ?)
            """,
            (990001, "2026-05-01", "2026-05-01T23:00:00Z", team_home, team_away, 990001),
        )
        payload = main_module._missing_espn_scores_payload(conn, limit=30)
    assert payload["count"] >= 1
    assert "2026-05-01" in payload["dates"]


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


def test_train_minutes_model_returns_model_with_history() -> None:
    load_test_history()
    clear_model_cache()
    with connect() as conn:
        conn.commit()
        model = train_minutes_model(conn)

    assert model is not None
    assert model.rows > 0


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
    assert injury["usage_multiplier"] == pytest.approx(1.1125)


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
            ) VALUES (8802, 8801, 'adaptive-context-v1', ?, 21.7, 'over', 0.58, 0.52, 0.06, 0.03, 'medium', 'validation-test')
            """,
            (datetime.now(timezone.utc).isoformat(),),
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
    assert {row["model_version"] for row in rows} == {"adaptive-context-v1", "component-pregame-v2"}
    assert result["metrics"]["points"]["settled_rows"] >= 1
    assert "side_accuracy" in result["metrics"]["points"]
    assert "residual_rows" in result["metrics"]["points"]
    assert result["metrics"]["overall"]["settled_rows"] >= 1
    assert "residual_rows" in result["metrics"]["overall"]
    assert result["metrics"]["game_ats"]["rows"] >= 1
    assert result["metrics"]["game_total"]["rows"] >= 1
    assert "baseline_mae" in result["metrics"]["game_ats"]
    assert "mae_improvement" in result["metrics"]["game_total"]


def test_run_parameter_tuning_returns_ranked_candidates() -> None:
    load_test_history()
    with connect() as conn:
        result = run_parameter_tuning(
            conn,
            ridge_penalties=[0.75, 1.25],
            market_weight_scales=[1.0],
            player_weight_scales=[1.0],
            stabilization_scales=[0.9],
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

    assert features[FEATURE_INDEX["pace_factor"]] == 1.05
    assert features[FEATURE_INDEX["opponent_factor"]] == 0.94
    assert features[FEATURE_INDEX["common_opponent_factor"]] == 1.03
    assert features[FEATURE_INDEX["h2h_factor"]] == 0.97
    assert features[FEATURE_INDEX["blowout_minutes_delta"]] == 2.5


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
    assert projection.recommended_side == "over"


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

    assert result["target_game_ids"] == [9910]
    assert result["synced_props"] >= 1
    assert active_prop_lines >= 1
    assert stale_prop_lines == 0


def test_repair_current_slate_props_rebuilds_only_changed_prop_lines(monkeypatch) -> None:
    rebuild_calls: list[tuple[list[int] | None, list[int] | None]] = []

    def fake_sync(conn, **kwargs):
        assert kwargs["game_ids"] == [9910]
        assert kwargs["include_change_details"] is True
        assert kwargs["rebuild_predictions_after"] is False
        return SyncPropLinesResult(
            synced_props=2,
            changed_prop_line_ids=[501, 502],
            touched_game_ids=[9910],
        )

    def fake_rebuild(conn, game_ids=None, prop_line_ids=None):
        rebuild_calls.append(
            (
                list(game_ids) if game_ids is not None else None,
                list(prop_line_ids) if prop_line_ids is not None else None,
            )
        )
        return []

    monkeypatch.setattr(main_module, "_active_slate_game_ids", lambda conn: [9910])
    monkeypatch.setattr(main_module, "sync_prop_lines_from_sportsbook", fake_sync)
    monkeypatch.setattr(main_module, "rebuild_predictions", fake_rebuild)
    monkeypatch.setattr(main_module, "_snapshot_watchlist", lambda conn, slate_date: None)

    with connect() as conn:
        result = main_module._repair_current_slate_props(conn)

    assert rebuild_calls == [([9910], [501, 502])]
    assert result["changed_prop_line_ids"] == [501, 502]
    assert result["rebuilt_predictions"] == 0


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

    assert metrics["game_ats"]["rows"] == 20
    assert metrics["game_total"]["rows"] == 20
    assert metrics["game_ats"]["baseline_mae"] is not None
    assert metrics["game_total"]["baseline_mae"] is not None
    assert metrics["game_ats"]["mae"] <= metrics["game_ats"]["baseline_mae"]
    assert metrics["game_total"]["mae"] <= metrics["game_total"]["baseline_mae"]
    assert metrics["game_overall"]["rows"] == 40


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
            "SELECT * FROM prop_predictions WHERE prop_line_id = 9941 AND model_version = ?",
            (MODEL_VERSION,),
        ).fetchone()
        old_watch_row = conn.execute(
            "SELECT 1 FROM watchlist_snapshot_items WHERE prediction_id = 9942",
        ).fetchone()

    assert projections
    assert replacement is not None
    assert replacement["id"] != 9942
    assert old_watch_row is None


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
    performance = main_module.watchlist_performance()
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
    assert rows[0][13] == 13.5
    assert rows[0][14] == 102
    assert rows[1][12] == "under"
    assert rows[1][14] == -130


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
        lambda conn, rows, games=None: {"events": 1, "imported": 1, "captured_at": "2026-05-22T20:00:00+00:00"},
    )
    monkeypatch.setattr(covers_import_module, "sync_prop_lines_from_sportsbook", lambda conn: 0)
    with connect() as conn:
        result = covers_import_module.import_covers_props(conn, force_refresh=True)

    assert result["status"] == "loaded_from_cache"
    assert result["source"] == "cache"
    assert result["imported"] == 1


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

    def fake_rebuild_predictions(conn, game_ids=None, refresh_models=False):
        rebuild_calls.append(list(game_ids) if game_ids is not None else None)
        return []

    monkeypatch.setattr("backend.app.odds_import.rebuild_predictions", fake_rebuild_predictions)

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


def test_gems_keep_one_direction_per_player_market_game(monkeypatch) -> None:
    with connect() as conn:
        monkeypatch.setattr(
            main_module,
            "_value_board_payload",
            lambda _conn: [
                {
                    "id": 1,
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
