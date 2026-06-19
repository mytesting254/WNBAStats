from __future__ import annotations

import json
import math
import sqlite3
from collections import defaultdict
from datetime import datetime, timezone

from .game_predictions import evaluate_game_residual_models
from .player_prop_model import DEFAULT_TUNING_CONFIG, MODEL_VERSION as LEARNED_MODEL_VERSION
from .player_prop_model import ModelTuningConfig, TRAINING_MARKETS, evaluate_market_model, evaluate_market_residual_model

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
        result.update(evaluate_market_residual_model(conn, market))
        metrics[market] = result
        total_rows += int(result["rows"])

    _ensure_overall_metrics(metrics)
    settled_validation = _settled_validation_metrics(conn, TRAINING_MODEL_VERSION)
    _merge_validation_metrics(metrics, settled_validation)
    metrics.update(evaluate_game_residual_models(conn))
    _ensure_overall_metrics(metrics)

    finished_at = datetime.now(timezone.utc).isoformat()
    status = "completed" if total_rows > 0 else "no_data"
    validation_rows = sum(int(item.get("settled_rows") or 0) for item in settled_validation.values())
    game_rows = int(metrics.get("game_overall", {}).get("rows") or 0)
    learned_run = {
        "model_version": TRAINING_MODEL_VERSION,
        "run_type": "chronological_holdout",
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "training_rows": total_rows,
        "markets": TRAINING_MARKETS,
        "metrics": metrics,
        "notes": (
            "Chronological 80/20 holdout for the adaptive history/context regression model. "
            f"Settled pipeline validation rows merged into market metrics: {validation_rows}. "
            f"Settled game residual evaluation rows: {game_rows}."
        ),
    }
    _save_model_run(conn, learned_run)
    conn.commit()
    return learned_run


def run_parameter_tuning(
    conn: sqlite3.Connection,
    *,
    ridge_penalties: list[float] | None = None,
    market_weight_scales: list[float] | None = None,
    player_weight_scales: list[float] | None = None,
    stabilization_scales: list[float] | None = None,
) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    candidates = _tuning_candidates(
        ridge_penalties=ridge_penalties,
        market_weight_scales=market_weight_scales,
        player_weight_scales=player_weight_scales,
        stabilization_scales=stabilization_scales,
    )
    results = []
    for config in candidates:
        metrics = {}
        total_rows = 0
        for market in TRAINING_MARKETS:
            result = evaluate_market_model(conn, market, config=config)
            result.update(evaluate_market_residual_model(conn, market, config=config))
            metrics[market] = result
            total_rows += int(result["rows"])
        summary = _aggregate_tuning_summary(metrics)
        results.append(
            {
                "config": config.to_dict(),
                "summary": summary,
                "metrics": metrics,
                "training_rows": total_rows,
            }
        )

    ranked = sorted(
        results,
        key=lambda item: (
            item["summary"]["avg_mae"] is None,
            item["summary"]["avg_mae"] if item["summary"]["avg_mae"] is not None else math.inf,
            item["summary"]["avg_rmse"] if item["summary"]["avg_rmse"] is not None else math.inf,
            -1 * (item["summary"]["avg_directional_accuracy"] or 0.0),
        ),
    )
    for idx, candidate in enumerate(ranked, start=1):
        candidate["rank"] = idx

    finished_at = datetime.now(timezone.utc).isoformat()
    best = ranked[0] if ranked else None
    return {
        "model_version": TRAINING_MODEL_VERSION,
        "run_type": "parameter_tuning",
        "started_at": started_at,
        "finished_at": finished_at,
        "candidate_count": len(ranked),
        "default_config": DEFAULT_TUNING_CONFIG.to_dict(),
        "best_candidate": best,
        "candidates": ranked,
    }


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
    return [_serialize_run(conn, row) for row in rows]


