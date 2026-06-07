from __future__ import annotations

import sqlite3
from typing import Any, Mapping


class _GamePredictionCache:
    def __init__(self, conn: sqlite3.Connection, team_ids: tuple[int, ...]):
        self.conn = conn
        self._team_summaries: dict[int, dict[str, float]] = {}
        self._league_possessions: float | None = None
        self._weighted_recent_cache: dict[tuple[int, str], float] = {}
        self._injury_factor_cache: dict[int, dict[str, float | int | str]] = {}
        self._prepare_team_summaries(team_ids)

    def _prepare_team_summaries(self, team_ids: tuple[int, ...]) -> None:
        unique_ids = tuple(dict.fromkeys(team_ids))
        if not unique_ids:
            return
        placeholders = ",".join("?" for _ in unique_ids)
        rows = self.conn.execute(
            f"""
            SELECT team_id,
                   AVG(points) AS avg_points,
                   AVG(opponent_points) AS avg_opponent_points,
                   AVG(possessions) AS avg_possessions,
                   COUNT(*) AS result_count
            FROM team_game_results
            WHERE team_id IN ({placeholders})
            GROUP BY team_id
            """,
            unique_ids,
        ).fetchall()
        for row in rows:
            self._team_summaries[int(row["team_id"])] = {
                "avg_points": float(row["avg_points"]) if row["avg_points"] is not None else None,
                "avg_opponent_points": float(row["avg_opponent_points"]) if row["avg_opponent_points"] is not None else None,
                "avg_possessions": float(row["avg_possessions"]) if row["avg_possessions"] is not None else None,
                "result_count": int(row["result_count"]),
            }

    def team_summary(self, team_id: int) -> dict[str, float]:
        return self._team_summaries.get(team_id, {
            "avg_points": None,
            "avg_opponent_points": None,
            "avg_possessions": None,
            "result_count": 0,
        })

    def league_possessions(self) -> float:
        if self._league_possessions is None:
            value = self.conn.execute("SELECT AVG(possessions) FROM team_game_results").fetchone()[0]
            self._league_possessions = float(value) if value is not None else 78.0
        return self._league_possessions

    def weighted_recent(self, team_id: int, column: str) -> float:
        key = (team_id, column)
        if key not in self._weighted_recent_cache:
            rows = self.conn.execute(
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
                self._weighted_recent_cache[key] = self.team_average(team_id, column)
            else:
                values = [float(row["value"]) for row in rows]
                weights = list(range(len(values), 0, -1))
                self._weighted_recent_cache[key] = sum(value * weight for value, weight in zip(values, weights)) / sum(weights)
        return self._weighted_recent_cache[key]

    def team_average(self, team_id: int, column: str) -> float:
        summary = self.team_summary(team_id)
        avg_value = summary["avg_points"] if column == "points" else summary["avg_opponent_points"] if column == "opponent_points" else summary["avg_possessions"] if column == "possessions" else None
        if avg_value is not None:
            return avg_value
        return self.league_possessions() if column == "possessions" else self.league_average(column)

    def league_average(self, column: str) -> float:
        value = self.conn.execute(f"SELECT AVG({column}) FROM team_game_results").fetchone()[0]
        return float(value) if value is not None else 80.0

    def team_count(self, team_id: int) -> int:
        return self.team_summary(team_id)["result_count"]

    def pace_factor(self, team_id: int, opponent_id: int) -> float:
        league = self.league_possessions()
        team = self.team_summary(team_id)["avg_possessions"] or league
        opponent = self.team_summary(opponent_id)["avg_possessions"] or league
        return _clamp(((float(team) + float(opponent)) / 2) / float(league), 0.94, 1.06)

    def injury_impact(self, team_id: int) -> dict[str, float | int | str]:
        if team_id in self._injury_factor_cache:
            return self._injury_factor_cache[team_id]
        impact = _team_injury_impact(self.conn, team_id)
        self._injury_factor_cache[team_id] = impact
        return impact


def project_game(conn: sqlite3.Connection, game: Mapping[str, Any]) -> dict:
    home_team_id = int(game["home_team_id"])
    away_team_id = int(game["away_team_id"])
    cache = _GamePredictionCache(conn, (home_team_id, away_team_id))
    home_history_count = cache.team_count(home_team_id)
    away_history_count = cache.team_count(away_team_id)

    home_injury = cache.injury_impact(home_team_id)
    away_injury = cache.injury_impact(away_team_id)
    home_projection = _project_team_points(
        cache,
        team_id=home_team_id,
        opponent_id=away_team_id,
        is_home=True,
        rest_days=int(game["rest_days_home"] or 2),
        injury_factor=float(home_injury["factor"]),
    )
    away_projection = _project_team_points(
        cache,
        team_id=away_team_id,
        opponent_id=home_team_id,
        is_home=False,
        rest_days=int(game["rest_days_away"] or 2),
        injury_factor=float(away_injury["factor"]),
    )
    projected_margin = home_projection - away_projection
    projected_total = home_projection + away_projection
    spread_home = float(game["spread_home"]) if game["spread_home"] is not None else None
    game_total = float(game["game_total"]) if game["game_total"] is not None and float(game["game_total"]) > 0 else None

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
            game["home_team"],
            game["away_team"],
            home_injury,
            away_injury,
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
    cache: _GamePredictionCache,
    team_id: int,
    opponent_id: int,
    is_home: bool,
    rest_days: int,
    injury_factor: float = 1.0,
) -> float:
    team_recent = cache.weighted_recent(team_id, "points")
    team_long = cache.team_average(team_id, "points")
    opponent_allowed = cache.team_average(opponent_id, "opponent_points")
    pace_factor = cache.pace_factor(team_id, opponent_id)
    home_factor = 1.025 if is_home else 0.985
    rest_factor = _rest_factor(rest_days)
    offense = (0.52 * team_recent) + (0.23 * team_long) + (0.25 * opponent_allowed)
    return offense * pace_factor * home_factor * rest_factor * injury_factor


