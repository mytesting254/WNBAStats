from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from dotenv import load_dotenv

from .bootstrap import ensure_team, ensure_teams
from .cache import read_json_cache, write_json_cache
from .odds_import import sync_prop_lines_from_sportsbook
from .projections import rebuild_predictions


SCORES_BASE_URL = "https://api.sportsdata.io/v3/wnba/scores/json"
ODDS_BASE_URL = "https://api.sportsdata.io/v3/wnba/odds/json"
PROVIDER = "sportsdataio"

MARKET_ALIASES = {
    "points": "points",
    "player points": "points",
    "player_points": "points",
    "rebounds": "rebounds",
    "player rebounds": "rebounds",
    "player_rebounds": "rebounds",
    "assists": "assists",
    "player assists": "assists",
    "player_assists": "assists",
    "three pointers made": "threes",
    "threes": "threes",
    "3-pointers made": "threes",
    "player threes": "threes",
    "points + rebounds + assists": "points_rebounds_assists",
    "points rebounds assists": "points_rebounds_assists",
    "player points rebounds assists": "points_rebounds_assists",
}


class SportsDataIOError(RuntimeError):
    pass


def import_sportsdataio(
    conn: sqlite3.Connection,
    season: int | None = None,
    force_refresh: bool = False,
    include_boxscores: bool = True,
    include_odds: bool = True,
) -> dict:
    load_dotenv()
    api_key = os.getenv("SPORTSDATAIO_API_KEY") or os.getenv("SPORTSDATA_API_KEY")
    if not api_key:
        return {
            "status": "missing_api_key",
            "message": "Set SPORTSDATAIO_API_KEY to import SportsDataIO WNBA data.",
            "source": PROVIDER,
        }

    target_season = season or datetime.now().year
    ensure_teams(conn)
    games_result = import_games(conn, api_key, target_season, force_refresh=force_refresh)
    boxscore_result = {"games_checked": 0, "inserted_players": 0, "inserted_player_game_stats": 0, "skipped_games": 0}
    odds_result = {"imported": 0, "events": 0}

    if include_boxscores:
        boxscore_result = import_boxscores(conn, api_key, target_season, force_refresh=force_refresh)
    if include_odds:
        odds_result = import_player_props(conn, api_key, force_refresh=force_refresh)

    synced_props = sync_prop_lines_from_sportsbook(conn)
    if synced_props:
        rebuild_predictions(conn)

    return {
        "status": "imported",
        "source": PROVIDER,
        "season": target_season,
        "games": games_result,
        "boxscores": boxscore_result,
        "odds": odds_result,
        "synced_props": synced_props,
    }


