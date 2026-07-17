from __future__ import annotations

import math
from dataclasses import dataclass
import sqlite3
from typing import Any, Mapping

try:
    import numpy as np
except ImportError:  # pragma: no cover
    np = None

try:
    from sklearn.ensemble import HistGradientBoostingRegressor
except ImportError:  # pragma: no cover
    HistGradientBoostingRegressor = None

from . import game_predictions as gp
from .segment_training_db import load_segment_training_rows
from .segment_training_db import _segment_history_context


SEGMENT_TARGETS = ("q1_total", "first_half_total")
_SEGMENT_MODEL_CACHE: dict[tuple[str, str, int], SegmentPredictionModel] = {}
Q1_TOTAL_MODEL_BLEND_WEIGHT = 0.20


@dataclass(frozen=True)
class SegmentPredictionModel:
    target: str
    rows: int
    model_kind: str
    residual_from_baseline: bool
    baseline_blend_weight: float | None
    feature_indices: list[int]
    intercept: float
    coefficients: list[float]
    feature_means: list[float]
    feature_scales: list[float]
    estimator: object | None = None


def project_game_segments(
    conn: sqlite3.Connection,
    game: Mapping[str, Any],
    *,
    runtime_cache: gp._GamePredictionCache | None = None,
) -> dict[str, float | None]:
    home_team_id = int(game["home_team_id"])
    away_team_id = int(game["away_team_id"])
    cache = runtime_cache or gp._GamePredictionCache(conn, (home_team_id, away_team_id))
    game_date = str(_game_value(game, "game_date") or "")
    home_context = _current_team_context(conn, team_id=home_team_id, game_date=game_date)
    away_context = _current_team_context(conn, team_id=away_team_id, game_date=game_date)
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
    q1_prediction = predict_segment_total(q1_model, segment_features) if q1_model else None
    first_half_prediction = predict_segment_total(first_half_model, segment_features) if first_half_model else None
    return {
        "projected_q1_total": round(q1_prediction, 1) if q1_prediction is not None else None,
        "projected_first_half_total": round(first_half_prediction, 1) if first_half_prediction is not None else None,
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
    model, metrics = _select_and_fit_segment_model(target, rows)
    return model, {**info, "target": target, **metrics}


def evaluate_segment_model(model: SegmentPredictionModel, rows: list[tuple[list[float], float]]) -> dict[str, object]:
    if not rows:
        return _empty_metrics()
    split = max(int(len(rows) * 0.8), min(50, len(rows)))
    train_rows = rows[:split]
    test_rows = rows[split:] if split < len(rows) else rows[-max(1, len(rows) // 5) :]
    if not test_rows:
        test_rows = train_rows
    refit = _fit_segment_model(model.target, train_rows, model_kind=model.model_kind)
    predictions = [
        _postprocess_segment_prediction(model.target, _predict_segment_model(refit, features), features)
        for features, _ in test_rows
    ]
    actuals = [actual for _, actual in test_rows]
    baselines = [_baseline_prediction(model.target, features) for features, _ in test_rows]
    return _metric_payload(predictions, actuals, baselines, len(train_rows), len(test_rows))


def predict_segment_total(model: SegmentPredictionModel, features: list[float]) -> float:
    return _postprocess_segment_prediction(model, _predict_segment_model(model, features), features)


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


def _fit_segment_model(
    target: str,
    rows: list[tuple[list[float], float]],
    *,
    model_kind: str,
) -> SegmentPredictionModel:
    feature_indices = _feature_indices_for_target(target)
    xs = [_select_feature_subset(row[0], feature_indices) for row in rows]
    raw_targets = [row[1] for row in rows]
    baselines = [_baseline_prediction(target, row[0]) for row in rows]
    residual_from_baseline = model_kind.endswith("_residual")
    baseline_blend_weight = _baseline_blend_weight(target, model_kind)
    ys = [
        float(target_value) - float(baseline)
        if residual_from_baseline
        else float(target_value)
        for target_value, baseline in zip(raw_targets, baselines)
    ]
    if model_kind.startswith("ridge"):
        means, scales, standardized = _standardize_rows(xs)
        coefficients = _ridge_regression(standardized, ys, penalty=1.0)
        return SegmentPredictionModel(
            target=target,
            rows=len(rows),
            model_kind=model_kind,
            residual_from_baseline=residual_from_baseline,
            baseline_blend_weight=baseline_blend_weight,
            feature_indices=feature_indices,
            intercept=coefficients[0],
            coefficients=coefficients[1:],
            feature_means=means,
            feature_scales=scales,
            estimator=None,
        )
    if model_kind == "hgb_residual" and HistGradientBoostingRegressor is not None:
        estimator = HistGradientBoostingRegressor(
            loss="squared_error",
            learning_rate=0.05,
            max_depth=3,
            max_iter=60,
            min_samples_leaf=20,
            l2_regularization=0.1,
            early_stopping=True,
            validation_fraction=0.15,
            n_iter_no_change=8,
            random_state=42,
        )
        estimator.fit(xs, ys)
        return SegmentPredictionModel(
            target=target,
            rows=len(rows),
            model_kind=model_kind,
            residual_from_baseline=True,
            baseline_blend_weight=baseline_blend_weight,
            feature_indices=feature_indices,
            intercept=0.0,
            coefficients=[],
            feature_means=[],
            feature_scales=[],
            estimator=estimator,
        )
    raise ValueError(f"Unsupported segment model kind: {model_kind}")


def _predict_segment_model(model: SegmentPredictionModel, features: list[float]) -> float:
    selected = _select_feature_subset(features, model.feature_indices)
    if model.model_kind == "hgb_residual" and model.estimator is not None:
        prediction = model.estimator.predict([selected])[0]
        return float(prediction)
    if np is not None:
        feature_array = np.asarray(selected, dtype=float)
        means = np.asarray(model.feature_means, dtype=float)
        scales = np.asarray(model.feature_scales, dtype=float)
        standardized = (feature_array - means) / scales
        return float(model.intercept + np.dot(np.asarray(model.coefficients, dtype=float), standardized))

    standardized = [
        (value - model.feature_means[idx]) / model.feature_scales[idx]
        for idx, value in enumerate(selected)
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


def _postprocess_segment_prediction(model: SegmentPredictionModel, prediction: float, features: list[float]) -> float:
    baseline = _baseline_prediction(model.target, features)
    adjusted = float(prediction)
    if model.residual_from_baseline:
        adjusted += baseline
    if model.baseline_blend_weight is not None:
        adjusted = (float(model.baseline_blend_weight) * adjusted) + ((1.0 - float(model.baseline_blend_weight)) * baseline)
    return adjusted


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


def _select_and_fit_segment_model(
    target: str,
    rows: list[tuple[list[float], float]],
) -> tuple[SegmentPredictionModel, dict[str, object]]:
    split = max(int(len(rows) * 0.8), min(50, len(rows)))
    train_rows = rows[:split]
    test_rows = rows[split:] if split < len(rows) else rows[-max(1, len(rows) // 5) :]
    if not test_rows:
        test_rows = train_rows
    candidate_kinds = _candidate_model_kinds(target)
    candidate_metrics: list[dict[str, object]] = []
    best_model: SegmentPredictionModel | None = None
    best_metrics: dict[str, object] | None = None
    best_score: tuple[float, float] | None = None
    for model_kind in candidate_kinds:
        candidate_model = _fit_segment_model(target, train_rows, model_kind=model_kind)
        predictions = [
            _postprocess_segment_prediction(candidate_model, _predict_segment_model(candidate_model, features), features)
            for features, _ in test_rows
        ]
        actuals = [actual for _, actual in test_rows]
        baselines = [_baseline_prediction(target, features) for features, _ in test_rows]
        metrics = _metric_payload(predictions, actuals, baselines, len(train_rows), len(test_rows))
        metrics["model_kind"] = model_kind
        candidate_metrics.append(metrics)
        score = (
            float(metrics["rmse"] or float("inf")),
            float(metrics["mae"] or float("inf")),
        )
        if best_score is None or score < best_score:
            best_score = score
            best_model = candidate_model
            best_metrics = metrics
    if best_model is None or best_metrics is None:
        raise RuntimeError(f"No segment model candidates available for {target}")
    final_model = _fit_segment_model(target, rows, model_kind=best_model.model_kind)
    return final_model, {
        **best_metrics,
        "model_kind": final_model.model_kind,
        "candidate_metrics": candidate_metrics,
    }


def _candidate_model_kinds(target: str) -> list[str]:
    candidates = ["ridge_raw", "ridge_residual"]
    if target == "q1_total":
        candidates.insert(1, "ridge_q1_anchor")
    if HistGradientBoostingRegressor is not None:
        candidates.append("hgb_residual")
    return candidates


def _baseline_blend_weight(target: str, model_kind: str) -> float | None:
    if target == "q1_total" and model_kind == "ridge_q1_anchor":
        return Q1_TOTAL_MODEL_BLEND_WEIGHT
    return None


def _feature_indices_for_target(target: str) -> list[int]:
    # First 59 features are the pre-Q1-expansion core set: 43 direct-game + 16 segment history features.
    if target == "first_half_total":
        return list(range(59))
    return list(range(73))


def _select_feature_subset(features: list[float], feature_indices: list[int]) -> list[float]:
    return [float(features[idx]) for idx in feature_indices if idx < len(features)]


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
    game_date = str(_game_value(game, "game_date") or "")
    pace_factor = gp._clamp(
        ((float(home_context["avg_possessions"]) + float(away_context["avg_possessions"])) / 2.0)
        / max(_league_half_possessions(conn, game_date=game_date), 1.0),
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
    home_segment_context = _team_segment_context(conn, team_id=home_team_id, game_date=game_date, current_is_home=True)
    away_segment_context = _team_segment_context(conn, team_id=away_team_id, game_date=game_date, current_is_home=False)
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
        float(home_segment_context["split_q1_points"]),
        float(away_segment_context["split_q1_points"]),
        float(home_segment_context["split_q1_allowed"]),
        float(away_segment_context["split_q1_allowed"]),
        float(home_segment_context["recent_q1_points"]) - float(away_segment_context["recent_q1_allowed"]),
        float(away_segment_context["recent_q1_points"]) - float(home_segment_context["recent_q1_allowed"]),
        float(home_segment_context["avg_q1_points"]) - float(away_segment_context["avg_q1_allowed"]),
        float(away_segment_context["avg_q1_points"]) - float(home_segment_context["avg_q1_allowed"]),
        float(home_segment_context["q1_points_volatility"]),
        float(away_segment_context["q1_points_volatility"]),
        float(home_segment_context["q1_allowed_volatility"]),
        float(away_segment_context["q1_allowed_volatility"]),
        float(home_segment_context["q1_fast_start_rate"]),
        float(away_segment_context["q1_fast_start_rate"]),
    ]


def _team_segment_context(conn: sqlite3.Connection, *, team_id: int, game_date: str, current_is_home: bool) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT
            g.game_date,
            CASE WHEN g.home_team_id = ? THEN 1 ELSE 0 END AS is_home,
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
        (team_id, team_id, team_id, team_id, team_id, team_id, game_date),
    ).fetchall()
    history: dict[str, object] | None = None
    for row in rows:
        if history is None:
            history = {}
        _append_segment_row(
            history,
            game_date=str(row["game_date"]),
            is_home=bool(int(row["is_home"] or 0)),
            q1_points=float(row["q1_points"]),
            q1_allowed=float(row["q1_allowed"]),
            first_half_points=float(row["first_half_points"]),
            first_half_allowed=float(row["first_half_allowed"]),
        )
    return _segment_history_context(history, current_is_home=current_is_home)


def _current_team_context(conn: sqlite3.Connection, *, team_id: int, game_date: str) -> dict[str, float]:
    rows = conn.execute(
        """
        SELECT
            g.game_date,
            CASE WHEN g.home_team_id = ? THEN segments.home_1h_points ELSE segments.away_1h_points END AS points,
            CASE WHEN g.home_team_id = ? THEN segments.away_1h_points ELSE segments.home_1h_points END AS opponent_points,
            (
                CASE
                    WHEN g.home_team_id = ? THEN COALESCE(home_result.possessions, away_result.possessions, 78.0)
                    ELSE COALESCE(away_result.possessions, home_result.possessions, 78.0)
                END
            ) / 2.0 AS possessions
        FROM games g
        JOIN team_game_results home_result ON home_result.game_id = g.id AND home_result.team_id = g.home_team_id
        JOIN team_game_results away_result ON away_result.game_id = g.id AND away_result.team_id = g.away_team_id
        JOIN game_segment_results segments ON segments.game_id = g.id
        WHERE g.status = 'final'
          AND ? IN (g.home_team_id, g.away_team_id)
          AND g.game_date < ?
          AND segments.home_1h_points IS NOT NULL
          AND segments.away_1h_points IS NOT NULL
        ORDER BY g.game_date ASC, g.start_time ASC, g.id ASC
        """,
        (team_id, team_id, team_id, team_id, game_date),
    ).fetchall()
    history: dict[int, dict[str, object]] = {}
    for row in rows:
        gp._append_team_history(
            history,
            team_id=team_id,
            game_date=str(row["game_date"]),
            points=float(row["points"] or 0.0),
            opponent_points=float(row["opponent_points"] or 0.0),
            possessions=float(row["possessions"] or 39.0),
        )
    return gp._team_history_context(history.get(team_id))


def _league_half_possessions(conn: sqlite3.Connection, *, game_date: str) -> float:
    row = conn.execute(
        """
        SELECT AVG(COALESCE(team_game_results.possessions, 78.0) / 2.0) AS avg_half_possessions
        FROM team_game_results
        JOIN games g ON g.id = team_game_results.game_id
        JOIN game_segment_results segments ON segments.game_id = g.id
        WHERE g.status = 'final'
          AND g.game_date < ?
          AND segments.home_1h_points IS NOT NULL
          AND segments.away_1h_points IS NOT NULL
        """,
        (game_date,),
    ).fetchone()
    value = float(row["avg_half_possessions"] or 39.0) if row is not None else 39.0
    return max(value, 1.0)


def _append_segment_row(
    history: dict[str, object],
    *,
    game_date: str,
    is_home: bool,
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
    split_prefix = "home" if is_home else "away"
    recent_split_q1_points = list(history.get(f"recent_{split_prefix}_q1_points") or [])
    recent_split_q1_allowed = list(history.get(f"recent_{split_prefix}_q1_allowed") or [])
    recent_q1_points.append(q1_points)
    recent_q1_allowed.append(q1_allowed)
    recent_first_half_points.append(first_half_points)
    recent_first_half_allowed.append(first_half_allowed)
    history[f"{split_prefix}_games"] = int(history.get(f"{split_prefix}_games") or 0) + 1
    history[f"{split_prefix}_q1_points_sum"] = float(history.get(f"{split_prefix}_q1_points_sum") or 0.0) + q1_points
    history[f"{split_prefix}_q1_allowed_sum"] = float(history.get(f"{split_prefix}_q1_allowed_sum") or 0.0) + q1_allowed
    recent_split_q1_points.append(q1_points)
    recent_split_q1_allowed.append(q1_allowed)
    history["recent_q1_points"] = recent_q1_points[-5:]
    history["recent_q1_allowed"] = recent_q1_allowed[-5:]
    history["recent_first_half_points"] = recent_first_half_points[-5:]
    history["recent_first_half_allowed"] = recent_first_half_allowed[-5:]
    history[f"recent_{split_prefix}_q1_points"] = recent_split_q1_points[-5:]
    history[f"recent_{split_prefix}_q1_allowed"] = recent_split_q1_allowed[-5:]
    history["last_game_date"] = game_date


def _game_value(game: Mapping[str, Any] | sqlite3.Row, key: str, default: Any = None) -> Any:
    if isinstance(game, sqlite3.Row):
        return game[key] if key in game.keys() else default
    return game.get(key, default)
