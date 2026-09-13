from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .bootstrap import ensure_team, ensure_teams, normalize_team_abbreviation
from .cache import read_json_cache, write_json_cache
from .espn_playbyplay import fetch_playbyplay, upsert_player_first_half_stats
from .game_resolver import resolve_or_create_game
from .player_identity import repair_shadow_player_identities
from .timezone_utils import APP_TIMEZONE


BASE_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/scoreboard"
SUMMARY_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/summary"
LOCAL_TZ = APP_TIMEZONE
ESPN_REQUEST_HEADERS = {
    # ESPN now rejects the repo-specific UA but still serves the same JSON to
    # generic HTTP clients.
    "User-Agent": "python-requests/2.31.0",
    "Accept": "application/json,text/plain,*/*",
}


def fetch_scoreboard(season: int, force_refresh: bool = False, selected_date: str | None = None) -> dict[str, Any]:
    dates_param = _scoreboard_dates_param(season, selected_date)
    cache_name = f"espn_wnba_scoreboard_{dates_param}.json"
    if not force_refresh:
        cached = read_json_cache(cache_name)
        if isinstance(cached, dict):
            return cached

    params = urlencode({"dates": dates_param, "limit": 500})
    request = Request(f"{BASE_URL}?{params}", headers=ESPN_REQUEST_HEADERS)
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

    request = Request(f"{SUMMARY_URL}?{urlencode({'event': str(event_id)})}", headers=ESPN_REQUEST_HEADERS)
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
    updated_game_segment_results = 0

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
        if int(home_team_id) == int(away_team_id):
            continue

        start_time = str(event.get("date") or competition.get("date") or "")
        game_date = _game_date_from_start_time(start_time)
        espn_event_id = int(event["id"])
        status = _status(competition)
        home_score = _parse_score(home)
        away_score = _parse_score(away)
        total = (
            float(home_score + away_score)
            if status == "final" and home_score is not None and away_score is not None
            else None
        )
        home_team_name = str((home.get("team") or {}).get("displayName") or (home.get("team") or {}).get("shortDisplayName") or "")
        away_team_name = str((away.get("team") or {}).get("displayName") or (away.get("team") or {}).get("shortDisplayName") or "")
        resolved = resolve_or_create_game(
            conn,
            home_team=home_team_name or str((home.get("team") or {}).get("abbreviation") or ""),
            away_team=away_team_name or str((away.get("team") or {}).get("abbreviation") or ""),
            start_time=start_time,
            game_date=game_date,
            espn_event_id=espn_event_id,
            game_total=total,
        )
        game_id = resolved or espn_event_id
        summary_payload = None
        if status == "final":
            summary_payload = read_json_cache(f"espn_wnba_summary_{espn_event_id}.json")
        possessions_by_team = _summary_team_possessions(conn, summary_payload) if isinstance(summary_payload, dict) else {}

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
                game_total = CASE
                    WHEN excluded.game_total IS NOT NULL AND excluded.game_total > 0 THEN excluded.game_total
                    ELSE games.game_total
                END,
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
                    possessions_source, closing_spread, closing_total, ats_result, total_result
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                [
                    (
                        home_team_id,
                        game_id,
                        1,
                        home_score,
                        away_score,
                        possessions_by_team.get(int(home_team_id), 78.0),
                        "espn_summary" if int(home_team_id) in possessions_by_team else "fallback",
                        0.0,
                        total,
                        "push",
                        "push",
                    ),
                    (
                        away_team_id,
                        game_id,
                        0,
                        away_score,
                        home_score,
                        possessions_by_team.get(int(away_team_id), 78.0),
                        "espn_summary" if int(away_team_id) in possessions_by_team else "fallback",
                        0.0,
                        total,
                        "push",
                        "push",
                    ),
                ],
            )
            inserted_results += 2
            updated_game_segment_results += _upsert_game_segment_results(conn, game_id, summary_payload)
        inserted_games += 1

    conn.commit()
    return {
        "season": season,
        "selected_date": selected_date,
        "inserted_games": inserted_games,
        "inserted_team_game_results": inserted_results,
        "updated_game_segment_results": updated_game_segment_results,
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
    include_playbyplay: bool = True,
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
    updated_team_game_results = 0
    updated_team_game_boxscores = 0
    updated_game_segment_results = 0
    updated_player_first_half_stats = 0
    existing_player_ids = {
        int(row["id"])
        for row in conn.execute("SELECT id FROM players").fetchall()
    }

    def fetch_game(game) -> tuple[int, int, dict[str, Any] | None, dict[str, Any] | None]:
        game_id = int(game["id"])
        summary_event_id = int(game["summary_event_id"])
        try:
            summary_payload = fetch_summary(summary_event_id, force_refresh=force_refresh)
        except Exception:
            return game_id, summary_event_id, None, None
        playbyplay_payload = None
        if include_playbyplay:
            try:
                playbyplay_payload = fetch_playbyplay(summary_event_id, force_refresh=force_refresh)
            except Exception:
                playbyplay_payload = None
        return game_id, summary_event_id, summary_payload, playbyplay_payload

    fetched_games: list[tuple[int, int, dict[str, Any] | None, dict[str, Any] | None]] = []
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

    players_by_id: dict[int, dict] = {}
    team_votes_by_player: dict[int, dict[int, int]] = defaultdict(lambda: defaultdict(int))
    stat_rows: list[tuple] = []
    availability_rows: list[tuple] = []
    team_history_rows: set[tuple[int, int, int, str, float, str]] = set()
    game_ids_to_replace = []
    first_half_inputs: list[tuple[int, int, dict[str, Any], dict[str, Any] | None]] = []

    for game_id, summary_event_id, payload, playbyplay_payload in fetched_games:
        if payload is None:
            skipped_games += 1
            continue

        updated_team_game_boxscores += _upsert_team_game_boxscores(conn, game_id, payload)
        updated_team_game_results += _update_team_game_result_possessions(conn, game_id, payload)
        updated_game_segment_results += _upsert_game_segment_results(conn, game_id, payload)
        first_half_inputs.append((game_id, summary_event_id, payload, playbyplay_payload))
        player_rows = _player_stat_rows(conn, game_id, payload)
        injury_rows = _injury_availability_rows(conn, game_id, payload, player_rows)
        if not player_rows and not injury_rows:
            skipped_games += 1
            continue

        game_ids_to_replace.append(game_id)
        for player in (player_rows["players"] if player_rows else []):
            player_id = int(player["id"])
            team_id = int(player["team_id"])
            players_by_id[player_id] = player
            team_votes_by_player[player_id][team_id] += 1
            team_history_rows.add((player_id, team_id, int(game_id), "espn_boxscore", 0.95, datetime.now(timezone.utc).isoformat()))
        if player_rows:
            stat_rows.extend(player_rows["stats"])
            availability_rows.extend(player_rows["availability"])
        for player in injury_rows["players"]:
            player_id = int(player["id"])
            team_id = int(player["team_id"])
            players_by_id[player_id] = player
            team_votes_by_player[player_id][team_id] += 1
            team_history_rows.add((player_id, team_id, int(game_id), "espn_summary_injury", 0.8, datetime.now(timezone.utc).isoformat()))
        availability_rows.extend(injury_rows["availability"])

    if game_ids_to_replace:
        new_player_ids = set(players_by_id) - existing_player_ids
        inserted_players = len(new_player_ids)
        conn.executemany(
            "DELETE FROM player_game_stats WHERE game_id = ?",
            [(game_id,) for game_id in game_ids_to_replace],
        )
        conn.executemany(
            "DELETE FROM player_game_availability WHERE game_id = ? AND source = ?",
            [(game_id, "espn_boxscore") for game_id in game_ids_to_replace],
        )
        conn.executemany(
            """
            INSERT INTO players (id, full_name, team_id, position, rotation_role)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(id) DO UPDATE SET
                full_name = excluded.full_name,
                team_id = excluded.team_id,
                position = excluded.position,
                rotation_role = excluded.rotation_role
            """,
            [
                (
                    player["id"],
                    player["full_name"],
                    max(
                        team_votes_by_player.get(int(player["id"]), {int(player["team_id"]): 1}).items(),
                        key=lambda item: item[1],
                    )[0],
                    player["position"],
                    player["rotation_role"],
                )
                for player in players_by_id.values()
            ],
        )
        conn.executemany(
            """
            INSERT INTO player_game_stats (
                player_id, game_id, minutes, points, rebounds, assists, threes, steals, blocks, turnovers
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            stat_rows,
        )
        conn.executemany(
            """
            INSERT INTO player_game_availability (
                player_id, game_id, team_id, source, is_active, did_not_play, status_reason, minutes_text, observed_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id, game_id, source) DO UPDATE SET
                team_id = excluded.team_id,
                is_active = excluded.is_active,
                did_not_play = excluded.did_not_play,
                status_reason = excluded.status_reason,
                minutes_text = excluded.minutes_text,
                observed_at = excluded.observed_at
            """,
            availability_rows,
        )
        conn.executemany(
            """
            INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(player_id, game_id, source) DO UPDATE SET
                team_id = excluded.team_id,
                confidence = excluded.confidence,
                observed_at = excluded.observed_at
            """,
            sorted(team_history_rows),
        )
        for game_id, summary_event_id, payload, playbyplay_payload in first_half_inputs:
            updated_player_first_half_stats += int(
                upsert_player_first_half_stats(
                    conn,
                    game_id=game_id,
                    event_id=summary_event_id,
                    summary_payload=payload,
                    playbyplay_payload=playbyplay_payload,
                ).get("players", 0)
            )
        repair_shadow_player_identities(conn)
        inserted_stats = len(stat_rows)
        deleted_dnp_prop_lines = _delete_explicit_dnp_prop_lines(conn, game_ids_to_replace)
    else:
        deleted_dnp_prop_lines = 0

    conn.commit()
    return {
        "season": season,
        "selected_date": selected_date,
        "games_checked": len(games),
        "skipped_games": skipped_games,
        "inserted_players": inserted_players,
        "inserted_player_game_stats": inserted_stats,
        "recorded_player_game_availability": len(availability_rows),
        "updated_team_game_results": updated_team_game_results,
        "updated_team_game_boxscores": updated_team_game_boxscores,
        "updated_game_segment_results": updated_game_segment_results,
        "updated_player_first_half_stats": updated_player_first_half_stats,
        "deleted_dnp_prop_lines": deleted_dnp_prop_lines,
        "missing_only": missing_only,
        "source": "espn_summary",
    }


def backfill_espn_team_possessions(
    conn: sqlite3.Connection,
    season: int,
    force_refresh: bool = False,
    selected_date: str | None = None,
    only_placeholder: bool = True,
    max_games: int | None = None,
    max_workers: int = 8,
) -> dict[str, Any]:
    date_filter = "AND g.game_date = ?" if selected_date else "AND g.game_date >= ? AND g.game_date < ?"
    date_params = (selected_date,) if selected_date else (f"{season}-01-01", f"{season + 1}-01-01")
    placeholder_filter = (
        "AND (ABS(tgr.possessions - 78.0) < 0.0001 OR COALESCE(tgr.possessions_source, 'fallback') = 'fallback')"
        if only_placeholder
        else ""
    )
    games = conn.execute(
        f"""
        SELECT DISTINCT
            g.id,
            COALESCE(g.espn_event_id, g.id) AS summary_event_id
        FROM games g
        JOIN team_game_results tgr ON tgr.game_id = g.id
        WHERE g.status = 'final'
          {date_filter}
          {placeholder_filter}
        ORDER BY g.game_date DESC, g.start_time DESC, g.id DESC
        """,
        date_params,
    ).fetchall()
    if max_games is not None:
        games = games[:max_games]

    updated_team_game_results = 0
    updated_team_game_boxscores = 0
    updated_game_segment_results = 0
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
        updated_team_game_boxscores += _upsert_team_game_boxscores(conn, game_id, payload)
        updated = _update_team_game_result_possessions(conn, game_id, payload)
        updated_game_segment_results += _upsert_game_segment_results(conn, game_id, payload)
        updated_team_game_results += updated
        if updated <= 0:
            skipped_games += 1

    conn.commit()
    return {
        "season": season,
        "selected_date": selected_date,
        "games_checked": len(games),
        "updated_team_game_results": updated_team_game_results,
        "updated_team_game_boxscores": updated_team_game_boxscores,
        "updated_game_segment_results": updated_game_segment_results,
        "games_with_updates": updated_team_game_results // 2,
        "skipped_games": skipped_games,
        "only_placeholder": only_placeholder,
        "source": "espn_summary",
    }


def _scoreboard_dates_param(season: int, selected_date: str | None = None) -> str:
    if not selected_date:
        return str(season)
    return str(selected_date).replace("-", "")


def _game_date_from_start_time(start_time: str) -> str:
    value = str(start_time or "").strip()
    if not value:
        return ""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return value[:10]
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(LOCAL_TZ).date().isoformat()


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
    abbreviation = normalize_team_abbreviation(str(team.get("abbreviation") or ""))
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
    availability = []
    observed_at = datetime.now(timezone.utc).isoformat()
    for team_box in teams:
        team_id = _boxscore_team_id(conn, team_box)
        if not team_id:
            continue
        for stat_group in team_box.get("statistics", []):
            labels = stat_group.get("labels") or []
            label_index = {str(label).upper(): index for index, label in enumerate(labels)}
            for row in stat_group.get("athletes", []):
                athlete = row.get("athlete") or {}
                raw_stats = row.get("stats") or []
                player_id = _parse_int(athlete.get("id"))
                full_name = str(athlete.get("displayName") or "").strip()
                if not player_id or not full_name:
                    continue
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
                minutes_text = _stat(raw_stats, label_index, "MIN") if raw_stats else ""
                did_not_play = bool(row.get("didNotPlay"))
                is_active = 0 if did_not_play or row.get("active") is False else 1
                availability.append(
                    (
                        player_id,
                        game_id,
                        team_id,
                        "espn_boxscore",
                        is_active,
                        1 if did_not_play else 0,
                        str(row.get("reason") or "").strip() or None,
                        minutes_text or None,
                        observed_at,
                    )
                )
                if did_not_play or not raw_stats:
                    continue
                minutes = _parse_minutes(minutes_text)
                if minutes <= 0:
                    continue
                threes = _made_from_attempt(_stat(raw_stats, label_index, "3PT"))
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
    if not stats and not availability:
        return None
    return {"players": players, "stats": stats, "availability": availability}


def _boxscore_team_id(conn: sqlite3.Connection, team_box: dict[str, Any]) -> int | None:
    abbreviation = normalize_team_abbreviation(str((team_box.get("team") or {}).get("abbreviation") or ""))
    team = team_box.get("team") or {}
    display_name = str(team.get("displayName") or team.get("shortDisplayName") or abbreviation).strip()
    return ensure_team(conn, abbreviation or display_name)


def _summary_team_possessions(conn: sqlite3.Connection, payload: dict[str, Any] | None) -> dict[int, float]:
    if not isinstance(payload, dict):
        return {}
    teams = ((payload.get("boxscore") or {}).get("teams") or [])
    possessions_by_team: dict[int, float] = {}
    for team_box in teams:
        team_id = _boxscore_team_id(conn, team_box)
        if not team_id:
            continue
        possessions = _derive_team_possessions(team_box.get("statistics") or [])
        if possessions is None or possessions <= 0:
            continue
        possessions_by_team[int(team_id)] = possessions
    return possessions_by_team


def _upsert_team_game_boxscores(conn: sqlite3.Connection, game_id: int, payload: dict[str, Any] | None) -> int:
    if not isinstance(payload, dict):
        return 0
    rows = []
    captured_at = datetime.now(timezone.utc).isoformat()
    teams = ((payload.get("boxscore") or {}).get("teams") or [])
    for team_box in teams:
        item = _summary_team_boxscore_row(conn, game_id, team_box, captured_at)
        if item is not None:
            rows.append(item)
    if not rows:
        return 0
    conn.executemany(
        """
        INSERT INTO team_game_boxscores (
            game_id, team_id, is_home, points, rebounds, offensive_rebounds, defensive_rebounds,
            assists, steals, blocks, turnovers, team_turnovers, total_turnovers, fouls,
            field_goals_made, field_goals_attempted, threes_made, threes_attempted,
            free_throws_made, free_throws_attempted, possessions, source, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id, team_id) DO UPDATE SET
            is_home = excluded.is_home,
            points = excluded.points,
            rebounds = excluded.rebounds,
            offensive_rebounds = excluded.offensive_rebounds,
            defensive_rebounds = excluded.defensive_rebounds,
            assists = excluded.assists,
            steals = excluded.steals,
            blocks = excluded.blocks,
            turnovers = excluded.turnovers,
            team_turnovers = excluded.team_turnovers,
            total_turnovers = excluded.total_turnovers,
            fouls = excluded.fouls,
            field_goals_made = excluded.field_goals_made,
            field_goals_attempted = excluded.field_goals_attempted,
            threes_made = excluded.threes_made,
            threes_attempted = excluded.threes_attempted,
            free_throws_made = excluded.free_throws_made,
            free_throws_attempted = excluded.free_throws_attempted,
            possessions = excluded.possessions,
            source = excluded.source,
            captured_at = excluded.captured_at
        """,
        rows,
    )
    return len(rows)


def _upsert_game_segment_results(conn: sqlite3.Connection, game_id: int, payload: dict[str, Any] | None) -> int:
    segment = _summary_game_segment_row(payload)
    if segment is None:
        return 0
    captured_at = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO game_segment_results (
            game_id,
            home_q1_points, away_q1_points,
            home_q2_points, away_q2_points,
            home_1h_points, away_1h_points,
            home_q3_points, away_q3_points,
            home_q4_points, away_q4_points,
            source, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id) DO UPDATE SET
            home_q1_points = excluded.home_q1_points,
            away_q1_points = excluded.away_q1_points,
            home_q2_points = excluded.home_q2_points,
            away_q2_points = excluded.away_q2_points,
            home_1h_points = excluded.home_1h_points,
            away_1h_points = excluded.away_1h_points,
            home_q3_points = excluded.home_q3_points,
            away_q3_points = excluded.away_q3_points,
            home_q4_points = excluded.home_q4_points,
            away_q4_points = excluded.away_q4_points,
            source = excluded.source,
            captured_at = excluded.captured_at
        """,
        (game_id, *segment, "espn_summary", captured_at),
    )
    return 1


def _summary_game_segment_row(payload: dict[str, Any] | None) -> tuple[int | None, ...] | None:
    if not isinstance(payload, dict):
        return None
    competitors = (((payload.get("header") or {}).get("competitions") or [{}])[0].get("competitors") or [])
    home = _competitor(competitors, "home")
    away = _competitor(competitors, "away")
    if not home or not away:
        return None

    home_lines = _competitor_linescores(home)
    away_lines = _competitor_linescores(away)
    if not home_lines or not away_lines:
        return None

    home_q1 = home_lines[0] if len(home_lines) >= 1 else None
    away_q1 = away_lines[0] if len(away_lines) >= 1 else None
    home_q2 = home_lines[1] if len(home_lines) >= 2 else None
    away_q2 = away_lines[1] if len(away_lines) >= 2 else None
    home_q3 = home_lines[2] if len(home_lines) >= 3 else None
    away_q3 = away_lines[2] if len(away_lines) >= 3 else None
    home_q4 = home_lines[3] if len(home_lines) >= 4 else None
    away_q4 = away_lines[3] if len(away_lines) >= 4 else None
    home_1h = (home_q1 or 0) + (home_q2 or 0) if home_q1 is not None or home_q2 is not None else None
    away_1h = (away_q1 or 0) + (away_q2 or 0) if away_q1 is not None or away_q2 is not None else None

    return (
        home_q1,
        away_q1,
        home_q2,
        away_q2,
        home_1h,
        away_1h,
        home_q3,
        away_q3,
        home_q4,
        away_q4,
    )


def _competitor_linescores(competitor: dict[str, Any]) -> list[int]:
    values: list[int] = []
    for item in competitor.get("linescores") or []:
        value = _parse_int(item.get("displayValue"))
        if value is None:
            continue
        values.append(value)
    return values


def _summary_team_boxscore_row(
    conn: sqlite3.Connection,
    game_id: int,
    team_box: dict[str, Any],
    captured_at: str,
) -> tuple[Any, ...] | None:
    team_id = _boxscore_team_id(conn, team_box)
    if not team_id:
        return None
    stat_map = _team_stat_map(team_box.get("statistics") or [])
    possessions = _derive_team_possessions(team_box.get("statistics") or [])
    home_away = str(team_box.get("homeAway") or "").strip().lower()
    is_home = 1 if home_away == "home" else 0
    fg_made, fg_attempted = _made_attempt_pair(_first_stat_value(stat_map, "fieldgoalsmadefieldgoalsattempted", "fg"))
    threes_made, threes_attempted = _made_attempt_pair(_first_stat_value(stat_map, "threepointfieldgoalsmadethreepointfieldgoalsattempted", "3pt"))
    ft_made, ft_attempted = _made_attempt_pair(_first_stat_value(stat_map, "freethrowsmadefreethrowsattempted", "ft"))
    return (
        game_id,
        team_id,
        is_home,
        _parse_int(_first_stat_value(stat_map, "points", "pts")),
        _parse_int(_first_stat_value(stat_map, "totalrebounds", "rebounds", "reb")),
        _parse_int(_first_stat_value(stat_map, "offensiverebounds", "oreb", "or")),
        _parse_int(_first_stat_value(stat_map, "defensiverebounds", "dreb", "dr")),
        _parse_int(_first_stat_value(stat_map, "assists", "ast")),
        _parse_int(_first_stat_value(stat_map, "steals", "stl")),
        _parse_int(_first_stat_value(stat_map, "blocks", "blk")),
        _parse_int(_first_stat_value(stat_map, "turnovers", "to")),
        _parse_int(_first_stat_value(stat_map, "teamturnovers", "tto")),
        _parse_int(_first_stat_value(stat_map, "totalturnovers", "toto")),
        _parse_int(_first_stat_value(stat_map, "fouls", "pf")),
        fg_made,
        fg_attempted,
        threes_made,
        threes_attempted,
        ft_made,
        ft_attempted,
        possessions,
        "espn_summary",
        captured_at,
    )


def _update_team_game_result_possessions(conn: sqlite3.Connection, game_id: int, payload: dict[str, Any] | None) -> int:
    possessions_by_team = _summary_team_possessions(conn, payload)
    if not possessions_by_team:
        return 0
    updated = 0
    for team_id, possessions in possessions_by_team.items():
        cursor = conn.execute(
            """
            UPDATE team_game_results
            SET possessions = ?,
                possessions_source = 'espn_summary'
            WHERE game_id = ?
              AND team_id = ?
            """,
            (possessions, game_id, team_id),
        )
        updated += max(cursor.rowcount, 0)
    return updated


def _derive_team_possessions(statistics: list[dict[str, Any]]) -> float | None:
    stat_map = _team_stat_map(statistics)
    fga = _resolve_team_attempts(stat_map, direct_keys=("fieldgoalsattempted", "fga"), composite_keys=("fg",))
    fta = _resolve_team_attempts(stat_map, direct_keys=("freethrowsattempted", "fta"), composite_keys=("ft",))
    offensive_rebounds = _first_stat_number(stat_map, "offensiverebounds", "oreb", "or")
    total_turnovers = _first_stat_number(stat_map, "totalturnovers", "toto")
    player_turnovers = _first_stat_number(stat_map, "turnovers", "to")
    team_turnovers = _first_stat_number(stat_map, "teamturnovers", "tto")
    turnovers = total_turnovers
    if turnovers is None:
        if player_turnovers is not None and team_turnovers is not None:
            turnovers = player_turnovers + team_turnovers
        else:
            turnovers = player_turnovers
    if fga is None or fta is None or offensive_rebounds is None or turnovers is None:
        return None
    return round(float(fga - offensive_rebounds + turnovers + (0.44 * fta)), 2)


def _team_stat_map(statistics: list[dict[str, Any]]) -> dict[str, str]:
    stat_map: dict[str, str] = {}
    for stat in statistics:
        display_value = str(stat.get("displayValue") or "").strip()
        if not display_value:
            continue
        for key in (stat.get("name"), stat.get("abbreviation"), stat.get("label")):
            normalized = _normalize_stat_key(key)
            if normalized and normalized not in stat_map:
                stat_map[normalized] = display_value
    return stat_map


def _resolve_team_attempts(
    stat_map: dict[str, str],
    *,
    direct_keys: tuple[str, ...],
    composite_keys: tuple[str, ...],
) -> float | None:
    direct_value = _first_stat_number(stat_map, *direct_keys)
    if direct_value is not None:
        return direct_value
    for key in composite_keys:
        attempts = _attempts_from_made_attempt(stat_map.get(key))
        if attempts is not None:
            return attempts
    return None


def _first_stat_value(stat_map: dict[str, str], *keys: str) -> str | None:
    for key in keys:
        value = stat_map.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return None


def _first_stat_number(stat_map: dict[str, str], *keys: str) -> float | None:
    for key in keys:
        value = _parse_float(stat_map.get(key))
        if value is not None:
            return value
    return None


def _attempts_from_made_attempt(value: str | None) -> float | None:
    text = str(value or "").strip()
    if not text or "-" not in text:
        return None
    _made, attempts = text.split("-", 1)
    return _parse_float(attempts)


def _made_attempt_pair(value: str | None) -> tuple[int | None, int | None]:
    text = str(value or "").strip()
    if not text or "-" not in text:
        return None, None
    made, attempts = text.split("-", 1)
    return _parse_int(made), _parse_int(attempts)


def _normalize_stat_key(value: Any) -> str:
    text = str(value or "").strip().lower()
    return "".join(ch for ch in text if ch.isalnum())


def _delete_explicit_dnp_prop_lines(conn: sqlite3.Connection, game_ids: list[int]) -> int:
    if not game_ids:
        return 0
    placeholders = ",".join("?" for _ in game_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT pl.id
        FROM prop_lines pl
        JOIN games g ON g.id = pl.game_id
        JOIN player_game_availability pga
          ON pga.game_id = pl.game_id
         AND pga.player_id = pl.player_id
        LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
        WHERE g.status = 'final'
          AND sp.id IS NULL
          AND pga.did_not_play = 1
          AND pga.source IN ('espn_boxscore', 'espn_summary_injury')
          AND pl.game_id IN ({placeholders})
        """,
        tuple(game_ids),
    ).fetchall()
    prop_line_ids = [int(row["id"]) for row in rows]
    if not prop_line_ids:
        return 0
    delete_placeholders = ",".join("?" for _ in prop_line_ids)
    params = tuple(prop_line_ids)
    conn.execute(
        f"""
        DELETE FROM dfs_first_half_projection_settlements
        WHERE snapshot_id IN (
            SELECT id
            FROM dfs_first_half_projection_snapshots
            WHERE prop_line_id IN ({delete_placeholders})
        )
        """,
        params,
    )
    conn.execute(
        f"DELETE FROM dfs_first_half_projection_snapshots WHERE prop_line_id IN ({delete_placeholders})",
        params,
    )
    conn.execute(f"DELETE FROM watchlist_snapshot_items WHERE prop_line_id IN ({delete_placeholders})", params)
    conn.execute(f"DELETE FROM gem_snapshot_items WHERE prop_line_id IN ({delete_placeholders})", params)
    conn.execute(f"DELETE FROM prop_predictions WHERE prop_line_id IN ({delete_placeholders})", params)
    conn.execute(f"DELETE FROM prop_lines WHERE id IN ({delete_placeholders})", params)
    return len(prop_line_ids)


def _injury_availability_rows(
    conn: sqlite3.Connection,
    game_id: int,
    payload: dict[str, Any],
    player_rows: dict[str, list] | None,
) -> dict[str, list]:
    boxscore_player_ids = {
        int(player["id"])
        for player in (player_rows or {}).get("players", [])
        if player.get("id") is not None
    }
    players: list[dict[str, Any]] = []
    availability: list[tuple] = []
    observed_at = datetime.now(timezone.utc).isoformat()
    for team_block in payload.get("injuries") or []:
        team = team_block.get("team") or {}
        team_id = ensure_team(
            conn,
            normalize_team_abbreviation(str(team.get("abbreviation") or "")) or str(team.get("displayName") or "").strip(),
        )
        if not team_id:
            continue
        for item in team_block.get("injuries") or []:
            athlete = item.get("athlete") or {}
            player_id = _parse_int(athlete.get("id"))
            full_name = str(athlete.get("displayName") or athlete.get("fullName") or "").strip()
            if not player_id or not full_name or player_id in boxscore_player_ids:
                continue
            position = str(((athlete.get("position") or {}).get("abbreviation")) or "")
            status_text = str(item.get("status") or "").strip()
            detail_type = str(((item.get("details") or {}).get("type")) or "").strip()
            reason = " ".join(part for part in [status_text, detail_type] if part).strip() or None
            players.append(
                {
                    "id": player_id,
                    "full_name": full_name,
                    "team_id": int(team_id),
                    "position": position,
                    "rotation_role": "rotation",
                }
            )
            availability.append(
                (
                    player_id,
                    game_id,
                    int(team_id),
                    "espn_summary_injury",
                    0,
                    1,
                    reason,
                    None,
                    observed_at,
                )
            )
    return {"players": players, "availability": availability}


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


def _parse_float(value: Any) -> float | None:
    try:
        return float(str(value).strip())
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
