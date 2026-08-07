from __future__ import annotations

import json
import re
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .bootstrap import ensure_team, normalize_team_abbreviation
from .cache import read_json_cache, write_json_cache
from .player_identity import normalize_player_lookup_name


PLAY_BY_PLAY_URLS = [
    "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/playbyplay",
    "https://sports.core.api.espn.com/v2/sports/basketball/leagues/wnba/events/{event_id}/competitions/{event_id}/plays",
]

THREE_POINT_TERMS = (
    "three point",
    "three-pointer",
    "three pointer",
    "3-point",
    "3pt",
    "3 pt",
)


def fetch_playbyplay(event_id: int, force_refresh: bool = False) -> dict[str, Any]:
    cache_name = f"espn_wnba_playbyplay_{event_id}.json"
    if not force_refresh:
        cached = read_json_cache(cache_name)
        if isinstance(cached, dict):
            return cached

    errors: list[str] = []
    for template in PLAY_BY_PLAY_URLS:
        url = template.format(event_id=event_id)
        if "{event_id}" not in template:
            url = f"{url}?{urlencode({'event': str(event_id)})}"
        request = Request(url, headers={"User-Agent": "WNBAStats/1.0"})
        try:
            with urlopen(request, timeout=30) as response:
                payload = json.loads(response.read().decode("utf-8"))
            payload["cached_at"] = datetime.now(timezone.utc).isoformat()
            payload["cached_from"] = "espn_playbyplay"
            write_json_cache(cache_name, payload)
            return payload
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{url}: {exc}")
    raise RuntimeError("; ".join(errors) if errors else f"Failed to fetch ESPN play-by-play for {event_id}")


def upsert_player_first_half_stats(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    event_id: int,
    summary_payload: dict[str, Any] | None,
    playbyplay_payload: dict[str, Any] | None,
) -> dict[str, int]:
    if not isinstance(summary_payload, dict):
        return {"events": 0, "players": 0}
    payload = _preferred_play_payload(summary_payload, playbyplay_payload)

    player_context = _player_context_from_summary(conn, game_id, summary_payload)
    plays = _normalized_plays(payload)
    if not plays:
        return {"events": 0, "players": 0}

    captured_at = datetime.now(timezone.utc).isoformat()
    raw_rows, player_rows = _derive_first_half_rows(
        conn,
        game_id=game_id,
        event_id=event_id,
        plays=plays,
        player_context=player_context,
        captured_at=captured_at,
    )
    conn.execute("DELETE FROM espn_play_by_play_events WHERE game_id = ?", (game_id,))
    conn.execute("DELETE FROM player_first_half_stats WHERE game_id = ?", (game_id,))
    if raw_rows:
        conn.executemany(
            """
            INSERT INTO espn_play_by_play_events (
                game_id, espn_event_id, event_index, period_number, clock_seconds_remaining, clock_display,
                home_score, away_score, team_id, player_id, secondary_player_id, event_type, event_text,
                stat_json, source, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id, event_index) DO UPDATE SET
                espn_event_id = excluded.espn_event_id,
                period_number = excluded.period_number,
                clock_seconds_remaining = excluded.clock_seconds_remaining,
                clock_display = excluded.clock_display,
                home_score = excluded.home_score,
                away_score = excluded.away_score,
                team_id = excluded.team_id,
                player_id = excluded.player_id,
                secondary_player_id = excluded.secondary_player_id,
                event_type = excluded.event_type,
                event_text = excluded.event_text,
                stat_json = excluded.stat_json,
                source = excluded.source,
                captured_at = excluded.captured_at
            """,
            raw_rows,
        )
    if player_rows:
        conn.executemany(
            """
            INSERT INTO player_first_half_stats (
                game_id, player_id, team_id, opponent_team_id, first_half_points, first_half_rebounds,
                first_half_assists, first_half_threes, first_half_steals, first_half_blocks, first_half_turnovers,
                first_half_field_goals_made, first_half_field_goals_attempted, first_half_free_throws_made,
                first_half_free_throws_attempted, first_half_minutes, minutes_source, source, captured_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(game_id, player_id) DO UPDATE SET
                team_id = excluded.team_id,
                opponent_team_id = excluded.opponent_team_id,
                first_half_points = excluded.first_half_points,
                first_half_rebounds = excluded.first_half_rebounds,
                first_half_assists = excluded.first_half_assists,
                first_half_threes = excluded.first_half_threes,
                first_half_steals = excluded.first_half_steals,
                first_half_blocks = excluded.first_half_blocks,
                first_half_turnovers = excluded.first_half_turnovers,
                first_half_field_goals_made = excluded.first_half_field_goals_made,
                first_half_field_goals_attempted = excluded.first_half_field_goals_attempted,
                first_half_free_throws_made = excluded.first_half_free_throws_made,
                first_half_free_throws_attempted = excluded.first_half_free_throws_attempted,
                first_half_minutes = excluded.first_half_minutes,
                minutes_source = excluded.minutes_source,
                source = excluded.source,
                captured_at = excluded.captured_at
            """,
            player_rows,
        )
    return {"events": len(raw_rows), "players": len(player_rows)}


