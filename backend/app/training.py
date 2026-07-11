from __future__ import annotations

import hashlib
import os
import json
import math
import re
import sqlite3
from concurrent.futures import ProcessPoolExecutor
from collections import defaultdict
from datetime import datetime, timezone

from .game_predictions import evaluate_game_residual_models
from .player_prop_model import DEFAULT_TUNING_CONFIG, MODEL_VERSION as LEARNED_MODEL_VERSION
from .player_prop_model import ModelTuningConfig, TRAINING_MARKETS, _training_start_date, evaluate_market_model, evaluate_market_residual_model

TRAINING_MODEL_VERSION = LEARNED_MODEL_VERSION
COMPONENT_MODEL_VERSION = "component-pregame-v2"
DATA_SIGNATURE_LABEL = "data_signature="
GAME_EVAL_SIGNATURE_LABEL = "game_eval_signature="
GAME_EVAL_SIGNATURE = "v6"
DEFAULT_TRAINING_MAX_WORKERS = 2


def run_walk_forward_training(conn: sqlite3.Connection) -> dict:
    data_signature = _training_data_signature(conn)
    cached_learned = _latest_matching_run(
        conn,
        model_version=TRAINING_MODEL_VERSION,
        run_type="walk_forward_segments",
        data_signature=data_signature,
        game_eval_signature=GAME_EVAL_SIGNATURE,
    )
    if cached_learned is not None:
        return cached_learned

    component_run = _latest_matching_run(
        conn,
        model_version=COMPONENT_MODEL_VERSION,
        run_type="walk_forward_backtest",
        data_signature=data_signature,
        game_eval_signature=GAME_EVAL_SIGNATURE,
    )
    if component_run is None:
        component_run = _run_component_benchmark(conn)
        component_run["notes"] = _append_data_signature(component_run.get("notes"), data_signature)
        _save_model_run(conn, component_run)

    started_at = datetime.now(timezone.utc).isoformat()
    metrics = {}
    total_rows = 0

    parallel_results = _parallel_market_metrics(conn)
    if parallel_results is None:
        for market in TRAINING_MARKETS:
            result = evaluate_market_model(conn, market)
            result.update(evaluate_market_residual_model(conn, market))
            metrics[market] = result
            total_rows += int(result["rows"])
    else:
        for market, result in parallel_results:
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
        "run_type": "walk_forward_segments",
        "status": status,
        "started_at": started_at,
        "finished_at": finished_at,
        "training_rows": total_rows,
        "markets": TRAINING_MARKETS,
        "metrics": metrics,
        "notes": (
            "Month-segmented walk-forward backtest for the adaptive history/context regression model. "
            f"Settled pipeline validation rows merged into market metrics: {validation_rows}. "
            f"Settled game residual evaluation rows: {game_rows}. "
            f"{DATA_SIGNATURE_LABEL}{data_signature}"
        ),
    }
    learned_run["notes"] = _append_game_eval_signature(learned_run.get("notes"), GAME_EVAL_SIGNATURE)
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
    recency_weight_scales: list[float] | None = None,
) -> dict:
    started_at = datetime.now(timezone.utc).isoformat()
    candidates = _tuning_candidates(
        ridge_penalties=ridge_penalties,
        market_weight_scales=market_weight_scales,
        player_weight_scales=player_weight_scales,
        stabilization_scales=stabilization_scales,
        recency_weight_scales=recency_weight_scales,
    )
    results = []
    for config in candidates:
        metrics = {}
        total_rows = 0
        parallel_results = _parallel_market_metrics(conn, config=config)
        if parallel_results is None:
            for market in TRAINING_MARKETS:
                result = evaluate_market_model(conn, market, config=config)
                result.update(evaluate_market_residual_model(conn, market, config=config))
                metrics[market] = result
                total_rows += int(result["rows"])
        else:
            for market, result in parallel_results:
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
    metrics = _evaluate_all_component_markets(conn)
    metrics.update(evaluate_game_residual_models(conn))
    total_rows = sum(int(metric["rows"]) for metric in metrics.values())
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
        "notes": _append_game_eval_signature(
            "Component formula benchmark using only prior player games for each target row.",
            GAME_EVAL_SIGNATURE,
        ),
    }


