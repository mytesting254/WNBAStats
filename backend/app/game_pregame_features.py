from __future__ import annotations

import sqlite3
from typing import Any

from .player_prop_model import _historical_row_team_id, _shared_projection_context

GAME_PREGAME_FEATURE_VERSION = "v1"
ROSTER_PLAYER_LIMIT = 10


def build_team_pregame_features(
    conn: sqlite3.Connection,
    *,
    team_id: int,
    game_id: int,
    before_game_date: str | None,
    use_injury_context: bool = False,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
) -> dict[str, float]:
    players = _candidate_team_players(
        conn,
        team_id=team_id,
        before_game_date=before_game_date,
    )
    if not players:
        return _empty_team_pregame_features()

    rows: list[dict[str, float]] = []
    for player_id in players:
        projection_row = _player_team_projection_row(
            conn,
            player_id=player_id,
            team_id=team_id,
            game_id=game_id,
            before_game_date=before_game_date,
            use_injury_context=use_injury_context,
            runtime_cache=runtime_cache,
        )
        if projection_row is not None:
            rows.append(projection_row)

    if not rows:
        return _empty_team_pregame_features()

    rows.sort(key=lambda item: (item["projected_minutes"], item["projected_points"]), reverse=True)
    total_minutes = sum(item["projected_minutes"] for item in rows)
    total_points = sum(item["projected_points"] for item in rows)
    total_rebounds = sum(item["projected_rebounds"] for item in rows)
    total_assists = sum(item["projected_assists"] for item in rows)
    top_3_minutes = sum(item["projected_minutes"] for item in rows[:3])
    top_5_points = sum(item["projected_points"] for item in rows[:5])
    rotation_count = sum(1 for item in rows if item["projected_minutes"] >= 8.0)
    creator_count = sum(1 for item in rows if item["projected_assists"] >= 2.5)
    rebounder_count = sum(1 for item in rows if item["projected_rebounds"] >= 4.0)
    minute_floor_players = sum(1 for item in rows if item["projected_minutes"] >= 16.0)

    return {
        "projected_minutes_total": round(total_minutes, 3),
        "projected_points_total": round(total_points, 3),
        "projected_rebounds_total": round(total_rebounds, 3),
        "projected_assists_total": round(total_assists, 3),
        "top3_minutes_share": round(top_3_minutes / max(total_minutes, 1.0), 6),
        "top5_points_share": round(top_5_points / max(total_points, 1.0), 6),
        "rotation_count": float(rotation_count),
        "creator_count": float(creator_count),
        "rebounder_count": float(rebounder_count),
        "core_minutes_count": float(minute_floor_players),
    }


def _candidate_team_players(
    conn: sqlite3.Connection,
    *,
    team_id: int,
    before_game_date: str | None,
) -> list[int]:
    params: list[object] = [int(team_id)]
    date_filter = ""
    if before_game_date:
        date_filter = "AND g.game_date < ?"
        params.append(str(before_game_date))
    history_rows = conn.execute(
        f"""
        SELECT h.player_id, MAX(g.game_date) AS last_game_date
        FROM player_team_history h
        JOIN games g ON g.id = h.game_id
        WHERE h.team_id = ?
          {date_filter}
        GROUP BY h.player_id
        ORDER BY last_game_date DESC, h.player_id ASC
        LIMIT {ROSTER_PLAYER_LIMIT}
        """,
        params,
    ).fetchall()
    player_ids = [int(row["player_id"]) for row in history_rows]
    if len(player_ids) >= ROSTER_PLAYER_LIMIT:
        return player_ids

    current_rows = conn.execute(
        """
        SELECT id
        FROM players
        WHERE team_id = ?
        ORDER BY id ASC
        LIMIT ?
        """,
        (int(team_id), int(ROSTER_PLAYER_LIMIT)),
    ).fetchall()
    for row in current_rows:
        player_id = int(row["id"])
        if player_id not in player_ids:
            player_ids.append(player_id)
        if len(player_ids) >= ROSTER_PLAYER_LIMIT:
            break
    return player_ids


