"""
Backtesting and accuracy analysis for player prop projections.
Compares predictions to actual outcomes and identifies projection quality.
"""

from __future__ import annotations

import sqlite3
import math
from dataclasses import dataclass
from typing import Optional
from datetime import datetime, timedelta, timezone


@dataclass
class PredictionAccuracy:
    """Accuracy metrics for a single prediction."""
    prop_line_id: int
    player_id: int
    player_name: str
    market: str
    game_date: str
    projection: float
    actual_result: float
    line: float
    error: float
    abs_error: float
    directional_hit: bool
    confidence: str
    edge: float
    rec_side: str
    actual_side: str
    correct_side: bool
    minutes: float


@dataclass
class AccuracyMetrics:
    """Overall accuracy statistics."""
    total_predictions: int
    mae: float  # Mean absolute error
    rmse: float  # Root mean squared error
    bias: float  # Mean error (systematic over/under)
    directional_accuracy: float  # % correct direction
    confidence_calibration: dict[str, float]  # Calibration by confidence level
    market_breakdown: dict[str, dict]  # Metrics per market
    error_by_minutes_played: dict[str, float]  # How accuracy varies with playing time
    performance_by_edge: dict[str, dict]  # ROI if betting on different edge thresholds


def build_accuracy_report(conn: sqlite3.Connection, model_version: Optional[str] = None) -> AccuracyMetrics:
    """
    Generate comprehensive accuracy analysis comparing projections to actual outcomes.
    Uses completed games where player_game_stats exist.
    """
    # Get all predictions that can be matched with actual outcomes
    query, params = _accuracy_query()
    if model_version:
        query += " AND pp.model_version = ?"
        params.append(model_version)

    query += " ORDER BY g.game_date, p.full_name"
    rows = conn.execute(query, params).fetchall()

    if not rows:
        return AccuracyMetrics(
            total_predictions=0,
            mae=0.0,
            rmse=0.0,
            bias=0.0,
            directional_accuracy=0.0,
            confidence_calibration={},
            market_breakdown={},
            error_by_minutes_played={},
            performance_by_edge={}
        )
    
    accuracies = [_accuracy_from_row(row) for row in rows]
    errors = [accuracy.error for accuracy in accuracies]
    abs_errors = [accuracy.abs_error for accuracy in accuracies]
    direction_hits = sum(1 for accuracy in accuracies if accuracy.directional_hit)

    confidence_by_level = {"high": [], "medium": [], "low": []}
    errors_by_market = {}
    abs_errors_by_market = {}
    direction_hits_by_market = {}
    total_by_market = {}

    for accuracy in accuracies:
        market = accuracy.market
        error = accuracy.error
        abs_error = accuracy.abs_error

        # Track by confidence level
        conf_level = accuracy.confidence
        if conf_level in confidence_by_level:
            confidence_by_level[conf_level].append(accuracy)

        # Track by market
        if market not in errors_by_market:
            errors_by_market[market] = []
            abs_errors_by_market[market] = []
            direction_hits_by_market[market] = 0
            total_by_market[market] = 0
        
        errors_by_market[market].append(error)
        abs_errors_by_market[market].append(abs_error)
        if accuracy.directional_hit:
            direction_hits_by_market[market] += 1
        total_by_market[market] += 1
    
    # Calculate overall metrics
    n = len(errors)
    mae = sum(abs_errors) / n
    rmse = math.sqrt(sum(e ** 2 for e in errors) / n)
    bias = sum(errors) / n
    directional_acc = direction_hits / n if n > 0 else 0.0
    
    # Calibration by confidence
    calibration = {}
    for conf_level, conf_accuracies in confidence_by_level.items():
        if conf_accuracies:
            errs = [accuracy.error for accuracy in conf_accuracies]
            calibration[conf_level] = {
                "count": len(errs),
                "mae": sum(abs(e) for e in errs) / len(errs),
                "bias": sum(errs) / len(errs),
                "directional_accuracy": sum(1 for accuracy in conf_accuracies if accuracy.directional_hit) / len(conf_accuracies),
            }
    
    # Market breakdown
    market_stats = {}
    for market in errors_by_market:
        errs = errors_by_market[market]
        abs_errs = abs_errors_by_market[market]
        hits = direction_hits_by_market[market]
        total = total_by_market[market]
        market_stats[market] = {
            "predictions": total,
            "mae": sum(abs_errs) / total,
            "rmse": math.sqrt(sum(e ** 2 for e in errs) / total),
            "bias": sum(errs) / total,
            "directional_accuracy": hits / total,
        }
    
    # Error distribution by minutes played (proxy for importance)
    min_buckets = {"0-10": [], "10-20": [], "20-30": [], "30+": []}
    for acc in accuracies:
        if acc.minutes < 10:
            bucket = "0-10"
        elif acc.minutes < 20:
            bucket = "10-20"
        elif acc.minutes < 30:
            bucket = "20-30"
        else:
            bucket = "30+"
        
        min_buckets[bucket].append(acc.abs_error)
    
    error_by_mins = {
        bucket: sum(errs) / len(errs) if errs else 0.0
        for bucket, errs in min_buckets.items() if errs
    }
    
    # Performance by edge threshold (for betting strategy)
    edge_thresholds = [0.02, 0.04, 0.06, 0.08, 0.10]
    edge_performance = {}
    for threshold in edge_thresholds:
        filtered_accs = [a for a in accuracies if abs(a.edge) >= threshold]
        if filtered_accs:
            correct = sum(1 for a in filtered_accs if a.correct_side)
            edge_performance[f"edge>={threshold}"] = {
                "predictions": len(filtered_accs),
                "correct_picks": correct,
                "accuracy": correct / len(filtered_accs),
                "avg_edge": sum(a.edge for a in filtered_accs) / len(filtered_accs),
            }
    
    return AccuracyMetrics(
        total_predictions=n,
        mae=round(mae, 3),
        rmse=round(rmse, 3),
        bias=round(bias, 3),
        directional_accuracy=round(directional_acc, 3),
        confidence_calibration=calibration,
        market_breakdown=market_stats,
        error_by_minutes_played=error_by_mins,
        performance_by_edge=edge_performance,
    )


