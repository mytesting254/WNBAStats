from __future__ import annotations

import math
from dataclasses import dataclass
import sqlite3
from typing import Any, Mapping

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

from . import game_predictions as gp
from .segment_training_db import load_segment_training_rows
from .segment_training_db import _segment_history_context


SEGMENT_TARGETS = ("q1_total", "first_half_total")
_SEGMENT_MODEL_CACHE: dict[tuple[str, str, int], SegmentPredictionModel] = {}


@dataclass(frozen=True)
class SegmentPredictionModel:
    target: str
    rows: int
    intercept: float
    coefficients: list[float]
    feature_means: list[float]
    feature_scales: list[float]


def project_game_segments(
    conn: sqlite3.Connection,
    game: Mapping[str, Any],
    *,
    runtime_cache: gp._GamePredictionCache | None = None,
) -> dict[str, float | None]:
    home_team_id = int(game["home_team_id"])
    away_team_id = int(game["away_team_id"])
    cache = runtime_cache or gp._GamePredictionCache(conn, (home_team_id, away_team_id))
    home_context = _current_team_context(cache, home_team_id)
    away_context = _current_team_context(cache, away_team_id)
    if home_context["games"] < 3 or away_context["games"] < 3:
        return {
            "projected_q1_total": None,
            "projected_first_half_total": None,
        }
    segment_features = _segment_features(
        conn,
        game,
        cache=cache,
        home_context=home_context,
        away_context=away_context,
    )
    q1_model = _train_segment_model_cached(conn, "q1_total")
    first_half_model = _train_segment_model_cached(conn, "first_half_total")
    return {
        "projected_q1_total": round(predict_segment_total(q1_model, segment_features), 1) if q1_model else None,
        "projected_first_half_total": round(predict_segment_total(first_half_model, segment_features), 1) if first_half_model else None,
    }


def train_segment_model(conn, *, target: str, force_rebuild: bool = False) -> tuple[SegmentPredictionModel | None, dict[str, object]]:
    rows, info = load_segment_training_rows(conn, target=target, force_rebuild=force_rebuild)
    if len(rows) < 25:
        return None, {
            **info,
            "target": target,
            "rows": len(rows),
            "error": "not_enough_rows",
        }
    model = _fit_segment_model(target, rows)
    metrics = evaluate_segment_model(model, rows)
    return model, {**info, "target": target, **metrics}


