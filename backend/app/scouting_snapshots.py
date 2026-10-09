"""Rebuildable, game-by-game WNBA team scouting reports.

Snapshots are calculated from local box scores. ESPN summary cache files supply
the season phase, which the games table does not currently store.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from pathlib import Path


VERSION = 2
STATS = (
    "points", "field_goals_made", "field_goals_attempted", "threes_made",
    "threes_attempted", "free_throws_made", "free_throws_attempted",
    "offensive_rebounds", "defensive_rebounds", "total_turnovers", "turnovers",
)


def ensure_schema(conn: sqlite3.Connection) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS team_scouting_snapshots (
            game_id INTEGER NOT NULL,
            team_id INTEGER NOT NULL,
            season INTEGER NOT NULL,
            phase TEXT NOT NULL,
            game_date TEXT NOT NULL,
            start_time TEXT NOT NULL,
            games_played INTEGER NOT NULL,
            regular_games INTEGER NOT NULL,
            postseason_games INTEGER NOT NULL,
            offense_json TEXT NOT NULL,
            defense_json TEXT NOT NULL,
            pace REAL,
            formula_version INTEGER NOT NULL,
            PRIMARY KEY (game_id, team_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_team_scouting_season_time
        ON team_scouting_snapshots(team_id, season, start_time, game_id)
    """)