def _preferred_play_payload(
    summary_payload: dict[str, Any],
    playbyplay_payload: dict[str, Any] | None,
) -> dict[str, Any]:
    candidates = [summary_payload]
    if isinstance(playbyplay_payload, dict):
        candidates.append(playbyplay_payload)
    return max(candidates, key=_payload_play_count)


def _payload_play_count(payload: dict[str, Any] | None) -> int:
    if not isinstance(payload, dict):
        return 0
    plays = payload.get("plays") or payload.get("items") or []
    return len(plays) if isinstance(plays, list) else 0


def _player_context_from_summary(conn: sqlite3.Connection, game_id: int, payload: dict[str, Any]) -> dict[str, Any]:
    game_row = conn.execute(
        "SELECT home_team_id, away_team_id FROM games WHERE id = ?",
        (game_id,),
    ).fetchone()
    home_team_id = int(game_row["home_team_id"]) if game_row else None
    away_team_id = int(game_row["away_team_id"]) if game_row else None
    players_by_id: dict[int, dict[str, Any]] = {}
    players_by_team: dict[int, list[int]] = defaultdict(list)
    starters_by_team: dict[int, set[int]] = defaultdict(set)
    name_to_player_id: dict[str, int] = {}
    normalized_name_pairs: list[tuple[str, int]] = []
    local_team_by_feed_id: dict[int, int] = {}
    for team_box in ((payload.get("boxscore") or {}).get("players") or []):
        team = team_box.get("team") or {}
        feed_team_id = _parse_int(team.get("id"))
        team_id = ensure_team(
            conn,
            normalize_team_abbreviation(str(team.get("abbreviation") or "")) or str(team.get("displayName") or "").strip(),
        )
        if not team_id:
            continue
        if feed_team_id is not None:
            local_team_by_feed_id[int(feed_team_id)] = int(team_id)
        for stat_group in team_box.get("statistics") or []:
            for athlete_row in stat_group.get("athletes") or []:
                athlete = athlete_row.get("athlete") or {}
                player_id = _parse_int(athlete.get("id"))
                full_name = str(athlete.get("displayName") or athlete.get("fullName") or "").strip()
                if not player_id or not full_name or player_id in players_by_id:
                    continue
                normalized = normalize_player_lookup_name(full_name)
                entry = {
                    "player_id": player_id,
                    "full_name": full_name,
                    "normalized_name": normalized,
                    "team_id": int(team_id),
                }
                players_by_id[player_id] = entry
                players_by_team[int(team_id)].append(player_id)
                name_to_player_id[normalized] = player_id
                normalized_name_pairs.append((normalized, player_id))
                if athlete_row.get("starter"):
                    starters_by_team[int(team_id)].add(player_id)
    normalized_name_pairs.sort(key=lambda item: len(item[0]), reverse=True)
    return {
        "players_by_id": players_by_id,
        "players_by_team": players_by_team,
        "starters_by_team": starters_by_team,
        "name_to_player_id": name_to_player_id,
        "normalized_name_pairs": normalized_name_pairs,
        "home_team_id": home_team_id,
        "away_team_id": away_team_id,
        "local_team_by_feed_id": local_team_by_feed_id,
    }


