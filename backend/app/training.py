from __future__ import annotations

import json
import math
import sqlite3
from datetime import datetime, timezone

from .player_prop_model import MODEL_VERSION as LEARNED_MODEL_VERSION
from .player_prop_model import TRAINING_MARKETS, evaluate_market_model

TRAINING_MODEL_VERSION = LEARNED_MODEL_VERSION
COMPONENT_MODEL_VERSION = "component-pregame-v2"


def run_walk_forward_training(conn: sqlite3.Connection) -> dict:
    component_run = _run_component_benchmark(conn)
    _save_model_run(conn, component_run)

    started_at = datetime.now(timezone.utc).isoformat()
    metrics = {}
    total_rows = 0

    for market in TRAINING_MARKETS:
        result = evaluate_market_model(conn, market)
        metrics[market] = result
        total_rows += int(result["rows"])

    finished_at = datetime.now(timezone.utc).isoformat()
    status = "completed" if total_rows > 0 else "no_data"
    learned_run = {
        "model_version": TRAINING_MODEL_VERSION,
        "run_type": "chronological_holdout",
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "training_rows": total_rows,
        "markets": TRAINING_MARKETS,
        "metrics": metrics,
        "notes": "Chronological 80/20 holdout for the adaptive history/context regression model.",
    }
    _save_model_run(conn, learned_run)
    conn.commit()
    return learned_run


def list_model_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = conn.execute(
        """
        SELECT *
        FROM model_runs
        ORDER BY started_at DESC, id DESC
        LIMIT ?
        """,
        (limit,),
    ).fetchall()
    return [_serialize_run(row) for row in rows]


def latest_model_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM model_runs
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return _serialize_run(row) if row else None


def _run_component_benchmark(conn: sqlite3.Connection) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    metrics = {}
    total_rows = 0
    for market in TRAINING_MARKETS:
        result = _evaluate_market(conn, market)
        metrics[market] = result
        total_rows += int(result["rows"])
    finished_at = datetime.now(timezone.utc).isoformat()
    return {
        "model_version": COMPONENT_MODEL_VERSION,
        "run_type": "walk_forward_backtest",
        "status": "completed" if total_rows > 0 else "no_data",
        "started_at": started_at,
        "finished_at": finished_at,
        "training_rows": total_rows,
        "markets": TRAINING_MARKETS,
        "metrics": metrics,
        "notes": "Component formula benchmark using only prior player games for each target row.",
    }


def _save_model_run(conn: sqlite3.Connection, run: dict) -> None:
    conn.execute(
        """
        INSERT INTO model_runs (
            model_version, run_type, status, started_at, finished_at,
            training_rows, markets, metrics_json, notes
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            run["model_version"],
            run["run_type"],
            run["status"],
            run["started_at"],
            run["finished_at"],
            run["training_rows"],
            json.dumps(run["markets"]),
            json.dumps(run["metrics"]),
            run.get("notes"),
        ),
    )


def _evaluate_market(conn: sqlite3.Connection, market: str) -> dict:
    players = conn.execute("SELECT id FROM players ORDER BY id").fetchall()
    errors = []
    absolute_errors = []
    squared_errors = []
    direction_hits = 0
    direction_total = 0

    for player in players:
        rows = conn.execute(
            """
            SELECT s.*, g.game_date
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            WHERE s.player_id = ?
            ORDER BY g.game_date ASC, s.game_id ASC
            """,
            (player["id"],),
        ).fetchall()
        values = [_market_value(row, market) for row in rows]
        minutes = [float(row["minutes"]) for row in rows]

        for idx in range(5, len(values)):
            history = values[max(0, idx - 10):idx]
            minute_history = minutes[max(0, idx - 10):idx]
            actual = values[idx]
            projection = _project_from_history(history, minute_history)
            error = projection - actual
            errors.append(error)
            absolute_errors.append(abs(error))
            squared_errors.append(error * error)

            baseline = sum(history) / len(history)
            if (projection >= baseline and actual >= baseline) or (projection < baseline and actual < baseline):
                direction_hits += 1
            direction_total += 1

    if not errors:
        return {
            "rows": 0,
            "mae": None,
            "rmse": None,
            "bias": None,
            "directional_accuracy": None,
        }

    rows = len(errors)
    return {
        "rows": rows,
        "mae": round(sum(absolute_errors) / rows, 3),
        "rmse": round(math.sqrt(sum(squared_errors) / rows), 3),
        "bias": round(sum(errors) / rows, 3),
        "directional_accuracy": round(direction_hits / max(direction_total, 1), 3),
    }


def _project_from_history(values: list[float], minutes: list[float]) -> float:
    weighted_recent = _weighted_average(values)
    last_5 = values[-5:]
    last_5_avg = sum(last_5) / len(last_5)
    last_10_avg = sum(values) / len(values)
    rates = [value / max(minute, 1.0) for value, minute in zip(values, minutes)]
    avg_minutes = sum(minutes[-5:]) / min(len(minutes), 5)
    rate_projection = _weighted_average(rates) * avg_minutes
    return (
        (0.40 * weighted_recent)
        + (0.25 * last_5_avg)
        + (0.20 * rate_projection)
        + (0.15 * last_10_avg)
    )


def _weighted_average(values: list[float]) -> float:
    weights = list(range(1, len(values) + 1))
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _market_value(row: sqlite3.Row, market: str) -> float:
    if market == "points_rebounds":
        return float(row["points"] + row["rebounds"])
    if market == "points_assists":
        return float(row["points"] + row["assists"])
    if market == "rebounds_assists":
        return float(row["rebounds"] + row["assists"])
    if market == "points_rebounds_assists":
        return float(row["points"] + row["rebounds"] + row["assists"])
    if market == "blocks_steals":
        return float(row["blocks"] + row["steals"])
    return float(row[market])


def _serialize_run(row: sqlite3.Row) -> dict:
    payload = dict(row)
    payload["markets"] = json.loads(payload["markets"])
    payload["metrics"] = json.loads(payload.pop("metrics_json"))
    return payload