def evaluate_segment_model(model: SegmentPredictionModel, rows: list[tuple[list[float], float]]) -> dict[str, object]:
    if not rows:
        return _empty_metrics()
    split = max(int(len(rows) * 0.8), min(50, len(rows)))
    train_rows = rows[:split]
    test_rows = rows[split:] if split < len(rows) else rows[-max(1, len(rows) // 5) :]
    if not test_rows:
        test_rows = train_rows
    refit = _fit_segment_model(model.target, train_rows)
    predictions = [_predict_segment_model(refit, features) for features, _ in test_rows]
    actuals = [actual for _, actual in test_rows]
    baselines = [_baseline_prediction(model.target, features) for features, _ in test_rows]
    return _metric_payload(predictions, actuals, baselines, len(train_rows), len(test_rows))


def predict_segment_total(model: SegmentPredictionModel, features: list[float]) -> float:
    return _predict_segment_model(model, features)


def _train_segment_model_cached(conn: sqlite3.Connection, target: str) -> SegmentPredictionModel | None:
    signature = _segment_training_signature(conn)
    cache_key = (target, signature, id(conn))
    if cache_key not in _SEGMENT_MODEL_CACHE:
        model, _info = train_segment_model(conn, target=target, force_rebuild=False)
        if model is None:
            return None
        _SEGMENT_MODEL_CACHE[cache_key] = model
    return _SEGMENT_MODEL_CACHE[cache_key]


def _segment_training_signature(conn: sqlite3.Connection) -> str:
    from .segment_training_db import segment_training_db_signature

    return segment_training_db_signature(conn)


def _fit_segment_model(target: str, rows: list[tuple[list[float], float]]) -> SegmentPredictionModel:
    xs = [row[0] for row in rows]
    ys = [row[1] for row in rows]
    means, scales, standardized = _standardize_rows(xs)
    coefficients = _ridge_regression(standardized, ys, penalty=1.0)
    return SegmentPredictionModel(
        target=target,
        rows=len(rows),
        intercept=coefficients[0],
        coefficients=coefficients[1:],
        feature_means=means,
        feature_scales=scales,
    )


def _predict_segment_model(model: SegmentPredictionModel, features: list[float]) -> float:
    if np is not None:
        feature_array = np.asarray(features, dtype=float)
        means = np.asarray(model.feature_means, dtype=float)
        scales = np.asarray(model.feature_scales, dtype=float)
        standardized = (feature_array - means) / scales
        return float(model.intercept + np.dot(np.asarray(model.coefficients, dtype=float), standardized))

    standardized = [
        (value - model.feature_means[idx]) / model.feature_scales[idx]
        for idx, value in enumerate(features)
    ]
    return model.intercept + sum(coef * value for coef, value in zip(model.coefficients, standardized))


def _standardize_rows(xs: list[list[float]]) -> tuple[list[float], list[float], list[list[float]]]:
    if np is not None:
        design = np.asarray(xs, dtype=float)
        means = design.mean(axis=0)
        if design.shape[0] > 1:
            scales = design.std(axis=0, ddof=1)
        else:
            scales = np.ones(design.shape[1], dtype=float)
        scales = np.where(scales < 1.0, 1.0, scales)
        standardized = (design - means) / scales
        return means.tolist(), scales.tolist(), standardized.tolist()

    means = [sum(values) / len(values) for values in zip(*xs)]
    scales = []
    standardized = []
    for features in xs:
        standardized.append([])
        for idx, value in enumerate(features):
            if len(scales) <= idx:
                variance = sum((item[idx] - means[idx]) ** 2 for item in xs) / max(len(xs) - 1, 1)
                scales.append(max(variance ** 0.5, 1.0))
            standardized[-1].append((value - means[idx]) / scales[idx])
    return means, scales, standardized


def _ridge_regression(xs: list[list[float]], ys: list[float], penalty: float) -> list[float]:
    if not xs:
        return [0.0]
    column_count = len(xs[0]) + 1

    if np is not None:
        design = np.ones((len(xs), column_count), dtype=float)
        if xs:
            design[:, 1:] = np.asarray(xs, dtype=float)
        target = np.asarray(ys, dtype=float)
        identity = np.eye(column_count, dtype=float)
        identity[0, 0] = 0.0
        solution = np.linalg.solve(design.T @ design + (penalty * identity), design.T @ target)
        return solution.tolist()

    xtx = [[0.0 for _ in range(column_count)] for _ in range(column_count)]
    xty = [0.0 for _ in range(column_count)]
    for features, target in zip(xs, ys):
        row = [1.0, *features]
        for i, left in enumerate(row):
            xty[i] += left * target
            for j, right in enumerate(row):
                xtx[i][j] += left * right
    for idx in range(1, column_count):
        xtx[idx][idx] += penalty
    return _solve_linear_system(xtx, xty)


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [vector[idx]] for idx, row in enumerate(matrix)]
    for pivot in range(size):
        best = max(range(pivot, size), key=lambda idx: abs(augmented[idx][pivot]))
        augmented[pivot], augmented[best] = augmented[best], augmented[pivot]
        pivot_value = augmented[pivot][pivot] or 1e-9
        for column in range(pivot, size + 1):
            augmented[pivot][column] /= pivot_value
        for row_idx in range(size):
            if row_idx == pivot:
                continue
            factor = augmented[row_idx][pivot]
            if factor == 0:
                continue
            for column in range(pivot, size + 1):
                augmented[row_idx][column] -= factor * augmented[pivot][column]
    return [augmented[idx][size] for idx in range(size)]


def _baseline_prediction(target: str, features: list[float]) -> float:
    game_total = float(features[39] if len(features) > 39 else 0.0)
    if target == "q1_total":
        return game_total * 0.25 if game_total > 0 else 41.0
    if target == "first_half_total":
        return game_total * 0.5 if game_total > 0 else 82.0
    raise ValueError(f"Unsupported segment target: {target}")


def _metric_payload(
    predictions: list[float],
    actuals: list[float],
    baselines: list[float],
    train_rows: int,
    test_rows: int,
) -> dict[str, object]:
    mae = _mae(predictions, actuals)
    rmse = _rmse(predictions, actuals)
    bias = _bias(predictions, actuals)
    baseline_mae = _mae(baselines, actuals)
    baseline_rmse = _rmse(baselines, actuals)
    baseline_bias = _bias(baselines, actuals)
    return {
        "rows": train_rows + test_rows,
        "train_rows": train_rows,
        "test_rows": test_rows,
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "bias": round(bias, 3),
        "baseline_mae": round(baseline_mae, 3),
        "baseline_rmse": round(baseline_rmse, 3),
        "baseline_bias": round(baseline_bias, 3),
        "mae_improvement": round(baseline_mae - mae, 3),
        "rmse_improvement": round(baseline_rmse - rmse, 3),
    }


def _mae(predictions: list[float], actuals: list[float]) -> float:
    return sum(abs(pred - actual) for pred, actual in zip(predictions, actuals)) / max(len(actuals), 1)


def _rmse(predictions: list[float], actuals: list[float]) -> float:
    return math.sqrt(sum((pred - actual) ** 2 for pred, actual in zip(predictions, actuals)) / max(len(actuals), 1))


def _bias(predictions: list[float], actuals: list[float]) -> float:
    return sum(pred - actual for pred, actual in zip(predictions, actuals)) / max(len(actuals), 1)


def _empty_metrics() -> dict[str, object]:
    return {
        "rows": 0,
        "train_rows": 0,
        "test_rows": 0,
        "mae": None,
        "rmse": None,
        "bias": None,
        "baseline_mae": None,
        "baseline_rmse": None,
        "baseline_bias": None,
        "mae_improvement": None,
        "rmse_improvement": None,
    }


def _current_team_context(cache: gp._GamePredictionCache, team_id: int) -> dict[str, float]:
    avg_points = float(cache.team_average(team_id, "points"))
    avg_allowed = float(cache.team_average(team_id, "opponent_points"))
    avg_possessions = float(cache.team_average(team_id, "possessions"))
    recent_points = float(cache.weighted_recent(team_id, "points"))
    recent_allowed = float(cache.weighted_recent(team_id, "opponent_points"))
    recent_possessions = float(cache.weighted_recent(team_id, "possessions"))
    return {
        "games": float(cache.team_count(team_id)),
        "avg_points": avg_points,
        "avg_allowed": avg_allowed,
        "avg_possessions": avg_possessions,
        "recent_points": recent_points,
        "recent_allowed": recent_allowed,
        "recent_possessions": recent_possessions,
    }


def _segment_features(
    conn: sqlite3.Connection,
    game: Mapping[str, Any],
    *,
    cache: gp._GamePredictionCache,
    home_context: dict[str, float],
    away_context: dict[str, float],
) -> list[float]:
    home_team_id = int(game["home_team_id"])
    away_team_id = int(game["away_team_id"])
    pace_factor = gp._clamp(
        ((float(home_context["avg_possessions"]) + float(away_context["avg_possessions"])) / 2.0)
        / max(float(cache.league_possessions()), 1.0),
        0.94,
        1.06,
    )
    base_features = gp._assemble_direct_game_features(
        home_recent_points=float(home_context["recent_points"]),
        away_recent_points=float(away_context["recent_points"]),
        home_recent_allowed=float(home_context["recent_allowed"]),
        away_recent_allowed=float(away_context["recent_allowed"]),
        home_average_points=float(home_context["avg_points"]),
        away_average_points=float(away_context["avg_points"]),
        home_average_allowed=float(home_context["avg_allowed"]),
        away_average_allowed=float(away_context["avg_allowed"]),
        home_average_possessions=float(home_context["avg_possessions"]),
        away_average_possessions=float(away_context["avg_possessions"]),
        pace_factor=pace_factor,
        rest_days_home=int(game["rest_days_home"] or 2),
        rest_days_away=int(game["rest_days_away"] or 2),
        home_game_count=int(home_context["games"]),
        away_game_count=int(away_context["games"]),
        home_recent_possessions=float(home_context["recent_possessions"]),
        away_recent_possessions=float(away_context["recent_possessions"]),
        home_season_off_rating=gp._team_rating(float(home_context["avg_points"]), float(home_context["avg_possessions"])),
        away_season_off_rating=gp._team_rating(float(away_context["avg_points"]), float(away_context["avg_possessions"])),
        home_season_def_rating=gp._team_rating(float(home_context["avg_allowed"]), float(home_context["avg_possessions"])),
        away_season_def_rating=gp._team_rating(float(away_context["avg_allowed"]), float(away_context["avg_possessions"])),
        home_recent_off_rating=gp._team_rating(float(home_context["recent_points"]), float(home_context["recent_possessions"])),
        away_recent_off_rating=gp._team_rating(float(away_context["recent_points"]), float(away_context["recent_possessions"])),
        home_recent_def_rating=gp._team_rating(float(home_context["recent_allowed"]), float(home_context["recent_possessions"])),
        away_recent_def_rating=gp._team_rating(float(away_context["recent_allowed"]), float(away_context["recent_possessions"])),
        spread_home=gp._coerce_float(game["spread_home"]),
        game_total=gp._coerce_float(game["game_total"]),
        home_moneyline=gp._coerce_float(game["home_moneyline"]),
        away_moneyline=gp._coerce_float(game["away_moneyline"]),
    )
    game_date = str(_game_value(game, "game_date") or "")
    home_segment_context = _team_segment_context(conn, team_id=home_team_id, game_date=game_date)
    away_segment_context = _team_segment_context(conn, team_id=away_team_id, game_date=game_date)
    return [
        *base_features,
        float(home_segment_context["avg_q1_points"]),
        float(away_segment_context["avg_q1_points"]),
        float(home_segment_context["avg_q1_allowed"]),
        float(away_segment_context["avg_q1_allowed"]),
        float(home_segment_context["recent_q1_points"]),
        float(away_segment_context["recent_q1_points"]),
        float(home_segment_context["recent_q1_allowed"]),
        float(away_segment_context["recent_q1_allowed"]),
        float(home_segment_context["avg_first_half_points"]),
        float(away_segment_context["avg_first_half_points"]),
        float(home_segment_context["avg_first_half_allowed"]),
        float(away_segment_context["avg_first_half_allowed"]),
        float(home_segment_context["recent_first_half_points"]),
        float(away_segment_context["recent_first_half_points"]),
        float(home_segment_context["recent_first_half_allowed"]),
        float(away_segment_context["recent_first_half_allowed"]),
    ]


def _team_segment_context(conn: sqlite3.Connection, *, team_id: int, game_date: str) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT
            g.game_date,
            CASE WHEN g.home_team_id = ? THEN segments.home_q1_points ELSE segments.away_q1_points END AS q1_points,
            CASE WHEN g.home_team_id = ? THEN segments.away_q1_points ELSE segments.home_q1_points END AS q1_allowed,
            CASE WHEN g.home_team_id = ? THEN segments.home_1h_points ELSE segments.away_1h_points END AS first_half_points,
            CASE WHEN g.home_team_id = ? THEN segments.away_1h_points ELSE segments.home_1h_points END AS first_half_allowed
        FROM games g
        JOIN game_segment_results segments ON segments.game_id = g.id
        WHERE g.status = 'final'
          AND ? IN (g.home_team_id, g.away_team_id)
          AND g.game_date < ?
          AND segments.home_q1_points IS NOT NULL
          AND segments.away_q1_points IS NOT NULL
          AND segments.home_1h_points IS NOT NULL
          AND segments.away_1h_points IS NOT NULL
        ORDER BY g.game_date ASC, g.start_time ASC, g.id ASC
        """,
        (team_id, team_id, team_id, team_id, team_id, game_date),
    ).fetchall()
    history: dict[str, object] | None = None
    for row in rows:
        if history is None:
            history = {}
        _append_segment_row(
            history,
            game_date=str(row["game_date"]),
            q1_points=float(row["q1_points"]),
            q1_allowed=float(row["q1_allowed"]),
            first_half_points=float(row["first_half_points"]),
            first_half_allowed=float(row["first_half_allowed"]),
        )
    return _segment_history_context(history)


def _append_segment_row(
    history: dict[str, object],
    *,
    game_date: str,
    q1_points: float,
    q1_allowed: float,
    first_half_points: float,
    first_half_allowed: float,
) -> None:
    games = int(history.get("games") or 0) + 1
    history["games"] = games
    history["q1_points_sum"] = float(history.get("q1_points_sum") or 0.0) + q1_points
    history["q1_allowed_sum"] = float(history.get("q1_allowed_sum") or 0.0) + q1_allowed
    history["first_half_points_sum"] = float(history.get("first_half_points_sum") or 0.0) + first_half_points
    history["first_half_allowed_sum"] = float(history.get("first_half_allowed_sum") or 0.0) + first_half_allowed
    recent_q1_points = list(history.get("recent_q1_points") or [])
    recent_q1_allowed = list(history.get("recent_q1_allowed") or [])
    recent_first_half_points = list(history.get("recent_first_half_points") or [])
    recent_first_half_allowed = list(history.get("recent_first_half_allowed") or [])
    recent_q1_points.append(q1_points)
    recent_q1_allowed.append(q1_allowed)
    recent_first_half_points.append(first_half_points)
    recent_first_half_allowed.append(first_half_allowed)
    history["recent_q1_points"] = recent_q1_points[-5:]
    history["recent_q1_allowed"] = recent_q1_allowed[-5:]
    history["recent_first_half_points"] = recent_first_half_points[-5:]
    history["recent_first_half_allowed"] = recent_first_half_allowed[-5:]
    history["last_game_date"] = game_date


def _game_value(game: Mapping[str, Any] | sqlite3.Row, key: str, default: Any = None) -> Any:
    if isinstance(game, sqlite3.Row):
        return game[key] if key in game.keys() else default
    return game.get(key, default)