def run_component_blend_tuning(conn: sqlite3.Connection) -> dict:
    """Evaluate conservative component-formula alternatives before promotion."""
    candidates = {
        "current": (0.40, 0.25, 0.20, 0.15),
        "rate_minutes": (0.32, 0.20, 0.34, 0.14),
        "recent_form": (0.48, 0.28, 0.14, 0.10),
        "longer_history": (0.30, 0.20, 0.20, 0.30),
    }
    results = []
    for name, weights in candidates.items():
        metrics = _evaluate_all_component_markets(conn, weights=weights)
        _ensure_overall_metrics(metrics)
        results.append({"name": name, "weights": weights, "metrics": metrics, "mae": metrics.get("overall", {}).get("mae")})
    results.sort(key=lambda item: float(item["mae"]) if item["mae"] is not None else math.inf)
    return {"run_type": "component_blend_tuning", "candidates": results, "best_candidate": results[0] if results else None}


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


def _training_data_signature(conn: sqlite3.Connection) -> str:
    payload = {
        "training_start_date": _training_start_date(),
        "model_tuning_config": DEFAULT_TUNING_CONFIG.to_dict(),
    }
    for table in (
        "games",
        "player_game_stats",
        "players",
        "settled_props",
        "prop_predictions",
        "game_predictions",
        "settled_game_predictions",
    ):
        row = conn.execute(
            f"SELECT COUNT(*) AS count, COALESCE(MAX(id), 0) AS max_id FROM {table}"
        ).fetchone()
        payload[table] = {
            "count": int(row["count"] if isinstance(row, sqlite3.Row) else row[0]),
            "max_id": int(row["max_id"] if isinstance(row, sqlite3.Row) else row[1]),
        }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _latest_matching_run(
    conn: sqlite3.Connection,
    *,
    model_version: str,
    run_type: str,
    data_signature: str,
    game_eval_signature: str,
) -> dict | None:
    row = conn.execute(
        """
        SELECT *
        FROM model_runs
        WHERE model_version = ?
          AND run_type = ?
        ORDER BY started_at DESC, id DESC
        LIMIT 5
        """,
        (model_version, run_type),
    ).fetchall()
    for item in row:
        notes = item["notes"] if isinstance(item, sqlite3.Row) else item[9]
        if _extract_data_signature(notes) == data_signature and _extract_game_eval_signature(notes) == game_eval_signature:
            return _serialize_run(conn, item)
    return None


def _append_data_signature(notes: str | None, data_signature: str) -> str:
    base = (notes or "").strip()
    suffix = f"{DATA_SIGNATURE_LABEL}{data_signature}"
    if not base:
        return suffix
    if suffix in base:
        return base
    return f"{base} {suffix}"


def _append_game_eval_signature(notes: str | None, game_eval_signature: str) -> str:
    base = (notes or "").strip()
    suffix = f"{GAME_EVAL_SIGNATURE_LABEL}{game_eval_signature}"
    if not base:
        return suffix
    if suffix in base:
        return base
    return f"{base} {suffix}"


def _extract_data_signature(notes: str | None) -> str | None:
    if not notes:
        return None
    match = re.search(rf"{re.escape(DATA_SIGNATURE_LABEL)}([0-9a-f]+)", str(notes))
    return match.group(1) if match else None


def _extract_game_eval_signature(notes: str | None) -> str | None:
    if not notes:
        return None
    match = re.search(rf"{re.escape(GAME_EVAL_SIGNATURE_LABEL)}([A-Za-z0-9._-]+)", str(notes))
    return match.group(1) if match else None