def import_games(conn: sqlite3.Connection, api_key: str, season: int, force_refresh: bool = False) -> dict:
    payload = _fetch_cached(api_key, "scores", f"Games/{season}", f"sportsdataio_games_{season}.json", force_refresh)
    inserted = 0
    final_games = 0
    for game in _as_list(payload):
        home_abbr = _team_abbr(game, "Home")
        away_abbr = _team_abbr(game, "Away")
        if not home_abbr or not away_abbr:
            continue
        home_team_id = ensure_team(conn, home_abbr)
        away_team_id = ensure_team(conn, away_abbr)
        if not home_team_id or not away_team_id:
            continue
        game_id = _int(game.get("GameID") or game.get("GameId") or game.get("GameID".lower()))
        if not game_id:
            continue
        start_time = _normalize_datetime(game.get("DateTime") or game.get("DateTimeUTC") or game.get("Day"))
        game_date = _normalize_date(game.get("Day") or start_time)
        status = _status(game)
        if status == "final":
            final_games += 1
        home_score = _int(game.get("HomeTeamScore") or game.get("HomeScore"))
        away_score = _int(game.get("AwayTeamScore") or game.get("AwayScore"))
        game_total = float(home_score + away_score) if home_score is not None and away_score is not None else None
        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total
            ) VALUES (?, ?, ?, ?, ?, ?, 2, 2, NULL, ?)
            ON CONFLICT(id) DO UPDATE SET
                game_date = excluded.game_date,
                start_time = excluded.start_time,
                home_team_id = excluded.home_team_id,
                away_team_id = excluded.away_team_id,
                status = excluded.status,
                game_total = COALESCE(excluded.game_total, games.game_total)
            """,
            (game_id, game_date, start_time, home_team_id, away_team_id, status, game_total),
        )
        if status == "final" and home_score is not None and away_score is not None:
            _replace_team_results(conn, game_id, home_team_id, away_team_id, home_score, away_score, game_total)
        inserted += 1
    conn.commit()
    return {"season": season, "games": inserted, "final_games": final_games}


def import_boxscores(conn: sqlite3.Connection, api_key: str, season: int, force_refresh: bool = False) -> dict:
    games = conn.execute(
        """
        SELECT id
        FROM games
        WHERE status = 'final'
          AND game_date >= ?
          AND game_date < ?
        ORDER BY game_date DESC, start_time DESC
        """,
        (f"{season}-01-01", f"{season + 1}-01-01"),
    ).fetchall()
    inserted_players = 0
    inserted_stats = 0
    skipped_games = 0
    for row in games:
        game_id = int(row["id"])
        payload = _fetch_boxscore(api_key, game_id, force_refresh=force_refresh)
        if payload is None:
            skipped_games += 1
            continue
        player_games = _player_games(payload)
        if not player_games:
            skipped_games += 1
            continue
        conn.execute("DELETE FROM player_game_stats WHERE game_id = ?", (game_id,))
        for item in player_games:
            player_id = _int(item.get("PlayerID") or item.get("PlayerId"))
            team_abbr = str(item.get("Team") or item.get("TeamKey") or "").upper()
            name = _player_name(item)
            if not player_id or not team_abbr or not name:
                continue
            team_id = ensure_team(conn, team_abbr)
            if not team_id:
                continue
            existing = conn.execute("SELECT id FROM players WHERE id = ?", (player_id,)).fetchone()
            rotation_role = "starter" if _bool(item.get("Started") or item.get("Starter")) else "rotation"
            position = str(item.get("Position") or "")
            if existing:
                conn.execute(
                    "UPDATE players SET full_name = ?, team_id = ?, position = ?, rotation_role = ? WHERE id = ?",
                    (name, team_id, position, rotation_role, player_id),
                )
            else:
                conn.execute(
                    "INSERT INTO players (id, full_name, team_id, position, rotation_role) VALUES (?, ?, ?, ?, ?)",
                    (player_id, name, team_id, position, rotation_role),
                )
                inserted_players += 1
            minutes = _float(item.get("Minutes") or item.get("Seconds") and (_float(item.get("Seconds")) / 60)) or 0.0
            if minutes <= 0:
                continue
            conn.execute(
                """
                INSERT INTO player_game_stats (
                    player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    player_id,
                    game_id,
                    minutes,
                    _int(item.get("Points")) or 0,
                    _int(item.get("Rebounds") or item.get("TotalRebounds")) or 0,
                    _int(item.get("Assists")) or 0,
                    _int(item.get("ThreePointersMade") or item.get("ThreePointFieldGoalsMade")) or 0,
                    _int(item.get("Steals")) or 0,
                    _int(item.get("BlockedShots") or item.get("Blocks")) or 0,
                    _int(item.get("Turnovers")) or 0,
                ),
            )
            inserted_stats += 1
    conn.commit()
    return {
        "games_checked": len(games),
        "skipped_games": skipped_games,
        "inserted_players": inserted_players,
        "inserted_player_game_stats": inserted_stats,
    }


