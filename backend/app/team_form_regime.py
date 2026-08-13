from __future__ import annotations

import math
import sqlite3


TEAM_FORM_REGIME_VERSION = "v1"
RECENT_WINDOW = 5
BASELINE_WINDOW = 20


def build_team_form_regime(
    conn: sqlite3.Connection,
    *,
    team_id: int,
    before_game_date: str | None,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
) -> dict[str, float]:
    cache = runtime_cache.setdefault("team_form_regime", {}) if runtime_cache is not None else None
    cache_key = (int(team_id), str(before_game_date or ""))
    if cache is not None and cache_key in cache:
        cached = cache[cache_key]
        if isinstance(cached, dict):
            return dict(cached)

    params: list[object] = [int(team_id)]
    date_filter = ""
    if before_game_date:
        date_filter = "AND g.game_date < ?"
        params.append(str(before_game_date))
    params.append(int(BASELINE_WINDOW))
    rows = conn.execute(
        f"""
        SELECT
            g.game_date,
            r.points,
            r.opponent_points,
            r.possessions,
            b.field_goals_made,
            b.field_goals_attempted,
            b.offensive_rebounds,
            b.defensive_rebounds,
            opp_box.field_goals_made AS opponent_field_goals_made,
            opp_box.field_goals_attempted AS opponent_field_goals_attempted,
            opp_box.offensive_rebounds AS opponent_offensive_rebounds,
            opp_box.defensive_rebounds AS opponent_defensive_rebounds,
            COALESCE(
                b.total_turnovers,
                COALESCE(b.turnovers, 0) + COALESCE(b.team_turnovers, 0),
                b.turnovers,
                b.team_turnovers,
                0
            ) AS team_turnovers_total,
            COALESCE(
                opp_box.total_turnovers,
                COALESCE(opp_box.turnovers, 0) + COALESCE(opp_box.team_turnovers, 0),
                opp_box.turnovers,
                opp_box.team_turnovers,
                0
            ) AS opponent_turnovers_total
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        LEFT JOIN team_game_boxscores b
          ON b.game_id = r.game_id
         AND b.team_id = r.team_id
        LEFT JOIN team_game_results opp
          ON opp.game_id = r.game_id
         AND opp.team_id != r.team_id
        LEFT JOIN team_game_boxscores opp_box
          ON opp_box.game_id = opp.game_id
         AND opp_box.team_id = opp.team_id
        WHERE r.team_id = ?
          {date_filter}
        ORDER BY g.game_date DESC, r.game_id DESC
        LIMIT ?
        """,
        params,
    ).fetchall()

    if not rows:
        empty = _empty_team_form_regime()
        if cache is not None:
            cache[cache_key] = dict(empty)
        return empty

    baseline_rows = list(rows)
    recent_rows = baseline_rows[:RECENT_WINDOW]

    baseline = _aggregate_form_rows(baseline_rows)
    recent = _aggregate_form_rows(recent_rows)
    recent_off_values = [_off_rating(row) for row in recent_rows]
    recent_def_values = [_def_rating(row) for row in recent_rows]

    off_delta = recent["off_rating"] - baseline["off_rating"]
    def_delta = recent["def_rating"] - baseline["def_rating"]
    pace_delta = recent["pace"] - baseline["pace"]
    fga_delta = recent["fga"] - baseline["fga"]
    fga_allowed_delta = recent["fga_allowed"] - baseline["fga_allowed"]
    miss_delta = recent["misses"] - baseline["misses"]
    miss_allowed_delta = recent["misses_allowed"] - baseline["misses_allowed"]
    oreb_rate_delta = recent["oreb_rate"] - baseline["oreb_rate"]
    oreb_allowed_rate_delta = recent["oreb_allowed_rate"] - baseline["oreb_allowed_rate"]
    turnover_rate_delta = recent["turnover_rate"] - baseline["turnover_rate"]
    forced_turnover_rate_delta = recent["forced_turnover_rate"] - baseline["forced_turnover_rate"]
    off_std = _stddev(recent_off_values)
    def_std = _stddev(recent_def_values)

    payload = {
        "off_form_delta": round(off_delta, 3),
        "def_form_delta": round(def_delta, 3),
        "pace_form_delta": round(pace_delta, 3),
        "fga_form_delta": round(fga_delta, 3),
        "fga_allowed_form_delta": round(fga_allowed_delta, 3),
        "miss_form_delta": round(miss_delta, 3),
        "miss_allowed_form_delta": round(miss_allowed_delta, 3),
        "oreb_rate_form_delta": round(oreb_rate_delta, 4),
        "oreb_allowed_rate_form_delta": round(oreb_allowed_rate_delta, 4),
        "turnover_rate_form_delta": round(turnover_rate_delta, 4),
        "forced_turnover_rate_form_delta": round(forced_turnover_rate_delta, 4),
        "off_form_volatility": round(off_std, 3),
        "def_form_volatility": round(def_std, 3),
        "hot_offense_flag": 1.0 if off_delta >= 4.0 else 0.0,
        "slump_offense_flag": 1.0 if off_delta <= -4.0 else 0.0,
        "hot_defense_flag": 1.0 if def_delta <= -4.0 else 0.0,
        "slump_defense_flag": 1.0 if def_delta >= 4.0 else 0.0,
    }
    if cache is not None:
        cache[cache_key] = dict(payload)
    return payload