def _normalized_plays(payload: dict[str, Any]) -> list[dict[str, Any]]:
    plays = payload.get("plays") or payload.get("items") or []
    if not isinstance(plays, list):
        return []
    normalized: list[dict[str, Any]] = []
    for index, play in enumerate(plays):
        if not isinstance(play, dict):
            continue
        period = _parse_period_number(play)
        if period is None or period > 2:
            continue
        normalized.append(
            {
                "event_index": index,
                "period_number": period,
                "clock_seconds_remaining": _parse_clock_seconds(play),
                "clock_display": _parse_clock_display(play),
                "text": str(play.get("text") or play.get("shortText") or play.get("description") or "").strip(),
                "type_text": str((play.get("type") or {}).get("text") or "").strip(),
                "home_score": _parse_int(play.get("homeScore")),
                "away_score": _parse_int(play.get("awayScore")),
                "team_id_hint": _parse_play_team(play),
                "participant_ids": _play_participant_ids(play),
                "raw": play,
            }
        )
    normalized.sort(key=lambda item: (item["period_number"], item["event_index"]))
    return normalized


def _derive_first_half_rows(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    event_id: int,
    plays: list[dict[str, Any]],
    player_context: dict[str, Any],
    captured_at: str,
) -> tuple[list[tuple[Any, ...]], list[tuple[Any, ...]]]:
    stats_by_player: dict[int, dict[str, float]] = defaultdict(_empty_half_stat_line)
    minutes_by_player: dict[int, float] = defaultdict(float)
    players_by_id: dict[int, dict[str, Any]] = dict(player_context["players_by_id"])
    starters_by_team: dict[int, set[int]] = player_context["starters_by_team"]
    on_court_by_team: dict[int, set[int]] = {team_id: set(starters) for team_id, starters in starters_by_team.items()}
    prev_clock_by_period = {1: 600.0, 2: 600.0}
    current_period = 1
    raw_rows: list[tuple[Any, ...]] = []

    for play in plays:
        period = int(play["period_number"])
        if period != current_period:
            for team_players in on_court_by_team.values():
                for player_id in team_players:
                    minutes_by_player[player_id] += max(prev_clock_by_period.get(current_period, 0.0), 0.0) / 60.0
            current_period = period
        clock_remaining = float(play["clock_seconds_remaining"] if play["clock_seconds_remaining"] is not None else prev_clock_by_period.get(period, 0.0))
        elapsed = max(prev_clock_by_period.get(period, 600.0) - clock_remaining, 0.0)
        if elapsed > 0:
            for team_players in on_court_by_team.values():
                for player_id in team_players:
                    minutes_by_player[player_id] += elapsed / 60.0
        prev_clock_by_period[period] = clock_remaining

        event_stats = _derive_play_stats(
            play["text"],
            player_context,
            int(play["team_id_hint"]) if play["team_id_hint"] is not None else None,
            participant_ids=list(play["participant_ids"]),
            type_text=str(play["type_text"] or ""),
        )
        for player_id, increments in event_stats["player_increments"].items():
            for key, value in increments.items():
                stats_by_player[player_id][key] += value

        substitution = event_stats["substitution"]
        if substitution is not None:
            team_id = substitution["team_id"]
            if team_id is not None:
                lineup = on_court_by_team.setdefault(int(team_id), set())
                if substitution["player_out_id"] is not None:
                    lineup.discard(int(substitution["player_out_id"]))
                if substitution["player_in_id"] is not None:
                    lineup.add(int(substitution["player_in_id"]))

        raw_rows.append(
            (
                game_id,
                event_id,
                int(play["event_index"]),
                period,
                int(clock_remaining),
                str(play["clock_display"] or ""),
                play["home_score"],
                play["away_score"],
                event_stats["team_id"],
                event_stats["primary_player_id"],
                event_stats["secondary_player_id"],
                event_stats["event_type"],
                play["text"],
                json.dumps(event_stats["player_increments"], sort_keys=True),
                "espn_playbyplay",
                captured_at,
            )
        )

    for team_players in on_court_by_team.values():
        for player_id in team_players:
            minutes_by_player[player_id] += max(prev_clock_by_period.get(current_period, 0.0), 0.0) / 60.0

    player_rows: list[tuple[Any, ...]] = []
    for player_id, stat_line in stats_by_player.items():
        player_meta = players_by_id.get(int(player_id))
        if not player_meta:
            continue
        team_id = int(player_meta["team_id"])
        opponent_team_id = _opponent_team_id(player_context, team_id)
        if opponent_team_id is None:
            continue
        player_rows.append(
            (
                game_id,
                int(player_id),
                team_id,
                int(opponent_team_id),
                int(round(stat_line["points"])),
                int(round(stat_line["rebounds"])),
                int(round(stat_line["assists"])),
                int(round(stat_line["threes"])),
                int(round(stat_line["steals"])),
                int(round(stat_line["blocks"])),
                int(round(stat_line["turnovers"])),
                int(round(stat_line["field_goals_made"])),
                int(round(stat_line["field_goals_attempted"])),
                int(round(stat_line["free_throws_made"])),
                int(round(stat_line["free_throws_attempted"])),
                round(minutes_by_player.get(int(player_id), 0.0), 3),
                "play_by_play_substitutions" if starters_by_team.get(team_id) else "unknown",
                "espn_playbyplay",
                captured_at,
            )
        )
    return raw_rows, player_rows


