from backend.app.db import connect
from backend.app.game_predictions import project_game
from backend.app.odds import american_to_implied_probability, expected_value
from backend.app.odds_import import RAW_CACHE_NAME, _merge_event_cache, import_the_odds_api_props, line_discrepancies
from backend.app.seed import seed_sample_data
from backend.app.training import run_walk_forward_training


def test_american_odds_helpers() -> None:
    assert round(american_to_implied_probability(-110), 4) == 0.5238
    assert round(american_to_implied_probability(150), 4) == 0.4
    assert expected_value(0.55, -110) > 0


def test_seed_builds_ranked_predictions() -> None:
    seed_sample_data()
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT pp.*, pl.market
            FROM prop_predictions pp
            JOIN prop_lines pl ON pl.id = pp.prop_line_id
            ORDER BY pp.expected_value DESC
            """
        ).fetchall()
    assert len(rows) == 34
    assert rows[0]["recommended_side"] in {"over", "under"}
    assert rows[0]["projection"] > 0
    assert rows[0]["market"] in {"points", "rebounds", "assists", "points_rebounds_assists"}


def test_seed_builds_matchup_results() -> None:
    seed_sample_data()
    with connect() as conn:
        rows = conn.execute("SELECT * FROM team_game_results").fetchall()
        scheduled = conn.execute("SELECT * FROM games WHERE status = 'scheduled'").fetchall()
    assert len(rows) == 124
    assert len(scheduled) == 6


def test_walk_forward_training_saves_model_run() -> None:
    seed_sample_data()
    with connect() as conn:
        result = run_walk_forward_training(conn)
        rows = conn.execute("SELECT * FROM model_runs").fetchall()
    assert result["status"] == "completed"
    assert result["training_rows"] > 0
    assert len(rows) == 1


def test_game_projection_returns_picks() -> None:
    seed_sample_data()
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
    seed_sample_data()
    with connect() as conn:
        result = import_the_odds_api_props(conn)
    assert result["status"] == "missing_api_key"
    assert result["imported"] == 0


def test_odds_import_loads_saved_json_without_api_key(monkeypatch) -> None:
    monkeypatch.delenv("ODDS_API_KEY", raising=False)
    monkeypatch.delenv("THE_ODDS_API_KEY", raising=False)
    seed_sample_data()
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
    seed_sample_data()
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
    seed_sample_data()
    with connect() as conn:
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                ("test", "evt", 201, "2026-05-08", "2026-05-08T19:30:00Z", "New York Liberty", "Connecticut Sun", "dk", "DraftKings", "player_points", "points", "Test Player", "over", 18.5, -110, "now"),
                ("test", "evt", 201, "2026-05-08", "2026-05-08T19:30:00Z", "New York Liberty", "Connecticut Sun", "fd", "FanDuel", "player_points", "points", "Test Player", "over", 19.5, +105, "now"),
            ],
        )
        rows = line_discrepancies(conn, 201)
    assert rows[0]["player_name"] == "Test Player"
    assert rows[0]["line_gap"] == 1.0
    assert rows[0]["best_price"]["sportsbook"] == "FanDuel"