def build_accuracy_report_for_days(
    conn: sqlite3.Connection,
    days: int,
    model_version: Optional[str] = None,
) -> AccuracyMetrics:
    if days <= 0:
        return AccuracyMetrics(
            total_predictions=0,
            mae=0.0,
            rmse=0.0,
            bias=0.0,
            directional_accuracy=0.0,
            confidence_calibration={},
            market_breakdown={},
            error_by_minutes_played={},
            performance_by_edge={},
        )
    cutoff_date = (datetime.now(timezone.utc).date() - timedelta(days=days)).isoformat()
    return _build_accuracy_report_with_cutoff(conn, cutoff_date, model_version)


def _build_accuracy_report_with_cutoff(
    conn: sqlite3.Connection,
    cutoff_date: str | None,
    model_version: Optional[str],
) -> AccuracyMetrics:
    query, params = _accuracy_query()
    if cutoff_date:
        query += " AND g.game_date >= ?"
        params.append(cutoff_date)
    if model_version:
        query += " AND pp.model_version = ?"
        params.append(model_version)
    query += " ORDER BY g.game_date, p.full_name"
    rows = conn.execute(query, params).fetchall()
    if not rows:
        return AccuracyMetrics(
            total_predictions=0,
            mae=0.0,
            rmse=0.0,
            bias=0.0,
            directional_accuracy=0.0,
            confidence_calibration={},
            market_breakdown={},
            error_by_minutes_played={},
            performance_by_edge={},
        )
    accuracies = [_accuracy_from_row(row) for row in rows]
    errors = [accuracy.error for accuracy in accuracies]
    abs_errors = [accuracy.abs_error for accuracy in accuracies]
    direction_hits = sum(1 for accuracy in accuracies if accuracy.directional_hit)

    confidence_by_level = {"high": [], "medium": [], "low": []}
    errors_by_market = {}
    abs_errors_by_market = {}
    direction_hits_by_market = {}
    total_by_market = {}

    for accuracy in accuracies:
        market = accuracy.market
        error = accuracy.error
        abs_error = accuracy.abs_error

        conf_level = accuracy.confidence
        if conf_level in confidence_by_level:
            confidence_by_level[conf_level].append(accuracy)

        if market not in errors_by_market:
            errors_by_market[market] = []
            abs_errors_by_market[market] = []
            direction_hits_by_market[market] = 0
            total_by_market[market] = 0

        errors_by_market[market].append(error)
        abs_errors_by_market[market].append(abs_error)
        if accuracy.directional_hit:
            direction_hits_by_market[market] += 1
        total_by_market[market] += 1

    n = len(errors)
    mae = sum(abs_errors) / n
    rmse = math.sqrt(sum(e ** 2 for e in errors) / n)
    bias = sum(errors) / n
    directional_acc = direction_hits / n if n > 0 else 0.0

    calibration = {}
    for conf_level, conf_accuracies in confidence_by_level.items():
        if conf_accuracies:
            errs = [accuracy.error for accuracy in conf_accuracies]
            calibration[conf_level] = {
                "count": len(errs),
                "mae": sum(abs(e) for e in errs) / len(errs),
                "bias": sum(errs) / len(errs),
                "directional_accuracy": sum(1 for accuracy in conf_accuracies if accuracy.directional_hit) / len(conf_accuracies),
            }

    market_stats = {}
    for market in errors_by_market:
        errs = errors_by_market[market]
        abs_errs = abs_errors_by_market[market]
        hits = direction_hits_by_market[market]
        total = total_by_market[market]
        market_stats[market] = {
            "predictions": total,
            "mae": sum(abs_errs) / total,
            "rmse": math.sqrt(sum(e ** 2 for e in errs) / total),
            "bias": sum(errs) / total,
            "directional_accuracy": hits / total,
        }

    min_buckets = {"0-10": [], "10-20": [], "20-30": [], "30+": []}
    for acc in accuracies:
        if acc.minutes < 10:
            bucket = "0-10"
        elif acc.minutes < 20:
            bucket = "10-20"
        elif acc.minutes < 30:
            bucket = "20-30"
        else:
            bucket = "30+"
        min_buckets[bucket].append(acc.abs_error)

    error_by_mins = {
        bucket: sum(errs) / len(errs) if errs else 0.0
        for bucket, errs in min_buckets.items() if errs
    }

    edge_thresholds = [0.02, 0.04, 0.06, 0.08, 0.10]
    edge_performance = {}
    for threshold in edge_thresholds:
        filtered_accs = [a for a in accuracies if abs(a.edge) >= threshold]
        if filtered_accs:
            correct = sum(1 for a in filtered_accs if a.correct_side)
            edge_performance[f"edge>={threshold}"] = {
                "predictions": len(filtered_accs),
                "correct_picks": correct,
                "accuracy": correct / len(filtered_accs),
                "avg_edge": sum(a.edge for a in filtered_accs) / len(filtered_accs),
            }

    return AccuracyMetrics(
        total_predictions=n,
        mae=round(mae, 3),
        rmse=round(rmse, 3),
        bias=round(bias, 3),
        directional_accuracy=round(directional_acc, 3),
        confidence_calibration=calibration,
        market_breakdown=market_stats,
        error_by_minutes_played=error_by_mins,
        performance_by_edge=edge_performance,
    )