def _aggregate_form_rows(rows: list[sqlite3.Row]) -> dict[str, float]:
    if not rows:
        return {
            "off_rating": 100.0,
            "def_rating": 100.0,
            "pace": 78.0,
            "fga": 0.0,
            "fga_allowed": 0.0,
            "misses": 0.0,
            "misses_allowed": 0.0,
            "oreb_rate": 0.0,
            "oreb_allowed_rate": 0.0,
            "turnover_rate": 0.0,
            "forced_turnover_rate": 0.0,
        }
    miss_values = [_misses(row, made_key="field_goals_made", attempted_key="field_goals_attempted") for row in rows]
    miss_allowed_values = [_misses(row, made_key="opponent_field_goals_made", attempted_key="opponent_field_goals_attempted") for row in rows]
    return {
        "off_rating": _mean([_off_rating(row) for row in rows]),
        "def_rating": _mean([_def_rating(row) for row in rows]),
        "pace": _mean([float(row["possessions"] or 78.0) for row in rows]),
        "fga": _mean([float(row["field_goals_attempted"] or 0.0) for row in rows]),
        "fga_allowed": _mean([float(row["opponent_field_goals_attempted"] or 0.0) for row in rows]),
        "misses": _mean(miss_values),
        "misses_allowed": _mean(miss_allowed_values),
        "oreb_rate": _mean([
            _rebound_rate(
                row,
                offensive_key="offensive_rebounds",
                own_miss_key="field_goals_attempted",
                own_made_key="field_goals_made",
            )
            for row in rows
        ]),
        "oreb_allowed_rate": _mean([
            _rebound_rate(
                row,
                offensive_key="opponent_offensive_rebounds",
                own_miss_key="opponent_field_goals_attempted",
                own_made_key="opponent_field_goals_made",
            )
            for row in rows
        ]),
        "turnover_rate": _mean([_turnover_rate(row, "team_turnovers_total") for row in rows]),
        "forced_turnover_rate": _mean([_turnover_rate(row, "opponent_turnovers_total") for row in rows]),
    }


def _off_rating(row: sqlite3.Row) -> float:
    possessions = max(float(row["possessions"] or 78.0), 1.0)
    return (100.0 * float(row["points"] or 0.0)) / possessions


def _def_rating(row: sqlite3.Row) -> float:
    possessions = max(float(row["possessions"] or 78.0), 1.0)
    return (100.0 * float(row["opponent_points"] or 0.0)) / possessions


def _turnover_rate(row: sqlite3.Row, key: str) -> float:
    possessions = max(float(row["possessions"] or 78.0), 1.0)
    return float(row[key] or 0.0) / possessions


def _misses(row: sqlite3.Row, *, made_key: str, attempted_key: str) -> float:
    attempted = float(row[attempted_key] or 0.0)
    made = float(row[made_key] or 0.0)
    return max(attempted - made, 0.0)


def _rebound_rate(
    row: sqlite3.Row,
    *,
    offensive_key: str,
    own_miss_key: str,
    own_made_key: str,
) -> float:
    misses = _misses(row, made_key=own_made_key, attempted_key=own_miss_key)
    if misses <= 0.0:
        return 0.0
    return float(row[offensive_key] or 0.0) / misses


def _mean(values: list[float]) -> float:
    if not values:
        return 0.0
    return sum(values) / len(values)


def _stddev(values: list[float]) -> float:
    if len(values) < 2:
        return 0.0
    mean = _mean(values)
    variance = sum((value - mean) * (value - mean) for value in values) / len(values)
    return math.sqrt(max(variance, 0.0))


def _empty_team_form_regime() -> dict[str, float]:
    return {
        "off_form_delta": 0.0,
        "def_form_delta": 0.0,
        "pace_form_delta": 0.0,
        "fga_form_delta": 0.0,
        "fga_allowed_form_delta": 0.0,
        "miss_form_delta": 0.0,
        "miss_allowed_form_delta": 0.0,
        "oreb_rate_form_delta": 0.0,
        "oreb_allowed_rate_form_delta": 0.0,
        "turnover_rate_form_delta": 0.0,
        "forced_turnover_rate_form_delta": 0.0,
        "off_form_volatility": 0.0,
        "def_form_volatility": 0.0,
        "hot_offense_flag": 0.0,
        "slump_offense_flag": 0.0,
        "hot_defense_flag": 0.0,
        "slump_defense_flag": 0.0,
    }