def _phase(cache_dir: Path, event_id: int) -> str | None:
    path = cache_dir / f"espn_wnba_summary_{event_id}.json"
    if not path.is_file():
        return None
    try:
        header = json.loads(path.read_text())["header"]
        season_type = header["season"]["type"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if "commissioner's cup championship" in str(header.get("gameNote") or "").lower():
        return "cup_final"
    return {1: "preseason", 2: "regular", 3: "postseason"}.get(season_type)


def _number(row: sqlite3.Row, key: str) -> float | None:
    value = row[key]
    return float(value) if value is not None else None


def _team_game(row: sqlite3.Row) -> dict[str, float] | None:
    result = {key: _number(row, key) for key in STATS}
    if result["points"] is None:
        result["points"] = _number(row, "result_points")
    if result["total_turnovers"] is None:
        player = _number(row, "turnovers")
        team = _number(row, "team_turnovers")
        result["total_turnovers"] = (player or 0.0) + (team or 0.0) if player is not None or team is not None else None
    if result["turnovers"] is None:
        result["turnovers"] = result["total_turnovers"]
    if any(result[key] is None for key in STATS):
        return None
    if result["field_goals_attempted"] <= 0:
        return None
    result["possessions"] = (result["field_goals_attempted"] - result["offensive_rebounds"]
                             + result["turnovers"] + 0.44 * result["free_throws_attempted"])
    return result if result["possessions"] > 0 else None


def _rate(numerator: float, denominator: float) -> float | None:
    return round(100.0 * numerator / denominator, 3) if denominator > 0 else None


def _report(own: dict[str, float], other: dict[str, float]) -> dict[str, float | None]:
    attempts = own["field_goals_attempted"]
    threes_attempted = own["threes_attempted"]
    twos_attempted = attempts - threes_attempted
    return {
        "efficiency": _rate(own["points"], own["possessions"]),
        "effective_fg_pct": _rate(own["field_goals_made"] + 0.5 * own["threes_made"], attempts),
        "turnover_pct": _rate(own["turnovers"], attempts + 0.44 * own["free_throws_attempted"] + own["turnovers"]),
        "off_reb_pct": _rate(own["offensive_rebounds"], own["offensive_rebounds"] + other["defensive_rebounds"]),
        "fta_per_fga": _rate(own["free_throws_attempted"], attempts),
        "three_pct": _rate(own["threes_made"], threes_attempted),
        "two_pct": _rate(own["field_goals_made"] - own["threes_made"], twos_attempted),
        "ft_pct": _rate(own["free_throws_made"], own["free_throws_attempted"]),
        "three_attempt_rate": _rate(threes_attempted, attempts),
    }


def _accumulate(target: dict[str, float], game: dict[str, float]) -> None:
    for key, value in game.items():
        target[key] += value


def rebuild_snapshots(conn: sqlite3.Connection, cache_dir: Path, *, season: int | None = None) -> dict[str, int]:
    """Replace selected season snapshots atomically from settled local games.

    Missing phase or box-score data is skipped and counted, never guessed.
    Regular and postseason games share one cumulative season timeline.
    """
    ensure_schema(conn)
    where = "WHERE substr(g.game_date, 1, 4) = ?" if season is not None else ""
    params = (str(season),) if season is not None else ()
    rows = conn.execute(f"""
        SELECT g.id AS game_id, g.game_date, g.start_time, g.espn_event_id,
               r.team_id, r.points AS result_points, r.possessions AS result_possessions,
               r.possessions_source, b.points, b.field_goals_made,
               b.field_goals_attempted, b.threes_made, b.threes_attempted,
               b.free_throws_made, b.free_throws_attempted, b.offensive_rebounds,
               b.defensive_rebounds, b.total_turnovers, b.turnovers,
               b.team_turnovers, b.possessions AS box_possessions
        FROM games g
        JOIN team_game_results r ON r.game_id = g.id
        LEFT JOIN team_game_boxscores b ON b.game_id = g.id AND b.team_id = r.team_id
        {where}
        ORDER BY g.start_time, g.id, r.team_id
    """, params).fetchall()
    games: dict[int, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        games[int(row["game_id"])].append(row)
    totals: dict[tuple[int, int], dict[str, float]] = defaultdict(lambda: defaultdict(float))
    counts: dict[tuple[int, int], dict[str, int]] = defaultdict(lambda: defaultdict(int))
    report = {"games": 0, "snapshots": 0, "missing_phase": 0, "missing_boxscore": 0, "preseason": 0, "cup_final": 0}
    replacements: list[tuple] = []
    for game_rows in games.values():
        first = game_rows[0]
        event_id = first["espn_event_id"]
        phase = _phase(cache_dir, int(event_id)) if event_id is not None else None
        if phase is None:
            report["missing_phase"] += 1
            continue
        if phase == "preseason":
            report["preseason"] += 1
            continue
        if phase == "cup_final":
            report["cup_final"] += 1
            continue
        if len(game_rows) != 2:
            report["missing_boxscore"] += 1
            continue
        sides = [_team_game(row) for row in game_rows]
        if any(side is None for side in sides):
            report["missing_boxscore"] += 1
            continue
        year = int(str(first["game_date"])[:4])
        report["games"] += 1
        for index, row in enumerate(game_rows):
            team_id = int(row["team_id"])
            key = (year, team_id)
            own = sides[index]
            other = sides[1 - index]
            _accumulate(totals[key], own)
            counts[key][phase] += 1
            totals[key]["opponent_possessions"] += other["possessions"]
            # Keep all opponent counts, including shot attempts and points.
            for stat in STATS:
                totals[key]["opponent_" + stat] += other[stat]
            own_totals = totals[key]
            opponent_totals = {stat: own_totals["opponent_" + stat] for stat in STATS}
            opponent_totals["possessions"] = own_totals["opponent_possessions"]
            offense = _report(own_totals, opponent_totals)
            defense = _report(opponent_totals, own_totals)
            played = counts[key]["regular"] + counts[key]["postseason"]
            replacements.append((int(row["game_id"]), team_id, year, phase, row["game_date"],
                                 row["start_time"], played, counts[key]["regular"],
                                 counts[key]["postseason"], json.dumps(offense, sort_keys=True),
                                 json.dumps(defense, sort_keys=True),
                                 round(1.2 * (own_totals["possessions"] + opponent_totals["possessions"]) / (2 * played), 3), VERSION))
            report["snapshots"] += 1
    if report["missing_phase"] or report["missing_boxscore"]:
        raise ValueError(f"Incomplete scouting source; existing snapshots preserved: {report}")
    with conn:
        if season is None:
            conn.execute("DELETE FROM team_scouting_snapshots")
        else:
            conn.execute("DELETE FROM team_scouting_snapshots WHERE season = ?", (season,))
        conn.executemany("""
            INSERT INTO team_scouting_snapshots
            (game_id, team_id, season, phase, game_date, start_time, games_played,
             regular_games, postseason_games, offense_json, defense_json, pace, formula_version)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, replacements)
    return report


def latest_snapshot_before(
    conn: sqlite3.Connection, *, team_id: int, season: int, start_time: str
) -> dict | None:
    """Fetch the last snapshot strictly before a scheduled game's start."""
    row = conn.execute("""
        SELECT * FROM team_scouting_snapshots
        WHERE team_id = ? AND season = ? AND start_time < ?
        ORDER BY start_time DESC, game_id DESC LIMIT 1
    """, (team_id, season, start_time)).fetchone()
    if row is None:
        return None
    return {**dict(row), "offense": json.loads(row["offense_json"]),
            "defense": json.loads(row["defense_json"])}
