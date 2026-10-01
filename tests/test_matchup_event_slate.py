from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.app import main, odds_import


EVENT = {
    "commence_time": "2026-10-02T01:00:00Z",
    "home_team": "Las Vegas Aces",
    "away_team": "Indiana Fever",
}


@pytest.mark.parametrize("events, expected", [([EVENT], [3]), ([], []), (None, [1, 2, 3])])
def test_matchups_follow_event_slate(monkeypatch, events, expected):
    games = [
        dict(id=1, start_time="2026-10-01T17:00:00Z", home_team_name="Minnesota Lynx", away_team_name="New York Liberty"),
        dict(id=2, start_time="2026-10-01T17:00:00Z", home_team_name="Las Vegas Aces", away_team_name="Indiana Fever"),
        dict(id=3, start_time="2026-10-02T01:00:00+00:00", home_team_name="Las Vegas Aces", away_team_name="Indiana Fever"),
    ]
    for game in games:
        game.update(game_date="2026-10-01", home_team_id=11 if game["id"] == 1 else 10, away_team_id=20)
        for field in ("spread_home", "game_total", "home_moneyline", "away_moneyline", "rest_days_home", "rest_days_away"):
            game[field] = None
    conn = SimpleNamespace(execute=lambda *args: SimpleNamespace(fetchall=lambda: games))
    monkeypatch.setattr(main, "_is_today_slate_game", lambda *args: True)
    monkeypatch.setattr(main, "current_odds_api_events", lambda: events)
    groups = main._scheduled_matchup_game_groups(conn)
    assert [game["id"] for game, _ in groups] == expected


def test_event_cache_preserves_empty_slate(monkeypatch):
    monkeypatch.setattr(odds_import, "read_json_cache", lambda _: {
        "cache_date": odds_import.local_today_iso(),
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "events": [],
    })
    monkeypatch.setattr(odds_import, "_fetch_json", lambda _: pytest.fail("Fresh event cache should avoid a request"))
    assert odds_import.current_odds_api_events() == []


@pytest.mark.parametrize("payload", [[EVENT], [], {"error": "unavailable"}])
def test_event_discovery_validates_and_caches_response(monkeypatch, payload):
    monkeypatch.setattr(odds_import, "read_json_cache", lambda _: None)
    monkeypatch.setattr(odds_import, "load_dotenv", lambda: None)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    monkeypatch.setattr(odds_import, "_fetch_json", lambda _: payload)
    writes = []
    monkeypatch.setattr(odds_import, "write_json_cache", lambda name, data: writes.append(data))
    result = odds_import.current_odds_api_events()
    if isinstance(payload, list):
        assert result == payload
        assert writes[0]["events"] == payload
    else:
        assert result is None
        assert writes == []


def test_event_discovery_uses_same_day_cache_on_outage(monkeypatch):
    monkeypatch.setattr(odds_import, "read_json_cache", lambda _: {
        "cache_date": odds_import.local_today_iso(),
        "captured_at": "2000-01-01T00:00:00Z",
        "events": [EVENT],
    })
    monkeypatch.setattr(odds_import, "load_dotenv", lambda: None)
    monkeypatch.setenv("ODDS_API_KEY", "test-key")
    def unavailable(_):
        raise RuntimeError("Provider unavailable")
    monkeypatch.setattr(odds_import, "_fetch_json", unavailable)
    assert odds_import.current_odds_api_events() == [EVENT]