def get_best_predictions(
    conn: sqlite3.Connection,
    limit: int = 20,
    min_edge: float = 0.0,
    confidence_level: Optional[str] = None,
    model_version: Optional[str] = None,
) -> list[PredictionAccuracy]:
    """
    Get the most accurate predictions (smallest error magnitude).
    Useful for identifying which projections are most reliable for parlays.
    """
    query, params = _accuracy_query("ABS(pp.edge) >= ?")
    params.append(min_edge)

    if confidence_level:
        query += " AND pp.confidence = ?"
        params.append(confidence_level)
    
    if model_version:
        query += " AND pp.model_version = ?"
        params.append(model_version)
    
    rows = conn.execute(query, params).fetchall()
    accuracies = [_accuracy_from_row(row) for row in rows]

    # Sort by accuracy (smallest error)
    accuracies.sort(key=lambda a: a.abs_error)
    return accuracies[:limit]


def get_worst_predictions(
    conn: sqlite3.Connection,
    limit: int = 20,
    model_version: Optional[str] = None,
) -> list[PredictionAccuracy]:
    """
    Get the most inaccurate predictions (largest error).
    Useful for identifying systematic biases or problem areas.
    """
    query, params = _accuracy_query()

    if model_version:
        query += " AND pp.model_version = ?"
        params.append(model_version)

    rows = conn.execute(query, params).fetchall()
    accuracies = [_accuracy_from_row(row) for row in rows]

    # Sort by error (largest first)
    accuracies.sort(key=lambda a: a.abs_error, reverse=True)
    return accuracies[:limit]


