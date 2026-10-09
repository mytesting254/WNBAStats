"""Pregame model features from reconstructed scouting snapshots."""

from __future__ import annotations

import json
import os
import sqlite3


SCOUTING_FEATURE_VERSION = "v1"


def scouting_model_enabled() -> bool:
    return os.getenv("WNBA_GAME_SCOUTING_FEATURES", "").strip().lower() in {"1", "true", "yes", "on"}
SCOUTING_METRICS = (
    "off_efficiency", "def_efficiency", "pace", "off_effective_fg_pct",
    "def_effective_fg_pct", "off_turnover_pct", "def_turnover_pct",
    "off_three_attempt_rate", "def_three_attempt_rate",
)
TREND_METRICS = ("off_efficiency", "def_efficiency", "pace", "off_effective_fg_pct", "def_effective_fg_pct")
TEAM_FEATURE_NAMES = ("available", "games_played", *SCOUTING_METRICS, *(f"{name}_last5_delta" for name in TREND_METRICS))
MATCHUP_FEATURE_NAMES = (
    *(f"home_scout_{name}" for name in TEAM_FEATURE_NAMES),
    *(f"away_scout_{name}" for name in TEAM_FEATURE_NAMES),
    "scout_off_efficiency_edge", "scout_def_efficiency_edge", "scout_pace_diff",
    "scout_home_attack_vs_away_defense", "scout_away_attack_vs_home_defense",
    "scout_home_efg_matchup", "scout_away_efg_matchup",
    "scout_home_turnover_matchup", "scout_away_turnover_matchup",
)
MODEL_SCOUTING_FEATURE_NAMES = (
    "scout_home_efg_matchup", "scout_away_efg_matchup",
    "scout_home_turnover_matchup", "scout_away_turnover_matchup",
    "home_scout_off_effective_fg_pct_last5_delta",
    "away_scout_off_effective_fg_pct_last5_delta",
)


def _table_exists(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='team_scouting_snapshots'").fetchone() is not None


def _values(row: sqlite3.Row) -> dict[str, float]:
    offense = json.loads(row["offense_json"])
    defense = json.loads(row["defense_json"])
    values = {"pace": float(row["pace"] or 0.0)}
    for prefix, report in (("off", offense), ("def", defense)):
        for metric in ("efficiency", "effective_fg_pct", "turnover_pct", "three_attempt_rate"):
            values[f"{prefix}_{metric}"] = float(report.get(metric) or 0.0)
    return values


def team_scouting_features(
    conn: sqlite3.Connection, *, team_id: int, game_date: str,
    runtime_cache: dict | None = None,
) -> dict[str, float]:
    """Use only prior calendar dates; snapshots after this game cannot enter."""
    cache = runtime_cache.setdefault("scouting_features", {}) if runtime_cache is not None else None
    cache_key = (int(team_id), game_date)
    if cache is not None and cache_key in cache:
        return dict(cache[cache_key])
    result = {name: 0.0 for name in TEAM_FEATURE_NAMES}
    if len(game_date) >= 10 and game_date[:4].isdigit() and _table_exists(conn):
        rows = conn.execute(
            """SELECT games_played, offense_json, defense_json, pace
               FROM team_scouting_snapshots
               WHERE team_id = ? AND season = ? AND game_date < ?
               ORDER BY start_time DESC, game_id DESC LIMIT 6""",
            (int(team_id), int(game_date[:4]), game_date),
        ).fetchall()
        if rows:
            current = _values(rows[0])
            result.update(current)
            result["available"] = 1.0
            result["games_played"] = float(rows[0]["games_played"])
            if len(rows) == 6:
                earlier = _values(rows[5])
                for name in TREND_METRICS:
                    result[f"{name}_last5_delta"] = current[name] - earlier[name]
    if cache is not None:
        cache[cache_key] = dict(result)
    return result


def matchup_scouting_features(
    conn: sqlite3.Connection, *, home_team_id: int, away_team_id: int,
    game_date: str, runtime_cache: dict | None = None,
) -> dict[str, float]:
    home = team_scouting_features(conn, team_id=home_team_id, game_date=game_date, runtime_cache=runtime_cache)
    away = team_scouting_features(conn, team_id=away_team_id, game_date=game_date, runtime_cache=runtime_cache)
    result = {f"home_scout_{name}": home[name] for name in TEAM_FEATURE_NAMES}
    result.update({f"away_scout_{name}": away[name] for name in TEAM_FEATURE_NAMES})
    if home["available"] and away["available"]:
        result.update({
            "scout_off_efficiency_edge": home["off_efficiency"] - away["off_efficiency"],
            "scout_def_efficiency_edge": home["def_efficiency"] - away["def_efficiency"],
            "scout_pace_diff": home["pace"] - away["pace"],
            "scout_home_attack_vs_away_defense": home["off_efficiency"] - away["def_efficiency"],
            "scout_away_attack_vs_home_defense": away["off_efficiency"] - home["def_efficiency"],
        })
    else:
        result.update({name: 0.0 for name in MATCHUP_FEATURE_NAMES if name.startswith("scout_")})
    result["scout_home_efg_matchup"] = (
        home["off_effective_fg_pct"] - away["def_effective_fg_pct"]
        if home["available"] and away["available"] else 0.0
    )
    result["scout_away_efg_matchup"] = (
        away["off_effective_fg_pct"] - home["def_effective_fg_pct"]
        if home["available"] and away["available"] else 0.0
    )
    result["scout_home_turnover_matchup"] = (
        home["off_turnover_pct"] - away["def_turnover_pct"]
        if home["available"] and away["available"] else 0.0
    )
    result["scout_away_turnover_matchup"] = (
        away["off_turnover_pct"] - home["def_turnover_pct"]
        if home["available"] and away["available"] else 0.0
    )
    return result