def latest_model_run(conn: sqlite3.Connection) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM model_runs
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """
    ).fetchone()
    return _serialize_run(conn, row) if row else None


def _run_component_benchmark(conn: sqlite3.Connection) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    metrics = {}
    total_rows = 0
    for market in TRAINING_MARKETS:
        result = _evaluate_market(conn, market)
        metrics[market] = result
        total_rows += int(result["rows"])
    _ensure_overall_metrics(metrics)
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


def _tuning_candidates(
    *,
    ridge_penalties: list[float] | None = None,
    market_weight_scales: list[float] | None = None,
    player_weight_scales: list[float] | None = None,
    stabilization_scales: list[float] | None = None,
) -> list[ModelTuningConfig]:
    penalties = ridge_penalties or [0.75, 1.25, 2.0]
    market_scales = market_weight_scales or [0.85, 1.0, 1.15]
    player_scales = player_weight_scales or [0.9, 1.0, 1.1]
    guard_scales = stabilization_scales or [0.9, 1.0, 1.1]
    return [
        ModelTuningConfig(
            ridge_penalty=penalty,
            market_weight_scale=market_scale,
            player_weight_scale=player_scale,
            stabilization_scale=guard_scale,
        )
        for penalty in penalties
        for market_scale in market_scales
        for player_scale in player_scales
        for guard_scale in guard_scales
    ]


def _aggregate_tuning_summary(metrics: dict[str, dict]) -> dict[str, float | int | None]:
    row_count = 0
    mae_total = 0.0
    rmse_total = 0.0
    direction_total = 0.0
    direction_rows = 0
    residual_row_count = 0
    residual_mae_total = 0.0
    residual_rmse_total = 0.0
    residual_direction_total = 0.0
    residual_direction_rows = 0
    for metric in metrics.values():
        rows = int(metric.get("rows") or 0)
        if not rows:
            continue
        row_count += rows
        if metric.get("mae") is not None:
            mae_total += float(metric["mae"]) * rows
        if metric.get("rmse") is not None:
            rmse_total += float(metric["rmse"]) * rows
        if metric.get("directional_accuracy") is not None:
            direction_total += float(metric["directional_accuracy"]) * rows
            direction_rows += rows
        residual_rows = int(metric.get("residual_rows") or 0)
        if residual_rows > 0:
            residual_row_count += residual_rows
            if metric.get("residual_mae") is not None:
                residual_mae_total += float(metric["residual_mae"]) * residual_rows
            if metric.get("residual_rmse") is not None:
                residual_rmse_total += float(metric["residual_rmse"]) * residual_rows
            if metric.get("residual_directional_accuracy") is not None:
                residual_direction_total += float(metric["residual_directional_accuracy"]) * residual_rows
                residual_direction_rows += residual_rows
    return {
        "total_rows": row_count,
        "avg_mae": round(mae_total / row_count, 4) if row_count else None,
        "avg_rmse": round(rmse_total / row_count, 4) if row_count else None,
        "avg_directional_accuracy": round(direction_total / direction_rows, 4) if direction_rows else None,
        "total_residual_rows": residual_row_count,
        "avg_residual_mae": round(residual_mae_total / residual_row_count, 4) if residual_row_count else None,
        "avg_residual_rmse": round(residual_rmse_total / residual_row_count, 4) if residual_row_count else None,
        "avg_residual_directional_accuracy": round(residual_direction_total / residual_direction_rows, 4) if residual_direction_rows else None,
    }


def _merge_validation_metrics(base_metrics: dict[str, dict], validation_metrics: dict[str, dict]) -> None:
    for market, validation in validation_metrics.items():
        target = base_metrics.setdefault(
            market,
            {
                "rows": 0,
                "mae": None,
                "rmse": None,
                "bias": None,
                "directional_accuracy": None,
            },
        )
        target.update(validation)


def _ensure_overall_metrics(metrics: dict[str, dict]) -> None:
    market_items = [
        (market, metric)
        for market, metric in metrics.items()
        if market != "overall" and not str(market).startswith("game_")
    ]
    if not market_items:
        return

    total_rows = 0
    mae_sum = 0.0
    rmse_sum = 0.0
    bias_sum = 0.0
    directional_sum = 0.0
    directional_rows = 0
    residual_rows_total = 0
    residual_mae_sum = 0.0
    residual_rmse_sum = 0.0
    residual_bias_sum = 0.0
    residual_directional_sum = 0.0
    residual_directional_rows = 0

    for _, metric in market_items:
        rows = int(metric.get("rows") or 0)
        if rows <= 0:
            continue
        total_rows += rows
        if metric.get("mae") is not None:
            mae_sum += float(metric["mae"]) * rows
        if metric.get("rmse") is not None:
            rmse_sum += float(metric["rmse"]) * rows
        if metric.get("bias") is not None:
            bias_sum += float(metric["bias"]) * rows
        if metric.get("directional_accuracy") is not None:
            directional_sum += float(metric["directional_accuracy"]) * rows
            directional_rows += rows
        residual_rows = int(metric.get("residual_rows") or 0)
        if residual_rows > 0:
            residual_rows_total += residual_rows
            if metric.get("residual_mae") is not None:
                residual_mae_sum += float(metric["residual_mae"]) * residual_rows
            if metric.get("residual_rmse") is not None:
                residual_rmse_sum += float(metric["residual_rmse"]) * residual_rows
            if metric.get("residual_bias") is not None:
                residual_bias_sum += float(metric["residual_bias"]) * residual_rows
            if metric.get("residual_directional_accuracy") is not None:
                residual_directional_sum += float(metric["residual_directional_accuracy"]) * residual_rows
                residual_directional_rows += residual_rows

    if total_rows <= 0:
        return

    overall = metrics.setdefault("overall", {})
    overall.setdefault("rows", total_rows)
    overall.setdefault("mae", round(mae_sum / total_rows, 3) if total_rows else None)
    overall.setdefault("rmse", round(rmse_sum / total_rows, 3) if total_rows else None)
    overall.setdefault("bias", round(bias_sum / total_rows, 3) if total_rows else None)
    overall.setdefault("directional_accuracy", round(directional_sum / directional_rows, 3) if directional_rows else None)
    overall.setdefault("residual_rows", residual_rows_total)
    overall.setdefault("residual_mae", round(residual_mae_sum / residual_rows_total, 3) if residual_rows_total else None)
    overall.setdefault("residual_rmse", round(residual_rmse_sum / residual_rows_total, 3) if residual_rows_total else None)
    overall.setdefault("residual_bias", round(residual_bias_sum / residual_rows_total, 3) if residual_rows_total else None)
    overall.setdefault(
        "residual_directional_accuracy",
        round(residual_directional_sum / residual_directional_rows, 3) if residual_directional_rows else None,
    )


def _settled_validation_metrics(conn: sqlite3.Connection, model_version: str) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT
            pp.model_probability,
            pp.edge,
            pp.expected_value,
            pp.recommended_side,
            pp.projection,
            pp.model_version,
            pl.market,
            pl.line,
            pl.over_odds,
            pl.under_odds,
            pgs.points,
            pgs.rebounds,
            pgs.assists,
            pgs.threes,
            pgs.steals,
            pgs.blocks
        FROM prop_predictions pp
        JOIN prop_lines pl ON pl.id = pp.prop_line_id
        JOIN player_game_stats pgs ON pgs.player_id = pl.player_id AND pgs.game_id = pl.game_id
        WHERE pp.model_version = ?
        """,
        (model_version,),
    ).fetchall()
    if not rows:
        return {}

    grouped: dict[str, list[sqlite3.Row]] = defaultdict(list)
    for row in rows:
        grouped[str(row["market"])].append(row)

    metrics = {market: _validation_metric(group_rows) for market, group_rows in grouped.items()}
    metrics["overall"] = _validation_metric(rows)
    return metrics


