from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from .cache import read_json_cache, write_json_cache

BASE_URL = "https://api.balldontlie.io/wnba/v1"
TEAM_ALIASES = {
    "atl": "Atlanta Dream",
    "chi": "Chicago Sky",
    "con": "Connecticut Sun",
    "dal": "Dallas Wings",
    "gs": "Golden State Valkyries",
    "ind": "Indiana Fever",
    "lv": "Las Vegas Aces",
    "la": "Los Angeles Sparks",
    "min": "Minnesota Lynx",
    "ny": "New York Liberty",
    "phx": "Phoenix Mercury",
    "por": "Portland Fire",
    "sea": "Seattle Storm",
    "tor": "Toronto Tempo",
    "wsh": "Washington Mystics",
}


def _fetch_json(url: str) -> dict[str, Any]:
    headers = {"User-Agent": "WNBAStats/1.0"}
    api_key = os.getenv("BALLDONTLIE_API_KEY")
    if api_key:
        headers["Authorization"] = api_key
    request = Request(url, headers=headers)
    try:
        with urlopen(request, timeout=30) as response:
            return json.load(response)
    except HTTPError as exc:
        if exc.code in {401, 403}:
            raise ValueError("Set BALLDONTLIE_API_KEY to fetch BallDontLie WNBA history.") from exc
        raise


def _team_query_name(team: str) -> str:
    if not team:
        raise ValueError("Team parameter is required.")
    query = team.strip().lower()
    if not query:
        raise ValueError("Team parameter cannot be empty.")
    return TEAM_ALIASES.get(query, query)


def list_teams() -> list[dict[str, Any]]:
    response = _fetch_json(f"{BASE_URL}/teams?per_page=100")
    return response.get("data", [])


def resolve_team_id(team: str) -> int:
    query = _team_query_name(team)
    teams = list_teams()
    for row in teams:
        abbreviation = str(row.get("abbreviation", "")).strip().lower()
        full_name = str(row.get("full_name", "")).strip().lower()
        name = str(row.get("name", "")).strip().lower()
        city_name = f"{row.get('city', '')} {row.get('name', '')}".strip().lower()
        if query == abbreviation or query == full_name or query == name:
            return int(row["id"])
        if query == city_name or query in full_name or query in name or query in city_name:
            return int(row["id"])
    raise ValueError(f"BallDontLie team not found for {team!r}")


def _cache_name(team: str, seasons: list[int] | None, start_date: str | None, end_date: str | None) -> str:
    normalized_team = team.strip().lower().replace(" ", "_")
    seasons_part = "all" if not seasons else "-".join(str(s) for s in seasons)
    start_part = start_date or "any"
    end_part = end_date or "any"
    return f"ball_dont_lie_history_{normalized_team}_seasons_{seasons_part}_from_{start_part}_to_{end_part}.json"


def fetch_team_history(
    team: str,
    seasons: list[int] | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    force_refresh: bool = False,
) -> dict[str, Any]:
    team_id = resolve_team_id(team)
    params: dict[str, Any] = {
        "team_ids[]": team_id,
        "per_page": 100,
        "page": 1,
    }

    if seasons:
        params["seasons[]"] = seasons
    if start_date:
        params["start_date"] = start_date
    if end_date:
        params["end_date"] = end_date

    cache_name = _cache_name(team, seasons, start_date, end_date)
    if not force_refresh:
        cached_payload = read_json_cache(cache_name)
        if cached_payload is not None:
            return cached_payload

    games: list[dict[str, Any]] = []
    meta: dict[str, Any] = {}
    while True:
        url = f"{BASE_URL}/games?{urlencode(params, doseq=True)}"
        response = _fetch_json(url)
        games.extend(response.get("data", []))
        meta = response.get("meta", {})
        if not meta or meta.get("current_page") >= meta.get("total_pages"):
            break
        params["page"] = params.get("page", 1) + 1

    result = {
        "team": team,
        "team_id": team_id,
        "seasons": seasons,
        "start_date": start_date,
        "end_date": end_date,
        "games": games,
        "meta": meta,
        "cached_at": datetime.now(timezone.utc).isoformat(),
        "cached_from": "balldontlie",
    }
    write_json_cache(cache_name, result)
    return result