def _parallel_market_metrics(
    conn: sqlite3.Connection,
    *,
    config: ModelTuningConfig | None = None,
) -> list[tuple[str, dict]] | None:
    db_path = _sqlite_db_path(conn)
    if not db_path:
        return None
    workers = _parallel_worker_count(len(TRAINING_MARKETS))
    if workers <= 1:
        return None
    with ProcessPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_market_metric_worker, [(db_path, market, config) for market in TRAINING_MARKETS]))
    return [(market, metric) for market, metric in results]


def _parallel_component_metrics(conn: sqlite3.Connection) -> list[tuple[str, dict]] | None:
    db_path = _sqlite_db_path(conn)
    if not db_path:
        return None
    workers = _parallel_worker_count(len(TRAINING_MARKETS))
    if workers <= 1:
        return None
    with ProcessPoolExecutor(max_workers=workers) as executor:
        results = list(executor.map(_component_metric_worker, [(db_path, market) for market in TRAINING_MARKETS]))
    return [(market, metric) for market, metric in results]


def _sqlite_db_path(conn: sqlite3.Connection) -> str | None:
    if not isinstance(conn, sqlite3.Connection):
        return None
    row = conn.execute("PRAGMA database_list").fetchone()
    if not row:
        return None
    file_value = row["file"] if isinstance(row, sqlite3.Row) else row[2]
    path = str(file_value or "").strip()
    return path or None


def _parallel_worker_count(task_count: int) -> int:
    configured = os.getenv("WNBA_TRAINING_MAX_WORKERS")
    if configured:
        try:
            return max(1, min(int(configured), task_count))
        except ValueError:
            pass
    return max(1, min(task_count, DEFAULT_TRAINING_MAX_WORKERS))


def _market_metric_worker(args: tuple[str, str, ModelTuningConfig | None]) -> tuple[str, dict]:
    db_path, market, config = args
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        result = evaluate_market_model(conn, market, config=config)
        result.update(evaluate_market_residual_model(conn, market, config=config))
        return market, result
    finally:
        conn.close()


def _component_metric_worker(args: tuple[str, str]) -> tuple[str, dict]:
    db_path, market = args
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return market, _evaluate_market(conn, market)
    finally:
        conn.close()


