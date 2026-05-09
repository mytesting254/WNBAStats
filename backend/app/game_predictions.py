from __future__ import annotations

import sqlite3
from typing import Any, Mapping


def project_game(conn: sqlite3.Connection, game: Mapping[str, Any]) -> dict:
    home_team_id = int(game["home_team_id"])
    away_team_id = int(game["away_team_id"])
    home_history_count = _team_result_count(conn, home_team_id)
    away_history_count = _team_result_count(conn, away_team_id)

    home_projection = _project_team_points(
        conn,
        team_id=home_team_id,
        opponent_id=away_team_id,
        is_home=True,
        rest_days=int(game["rest_days_home"] or 2),
    )
    away_projection = _project_team_points(
        conn,
        team_id=away_team_id,
        opponent_id=home_team_id,
        is_home=False,
        rest_days=int(game["rest_days_away"] or 2),
    )
    projected_margin = home_projection - away_projection
    projected_total = home_projection + away_projection
    spread_home = float(game["spread_home"]) if game["spread_home"] is not None else None
    game_total = float(game["game_total"]) if game["game_total"] is not None else None

    ats_edge = None
    ats_pick = "N/A"
    if spread_home is not None:
        ats_edge = projected_margin + spread_home
        ats_pick = f"{game['home_team']} {format_spread(spread_home)}" if ats_edge > 0 else f"{game['away_team']} {format_spread(-spread_home)}"

    total_edge = None
    total_pick = "N/A"
    if game_total is not None:
        total_edge = projected_total - game_total
        total_pick = "Over" if total_edge > 0 else "Under"

    winner = game["home_team"] if projected_margin >= 0 else game["away_team"]
    confidence = _confidence(abs(ats_edge or 0), abs(total_edge or 0), abs(projected_margin))

    return {
        "home_projected_points": round(home_projection, 1),
        "away_projected_points": round(away_projection, 1),
        "projected_margin": round(projected_margin, 1),
        "projected_total": round(projected_total, 1),
        "winner_pick": winner,
        "ats_pick": ats_pick,
        "ats_edge": round(ats_edge, 1) if ats_edge is not None else None,
        "total_pick": total_pick,
        "total_edge": round(total_edge, 1) if total_edge is not None else None,
        "game_confidence": confidence,
        "game_reason": _reason(
            home_projection,
            away_projection,
            projected_margin,
            projected_total,
            home_history_count,
            away_history_count,
        ),
    }


def format_spread(value: float) -> str:
    return f"+{value:.1f}" if value > 0 else f"{value:.1f}"


def _insufficient_history_payload(game: Mapping[str, Any], home_history_count: int, away_history_count: int) -> dict:
    return {
        "home_projected_points": None,
        "away_projected_points": None,
        "projected_margin": None,
        "projected_total": None,
        "winner_pick": "N/A",
        "ats_pick": "N/A",
        "ats_edge": None,
        "total_pick": "N/A",
        "total_edge": None,
        "game_confidence": "insufficient history",
        "game_reason": (
            f"Not enough imported history to calculate a model projection for {game['away_team']} at {game['home_team']}. "
            f"History rows available: {game['home_team']} {home_history_count}, {game['away_team']} {away_history_count}. "
            "Use sportsbook props and line discrepancies for this game until real team results are imported."
        ),
    }


def _project_team_points(
    conn: sqlite3.Connection,
    team_id: int,
    opponent_id: int,
    is_home: bool,
    rest_days: int,
) -> float:
    team_recent = _weighted_recent(conn, team_id, "points")
    team_long = _average(conn, team_id, "points")
    opponent_allowed = _average(conn, opponent_id, "opponent_points")
    pace_factor = _pace_factor(conn, team_id, opponent_id)
    home_factor = 1.025 if is_home else 0.985
    rest_factor = _rest_factor(rest_days)
    offense = (0.52 * team_recent) + (0.23 * team_long) + (0.25 * opponent_allowed)
    return offense * pace_factor * home_factor * rest_factor


def _weighted_recent(conn: sqlite3.Connection, team_id: int, column: str) -> float:
    rows = conn.execute(
        f"""
        SELECT {column} AS value
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        WHERE r.team_id = ?
        ORDER BY g.game_date DESC
        LIMIT 5
        """,
        (team_id,),
    ).fetchall()
    if not rows:
        return _league_average(conn, column)
    values = [float(row["value"]) for row in rows]
    weights = list(range(len(values), 0, -1))
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _average(conn: sqlite3.Connection, team_id: int, column: str) -> float:
    value = conn.execute(
        f"SELECT AVG({column}) FROM team_game_results WHERE team_id = ?",
        (team_id,),
    ).fetchone()[0]
    return float(value) if value is not None else _league_average(conn, column)


def _league_average(conn: sqlite3.Connection, column: str) -> float:
    value = conn.execute(f"SELECT AVG({column}) FROM team_game_results").fetchone()[0]
    return float(value) if value is not None else 80.0


def _team_result_count(conn: sqlite3.Connection, team_id: int) -> int:
    return int(conn.execute("SELECT COUNT(*) FROM team_game_results WHERE team_id = ?", (team_id,)).fetchone()[0])


def _pace_factor(conn: sqlite3.Connection, team_id: int, opponent_id: int) -> float:
    league = conn.execute("SELECT AVG(possessions) FROM team_game_results").fetchone()[0] or 78.0
    team = conn.execute("SELECT AVG(possessions) FROM team_game_results WHERE team_id = ?", (team_id,)).fetchone()[0] or league
    opponent = conn.execute("SELECT AVG(possessions) FROM team_game_results WHERE team_id = ?", (opponent_id,)).fetchone()[0] or league
    return _clamp(((float(team) + float(opponent)) / 2) / float(league), 0.94, 1.06)


def _rest_factor(rest_days: int) -> float:
    if rest_days <= 0:
        return 0.97
    if rest_days == 1:
        return 0.99
    if rest_days >= 4:
        return 1.01
    return 1.0


def _confidence(ats_edge: float, total_edge: float, margin: float) -> str:
    strongest_edge = max(ats_edge, total_edge)
    if strongest_edge >= 5 or margin >= 10:
        return "high"
    if strongest_edge >= 2.5 or margin >= 5:
        return "medium"
    return "low"


def _reason(
    home_projection: float,
    away_projection: float,
    margin: float,
    total: float,
    home_history_count: int,
    away_history_count: int,
) -> str:
    if min(home_history_count, away_history_count) < 3:
        return (
            f"Projected score {home_projection:.1f}-{away_projection:.1f}; margin {margin:.1f}, total {total:.1f}. "
            f"Limited imported history for this matchup ({home_history_count} home-team rows, "
            f"{away_history_count} away-team rows), so treat this as a market/context view, not a model edge."
        )
    return (
        f"Projected score {home_projection:.1f}-{away_projection:.1f}; "
        f"margin {margin:.1f}, total {total:.1f}. Built from recent scoring, season scoring, "
        "opponent points allowed, pace, home/away, and rest."
    )


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