def import_player_props(conn: sqlite3.Connection, api_key: str, force_refresh: bool = False) -> dict:
    games = conn.execute(
        """
        SELECT id, game_date
        FROM games
        WHERE status = 'scheduled'
        ORDER BY start_time
        """
    ).fetchall()
    rows = []
    events = 0
    captured_at = datetime.now(timezone.utc).isoformat()
    for game in games:
        game_id = int(game["id"])
        payload = _fetch_player_props(api_key, game_id, force_refresh=force_refresh)
        if payload is None:
            continue
        events += 1
        rows.extend(_sportsbook_rows_for_game(conn, game_id, payload, captured_at))

    if rows:
        conn.execute("DELETE FROM sportsbook_prop_lines WHERE provider = ?", (PROVIDER,))
        conn.executemany(
            """
            INSERT INTO sportsbook_prop_lines (
                provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at,
                provider_player_id, provider_game_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
    conn.commit()
    return {"events": events, "imported": len(rows)}


def _fetch_boxscore(api_key: str, game_id: int, force_refresh: bool = False) -> dict[str, Any] | None:
    cache_name = f"sportsdataio_boxscore_{game_id}.json"
    for path in (f"BoxScore/{game_id}", f"BoxScores/{game_id}"):
        try:
            return _fetch_cached(api_key, "scores", path, cache_name, force_refresh)
        except SportsDataIOError:
            continue
    return None


def _fetch_player_props(api_key: str, game_id: int, force_refresh: bool = False) -> Any | None:
    cache_name = f"sportsdataio_player_props_{game_id}.json"
    for path in (f"BettingPlayerPropsByGameID/{game_id}", f"BettingPlayerPropsByGame/{game_id}"):
        try:
            return _fetch_cached(api_key, "odds", path, cache_name, force_refresh)
        except SportsDataIOError:
            continue
    return None


def _fetch_cached(api_key: str, product: str, path: str, cache_name: str, force_refresh: bool) -> Any:
    if not force_refresh:
        cached = read_json_cache(cache_name)
        if cached is not None:
            return cached
    base_url = SCORES_BASE_URL if product == "scores" else ODDS_BASE_URL
    url = f"{base_url}/{path}"
    request = Request(
        f"{url}?{urlencode({'key': api_key})}",
        headers={"Ocp-Apim-Subscription-Key": api_key, "User-Agent": "WNBAStats/1.0"},
    )
    try:
        with urlopen(request, timeout=30) as response:
            payload = json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise SportsDataIOError("SportsDataIO API key is missing access for this endpoint.") from exc
        if exc.code == 404:
            raise SportsDataIOError(f"SportsDataIO endpoint not found: {path}") from exc
        raise
    write_json_cache(cache_name, payload)
    return payload


def _replace_team_results(
    conn: sqlite3.Connection,
    game_id: int,
    home_team_id: int,
    away_team_id: int,
    home_score: int,
    away_score: int,
    game_total: float | None,
) -> None:
    total = game_total if game_total is not None else float(home_score + away_score)
    conn.execute("DELETE FROM team_game_results WHERE game_id = ?", (game_id,))
    conn.executemany(
        """
        INSERT INTO team_game_results (
            team_id, game_id, is_home, points, opponent_points, possessions,
            closing_spread, closing_total, ats_result, total_result
        ) VALUES (?, ?, ?, ?, ?, 78.0, 0.0, ?, 'push', 'push')
        """,
        [
            (home_team_id, game_id, 1, home_score, away_score, total),
            (away_team_id, game_id, 0, away_score, home_score, total),
        ],
    )


def _sportsbook_rows_for_game(conn: sqlite3.Connection, game_id: int, payload: Any, captured_at: str) -> list[tuple]:
    game = conn.execute(
        """
        SELECT g.game_date, g.start_time, home.name AS home_team, away.name AS away_team
        FROM games g
        JOIN teams home ON home.id = g.home_team_id
        JOIN teams away ON away.id = g.away_team_id
        WHERE g.id = ?
        """,
        (game_id,),
    ).fetchone()
    if not game:
        return []
    rows = []
    for market in _as_list(payload):
        market_name = _market_name(market)
        app_market = _normalize_market(market_name)
        if not app_market:
            continue
        outcomes = market.get("BettingOutcomes") or market.get("Outcomes") or []
        over_by_key: dict[tuple[int | None, str, float, str], dict] = {}
        under_by_key: dict[tuple[int | None, str, float, str], dict] = {}
        for outcome in _as_list(outcomes):
            side = str(outcome.get("BettingOutcomeType") or outcome.get("OutcomeType") or outcome.get("Type") or "").lower()
            if side not in {"over", "under"}:
                continue
            player_id = _int(outcome.get("PlayerID") or market.get("PlayerID"))
            player_name = str(outcome.get("Participant") or outcome.get("PlayerName") or market.get("PlayerName") or "").strip()
            line = _float(outcome.get("Value") or outcome.get("Line"))
            price = _int(outcome.get("PayoutAmerican") or outcome.get("AmericanOdds") or outcome.get("Odds"))
            sportsbook = _sportsbook_name(outcome)
            if not player_name or line is None or price is None:
                continue
            key = (player_id, player_name.lower(), float(line), sportsbook)
            target = over_by_key if side == "over" else under_by_key
            target[key] = {
                "player_id": player_id,
                "player_name": player_name,
                "side": side,
                "line": float(line),
                "price": int(price),
                "sportsbook": sportsbook,
                "bookmaker_key": str(outcome.get("SportsbookID") or sportsbook).lower().replace(" ", "_"),
            }
        for target in (over_by_key, under_by_key):
            for item in target.values():
                rows.append(
                    (
                        PROVIDER,
                        str(market.get("BettingEventID") or game_id),
                        game_id,
                        game["game_date"],
                        game["start_time"],
                        game["home_team"],
                        game["away_team"],
                        item["bookmaker_key"],
                        item["sportsbook"],
                        market_name,
                        app_market,
                        item["player_name"],
                        item["side"],
                        item["line"],
                        item["price"],
                        captured_at,
                        item["player_id"],
                        game_id,
                    )
                )
    return rows


def _as_list(value: Any) -> list[dict[str, Any]]:
    if isinstance(value, list):
        return [item for item in value if isinstance(item, dict)]
    if isinstance(value, dict):
        for key in ("PlayerGames", "Games", "BettingMarkets", "Markets"):
            nested = value.get(key)
            if isinstance(nested, list):
                return [item for item in nested if isinstance(item, dict)]
        return [value]
    return []


def _player_games(payload: dict[str, Any]) -> list[dict[str, Any]]:
    if isinstance(payload.get("PlayerGames"), list):
        return payload["PlayerGames"]
    for key in ("PlayerGameStats", "Players"):
        if isinstance(payload.get(key), list):
            return payload[key]
    return []


def _team_abbr(game: dict[str, Any], side: str) -> str | None:
    value = game.get(f"{side}Team") or game.get(f"{side}TeamKey") or game.get(f"{side}TeamName")
    return str(value).upper() if value else None


def _status(game: dict[str, Any]) -> str:
    value = str(game.get("Status") or "").lower()
    if value in {"final", "f/ot", "closed"}:
        return "final"
    if value in {"scheduled", "inprogress", "in progress"}:
        return "scheduled" if value == "scheduled" else "in_progress"
    return value or "scheduled"


def _player_name(item: dict[str, Any]) -> str:
    name = item.get("Name") or item.get("PlayerName")
    if name:
        return str(name).strip()
    first = str(item.get("FirstName") or "").strip()
    last = str(item.get("LastName") or "").strip()
    return f"{first} {last}".strip()


def _market_name(market: dict[str, Any]) -> str:
    return str(
        market.get("BettingBetType")
        or market.get("BettingMarketType")
        or market.get("MarketType")
        or market.get("Name")
        or market.get("Type")
        or ""
    ).strip()


def _normalize_market(value: str) -> str | None:
    normalized = value.lower().replace("_", " ").replace("-", " ")
    normalized = " ".join(normalized.split())
    return MARKET_ALIASES.get(normalized)


def _sportsbook_name(outcome: dict[str, Any]) -> str:
    sportsbook = outcome.get("SportsBook") or outcome.get("Sportsbook") or {}
    if isinstance(sportsbook, dict):
        return str(sportsbook.get("Name") or sportsbook.get("Key") or sportsbook.get("SportsbookID") or "SportsDataIO")
    return str(sportsbook or outcome.get("SportsbookName") or "SportsDataIO")


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _float(value: Any) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return str(value).lower() in {"true", "1", "yes"}


def _normalize_datetime(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    if len(text) >= 19:
        return text[:19]
    return text


def _normalize_date(value: Any) -> str:
    text = str(value or "")
    if not text:
        return ""
    return text[:10]
