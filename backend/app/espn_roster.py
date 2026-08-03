from __future__ import annotations

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any
from urllib.request import Request, urlopen

from .bootstrap import WNBA_TEAMS, ensure_teams
from .cache import read_json_cache, write_json_cache


ROSTER_URL = "https://site.api.espn.com/apis/site/v2/sports/basketball/wnba/teams/{team}/roster"
ROSTER_CACHE_NAME = "espn_wnba_rosters_current.json"


def fetch_espn_team_roster(team_abbreviation: str) -> dict[str, Any]:
    abbreviation = team_abbreviation.strip().lower()
    request = Request(
        ROSTER_URL.format(team=abbreviation),
        headers={"User-Agent": "WNBAStats/1.0"},
    )
    with urlopen(request, timeout=30) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict) or not isinstance(payload.get("athletes"), list):
        raise ValueError(f"ESPN returned an invalid {team_abbreviation} roster payload")
    return payload


def sync_espn_rosters(conn: sqlite3.Connection) -> dict[str, Any]:
    """Refresh ESPN rosters and persist current player/team assignments."""
    ensure_teams(conn)
    team_abbreviations = [team[1] for team in WNBA_TEAMS]
    previous_snapshot = read_json_cache(ROSTER_CACHE_NAME)
    payloads: dict[str, dict[str, Any]] = {}
    errors: list[dict[str, str]] = []

    with ThreadPoolExecutor(max_workers=6) as executor:
        futures = {
            executor.submit(fetch_espn_team_roster, abbreviation): abbreviation
            for abbreviation in team_abbreviations
        }
        for future in as_completed(futures):
            abbreviation = futures[future]
            try:
                payloads[abbreviation] = future.result()
            except Exception as exc:
                errors.append({"team": abbreviation, "error": str(exc)})

    if not payloads:
        raise RuntimeError("ESPN roster sync failed for every WNBA team")

    team_rows = conn.execute("SELECT id, upper(abbreviation) AS abbreviation FROM teams").fetchall()
    team_ids = {str(row["abbreviation"]): int(row["id"]) for row in team_rows}
    existing_rows = conn.execute("SELECT id, team_id FROM players").fetchall()
    existing_teams = {int(row["id"]): int(row["team_id"]) for row in existing_rows}
    observed_at = datetime.now(timezone.utc).isoformat()
    roster_rows: list[dict[str, Any]] = []
    inserted_players = 0
    moved_players: list[dict[str, Any]] = []
    changed_player_ids: list[int] = []
    changed_team_ids: set[int] = set()

    for abbreviation in team_abbreviations:
        payload = payloads.get(abbreviation)
        team_id = team_ids.get(abbreviation)
        if payload is None or team_id is None:
            continue
        for athlete in payload.get("athletes", []):
            try:
                player_id = int(athlete["id"])
            except (KeyError, TypeError, ValueError):
                continue
            full_name = str(athlete.get("fullName") or athlete.get("displayName") or "").strip()
            if not full_name:
                continue
            position = str((athlete.get("position") or {}).get("abbreviation") or "").strip() or None
            previous_team_id = existing_teams.get(player_id)
            changed = previous_team_id is None or previous_team_id != team_id

            conn.execute(
                """
                INSERT INTO players (id, full_name, team_id, position, rotation_role)
                VALUES (?, ?, ?, ?, NULL)
                ON CONFLICT(id) DO UPDATE SET
                    full_name = excluded.full_name,
                    team_id = excluded.team_id,
                    position = COALESCE(excluded.position, players.position)
                """,
                (player_id, full_name, team_id, position),
            )
            if previous_team_id is None:
                inserted_players += 1
            elif previous_team_id != team_id:
                moved_players.append(
                    {
                        "player_id": player_id,
                        "player_name": full_name,
                        "from_team_id": previous_team_id,
                        "to_team_id": team_id,
                        "to_team": abbreviation,
                    }
                )
                changed_team_ids.add(previous_team_id)
            if changed:
                changed_player_ids.append(player_id)
                changed_team_ids.add(team_id)
                conn.execute(
                    "DELETE FROM player_team_history WHERE player_id = ? AND game_id IS NULL AND source = ?",
                    (player_id, "espn_roster_current"),
                )
                conn.execute(
                    """
                    INSERT INTO player_team_history (
                        player_id, team_id, game_id, source, confidence, observed_at
                    ) VALUES (?, ?, NULL, 'espn_roster_current', 1.0, ?)
                    """,
                    (player_id, team_id, observed_at),
                )
            roster_rows.append(
                {
                    "player_id": player_id,
                    "player_name": full_name,
                    "team": abbreviation,
                    "team_id": team_id,
                    "position": position,
                    "status": str((athlete.get("status") or {}).get("type") or ""),
                }
            )

    conn.commit()
    failed_teams = {item["team"] for item in errors}
    if isinstance(previous_snapshot, dict) and failed_teams:
        roster_rows.extend(
            row
            for row in previous_snapshot.get("rows", [])
            if isinstance(row, dict) and row.get("team") in failed_teams
        )
    snapshot = {
        "captured_at": observed_at,
        "source": "espn_roster",
        "teams_refreshed": sorted(payloads),
        "rows": sorted(roster_rows, key=lambda row: (row["team"], row["player_name"])),
        "errors": sorted(errors, key=lambda item: item["team"]),
    }
    previous_rows = previous_snapshot.get("rows") if isinstance(previous_snapshot, dict) else None
    snapshot["roster_changed"] = previous_rows != snapshot["rows"]
    write_json_cache(ROSTER_CACHE_NAME, snapshot)

    return {
        "source": "espn_roster",
        "captured_at": observed_at,
        "teams_requested": len(team_abbreviations),
        "teams_refreshed": len(payloads),
        "roster_rows": len(roster_rows),
        "inserted_players": inserted_players,
        "moved_players": moved_players,
        "changed_player_ids": changed_player_ids,
        "changed_team_ids": sorted(changed_team_ids),
        "roster_changed": bool(snapshot["roster_changed"]),
        "errors": snapshot["errors"],
    }