def _accuracy_query(extra_condition: str | None = None) -> tuple[str, list]:
    query = """
        SELECT
            pp.id as prop_id,
            pp.prop_line_id,
            pp.projection,
            pp.confidence,
            pp.edge,
            pp.recommended_side,
            pp.model_version,
            pl.line,
            pl.market,
            pl.player_id,
            pl.game_id,
            p.full_name,
            g.game_date,
            pgs.points,
            pgs.rebounds,
            pgs.assists,
            pgs.threes,
            pgs.minutes
        FROM prop_predictions pp
        JOIN prop_lines pl ON pl.id = pp.prop_line_id
        JOIN players p ON p.id = pl.player_id
        JOIN games g ON g.id = pl.game_id
        LEFT JOIN player_game_stats pgs ON pgs.player_id = pl.player_id AND pgs.game_id = pl.game_id
        WHERE pgs.id IS NOT NULL
    """

    if extra_condition:
        query += f" AND {extra_condition}"

    return query, []


def _accuracy_from_row(row: sqlite3.Row) -> PredictionAccuracy:
    market = row["market"]
    actual = _market_value(row, market)
    projection = float(row["projection"])
    line = float(row["line"])
    error = actual - projection
    abs_error = abs(error)
    rec_side = row["recommended_side"]
    actual_side = "over" if actual > line else "under"
    correct_side = rec_side == actual_side
    direction_hit = (actual >= projection) if rec_side == "over" else (actual <= projection)

    return PredictionAccuracy(
        prop_line_id=row["prop_line_id"],
        player_id=row["player_id"],
        player_name=row["full_name"],
        market=market,
        game_date=row["game_date"],
        projection=projection,
        actual_result=actual,
        line=line,
        error=error,
        abs_error=abs_error,
        directional_hit=direction_hit,
        confidence=row["confidence"],
        edge=row["edge"],
        rec_side=rec_side,
        actual_side=actual_side,
        correct_side=correct_side,
        minutes=float(row["minutes"] or 0.0),
    )


def _market_value(row: sqlite3.Row, market: str) -> float:
    """Extract the stat value for a given market."""
    if market == "points_rebounds":
        return float(row["points"] + row["rebounds"])
    elif market == "points_assists":
        return float(row["points"] + row["assists"])
    elif market == "rebounds_assists":
        return float(row["rebounds"] + row["assists"])
    if market == "points_rebounds_assists":
        return float(row["points"] + row["rebounds"] + row["assists"])
    elif market == "points":
        return float(row["points"])
    elif market == "rebounds":
        return float(row["rebounds"])
    elif market == "assists":
        return float(row["assists"])
    elif market == "threes":
        return float(row["threes"])
    elif market == "steals":
        return float(row["steals"])
    elif market == "blocks":
        return float(row["blocks"])
    elif market == "blocks_steals":
        return float(row["blocks"] + row["steals"])
    else:
        raise ValueError(f"Unsupported market: {market}")