def _empty_half_stat_line() -> dict[str, float]:
    return {
        "points": 0.0,
        "rebounds": 0.0,
        "assists": 0.0,
        "threes": 0.0,
        "steals": 0.0,
        "blocks": 0.0,
        "turnovers": 0.0,
        "field_goals_made": 0.0,
        "field_goals_attempted": 0.0,
        "free_throws_made": 0.0,
        "free_throws_attempted": 0.0,
    }


def _derive_play_stats(
    text: str,
    player_context: dict[str, Any],
    team_id_hint: int | None,
    *,
    participant_ids: list[int],
    type_text: str,
) -> dict[str, Any]:
    clean_text = str(text or "").strip()
    normalized_text = normalize_player_lookup_name(clean_text)
    players = [player_id for player_id in participant_ids if player_id in player_context["players_by_id"]] or _match_player_ids_in_text(normalized_text, player_context)
    primary_player_id = players[0] if players else None
    secondary_player_id = players[1] if len(players) > 1 else None
    team_id = _map_team_id_hint(player_context, team_id_hint) or _team_id_from_players(player_context, players)
    increments: dict[int, dict[str, float]] = defaultdict(dict)
    event_type = "other"
    substitution = None
    type_key = normalize_player_lookup_name(type_text).replace(" ", "_")

    if "enters the game for" in clean_text.lower():
        event_type = "substitution"
        player_in_id, player_out_id = _parse_substitution_players(normalized_text, player_context)
        substitution = {
            "team_id": _team_id_from_players(player_context, [value for value in (player_in_id, player_out_id) if value is not None]),
            "player_in_id": player_in_id,
            "player_out_id": player_out_id,
        }
        primary_player_id = player_in_id
        secondary_player_id = player_out_id
    elif "technical free throw" in clean_text.lower():
        event_type = "made_free_throw" if "makes" in clean_text.lower() else "missed_free_throw"
        if primary_player_id is not None:
            if "makes" in clean_text.lower():
                _add_stat(increments, primary_player_id, "points", 1)
                _add_stat(increments, primary_player_id, "free_throws_made", 1)
            _add_stat(increments, primary_player_id, "free_throws_attempted", 1)
    elif "makes free throw" in clean_text.lower():
        event_type = "made_free_throw"
        if primary_player_id is not None:
            _add_stat(increments, primary_player_id, "points", 1)
            _add_stat(increments, primary_player_id, "free_throws_made", 1)
            _add_stat(increments, primary_player_id, "free_throws_attempted", 1)
    elif "misses free throw" in clean_text.lower():
        event_type = "missed_free_throw"
        if primary_player_id is not None:
            _add_stat(increments, primary_player_id, "free_throws_attempted", 1)
    elif "rebound" in clean_text.lower() or "rebound" in type_key:
        event_type = "rebound"
        rebound_player_id = primary_player_id
        if rebound_player_id is not None:
            _add_stat(increments, rebound_player_id, "rebounds", 1)
    elif "block" in clean_text.lower() or "block" in type_key:
        event_type = "block"
        block_player_id = secondary_player_id if len(players) >= 2 else primary_player_id
        shot_player_id = primary_player_id if len(players) >= 2 else secondary_player_id
        if block_player_id is not None:
            _add_stat(increments, block_player_id, "blocks", 1)
        if shot_player_id is not None:
            _add_stat(increments, shot_player_id, "field_goals_attempted", 1)
    elif "steal" in clean_text.lower() or "steal" in type_key:
        event_type = "steal"
        turnover_player_id, steal_player_id = _parse_turnover_steal_players(
            normalized_text,
            player_context,
            default_primary=primary_player_id,
            default_secondary=secondary_player_id,
        )
        if steal_player_id is not None:
            _add_stat(increments, steal_player_id, "steals", 1)
        if turnover_player_id is not None and "turnover" in clean_text.lower():
            _add_stat(increments, turnover_player_id, "turnovers", 1)
    elif "turnover" in clean_text.lower() or "turnover" in type_key:
        event_type = "turnover"
        if primary_player_id is not None:
            _add_stat(increments, primary_player_id, "turnovers", 1)
        if "steal by" in clean_text.lower() and secondary_player_id is not None:
            _add_stat(increments, secondary_player_id, "steals", 1)
    elif "misses" in clean_text.lower() or "missed" in type_key:
        event_type = "missed_field_goal"
        if primary_player_id is not None:
            _add_stat(increments, primary_player_id, "field_goals_attempted", 1)
    elif "makes" in clean_text.lower() or "shot" in type_key:
        event_type = "made_field_goal"
        if primary_player_id is not None:
            made_points = _made_shot_points(clean_text)
            _add_stat(increments, primary_player_id, "points", made_points)
            _add_stat(increments, primary_player_id, "field_goals_made", 1)
            _add_stat(increments, primary_player_id, "field_goals_attempted", 1)
            if made_points == 3:
                _add_stat(increments, primary_player_id, "threes", 1)
        assist_player_id = secondary_player_id if secondary_player_id is not None else _assist_player_id(normalized_text, player_context, exclude=primary_player_id)
        if assist_player_id is not None:
            secondary_player_id = assist_player_id
            _add_stat(increments, assist_player_id, "assists", 1)

    return {
        "team_id": team_id,
        "primary_player_id": primary_player_id,
        "secondary_player_id": secondary_player_id,
        "event_type": event_type,
        "player_increments": {player_id: dict(values) for player_id, values in increments.items()},
        "substitution": substitution,
    }


