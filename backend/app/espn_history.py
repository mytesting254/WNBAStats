from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .bootstrap import ensure_team, ensure_teams
from .cache import read_json_cache, write_json_cache


BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"
ESPN_TEAM_ALIASES = {
    "CONN": "CON",
    "CON": "CON",
    "WSH": "WSH",
    "WSHINGTON": "WSH",
}


def fetch_scoreboard(season: int, force_refresh: bool = False, selected_date: str | None = None) -> dict[str, Any]:
    dates_param = _scoreboard_dates_param(season, selected_date)
    cache_name = f"espn_wnba_scoreboard_{dates_param}.json"
    if not force_refresh:
        cached = read_json_cache(cache_name)
        if isinstance(cached, dict):
            return cached

    params = urlencode({"dates": dates_param, "limit": 500})
    request = Request(f"{BASE_URL}?{params}", headers={"User-Agent": "WNBAStats/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    payload["cached_at"] = datetime.now(timezone.utc).isoformat()
    payload["cached_from"] = "espn_scoreboard"
    write_json_cache(cache_name, payload)
    return payload


def fetch_summary(event_id: int, force_refresh: bool = False) -> dict[str, Any]:
    cache_name = f"espn_wnba_summary_{event_id}.json"
    if not force_refresh:
        cached = read_json_cache(cache_name)
        if isinstance(cached, dict):
            return cached

    request = Request(f"{SUMMARY_URL}?{urlencode({'event': str(event_id)})}", headers={"User-Agent": "WNBAStats/1.0"})
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    payload["cached_at"] = datetime.now(timezone.utc).isoformat()
    payload["cached_from"] = "espn_summary"
    write_json_cache(cache_name, payload)
    return payload


def import_espn_scoreboard(
    conn: sqlite3.Connection,
    season: int,
    force_refresh: bool = False,
    selected_date: str | None = None,
) -> dict:
    ensure_teams(conn)
    payload = fetch_scoreboard(season, force_refresh=force_refresh, selected_date=selected_date)
    inserted_games = 0
    inserted_results = 0

    for event in payload.get("events", []):
        competition = _competition(event)
        if not competition:
            continue

        competitors = competition.get("competitors", [])
        home = _competitor(competitors, "home")
        away = _competitor(competitors, "away")
        if not home or not away:
            continue

        home_team_id = _team_id(conn, home)
        away_team_id = _team_id(conn, away)
        if not home_team_id or not away_team_id:
            continue

        start_time = str(event.get("date") or competition.get("date") or "")
        game_date = start_time[:10]
        espn_event_id = int(event["id"])
        game_id = _local_game_id(conn, game_date, home_team_id, away_team_id) or espn_event_id
        status = _status(competition)
        home_score = _parse_score(home)
        away_score = _parse_score(away)
        total = float(home_score + away_score) if home_score is not None and away_score is not None else None

        conn.execute(
            """
            INSERT INTO games (
                id, game_date, start_time, home_team_id, away_team_id, status,
                rest_days_home, rest_days_away, spread_home, game_total, espn_event_id
            ) VALUES (?, ?, ?, ?, ?, ?, 2, 2, NULL, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                game_date = excluded.game_date,
                start_time = excluded.start_time,
                home_team_id = excluded.home_team_id,
                away_team_id = excluded.away_team_id,
                status = excluded.status,
                game_total = COALESCE(excluded.game_total, games.game_total),
                espn_event_id = excluded.espn_event_id
            """,
            (game_id, game_date, start_time, home_team_id, away_team_id, status, total, espn_event_id),
        )
        if status == "final" and home_score is not None and away_score is not None:
            conn.execute("DELETE FROM team_game_results WHERE game_id = ?", (game_id,))
            conn.executemany(
                """
                INSERT INTO team_game_results (
                    team_id, game_id, is_home, points, opponent_points, possessions,
                    closing_spread, closing_total, ats_result, total_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (home_team_id, game_id, 1, home_score, away_score, 78.0, 0.0, total, "push", "push"),
                    (away_team_id, game_id, 0, away_score, home_score, 78.0, 0.0, total, "push", "push"),
                ],
            )
            inserted_results += 2
        inserted_games += 1

    conn.commit()
    return {
        "season": season,
        "selected_date": selected_date,
        "inserted_games": inserted_games,
        "inserted_team_game_results": inserted_results,
        "source": "espn_scoreboard",
    }


def import_espn_player_boxscores(
    conn: sqlite3.Connection,
    season: int,
    force_refresh: bool = False,
    max_games: int | None = None,
    missing_only: bool = False,
    selected_date: str | None = None,
    max_workers: int = 8,
) -> dict:
    ensure_teams(conn)
    date_filter = "AND game_date = ?" if selected_date else "AND game_date >= ? AND game_date < ?"
    date_params = (selected_date,) if selected_date else (f"{season}-01-01", f"{season + 1}-01-01")
    missing_filter = (
        """
          AND NOT EXISTS (
              SELECT 1
              FROM player_game_stats stats
              WHERE stats.game_id = games.id
          )
        """
        if missing_only
        else ""
    )
    games = conn.execute(
        f"""
        SELECT id
             , COALESCE(espn_event_id, id) AS summary_event_id
        FROM games
        WHERE status = 'final'
          {date_filter}
          {missing_filter}
        ORDER BY game_date DESC, start_time DESC
        """,
        date_params,
    ).fetchall()
    if max_games is not None:
        games = games[:max_games]

    inserted_stats = 0
    inserted_players = 0
    skipped_games = 0

    def fetch_game(game) -> tuple[int, int, dict[str, Any] | None]:
        game_id = int(game["id"])
        summary_event_id = int(game["summary_event_id"])
        try:
            return game_id, summary_event_id, fetch_summary(summary_event_id, force_refresh=force_refresh)
        except Exception:
            return game_id, summary_event_id, None

    fetched_games: list[tuple[int, int, dict[str, Any] | None]] = []
    workers = max(1, min(max_workers, len(games) or 1))
    game_order = {int(game["id"]): index for index, game in enumerate(games)}
    if workers == 1:
        fetched_games = [fetch_game(game) for game in games]
    else:
        with ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [executor.submit(fetch_game, game) for game in games]
            for future in as_completed(futures):
                fetched_games.append(future.result())
        fetched_games.sort(key=lambda item: game_order[item[0]])

    for game_id, _summary_event_id, payload in fetched_games:
        if payload is None:
            skipped_games += 1
            continue

        player_rows = _player_stat_rows(conn, game_id, payload)
        if not player_rows:
            skipped_games += 1
            continue

        conn.execute("DELETE FROM player_game_stats WHERE game_id = ?", (game_id,))
        for player in player_rows["players"]:
            existing = conn.execute("SELECT id FROM players WHERE id = ?", (player["id"],)).fetchone()
            if existing:
                conn.execute(
                    """
                    UPDATE players
                    SET full_name = ?, team_id = ?, position = ?, rotation_role = ?
                    WHERE id = ?
                    """,
                    (player["full_name"], player["team_id"], player["position"], player["rotation_role"], player["id"]),
                )
            else:
                conn.execute(
                    """
                    INSERT INTO players (id, full_name, team_id, position, rotation_role)
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (player["id"], player["full_name"], player["team_id"], player["position"], player["rotation_role"]),
                )
                inserted_players += 1
        conn.executemany(
            """
            INSERT INTO player_game_stats (
                player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            player_rows["stats"],
        )
        inserted_stats += len(player_rows["stats"])

    conn.commit()
    return {
        "season": season,
        "selected_date": selected_date,
        "games_checked": len(games),
        "skipped_games": skipped_games,
        "inserted_players": inserted_players,
        "inserted_player_game_stats": inserted_stats,
        "missing_only": missing_only,
        "source": "espn_summary",
    }


def _scoreboard_dates_param(season: int, selected_date: str | None = None) -> str:
    if not selected_date:
        return str(season)
    return str(selected_date).replace("-", "")


def _competition(event: dict[str, Any]) -> dict[str, Any] | None:
    competitions = event.get("competitions") or []
    return competitions[0] if competitions else None


def _is_completed(competition: dict[str, Any]) -> bool:
    status_type = (competition.get("status") or {}).get("type") or {}
    return bool(status_type.get("completed")) or str(status_type.get("name", "")).upper() in {"STATUS_FINAL", "FINAL"}


def _status(competition: dict[str, Any]) -> str:
    status_type = (competition.get("status") or {}).get("type") or {}
    name = str(status_type.get("name") or "").upper()
    state = str(status_type.get("state") or "").lower()
    if bool(status_type.get("completed")) or name in {"STATUS_FINAL", "FINAL"}:
        return "final"
    if name in {"STATUS_CANCELED", "STATUS_CANCELLED", "CANCELED", "CANCELLED"}:
        return "canceled"
    if state == "in":
        return "in_progress"
    return "scheduled"


def _competitor(competitors: list[dict[str, Any]], home_away: str) -> dict[str, Any] | None:
    for competitor in competitors:
        if competitor.get("homeAway") == home_away:
            return competitor
    return None


def _parse_score(competitor: dict[str, Any]) -> int | None:
    try:
        return int(competitor.get("score"))
    except (TypeError, ValueError):
        return None


def _team_id(conn: sqlite3.Connection, competitor: dict[str, Any]) -> int | None:
    team = competitor.get("team") or {}
    abbreviation = str(team.get("abbreviation") or "").upper()
    abbreviation = ESPN_TEAM_ALIASES.get(abbreviation, abbreviation)
    display_name = str(team.get("displayName") or team.get("shortDisplayName") or abbreviation)
    return ensure_team(conn, abbreviation or display_name)


def _local_game_id(conn: sqlite3.Connection, game_date: str, home_team_id: int, away_team_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM games
        WHERE game_date = ?
          AND home_team_id = ?
          AND away_team_id = ?
        ORDER BY
          CASE WHEN id >= 400000000 THEN 0 ELSE 1 END,
          id
        LIMIT 1
        """,
        (game_date, home_team_id, away_team_id),
    ).fetchone()
    return int(row["id"]) if row else None


def _player_stat_rows(conn: sqlite3.Connection, game_id: int, payload: dict[str, Any]) -> dict[str, list] | None:
    teams = payload.get("boxscore", {}).get("players", [])
    players = []
    stats = []
    for team_box in teams:
        team_id = ensure_team(conn, str((team_box.get("team") or {}).get("abbreviation") or ""))
        if not team_id:
            continue
        for stat_group in team_box.get("statistics", []):
            labels = stat_group.get("labels") or []
            label_index = {str(label).upper(): index for index, label in enumerate(labels)}
            for row in stat_group.get("athletes", []):
                if row.get("didNotPlay"):
                    continue
                athlete = row.get("athlete") or {}
                raw_stats = row.get("stats") or []
                player_id = _parse_int(athlete.get("id"))
                full_name = str(athlete.get("displayName") or "").strip()
                if not player_id or not full_name or not raw_stats:
                    continue
                minutes = _parse_minutes(_stat(raw_stats, label_index, "MIN"))
                if minutes <= 0:
                    continue
                threes = _made_from_attempt(_stat(raw_stats, label_index, "3PT"))
                position = str(((athlete.get("position") or {}).get("abbreviation")) or "")
                rotation_role = "starter" if row.get("starter") else "rotation"
                players.append(
                    {
                        "id": player_id,
                        "full_name": full_name,
                        "team_id": team_id,
                        "position": position,
                        "rotation_role": rotation_role,
                    }
                )
                stats.append(
                    (
                        player_id,
                        game_id,
                        minutes,
                        _parse_int(_stat(raw_stats, label_index, "PTS")) or 0,
                        _parse_int(_stat(raw_stats, label_index, "REB")) or 0,
                        _parse_int(_stat(raw_stats, label_index, "AST")) or 0,
                        threes,
                        _parse_int(_stat(raw_stats, label_index, "STL")) or 0,
                        _parse_int(_stat(raw_stats, label_index, "BLK")) or 0,
                        _parse_int(_stat(raw_stats, label_index, "TO")) or 0,
                    )
                )
    if not stats:
        return None
    return {"players": players, "stats": stats}


def _stat(raw_stats: list[Any], label_index: dict[str, int], label: str) -> str:
    index = label_index.get(label)
    if index is None or index >= len(raw_stats):
        return ""
    return str(raw_stats[index])


def _parse_int(value: Any) -> int | None:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _parse_minutes(value: str) -> float:
    value = str(value or "").strip()
    if not value:
        return 0.0
    if ":" in value:
        minutes, seconds = value.split(":", 1)
        return (_parse_int(minutes) or 0) + ((_parse_int(seconds) or 0) / 60)
    try:
        return float(value)
    except ValueError:
        return 0.0


def _made_from_attempt(value: str) -> int:
    made = str(value or "").split("-", 1)[0]
    return _parse_int(made) or 0