def _validation_metric(rows: list[sqlite3.Row]) -> dict:
    if not rows:
        return {
            "settled_rows": 0,
            "side_accuracy": None,
            "calibration_gap": None,
            "brier_score": None,
            "avg_edge": None,
            "avg_expected_value": None,
            "realized_roi": None,
        }

    errors = []
    squared_errors = []
    side_hits = 0
    probability_sum = 0.0
    outcome_sum = 0.0
    brier_sum = 0.0
    edge_sum = 0.0
    ev_sum = 0.0
    roi_sum = 0.0

    for row in rows:
        actual = _validation_market_value(row, str(row["market"]))
        projection = float(row["projection"])
        error = projection - actual
        errors.append(error)
        squared_errors.append(error * error)

        line = float(row["line"])
        recommended_side = str(row["recommended_side"])
        actual_side = "over" if actual > line else "under"
        win = 1.0 if recommended_side == actual_side else 0.0
        side_hits += int(win)

        probability = float(row["model_probability"])
        probability_sum += probability
        outcome_sum += win
        brier_sum += (probability - win) ** 2

        edge_sum += float(row["edge"] or 0.0)
        ev_sum += float(row["expected_value"] or 0.0)
        odds = int(row["over_odds"] if recommended_side == "over" else row["under_odds"])
        roi_sum += _bet_roi(win, odds)

    count = len(rows)
    side_accuracy = side_hits / count
    calibration_gap = abs((probability_sum / count) - (outcome_sum / count))
    return {
        "settled_rows": count,
        "side_accuracy": round(side_accuracy, 3),
        "calibration_gap": round(calibration_gap, 3),
        "brier_score": round(brier_sum / count, 4),
        "avg_edge": round(edge_sum / count, 4),
        "avg_expected_value": round(ev_sum / count, 4),
        "realized_roi": round(roi_sum / count, 4),
    }


def _bet_roi(win: float, odds: int) -> float:
    if not win:
        return -1.0
    if odds > 0:
        return odds / 100.0
    return 100.0 / abs(odds)


def _validation_market_value(row: sqlite3.Row, market: str) -> float:
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


def _serialize_run(conn: sqlite3.Connection, row: sqlite3.Row) -> dict:
    payload = dict(row)
    payload["markets"] = json.loads(payload["markets"])
    payload["metrics"] = json.loads(payload.pop("metrics_json"))
    _ensure_overall_metrics(payload["metrics"])
    validation = _settled_validation_metrics(conn, str(payload.get("model_version") or ""))
    if validation:
        _merge_validation_metrics(payload["metrics"], validation)
        _ensure_overall_metrics(payload["metrics"])
    return payload