def _match_player_ids_in_text(text: str, player_context: dict[str, Any]) -> list[int]:
    matches: list[int] = []
    used_ids: set[int] = set()
    for normalized_name, player_id in player_context["normalized_name_pairs"]:
        if not normalized_name or player_id in used_ids:
            continue
        if re.search(rf"(?<!\w){re.escape(normalized_name)}(?!\w)", text):
            matches.append(int(player_id))
            used_ids.add(int(player_id))
    return matches


def _assist_player_id(text: str, player_context: dict[str, Any], *, exclude: int | None) -> int | None:
    match = re.search(r"\(([^)]+)\sassists\)", text)
    if not match:
        return None
    return _player_id_from_name_fragment(match.group(1), player_context, exclude=exclude)


def _parse_substitution_players(text: str, player_context: dict[str, Any]) -> tuple[int | None, int | None]:
    match = re.search(r"(.+?) enters the game for (.+)", text)
    if not match:
        return None, None
    return (
        _player_id_from_name_fragment(match.group(1), player_context),
        _player_id_from_name_fragment(match.group(2), player_context),
    )


def _parse_turnover_steal_players(
    text: str,
    player_context: dict[str, Any],
    *,
    default_primary: int | None,
    default_secondary: int | None,
) -> tuple[int | None, int | None]:
    match = re.search(r"(.+?) turnover\s*\((.+?) steals\)", text)
    if match:
        turnover_player_id = _player_id_from_name_fragment(match.group(1), player_context)
        steal_player_id = _player_id_from_name_fragment(match.group(2), player_context, exclude=turnover_player_id)
        return turnover_player_id, steal_player_id
    if "steal by" in text:
        return default_primary, default_secondary
    return default_primary, default_secondary