def _player_team_projection_row(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    team_id: int,
    game_id: int,
    before_game_date: str | None,
    use_injury_context: bool,
    runtime_cache: dict[str, dict[tuple, object]] | None,
) -> dict[str, float] | None:
    shared = _shared_projection_context(
        conn,
        player_id=player_id,
        game_id=game_id,
        before_game_date=before_game_date,
        allow_training=False,
        use_injury_context=use_injury_context,
        use_live_minutes_context=True,
        runtime_cache=runtime_cache,
    )
    context = shared.get("context") if isinstance(shared, dict) else None
    if not isinstance(context, dict) or int(context.get("team_id") or 0) != int(team_id):
        return None
    history_rows = list(shared.get("history_rows") or [])
    if not history_rows:
        return None
    current_team_rows = _current_team_rows(history_rows, team_id=team_id)
    sample_rows = current_team_rows or history_rows[:5]
    projected_minutes = float(shared.get("projected_minutes") or 0.0)
    if projected_minutes <= 0.0:
        return None

    points_rate = _weighted_rate(sample_rows, "points")
    rebounds_rate = _weighted_rate(sample_rows, "rebounds")
    assists_rate = _weighted_rate(sample_rows, "assists")
    return {
        "projected_minutes": projected_minutes,
        "projected_points": max(0.0, projected_minutes * points_rate),
        "projected_rebounds": max(0.0, projected_minutes * rebounds_rate),
        "projected_assists": max(0.0, projected_minutes * assists_rate),
    }


def _current_team_rows(history_rows: list[sqlite3.Row], *, team_id: int) -> list[sqlite3.Row]:
    current_rows: list[sqlite3.Row] = []
    for row in history_rows:
        if _historical_row_team_id(row) != int(team_id):
            break
        current_rows.append(row)
    return current_rows


def _weighted_rate(rows: list[sqlite3.Row], stat_key: str) -> float:
    weighted_value = 0.0
    weighted_minutes = 0.0
    for idx, row in enumerate(rows):
        weight = float(len(rows) - idx)
        minutes = max(float(row["minutes"] or 0.0), 1.0)
        value = float(row[stat_key] or 0.0)
        weighted_value += weight * value
        weighted_minutes += weight * minutes
    if weighted_minutes <= 0.0:
        return 0.0
    return weighted_value / weighted_minutes


def _empty_team_pregame_features() -> dict[str, float]:
    return {
        "projected_minutes_total": 0.0,
        "projected_points_total": 0.0,
        "projected_rebounds_total": 0.0,
        "projected_assists_total": 0.0,
        "top3_minutes_share": 0.0,
        "top5_points_share": 0.0,
        "rotation_count": 0.0,
        "creator_count": 0.0,
        "rebounder_count": 0.0,
        "core_minutes_count": 0.0,
    }


def build_matchup_pregame_features(
    conn: sqlite3.Connection,
    *,
    home_team_id: int,
    away_team_id: int,
    game_id: int,
    game_date: str | None,
    use_injury_context: bool = False,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
) -> dict[str, float]:
    home = build_team_pregame_features(
        conn,
        team_id=home_team_id,
        game_id=game_id,
        before_game_date=game_date,
        use_injury_context=use_injury_context,
        runtime_cache=runtime_cache,
    )
    away = build_team_pregame_features(
        conn,
        team_id=away_team_id,
        game_id=game_id,
        before_game_date=game_date,
        use_injury_context=use_injury_context,
        runtime_cache=runtime_cache,
    )
    return {
        "home_projected_minutes_total": home["projected_minutes_total"],
        "away_projected_minutes_total": away["projected_minutes_total"],
        "home_projected_points_total": home["projected_points_total"],
        "away_projected_points_total": away["projected_points_total"],
        "home_projected_rebounds_total": home["projected_rebounds_total"],
        "away_projected_rebounds_total": away["projected_rebounds_total"],
        "home_projected_assists_total": home["projected_assists_total"],
        "away_projected_assists_total": away["projected_assists_total"],
        "home_top3_minutes_share": home["top3_minutes_share"],
        "away_top3_minutes_share": away["top3_minutes_share"],
        "home_top5_points_share": home["top5_points_share"],
        "away_top5_points_share": away["top5_points_share"],
        "home_rotation_count": home["rotation_count"],
        "away_rotation_count": away["rotation_count"],
        "home_creator_count": home["creator_count"],
        "away_creator_count": away["creator_count"],
        "home_rebounder_count": home["rebounder_count"],
        "away_rebounder_count": away["rebounder_count"],
        "home_core_minutes_count": home["core_minutes_count"],
        "away_core_minutes_count": away["core_minutes_count"],
    }