def _weighted_recent(cache: _GamePredictionCache, team_id: int, column: str) -> float:
    return cache.weighted_recent(team_id, column)


def _average(cache: _GamePredictionCache, team_id: int, column: str) -> float:
    return cache.team_average(team_id, column)


def _league_average(cache: _GamePredictionCache, column: str) -> float:
    return cache.league_average(column)


def _team_result_count(cache: _GamePredictionCache, team_id: int) -> int:
    return cache.team_count(team_id)


def _pace_factor(cache: _GamePredictionCache, team_id: int, opponent_id: int) -> float:
    return cache.pace_factor(team_id, opponent_id)


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
    home_team: str,
    away_team: str,
    home_injury: Mapping[str, float | int | str],
    away_injury: Mapping[str, float | int | str],
) -> str:
    injury_detail = _injury_reason_detail(home_team, away_team, home_injury, away_injury)
    if min(home_history_count, away_history_count) < 3:
        return (
            f"Projected score {home_projection:.1f}-{away_projection:.1f}; margin {margin:.1f}, total {total:.1f}. "
            f"Limited imported history for this matchup ({home_history_count} home-team rows, "
            f"{away_history_count} away-team rows), so treat this as a market/context view, not a model edge."
            f"{injury_detail}"
        )
    return (
        f"Projected score {home_projection:.1f}-{away_projection:.1f}; "
        f"margin {margin:.1f}, total {total:.1f}. Built from recent scoring, season scoring, "
        f"opponent points allowed, pace, home/away, rest, and injury availability.{injury_detail}"
    )


def _team_injury_impact(conn: sqlite3.Connection, team_id: int) -> dict[str, float | int | str]:
    rows = conn.execute(
        """
        SELECT
            p.id AS player_id,
            lower(trim(i.status)) AS status,
            p.rotation_role,
            COALESCE(
                (
                    SELECT AVG(sample.contrib)
                    FROM (
                        SELECT
                            (s.points + (0.70 * s.rebounds) + (0.70 * s.assists)) AS contrib
                        FROM player_game_stats s
                        JOIN games g ON g.id = s.game_id
                        WHERE s.player_id = p.id
                        ORDER BY g.game_date DESC
                        LIMIT 10
                    ) sample
                ),
                0.0
            ) AS contribution
        FROM injuries i
        JOIN players p ON p.id = i.player_id
        WHERE p.team_id = ?
          AND i.captured_at = (
              SELECT MAX(i2.captured_at)
              FROM injuries i2
              WHERE i2.player_id = i.player_id
          )
        """,
        (team_id,),
    ).fetchall()
    if not rows:
        return {"factor": 1.0, "missing_key_players": 0, "penalty_points": 0.0}

    status_weight = {
        "out": 1.0,
        "inactive": 1.0,
        "suspended": 1.0,
        "unavailable": 1.0,
        "doubtful": 0.75,
        "questionable": 0.35,
        "probable": 0.10,
    }
    role_weight = {
        "star": 1.25,
        "starter": 1.0,
        "rotation": 0.75,
        "bench": 0.5,
    }

    penalty_points = 0.0
    missing_key_players = 0
    for row in rows:
        status = str(row["status"] or "").strip()
        status_factor = status_weight.get(status)
        if status_factor is None:
            continue
        contribution = float(row["contribution"] or 0.0)
        if contribution <= 0:
            continue
        role_factor = role_weight.get(str(row["rotation_role"] or "starter").strip().lower(), 0.9)
        weighted_impact = contribution * status_factor * role_factor
        penalty_points += weighted_impact
        if status_factor >= 0.75 and role_factor >= 1.0:
            missing_key_players += 1

    # Translate contribution penalty into a bounded offense multiplier.
    # About 18 contribution points roughly maps to ~8% team offense impact.
    penalty_ratio = _clamp((penalty_points / 18.0) * 0.08, 0.0, 0.18)
    return {
        "factor": _clamp(1.0 - penalty_ratio, 0.82, 1.0),
        "missing_key_players": missing_key_players,
        "penalty_points": round(penalty_points, 2),
    }


def _injury_reason_detail(
    home_team: str,
    away_team: str,
    home_injury: Mapping[str, float | int | str],
    away_injury: Mapping[str, float | int | str],
) -> str:
    details = []
    home_missing = int(home_injury.get("missing_key_players", 0) or 0)
    away_missing = int(away_injury.get("missing_key_players", 0) or 0)
    if home_missing > 0:
        details.append(f"{home_team} missing key contributors: {home_missing}")
    if away_missing > 0:
        details.append(f"{away_team} missing key contributors: {away_missing}")
    if not details:
        return " Injury adjustment: no major absences detected."
    return " Injury adjustment: " + "; ".join(details) + "."


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