def _player_id_from_name_fragment(fragment: str, player_context: dict[str, Any], *, exclude: int | None = None) -> int | None:
    normalized = normalize_player_lookup_name(fragment)
    if not normalized:
        return None
    direct = player_context["name_to_player_id"].get(normalized)
    if direct is not None and direct != exclude:
        return int(direct)
    for normalized_name, player_id in player_context["normalized_name_pairs"]:
        if player_id == exclude or not normalized_name:
            continue
        if normalized == normalized_name or normalized in normalized_name or normalized_name in normalized:
            return int(player_id)
    return None


def _team_id_from_players(player_context: dict[str, Any], player_ids: list[int | None]) -> int | None:
    for player_id in player_ids:
        if player_id is None:
            continue
        player = player_context["players_by_id"].get(int(player_id))
        if player is not None:
            return int(player["team_id"])
    return None


def _map_team_id_hint(player_context: dict[str, Any], team_id_hint: int | None) -> int | None:
    if team_id_hint is None:
        return None
    mapped = (player_context.get("local_team_by_feed_id") or {}).get(int(team_id_hint))
    return int(mapped) if mapped is not None else None


def _opponent_team_id(player_context: dict[str, Any], team_id: int) -> int | None:
    home_team_id = player_context.get("home_team_id")
    away_team_id = player_context.get("away_team_id")
    if home_team_id is None or away_team_id is None:
        return None
    return int(away_team_id) if int(team_id) == int(home_team_id) else int(home_team_id)


def _add_stat(increments: dict[int, dict[str, float]], player_id: int, key: str, value: float) -> None:
    bucket = increments.setdefault(int(player_id), {})
    bucket[key] = float(bucket.get(key, 0.0)) + float(value)


def _is_three_point_text(text: str) -> bool:
    lowered = str(text or "").lower()
    if "two point" in lowered:
        return False
    if any(term in lowered for term in THREE_POINT_TERMS):
        return True
    feet_match = re.search(r"(\d+)-foot", lowered)
    if feet_match:
        try:
            return int(feet_match.group(1)) >= 22
        except ValueError:
            return False
    return False


def _made_shot_points(text: str) -> int:
    lowered = str(text or "").lower()
    if "free throw" in lowered:
        return 1
    return 3 if _is_three_point_text(text) else 2


def _parse_period_number(play: dict[str, Any]) -> int | None:
    period = play.get("period")
    if isinstance(period, dict):
        value = _parse_int(period.get("number"))
        if value is not None:
            return value
    return _parse_int(play.get("period"))


def _parse_clock_display(play: dict[str, Any]) -> str | None:
    clock = play.get("clock")
    if isinstance(clock, dict):
        value = str(clock.get("displayValue") or "").strip()
        return value or None
    value = str(play.get("clock") or "").strip()
    return value or None


def _parse_clock_seconds(play: dict[str, Any]) -> int | None:
    clock = play.get("clock")
    if isinstance(clock, dict):
        if clock.get("value") is not None:
            raw = _parse_float(clock.get("value"))
            if raw is not None:
                return int(round(raw))
        display = str(clock.get("displayValue") or "").strip()
        return _clock_display_to_seconds(display)
    return _clock_display_to_seconds(str(play.get("clock") or "").strip())


def _parse_play_team(play: dict[str, Any]) -> int | None:
    team = play.get("team")
    if isinstance(team, dict):
        team_id = _parse_int(team.get("id"))
        if team_id is not None:
            return team_id
    return None


def _play_participant_ids(play: dict[str, Any]) -> list[int]:
    ids: list[int] = []
    for participant in play.get("participants") or []:
        athlete = participant.get("athlete") or {}
        player_id = _parse_int(athlete.get("id"))
        if player_id is not None:
            ids.append(player_id)
    return ids


def _clock_display_to_seconds(value: str) -> int | None:
    text = str(value or "").strip()
    if not text or ":" not in text:
        return None
    minutes_text, seconds_text = text.split(":", 1)
    minutes = _parse_int(minutes_text)
    seconds = _parse_int(seconds_text)
    if minutes is None or seconds is None:
        return None
    return max((minutes * 60) + seconds, 0)


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