def _tuning_candidates(
    *,
    ridge_penalties: list[float] | None = None,
    market_weight_scales: list[float] | None = None,
    player_weight_scales: list[float] | None = None,
    stabilization_scales: list[float] | None = None,
    recency_weight_scales: list[float] | None = None,
) -> list[ModelTuningConfig]:
    penalties = ridge_penalties or [0.75, 1.25, 2.0]
    market_scales = market_weight_scales or [0.85, 1.0, 1.15]
    player_scales = player_weight_scales or [0.9, 1.0, 1.1]
    guard_scales = stabilization_scales or [0.9, 1.0, 1.1]
    recency_scales = recency_weight_scales or [0.0, 0.5, 1.0, 1.5]
    return [
        ModelTuningConfig(
            ridge_penalty=penalty,
            market_weight_scale=market_scale,
            player_weight_scale=player_scale,
            stabilization_scale=guard_scale,
            recency_weight_scale=recency_scale,
        )
        for penalty in penalties
        for market_scale in market_scales
        for player_scale in player_scales
        for guard_scale in guard_scales
        for recency_scale in recency_scales
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
    baseline_rows = 0
    baseline_mae_sum = 0.0
    baseline_rmse_sum = 0.0
    baseline_bias_sum = 0.0
    segment_count = 0
    skipped_segments = 0

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
        if metric.get("baseline_mae") is not None:
            baseline_rows += rows
            baseline_mae_sum += float(metric["baseline_mae"]) * rows
        if metric.get("baseline_rmse") is not None:
            baseline_rmse_sum += float(metric["baseline_rmse"]) * rows
        if metric.get("baseline_bias") is not None:
            baseline_bias_sum += float(metric["baseline_bias"]) * rows
        segment_count += int(metric.get("segment_count") or 0)
        skipped_segments += int(metric.get("skipped_segments") or 0)
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
    overall.setdefault("baseline_mae", round(baseline_mae_sum / baseline_rows, 3) if baseline_rows else None)
    overall.setdefault("baseline_rmse", round(baseline_rmse_sum / baseline_rows, 3) if baseline_rows else None)
    overall.setdefault("baseline_bias", round(baseline_bias_sum / baseline_rows, 3) if baseline_rows else None)
    overall.setdefault(
        "mae_improvement",
        round(float(overall["baseline_mae"]) - float(overall["mae"]), 3)
        if overall.get("baseline_mae") is not None and overall.get("mae") is not None
        else None,
    )
    overall.setdefault(
        "rmse_improvement",
        round(float(overall["baseline_rmse"]) - float(overall["rmse"]), 3)
        if overall.get("baseline_rmse") is not None and overall.get("rmse") is not None
        else None,
    )
    overall.setdefault("baseline_directional_accuracy", None)
    overall.setdefault("directional_accuracy_improvement", None)
    overall.setdefault("segment_count", segment_count)
    overall.setdefault("skipped_segments", skipped_segments)
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


def _empty_component_accumulator() -> dict[str, float]:
    return {
        "rows": 0,
        "error_sum": 0.0,
        "absolute_error_sum": 0.0,
        "squared_error_sum": 0.0,
        "direction_hits": 0.0,
        "direction_total": 0.0,
    }


def _evaluate_all_component_markets(conn: sqlite3.Connection, weights: tuple[float, float, float, float] | None = None) -> dict[str, dict]:
    players = conn.execute("SELECT id FROM players ORDER BY id").fetchall()
    accumulators = {market: _empty_component_accumulator() for market in TRAINING_MARKETS}

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
        if len(rows) < 6:
            continue

        minutes = [float(row["minutes"]) for row in rows]
        values_by_market = {
            market: [_market_value(row, market) for row in rows]
            for market in TRAINING_MARKETS
        }

        for market, values in values_by_market.items():
            acc = accumulators[market]
            for idx in range(5, len(values)):
                history = values[max(0, idx - 10):idx]
                minute_history = minutes[max(0, idx - 10):idx]
                actual = values[idx]
                projection = _project_from_history(history, minute_history, weights=weights)
                error = projection - actual
                baseline = sum(history) / len(history)

                acc["rows"] += 1
                acc["error_sum"] += error
                acc["absolute_error_sum"] += abs(error)
                acc["squared_error_sum"] += error * error
                acc["direction_total"] += 1
                if (projection >= baseline and actual >= baseline) or (projection < baseline and actual < baseline):
                    acc["direction_hits"] += 1

    metrics: dict[str, dict] = {}
    for market, acc in accumulators.items():
        rows = int(acc["rows"])
        if rows <= 0:
            metrics[market] = {
                "rows": 0,
                "mae": None,
                "rmse": None,
                "bias": None,
                "directional_accuracy": None,
            }
            continue
        metrics[market] = {
            "rows": rows,
            "mae": round(float(acc["absolute_error_sum"]) / rows, 3),
            "rmse": round(math.sqrt(float(acc["squared_error_sum"]) / rows), 3),
            "bias": round(float(acc["error_sum"]) / rows, 3),
            "directional_accuracy": round(float(acc["direction_hits"]) / max(float(acc["direction_total"]), 1.0), 3),
        }
    return metrics


def _project_from_history(values: list[float], minutes: list[float], weights: tuple[float, float, float, float] | None = None) -> float:
    weighted_recent = _weighted_average(values)
    last_5 = values[-5:]
    last_5_avg = sum(last_5) / len(last_5)
    last_10_avg = sum(values) / len(values)
    rates = [value / max(minute, 1.0) for value, minute in zip(values, minutes)]
    avg_minutes = sum(minutes[-5:]) / min(len(minutes), 5)
    rate_projection = _weighted_average(rates) * avg_minutes
    recent_weight, last_5_weight, rate_weight, history_weight = weights or (0.30, 0.20, 0.20, 0.30)
    return (recent_weight * weighted_recent) + (last_5_weight * last_5_avg) + (rate_weight * rate_projection) + (history_weight * last_10_avg)


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
