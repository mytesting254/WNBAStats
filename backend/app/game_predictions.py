from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Mapping

try:
    import numpy as np
except ImportError:  # pragma: no cover - fallback remains exercised without numpy installed
    np = None

from .game_pregame_features import build_matchup_pregame_features
from .game_training_db import game_training_db_signature, load_game_training_rows
from .player_prop_model import _before_training_start, _training_start_date

TOTAL_CALIBRATION_MIN_SAMPLES = 12
TOTAL_BIAS_CORRECTION_WEIGHT = 0.8
TOTAL_MARKET_BLEND_WEIGHT = 0.3
TOTAL_MARKET_EDGE_CAP = 12.0
TOTAL_SCALE_MIN = 0.985
TOTAL_SCALE_MAX = 1.03
GAME_EDGE_MODEL_MIN_ROWS = 15
GAME_EDGE_MODEL_MAX_ADJUSTMENT = 8.0
GAME_MARGIN_BLEND_WEIGHT = 0.16
GAME_TOTAL_BLEND_WEIGHT = 0.18
GAME_MARGIN_POST_MARKET_ANCHOR = 0.08
GAME_TOTAL_POST_MARKET_ANCHOR = 0.10
GAME_DIRECT_MODEL_MIN_ROWS = 120
GAME_DIRECT_MODEL_MAX_POINTS = 25.0
GAME_DIRECT_BLEND_MAX = 0.72
GAME_TOTAL_DECISION_BLEND_MAX = 0.68
GAME_TOTAL_DECISION_MODEL_MIN_ROWS = 120
GAME_TOTAL_DECISION_MIN_EDGE = 1.0
GAME_DIRECT_FEATURE_NAMES = [
    "home_recent_points",
    "away_recent_points",
    "home_recent_allowed",
    "away_recent_allowed",
    "home_average_points",
    "away_average_points",
    "home_average_allowed",
    "away_average_allowed",
    "home_average_possessions",
    "away_average_possessions",
    "pace_factor",
    "rest_days_home",
    "rest_days_away",
    "rest_day_diff",
    "home_game_count",
    "away_game_count",
    "recent_scoring_delta",
    "recent_allowed_delta",
    "season_scoring_delta",
    "season_allowed_delta",
    "recent_possessions_delta",
    "season_possessions_delta",
    "home_recent_vs_season_points",
    "away_recent_vs_season_points",
    "home_recent_vs_season_allowed",
    "away_recent_vs_season_allowed",
    "home_recent_vs_season_possessions",
    "away_recent_vs_season_possessions",
    "home_season_off_rating",
    "away_season_off_rating",
    "home_season_def_rating",
    "away_season_def_rating",
    "home_recent_off_rating",
    "away_recent_off_rating",
    "home_recent_def_rating",
    "away_recent_def_rating",
    "season_net_rating_diff",
    "recent_net_rating_diff",
    "spread_home",
    "game_total",
    "home_implied_prob",
    "away_implied_prob",
    "vig_free_home_prob",
    "home_spread_price",
    "away_spread_price",
    "spread_price_gap",
    "over_price",
    "under_price",
    "total_price_gap",
    "over_implied_prob",
    "under_implied_prob",
    "vig_free_over_prob",
    "home_projected_minutes_total",
    "away_projected_minutes_total",
    "home_projected_points_total",
    "away_projected_points_total",
    "home_top3_minutes_share",
    "away_top3_minutes_share",
    "home_top5_points_share",
    "away_top5_points_share",
    "home_rotation_count",
    "away_rotation_count",
    "home_creator_count",
    "away_creator_count",
    "projected_minutes_delta",
    "projected_points_delta",
    "rotation_count_diff",
    "creator_count_diff",
    "home_off_form_delta",
    "away_off_form_delta",
    "home_def_form_delta",
    "away_def_form_delta",
    "home_pace_form_delta",
    "away_pace_form_delta",
    "home_fga_form_delta",
    "away_fga_form_delta",
    "home_fga_allowed_form_delta",
    "away_fga_allowed_form_delta",
    "home_turnover_rate_form_delta",
    "away_turnover_rate_form_delta",
    "home_forced_turnover_rate_form_delta",
    "away_forced_turnover_rate_form_delta",
    "home_off_form_volatility",
    "away_off_form_volatility",
    "home_def_form_volatility",
    "away_def_form_volatility",
    "off_form_delta_diff",
    "def_form_delta_diff",
    "pace_form_delta_diff",
    "fga_form_delta_diff",
    "fga_allowed_form_delta_diff",
    "turnover_rate_form_delta_diff",
    "forced_turnover_rate_form_delta_diff",
    "home_hot_offense_flag",
    "away_hot_offense_flag",
    "home_slump_offense_flag",
    "away_slump_offense_flag",
    "home_hot_defense_flag",
    "away_hot_defense_flag",
    "home_slump_defense_flag",
    "away_slump_defense_flag",
]
GAME_MARGIN_FEATURE_NAMES = [
    "projected_margin",
    "market_edge",
    "spread_home",
    "rest_days_home",
    "rest_days_away",
]
GAME_TOTAL_FEATURE_NAMES = [
    "projected_total",
    "market_edge",
    "game_total",
    "rest_days_home",
    "rest_days_away",
]


@dataclass(frozen=True)
class _GameEdgeModel:
    edge_type: str
    rows: int
    intercept: float
    coefficients: list[float]
    feature_means: list[float]
    feature_scales: list[float]


@dataclass(frozen=True)
class _DirectGameModel:
    target: str
    rows: int
    intercept: float
    coefficients: list[float]
    feature_means: list[float]
    feature_scales: list[float]


class _GamePredictionCache:
    def __init__(self, conn: sqlite3.Connection, team_ids: tuple[int, ...]):
        self.conn = conn
        self._team_summaries: dict[int, dict[str, float]] = {}
        self._league_possessions: float | None = None
        self._weighted_recent_cache: dict[tuple[int, str], float] = {}
        self._injury_factor_cache: dict[int, dict[str, float | int | str]] = {}
        self.runtime_cache: dict[str, dict[tuple, object]] = {}
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


_DIRECT_MODEL_CACHE: dict[tuple[str, str, int, int], _DirectGameModel] = {}


def _row_value(row: Mapping[str, Any] | sqlite3.Row, key: str, default: Any = None) -> Any:
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else default
    return row.get(key, default)


def project_game(
    conn: sqlite3.Connection,
    game: Mapping[str, Any],
    *,
    runtime_cache: _GamePredictionCache | None = None,
) -> dict:
    home_team_id = int(game["home_team_id"])
    away_team_id = int(game["away_team_id"])
    cache = runtime_cache or _GamePredictionCache(conn, (home_team_id, away_team_id))
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
    calibrated_total = _calibrate_total_projection(conn, projected_total, game_total)
    residual_notes: list[str] = []

    direct_features = _direct_game_features(
        cache,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        game_id=int(game["id"]),
        game_date=str(_row_value(game, "game_date") or "") or None,
        rest_days_home=int(game["rest_days_home"] or 2),
        rest_days_away=int(game["rest_days_away"] or 2),
        spread_home=spread_home,
        game_total=game_total,
        home_moneyline=_coerce_float(_row_value(game, "home_moneyline")),
        away_moneyline=_coerce_float(_row_value(game, "away_moneyline")),
        home_spread_price=_coerce_float(_row_value(game, "home_spread_price")),
        away_spread_price=_coerce_float(_row_value(game, "away_spread_price")),
        over_price=_coerce_float(_row_value(game, "over_price")),
        under_price=_coerce_float(_row_value(game, "under_price")),
    )
    direct_margin = _predict_direct_game_value(conn, "margin", direct_features)
    direct_total = _predict_direct_game_value(conn, "total", direct_features)
    if direct_margin is not None:
        weight = _direct_game_blend_weight(direct_margin.rows)
        projected_margin = ((1 - weight) * projected_margin) + (weight * direct_margin.value)
        residual_notes.append(f"trained margin blend {weight:.0%} ({direct_margin.rows} rows)")
    if spread_home is not None:
        projected_margin = ((1 - GAME_MARGIN_POST_MARKET_ANCHOR) * projected_margin) + (GAME_MARGIN_POST_MARKET_ANCHOR * (-spread_home))
        residual_notes.append(f"market spread anchor {GAME_MARGIN_POST_MARKET_ANCHOR:.0%}")
    if direct_total is not None:
        weight = _direct_game_blend_weight(direct_total.rows)
        calibrated_total = ((1 - weight) * calibrated_total) + (weight * direct_total.value)
        residual_notes.append(f"trained total blend {weight:.0%} ({direct_total.rows} rows)")
    if game_total is not None:
        calibrated_total = ((1 - GAME_TOTAL_POST_MARKET_ANCHOR) * calibrated_total) + (GAME_TOTAL_POST_MARKET_ANCHOR * game_total)
        residual_notes.append(f"market total anchor {GAME_TOTAL_POST_MARKET_ANCHOR:.0%}")

    adjusted_margin = projected_margin
    if spread_home is not None:
        adjusted_margin, margin_note = _apply_game_margin_residual(
            conn,
            projected_margin,
            spread_home,
            int(game["rest_days_home"] or 2),
            int(game["rest_days_away"] or 2),
        )
        if margin_note:
            residual_notes.append(margin_note)

    adjusted_total = calibrated_total
    if game_total is not None:
        adjusted_total, total_note = _apply_game_total_residual(
            conn,
            calibrated_total,
            game_total,
            int(game["rest_days_home"] or 2),
            int(game["rest_days_away"] or 2),
            over_price=_coerce_float(_row_value(game, "over_price")),
            under_price=_coerce_float(_row_value(game, "under_price")),
            spread_home=spread_home,
        )
        if total_note:
            residual_notes.append(total_note)

    home_projection = max(0.0, (adjusted_total + adjusted_margin) / 2)
    away_projection = max(0.0, adjusted_total - home_projection)

    ats_edge = None
    ats_pick = "N/A"
    if spread_home is not None:
        ats_edge = adjusted_margin + spread_home
        ats_pick = f"{game['home_team']} {format_spread(spread_home)}" if ats_edge > 0 else f"{game['away_team']} {format_spread(-spread_home)}"

    total_edge = None
    total_pick = "N/A"
    baseline_total_edge = None
    total_market_decision = None
    if game_total is not None:
        baseline_total_edge = adjusted_total - game_total
        total_market_decision = _predict_market_total_edge(
            conn,
            direct_features,
            baseline_total_edge,
            game_total,
        )
        total_edge = total_market_decision.edge if total_market_decision is not None else baseline_total_edge
        total_pick = "Over" if total_edge > 0 else "Under"

    winner = game["home_team"] if adjusted_margin >= 0 else game["away_team"]
    confidence = _confidence(abs(ats_edge or 0), abs(total_edge or 0), abs(adjusted_margin))
    total_confidence = _total_confidence(total_edge)
    total_reason = _total_reason(
        projected_total=adjusted_total,
        market_total=game_total,
        total_edge=total_edge,
        direct_total_rows=direct_total.rows if direct_total is not None else None,
        baseline_total_edge=baseline_total_edge,
        market_decision_rows=total_market_decision.rows if total_market_decision is not None else None,
        market_decision_weight=total_market_decision.weight if total_market_decision is not None else None,
        residual_notes=[note for note in residual_notes if "total" in note.lower()],
    )

    return {
        "home_projected_points": round(home_projection, 1),
        "away_projected_points": round(away_projection, 1),
        "projected_margin": round(adjusted_margin, 1),
        "projected_total": round(adjusted_total, 1),
        "winner_pick": winner,
        "ats_pick": ats_pick,
        "ats_edge": round(ats_edge, 1) if ats_edge is not None else None,
        "total_pick": total_pick,
        "total_edge": round(total_edge, 1) if total_edge is not None else None,
        "game_confidence": confidence,
        "total_confidence": total_confidence,
        "game_reason": _reason(
            home_projection,
            away_projection,
            adjusted_margin,
            adjusted_total,
            home_history_count,
            away_history_count,
            game["home_team"],
            game["away_team"],
            home_injury,
            away_injury,
            residual_notes,
        ),
        "total_reason": total_reason,
    }


def format_spread(value: float) -> str:
    return f"+{value:.1f}" if value > 0 else f"{value:.1f}"


def _coerce_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _moneyline_implied_probability(value: float | None) -> float | None:
    if value is None:
        return None
    if value > 0:
        return 100.0 / (value + 100.0)
    if value < 0:
        return (-value) / ((-value) + 100.0)
    return None


def _vig_free_home_probability(home_moneyline: float | None, away_moneyline: float | None) -> float | None:
    home_prob = _moneyline_implied_probability(home_moneyline)
    away_prob = _moneyline_implied_probability(away_moneyline)
    return _vig_free_probability_from_probabilities(home_prob, away_prob)


def _vig_free_probability(price_a: float | None, price_b: float | None) -> float | None:
    prob_a = _moneyline_implied_probability(price_a)
    prob_b = _moneyline_implied_probability(price_b)
    return _vig_free_probability_from_probabilities(prob_a, prob_b)


def _vig_free_probability_from_probabilities(prob_a: float | None, prob_b: float | None) -> float | None:
    if prob_a is None or prob_b is None:
        return None
    denom = prob_a + prob_b
    if denom <= 0:
        return None
    return prob_a / denom


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


def _total_confidence(total_edge: float | None) -> str:
    if total_edge is None:
        return "no market"
    absolute_edge = abs(float(total_edge))
    if absolute_edge >= 5.0:
        return "high"
    if absolute_edge >= 2.5:
        return "medium"
    return "low"


def _total_reason(
    *,
    projected_total: float,
    market_total: float | None,
    total_edge: float | None,
    direct_total_rows: int | None,
    baseline_total_edge: float | None,
    market_decision_rows: int | None,
    market_decision_weight: float | None,
    residual_notes: list[str] | None = None,
) -> str:
    if market_total is None or total_edge is None:
        return (
            f"No trusted market total was available. Model projected {projected_total:.1f} "
            "from team scoring, pace, rest, and injury context only."
        )
    details = [
        f"Model total {projected_total:.1f} versus market {market_total:.1f}",
        f"edge {total_edge:+.1f}",
    ]
    if baseline_total_edge is not None and abs(float(total_edge) - float(baseline_total_edge)) >= 0.05:
        details.append(f"score-model edge {baseline_total_edge:+.1f}")
    if direct_total_rows is not None:
        details.append(f"direct total model support {direct_total_rows} rows")
    if market_decision_rows is not None and market_decision_weight is not None:
        details.append(f"total-market decision blend {market_decision_weight:.0%} ({market_decision_rows} rows)")
    if residual_notes:
        details.append("; ".join(residual_notes))
    return ". ".join(details) + "."


def _calibrate_total_projection(
    conn: sqlite3.Connection,
    projected_total: float,
    game_total: float | None,
) -> float:
    metrics = _historical_total_calibration_metrics(conn)
    calibrated = projected_total
    if metrics is not None:
        calibrated *= float(metrics["scale"])
        calibrated += float(metrics["bias_adjustment"])
    if game_total is not None:
        market_gap = _clamp(float(game_total) - calibrated, -TOTAL_MARKET_EDGE_CAP, TOTAL_MARKET_EDGE_CAP)
        calibrated += market_gap * TOTAL_MARKET_BLEND_WEIGHT
    return round(calibrated, 3)


def _historical_total_calibration_metrics(conn: sqlite3.Connection) -> dict[str, float] | None:
    row = conn.execute(
        """
        SELECT
            COUNT(*) AS sample_count,
            AVG(gp.projected_total) AS avg_projected_total,
            AVG(sgp.actual_total) AS avg_actual_total,
            AVG(gp.projected_total - sgp.actual_total) AS avg_bias
        FROM settled_game_predictions sgp
        JOIN game_predictions gp ON gp.id = sgp.game_prediction_id
        WHERE gp.projected_total IS NOT NULL
          AND sgp.actual_total IS NOT NULL
        """
    ).fetchone()
    sample_count = int(row["sample_count"] or 0)
    avg_projected_total = float(row["avg_projected_total"] or 0.0)
    avg_actual_total = float(row["avg_actual_total"] or 0.0)
    avg_bias = float(row["avg_bias"] or 0.0)
    if sample_count < TOTAL_CALIBRATION_MIN_SAMPLES or avg_projected_total <= 0:
        return None
    scale = _clamp(avg_actual_total / avg_projected_total, TOTAL_SCALE_MIN, TOTAL_SCALE_MAX)
    bias_adjustment = _clamp(-avg_bias * TOTAL_BIAS_CORRECTION_WEIGHT, -4.0, 4.0)
    return {
        "sample_count": float(sample_count),
        "scale": scale,
        "bias_adjustment": bias_adjustment,
    }


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
    residual_notes: list[str] | None = None,
) -> str:
    injury_detail = _injury_reason_detail(home_team, away_team, home_injury, away_injury)
    residual_detail = ""
    if residual_notes:
        residual_detail = " Residual adjustments: " + "; ".join(residual_notes) + "."
    if min(home_history_count, away_history_count) < 3:
        return (
            f"Projected score {home_projection:.1f}-{away_projection:.1f}; margin {margin:.1f}, total {total:.1f}. "
            f"Limited imported history for this matchup ({home_history_count} home-team rows, "
            f"{away_history_count} away-team rows), so treat this as a market/context view, not a model edge."
            f"{injury_detail}{residual_detail}"
        )
    return (
        f"Projected score {home_projection:.1f}-{away_projection:.1f}; "
        f"margin {margin:.1f}, total {total:.1f}. Built from direct historical game training, recent scoring, "
        f"season scoring, opponent points allowed, pace, home/away, rest, injury availability, and settled game residuals.{injury_detail}{residual_detail}"
    )


@dataclass(frozen=True)
class _DirectPrediction:
    rows: int
    value: float


@dataclass(frozen=True)
class _MarketTotalDecision:
    rows: int
    edge: float
    weight: float


def _direct_game_features(
    cache: _GamePredictionCache,
    *,
    home_team_id: int,
    away_team_id: int,
    game_id: int,
    game_date: str | None,
    rest_days_home: int,
    rest_days_away: int,
    spread_home: float | None,
    game_total: float | None,
    home_moneyline: float | None,
    away_moneyline: float | None,
    home_spread_price: float | None,
    away_spread_price: float | None,
    over_price: float | None,
    under_price: float | None,
) -> list[float]:
    home_recent_points = cache.weighted_recent(home_team_id, "points")
    away_recent_points = cache.weighted_recent(away_team_id, "points")
    home_recent_allowed = cache.weighted_recent(home_team_id, "opponent_points")
    away_recent_allowed = cache.weighted_recent(away_team_id, "opponent_points")
    home_average_points = cache.team_average(home_team_id, "points")
    away_average_points = cache.team_average(away_team_id, "points")
    home_average_allowed = cache.team_average(home_team_id, "opponent_points")
    away_average_allowed = cache.team_average(away_team_id, "opponent_points")
    home_average_possessions = cache.team_average(home_team_id, "possessions")
    away_average_possessions = cache.team_average(away_team_id, "possessions")
    home_recent_possessions = cache.weighted_recent(home_team_id, "possessions")
    away_recent_possessions = cache.weighted_recent(away_team_id, "possessions")
    matchup_pregame = build_matchup_pregame_features(
        cache.conn,
        home_team_id=home_team_id,
        away_team_id=away_team_id,
        game_id=game_id,
        game_date=game_date,
        use_injury_context=False,
        runtime_cache=cache.runtime_cache,
    )
    return _assemble_direct_game_features(
        home_recent_points=home_recent_points,
        away_recent_points=away_recent_points,
        home_recent_allowed=home_recent_allowed,
        away_recent_allowed=away_recent_allowed,
        home_average_points=home_average_points,
        away_average_points=away_average_points,
        home_average_allowed=home_average_allowed,
        away_average_allowed=away_average_allowed,
        home_average_possessions=home_average_possessions,
        away_average_possessions=away_average_possessions,
        pace_factor=cache.pace_factor(home_team_id, away_team_id),
        rest_days_home=rest_days_home,
        rest_days_away=rest_days_away,
        home_game_count=cache.team_count(home_team_id),
        away_game_count=cache.team_count(away_team_id),
        home_recent_possessions=home_recent_possessions,
        away_recent_possessions=away_recent_possessions,
        home_season_off_rating=_team_rating(home_average_points, home_average_possessions),
        away_season_off_rating=_team_rating(away_average_points, away_average_possessions),
        home_season_def_rating=_team_rating(home_average_allowed, home_average_possessions),
        away_season_def_rating=_team_rating(away_average_allowed, away_average_possessions),
        home_recent_off_rating=_team_rating(home_recent_points, home_recent_possessions),
        away_recent_off_rating=_team_rating(away_recent_points, away_recent_possessions),
        home_recent_def_rating=_team_rating(home_recent_allowed, home_recent_possessions),
        away_recent_def_rating=_team_rating(away_recent_allowed, away_recent_possessions),
        spread_home=spread_home,
        game_total=game_total,
        home_moneyline=home_moneyline,
        away_moneyline=away_moneyline,
        home_spread_price=home_spread_price,
        away_spread_price=away_spread_price,
        over_price=over_price,
        under_price=under_price,
        matchup_pregame=matchup_pregame,
    )


def _assemble_direct_game_features(
    *,
    home_recent_points: float,
    away_recent_points: float,
    home_recent_allowed: float,
    away_recent_allowed: float,
    home_average_points: float,
    away_average_points: float,
    home_average_allowed: float,
    away_average_allowed: float,
    home_average_possessions: float,
    away_average_possessions: float,
    pace_factor: float,
    rest_days_home: int,
    rest_days_away: int,
    home_game_count: int | float,
    away_game_count: int | float,
    home_recent_possessions: float,
    away_recent_possessions: float,
    home_season_off_rating: float,
    away_season_off_rating: float,
    home_season_def_rating: float,
    away_season_def_rating: float,
    home_recent_off_rating: float,
    away_recent_off_rating: float,
    home_recent_def_rating: float,
    away_recent_def_rating: float,
    spread_home: float | None,
    game_total: float | None,
    home_moneyline: float | None,
    away_moneyline: float | None,
    home_spread_price: float | None,
    away_spread_price: float | None,
    over_price: float | None,
    under_price: float | None,
    matchup_pregame: Mapping[str, float] | None = None,
) -> list[float]:
    recent_scoring_delta = float(home_recent_points) - float(away_recent_points)
    recent_allowed_delta = float(home_recent_allowed) - float(away_recent_allowed)
    season_scoring_delta = float(home_average_points) - float(away_average_points)
    season_allowed_delta = float(home_average_allowed) - float(away_average_allowed)
    recent_possessions_delta = float(home_recent_possessions) - float(away_recent_possessions)
    season_possessions_delta = float(home_average_possessions) - float(away_average_possessions)
    season_net_rating_diff = (
        float(home_season_off_rating)
        - float(home_season_def_rating)
        - float(away_season_off_rating)
        + float(away_season_def_rating)
    )
    recent_net_rating_diff = (
        float(home_recent_off_rating)
        - float(home_recent_def_rating)
        - float(away_recent_off_rating)
        + float(away_recent_def_rating)
    )
    home_implied_prob = _moneyline_implied_probability(home_moneyline) or 0.5
    away_implied_prob = _moneyline_implied_probability(away_moneyline) or 0.5
    vig_free_home_prob = _vig_free_home_probability(home_moneyline, away_moneyline) or 0.5
    over_implied_prob = _moneyline_implied_probability(over_price) or 0.5
    under_implied_prob = _moneyline_implied_probability(under_price) or 0.5
    vig_free_over_prob = _vig_free_probability(over_price, under_price) or 0.5
    matchup = dict(matchup_pregame or {})
    home_projected_minutes_total = float(matchup.get("home_projected_minutes_total") or 0.0)
    away_projected_minutes_total = float(matchup.get("away_projected_minutes_total") or 0.0)
    home_projected_points_total = float(matchup.get("home_projected_points_total") or 0.0)
    away_projected_points_total = float(matchup.get("away_projected_points_total") or 0.0)
    home_top3_minutes_share = float(matchup.get("home_top3_minutes_share") or 0.0)
    away_top3_minutes_share = float(matchup.get("away_top3_minutes_share") or 0.0)
    home_top5_points_share = float(matchup.get("home_top5_points_share") or 0.0)
    away_top5_points_share = float(matchup.get("away_top5_points_share") or 0.0)
    home_rotation_count = float(matchup.get("home_rotation_count") or 0.0)
    away_rotation_count = float(matchup.get("away_rotation_count") or 0.0)
    home_creator_count = float(matchup.get("home_creator_count") or 0.0)
    away_creator_count = float(matchup.get("away_creator_count") or 0.0)
    home_off_form_delta = float(matchup.get("home_off_form_delta") or 0.0)
    away_off_form_delta = float(matchup.get("away_off_form_delta") or 0.0)
    home_def_form_delta = float(matchup.get("home_def_form_delta") or 0.0)
    away_def_form_delta = float(matchup.get("away_def_form_delta") or 0.0)
    home_pace_form_delta = float(matchup.get("home_pace_form_delta") or 0.0)
    away_pace_form_delta = float(matchup.get("away_pace_form_delta") or 0.0)
    home_fga_form_delta = float(matchup.get("home_fga_form_delta") or 0.0)
    away_fga_form_delta = float(matchup.get("away_fga_form_delta") or 0.0)
    home_fga_allowed_form_delta = float(matchup.get("home_fga_allowed_form_delta") or 0.0)
    away_fga_allowed_form_delta = float(matchup.get("away_fga_allowed_form_delta") or 0.0)
    home_turnover_rate_form_delta = float(matchup.get("home_turnover_rate_form_delta") or 0.0)
    away_turnover_rate_form_delta = float(matchup.get("away_turnover_rate_form_delta") or 0.0)
    home_forced_turnover_rate_form_delta = float(matchup.get("home_forced_turnover_rate_form_delta") or 0.0)
    away_forced_turnover_rate_form_delta = float(matchup.get("away_forced_turnover_rate_form_delta") or 0.0)
    home_off_form_volatility = float(matchup.get("home_off_form_volatility") or 0.0)
    away_off_form_volatility = float(matchup.get("away_off_form_volatility") or 0.0)
    home_def_form_volatility = float(matchup.get("home_def_form_volatility") or 0.0)
    away_def_form_volatility = float(matchup.get("away_def_form_volatility") or 0.0)
    home_hot_offense_flag = float(matchup.get("home_hot_offense_flag") or 0.0)
    away_hot_offense_flag = float(matchup.get("away_hot_offense_flag") or 0.0)
    home_slump_offense_flag = float(matchup.get("home_slump_offense_flag") or 0.0)
    away_slump_offense_flag = float(matchup.get("away_slump_offense_flag") or 0.0)
    home_hot_defense_flag = float(matchup.get("home_hot_defense_flag") or 0.0)
    away_hot_defense_flag = float(matchup.get("away_hot_defense_flag") or 0.0)
    home_slump_defense_flag = float(matchup.get("home_slump_defense_flag") or 0.0)
    away_slump_defense_flag = float(matchup.get("away_slump_defense_flag") or 0.0)
    return [
        home_recent_points,
        away_recent_points,
        home_recent_allowed,
        away_recent_allowed,
        home_average_points,
        away_average_points,
        home_average_allowed,
        away_average_allowed,
        home_average_possessions,
        away_average_possessions,
        pace_factor,
        float(rest_days_home),
        float(rest_days_away),
        float(rest_days_home) - float(rest_days_away),
        float(home_game_count),
        float(away_game_count),
        recent_scoring_delta,
        recent_allowed_delta,
        season_scoring_delta,
        season_allowed_delta,
        recent_possessions_delta,
        season_possessions_delta,
        float(home_recent_points) - float(home_average_points),
        float(away_recent_points) - float(away_average_points),
        float(home_recent_allowed) - float(home_average_allowed),
        float(away_recent_allowed) - float(away_average_allowed),
        float(home_recent_possessions) - float(home_average_possessions),
        float(away_recent_possessions) - float(away_average_possessions),
        home_season_off_rating,
        away_season_off_rating,
        home_season_def_rating,
        away_season_def_rating,
        home_recent_off_rating,
        away_recent_off_rating,
        home_recent_def_rating,
        away_recent_def_rating,
        season_net_rating_diff,
        recent_net_rating_diff,
        float(spread_home or 0.0),
        float(game_total or 0.0),
        home_implied_prob,
        away_implied_prob,
        vig_free_home_prob,
        float(home_spread_price or 0.0),
        float(away_spread_price or 0.0),
        float(home_spread_price or 0.0) - float(away_spread_price or 0.0),
        float(over_price or 0.0),
        float(under_price or 0.0),
        float(over_price or 0.0) - float(under_price or 0.0),
        over_implied_prob,
        under_implied_prob,
        vig_free_over_prob,
        home_projected_minutes_total,
        away_projected_minutes_total,
        home_projected_points_total,
        away_projected_points_total,
        home_top3_minutes_share,
        away_top3_minutes_share,
        home_top5_points_share,
        away_top5_points_share,
        home_rotation_count,
        away_rotation_count,
        home_creator_count,
        away_creator_count,
        home_projected_minutes_total - away_projected_minutes_total,
        home_projected_points_total - away_projected_points_total,
        home_rotation_count - away_rotation_count,
        home_creator_count - away_creator_count,
        home_off_form_delta,
        away_off_form_delta,
        home_def_form_delta,
        away_def_form_delta,
        home_pace_form_delta,
        away_pace_form_delta,
        home_fga_form_delta,
        away_fga_form_delta,
        home_fga_allowed_form_delta,
        away_fga_allowed_form_delta,
        home_turnover_rate_form_delta,
        away_turnover_rate_form_delta,
        home_forced_turnover_rate_form_delta,
        away_forced_turnover_rate_form_delta,
        home_off_form_volatility,
        away_off_form_volatility,
        home_def_form_volatility,
        away_def_form_volatility,
        home_off_form_delta - away_off_form_delta,
        home_def_form_delta - away_def_form_delta,
        home_pace_form_delta - away_pace_form_delta,
        home_fga_form_delta - away_fga_form_delta,
        home_fga_allowed_form_delta - away_fga_allowed_form_delta,
        home_turnover_rate_form_delta - away_turnover_rate_form_delta,
        home_forced_turnover_rate_form_delta - away_forced_turnover_rate_form_delta,
        home_hot_offense_flag,
        away_hot_offense_flag,
        home_slump_offense_flag,
        away_slump_offense_flag,
        home_hot_defense_flag,
        away_hot_defense_flag,
        home_slump_defense_flag,
        away_slump_defense_flag,
    ]


def _predict_direct_game_value(
    conn: sqlite3.Connection,
    target: str,
    features: list[float],
) -> _DirectPrediction | None:
    model = _train_direct_game_model(conn, target)
    if model is None:
        return None
    value = _predict_direct_model(model, features)
    return _DirectPrediction(rows=model.rows, value=value)


def _predict_market_total_edge(
    conn: sqlite3.Connection,
    direct_features: list[float],
    baseline_edge: float,
    game_total: float,
) -> _MarketTotalDecision | None:
    model = _train_direct_game_model(conn, "total_market")
    if model is None:
        return None
    features = [*direct_features, baseline_edge, game_total]
    predicted_edge = _predict_direct_model(model, features)
    weight = _direct_total_decision_blend_weight(model.rows)
    edge = ((1 - weight) * baseline_edge) + (weight * predicted_edge)
    if abs(edge) < GAME_TOTAL_DECISION_MIN_EDGE:
        edge = baseline_edge
    return _MarketTotalDecision(rows=model.rows, edge=edge, weight=weight)


def _direct_game_blend_weight(rows: int) -> float:
    if rows <= GAME_DIRECT_MODEL_MIN_ROWS:
        return 0.42
    extra = min(rows - GAME_DIRECT_MODEL_MIN_ROWS, 500)
    return min(GAME_DIRECT_BLEND_MAX, 0.42 + (extra / 500.0) * 0.30)


def _direct_total_decision_blend_weight(rows: int) -> float:
    if rows <= GAME_TOTAL_DECISION_MODEL_MIN_ROWS:
        return 0.45
    extra = min(rows - GAME_TOTAL_DECISION_MODEL_MIN_ROWS, 500)
    return min(GAME_TOTAL_DECISION_BLEND_MAX, 0.45 + (extra / 500.0) * 0.23)


def _apply_game_margin_residual(
    conn: sqlite3.Connection,
    projected_margin: float,
    spread_home: float,
    rest_days_home: int,
    rest_days_away: int,
) -> tuple[float, str | None]:
    model = _train_game_edge_model(conn, "margin")
    if model is None:
        return projected_margin, None
    features = [
        projected_margin,
        projected_margin + spread_home,
        spread_home,
        float(rest_days_home),
        float(rest_days_away),
    ]
    predicted_edge = _predict_game_edge(model, features)
    margin_from_model = predicted_edge - spread_home
    weight = _game_edge_blend_weight(model.rows, "margin")
    adjusted = ((1 - weight) * projected_margin) + (weight * margin_from_model)
    return adjusted, f"ATS residual blend {weight:.0%} ({model.rows} settled rows)"


def _apply_game_total_residual(
    conn: sqlite3.Connection,
    projected_total: float,
    game_total: float,
    rest_days_home: int,
    rest_days_away: int,
    *,
    over_price: float | None = None,
    under_price: float | None = None,
    spread_home: float | None = None,
) -> tuple[float, str | None]:
    model = _train_game_edge_model(conn, "total")
    if model is None:
        return projected_total, None
    features = _game_total_residual_features(
        projected_total=projected_total,
        game_total=game_total,
        rest_days_home=rest_days_home,
        rest_days_away=rest_days_away,
        over_price=over_price,
        under_price=under_price,
        spread_home=spread_home,
    )
    predicted_edge = _predict_game_edge(model, features)
    total_from_model = game_total + predicted_edge
    weight = _game_edge_blend_weight(model.rows, "total")
    adjusted = ((1 - weight) * projected_total) + (weight * total_from_model)
    return adjusted, f"total residual blend {weight:.0%} ({model.rows} settled rows)"


def _game_total_residual_features(
    *,
    projected_total: float,
    game_total: float,
    rest_days_home: int,
    rest_days_away: int,
    over_price: float | None,
    under_price: float | None,
    spread_home: float | None,
) -> list[float]:
    projected_edge = float(projected_total) - float(game_total)
    over_implied = _moneyline_implied_probability(over_price) or 0.5
    under_implied = _moneyline_implied_probability(under_price) or 0.5
    vig_free_over = _vig_free_probability(over_price, under_price) or 0.5
    return [
        float(projected_total),
        projected_edge,
        float(game_total),
        float(rest_days_home),
        float(rest_days_away),
        float(over_price or 0.0),
        float(under_price or 0.0),
        float(over_price or 0.0) - float(under_price or 0.0),
        over_implied,
        under_implied,
        vig_free_over,
        abs(float(spread_home or 0.0)),
    ]


def _train_game_edge_model(conn: sqlite3.Connection, edge_type: str) -> _GameEdgeModel | None:
    rows = _game_edge_training_rows(conn, edge_type)
    if len(rows) < GAME_EDGE_MODEL_MIN_ROWS:
        return None
    return _fit_game_edge_model(edge_type, rows)


def evaluate_game_residual_models(conn: sqlite3.Connection) -> dict[str, dict]:
    rows = conn.execute(
        """
        SELECT
            g.id,
            g.game_date,
            g.start_time,
            g.home_team_id,
            g.away_team_id,
            g.rest_days_home,
            g.rest_days_away,
            g.spread_home,
            g.game_total,
            g.home_moneyline,
            g.away_moneyline,
            g.home_spread_price,
            g.away_spread_price,
            g.over_price,
            g.under_price,
            home_result.points AS home_points,
            away_result.points AS away_points,
            home_result.possessions AS home_possessions,
            away_result.possessions AS away_possessions
        FROM games g
        JOIN team_game_results home_result ON home_result.game_id = g.id AND home_result.team_id = g.home_team_id
        JOIN team_game_results away_result ON away_result.game_id = g.id AND away_result.team_id = g.away_team_id
        WHERE g.status = 'final'
          AND home_result.points IS NOT NULL
          AND away_result.points IS NOT NULL
        ORDER BY g.game_date ASC, g.start_time ASC, g.id ASC
        """
    ).fetchall()
    if not rows:
        empty = _empty_game_metric()
        return {
            "game_ats": empty.copy(),
            "game_total": empty.copy(),
            "game_overall": empty.copy(),
        }

    team_history: dict[int, dict[str, Any]] = {}
    runtime_cache: dict[str, dict[tuple, object]] = {}
    margin_training_rows: list[tuple[list[float], float]] = []
    total_training_rows: list[tuple[list[float], float]] = []
    total_market_training_rows: list[tuple[list[float], float]] = []
    pending_margin_rows: list[tuple[list[float], float]] = []
    pending_total_rows: list[tuple[list[float], float]] = []
    pending_total_market_rows: list[tuple[list[float], float]] = []
    ats_records: list[tuple[float, float, float]] = []
    total_records: list[tuple[float, float, float]] = []
    margin_records: list[tuple[float, float, float]] = []
    training_start = _training_start_date()
    current_segment: str | None = None
    direct_margin_model: _DirectGameModel | None = None
    direct_total_model: _DirectGameModel | None = None
    decision_model: _DirectGameModel | None = None

    def _refresh_segment_models() -> None:
        nonlocal direct_margin_model, direct_total_model, decision_model
        direct_margin_model = (
            _fit_direct_game_model("margin", margin_training_rows)
            if len(margin_training_rows) >= GAME_DIRECT_MODEL_MIN_ROWS
            else None
        )
        direct_total_model = (
            _fit_direct_game_model("total", total_training_rows)
            if len(total_training_rows) >= GAME_DIRECT_MODEL_MIN_ROWS
            else None
        )
        decision_model = (
            _fit_direct_game_model("total_market", total_market_training_rows)
            if len(total_market_training_rows) >= GAME_TOTAL_DECISION_MODEL_MIN_ROWS
            else None
        )

    def _advance_segment(next_segment: str) -> None:
        nonlocal current_segment
        if current_segment is not None:
            margin_training_rows.extend(pending_margin_rows)
            total_training_rows.extend(pending_total_rows)
            total_market_training_rows.extend(pending_total_market_rows)
            pending_margin_rows.clear()
            pending_total_rows.clear()
            pending_total_market_rows.clear()
        current_segment = next_segment
        _refresh_segment_models()

    for row in rows:
        use_target_row = not _before_training_start(str(row["game_date"]), training_start)
        home_team_id = int(row["home_team_id"])
        away_team_id = int(row["away_team_id"])
        home_context = _team_history_context(team_history.get(home_team_id))
        away_context = _team_history_context(team_history.get(away_team_id))
        if home_context["games"] >= 3 and away_context["games"] >= 3:
            segment_key = _game_training_segment_key(str(row["game_date"] or ""))
            if current_segment != segment_key:
                _advance_segment(segment_key)
            baseline_home = _baseline_points_from_context(
                team_context=home_context,
                opponent_context=away_context,
                is_home=True,
                rest_days=int(row["rest_days_home"] or 2),
            )
            baseline_away = _baseline_points_from_context(
                team_context=away_context,
                opponent_context=home_context,
                is_home=False,
                rest_days=int(row["rest_days_away"] or 2),
            )
            baseline_margin = baseline_home - baseline_away
            baseline_total = baseline_home + baseline_away
            matchup_pregame = build_matchup_pregame_features(
                conn,
                home_team_id=home_team_id,
                away_team_id=away_team_id,
                game_id=int(row["id"]),
                game_date=str(row["game_date"] or "") or None,
                use_injury_context=False,
                runtime_cache=runtime_cache,
            )
            direct_features = _assemble_direct_game_features(
                home_recent_points=home_context["recent_points"],
                away_recent_points=away_context["recent_points"],
                home_recent_allowed=home_context["recent_allowed"],
                away_recent_allowed=away_context["recent_allowed"],
                home_average_points=home_context["avg_points"],
                away_average_points=away_context["avg_points"],
                home_average_allowed=home_context["avg_allowed"],
                away_average_allowed=away_context["avg_allowed"],
                home_average_possessions=home_context["avg_possessions"],
                away_average_possessions=away_context["avg_possessions"],
                pace_factor=_historical_pace_factor(home_context, away_context, _historical_league_possessions(team_history)),
                rest_days_home=int(row["rest_days_home"] or 2),
                rest_days_away=int(row["rest_days_away"] or 2),
                home_game_count=home_context["games"],
                away_game_count=away_context["games"],
                home_recent_possessions=home_context["recent_possessions"],
                away_recent_possessions=away_context["recent_possessions"],
                home_season_off_rating=_team_rating(float(home_context["avg_points"]), float(home_context["avg_possessions"])),
                away_season_off_rating=_team_rating(float(away_context["avg_points"]), float(away_context["avg_possessions"])),
                home_season_def_rating=_team_rating(float(home_context["avg_allowed"]), float(home_context["avg_possessions"])),
                away_season_def_rating=_team_rating(float(away_context["avg_allowed"]), float(away_context["avg_possessions"])),
                home_recent_off_rating=_team_rating(float(home_context["recent_points"]), float(home_context["recent_possessions"])),
                away_recent_off_rating=_team_rating(float(away_context["recent_points"]), float(away_context["recent_possessions"])),
                home_recent_def_rating=_team_rating(float(home_context["recent_allowed"]), float(home_context["recent_possessions"])),
                away_recent_def_rating=_team_rating(float(away_context["recent_allowed"]), float(away_context["recent_possessions"])),
                spread_home=_coerce_float(row["spread_home"]),
                game_total=_coerce_float(row["game_total"]),
                home_moneyline=_coerce_float(row["home_moneyline"]),
                away_moneyline=_coerce_float(row["away_moneyline"]),
                home_spread_price=_coerce_float(row["home_spread_price"]),
                away_spread_price=_coerce_float(row["away_spread_price"]),
                over_price=_coerce_float(row["over_price"]),
                under_price=_coerce_float(row["under_price"]),
                matchup_pregame=matchup_pregame,
            )
            adjusted_margin = baseline_margin
            adjusted_total = baseline_total
            if direct_margin_model is not None:
                direct_margin = _predict_direct_model(direct_margin_model, direct_features)
                margin_weight = _direct_game_blend_weight(direct_margin_model.rows)
                adjusted_margin = ((1 - margin_weight) * baseline_margin) + (margin_weight * direct_margin)
            if row["spread_home"] is not None:
                adjusted_margin = (
                    (1 - GAME_MARGIN_POST_MARKET_ANCHOR) * adjusted_margin
                ) + (GAME_MARGIN_POST_MARKET_ANCHOR * (-float(row["spread_home"])))
            if direct_total_model is not None:
                direct_total = _predict_direct_model(direct_total_model, direct_features)
                total_weight = _direct_game_blend_weight(direct_total_model.rows)
                adjusted_total = ((1 - total_weight) * baseline_total) + (total_weight * direct_total)
            game_total = row["game_total"]
            if game_total is not None and float(game_total) > 0:
                adjusted_total = ((1 - GAME_TOTAL_POST_MARKET_ANCHOR) * adjusted_total) + (GAME_TOTAL_POST_MARKET_ANCHOR * float(game_total))

            actual_margin = float(row["home_points"]) - float(row["away_points"])
            actual_total = float(row["home_points"]) + float(row["away_points"])
            if use_target_row:
                margin_records.append((baseline_margin, adjusted_margin, actual_margin))
            if row["spread_home"] is not None:
                spread_home = float(row["spread_home"])
                if use_target_row:
                    ats_records.append(
                        (
                            baseline_margin + spread_home,
                            adjusted_margin + spread_home,
                            actual_margin + spread_home,
                        )
                    )
            if game_total is not None and float(game_total) > 0:
                game_total_value = float(game_total)
                baseline_total_edge = baseline_total - game_total_value
                adjusted_total_edge = adjusted_total - game_total_value
                if decision_model is not None:
                    decision_features = [*direct_features, baseline_total_edge, game_total_value]
                    predicted_total_edge = _predict_direct_model(decision_model, decision_features)
                    decision_weight = _direct_total_decision_blend_weight(decision_model.rows)
                    adjusted_total_edge = ((1 - decision_weight) * baseline_total_edge) + (decision_weight * predicted_total_edge)
                    if abs(adjusted_total_edge) < GAME_TOTAL_DECISION_MIN_EDGE:
                        adjusted_total_edge = baseline_total_edge
                if use_target_row:
                    total_records.append(
                        (
                            baseline_total_edge,
                            adjusted_total_edge,
                            actual_total - game_total_value,
                        )
                    )
            if use_target_row:
                pending_margin_rows.append((direct_features, actual_margin))
                pending_total_rows.append((direct_features, actual_total))
                if game_total is not None and float(game_total) > 0:
                    pending_total_market_rows.append(([*direct_features, baseline_total - float(game_total), float(game_total)], actual_total - float(game_total)))

        _append_team_history(
            team_history,
            team_id=home_team_id,
            game_date=str(row["game_date"]),
            points=float(row["home_points"]),
            opponent_points=float(row["away_points"]),
            possessions=float(row["home_possessions"] or row["away_possessions"] or 78.0),
        )
        _append_team_history(
            team_history,
            team_id=away_team_id,
            game_date=str(row["game_date"]),
            points=float(row["away_points"]),
            opponent_points=float(row["home_points"]),
            possessions=float(row["away_possessions"] or row["home_possessions"] or 78.0),
        )

    ats_metrics = _game_metric_from_records(ats_records)
    total_metrics = _game_metric_from_records(total_records)
    margin_metrics = _game_metric_from_records(margin_records)
    overall_metrics = _combine_game_metric_rows([ats_metrics, total_metrics, margin_metrics])
    return {
        "game_ats": ats_metrics,
        "game_total": total_metrics,
        "game_overall": overall_metrics,
    }


def _game_edge_training_rows(conn: sqlite3.Connection, edge_type: str) -> list[tuple[list[float], float]]:
    training_start = _training_start_date()
    if edge_type == "margin":
        rows = conn.execute(
            """
            SELECT
                g.game_date,
                gp.projected_margin,
                gp.spread_home,
                gp.home_rest_days,
                gp.away_rest_days,
                sgp.actual_margin
            FROM settled_game_predictions sgp
            JOIN game_predictions gp ON gp.id = sgp.game_prediction_id
            JOIN games g ON g.id = gp.game_id
            WHERE gp.projected_margin IS NOT NULL
              AND gp.spread_home IS NOT NULL
              AND sgp.actual_margin IS NOT NULL
            """
        ).fetchall()
        samples = []
        for row in rows:
            if _before_training_start(str(row["game_date"]), training_start):
                continue
            projected_margin = float(row["projected_margin"])
            spread_home = float(row["spread_home"])
            features = [
                projected_margin,
                projected_margin + spread_home,
                spread_home,
                float(row["home_rest_days"] or 2),
                float(row["away_rest_days"] or 2),
            ]
            samples.append((features, float(row["actual_margin"]) + spread_home))
        return samples

    rows = conn.execute(
        """
        SELECT
            g.game_date,
            gp.projected_total,
            gp.game_total,
            gp.home_rest_days,
            gp.away_rest_days,
            g.spread_home,
            g.over_price,
            g.under_price,
            sgp.actual_total
        FROM settled_game_predictions sgp
        JOIN game_predictions gp ON gp.id = sgp.game_prediction_id
        JOIN games g ON g.id = gp.game_id
        WHERE gp.projected_total IS NOT NULL
          AND gp.game_total IS NOT NULL
          AND gp.game_total > 0
          AND sgp.actual_total IS NOT NULL
        """
    ).fetchall()
    samples = []
    for row in rows:
        if _before_training_start(str(row["game_date"]), training_start):
            continue
        projected_total = float(row["projected_total"])
        game_total = float(row["game_total"])
        features = _game_total_residual_features(
            projected_total=projected_total,
            game_total=game_total,
            rest_days_home=int(row["home_rest_days"] or 2),
            rest_days_away=int(row["away_rest_days"] or 2),
            over_price=_coerce_float(row["over_price"]),
            under_price=_coerce_float(row["under_price"]),
            spread_home=_coerce_float(row["spread_home"]),
        )
        samples.append((features, float(row["actual_total"]) - game_total))
    return samples


def _game_training_segment_key(game_date: str) -> str:
    normalized = str(game_date or "").strip()
    if len(normalized) >= 7:
        return normalized[:7]
    return normalized


def _train_direct_game_model(conn: sqlite3.Connection, target: str) -> _DirectGameModel | None:
    signature = _direct_model_signature(conn)
    cache_key = (signature[0], target, signature[1], signature[2], signature[3])
    cached = _DIRECT_MODEL_CACHE.get(cache_key)
    if cached is not None:
        return cached
    rows, _info = load_game_training_rows(conn, target=target, force_rebuild=False)
    min_rows = GAME_TOTAL_DECISION_MODEL_MIN_ROWS if target in {"total_market", "ats_market"} else GAME_DIRECT_MODEL_MIN_ROWS
    if len(rows) < min_rows:
        return None
    model = _fit_direct_game_model(target, rows)
    _DIRECT_MODEL_CACHE.clear()
    _DIRECT_MODEL_CACHE[cache_key] = model
    return model


def _direct_model_signature(conn: sqlite3.Connection) -> tuple[str, int, int, str]:
    db_row = conn.execute("PRAGMA database_list").fetchone()
    db_path = str(db_row["file"] if isinstance(db_row, sqlite3.Row) else db_row[2])
    signature = game_training_db_signature(conn)
    return db_path, len(signature), int(signature[:8], 16), signature


def _fit_direct_game_model(
    target: str,
    rows: list[tuple[list[float], float]],
) -> _DirectGameModel:
    xs = [row[0] for row in rows]
    ys = [row[1] for row in rows]
    means, scales, standardized = _standardize_game_edge_rows(xs)
    coefficients = _ridge_regression(standardized, ys, penalty=2.5)
    return _DirectGameModel(
        target=target,
        rows=len(rows),
        intercept=coefficients[0],
        coefficients=coefficients[1:],
        feature_means=means,
        feature_scales=scales,
    )


def _predict_direct_model(model: _DirectGameModel, features: list[float]) -> float:
    if np is not None:
        feature_array = np.asarray(features, dtype=float)
        means = np.asarray(model.feature_means, dtype=float)
        scales = np.asarray(model.feature_scales, dtype=float)
        standardized = (feature_array - means) / scales
        prediction = float(model.intercept + np.dot(np.asarray(model.coefficients, dtype=float), standardized))
    else:
        standardized = [
            (value - model.feature_means[idx]) / model.feature_scales[idx]
            for idx, value in enumerate(features)
        ]
        prediction = model.intercept + sum(coef * value for coef, value in zip(model.coefficients, standardized))
    if model.target == "margin":
        return _clamp(prediction, -GAME_DIRECT_MODEL_MAX_POINTS, GAME_DIRECT_MODEL_MAX_POINTS)
    if model.target in {"total_market", "ats_market"}:
        return _clamp(prediction, -30.0, 30.0)
    return _clamp(prediction, 120.0, 220.0)


def _team_rating(points_or_allowed: float, possessions: float) -> float:
    return (100.0 * float(points_or_allowed)) / max(float(possessions), 1.0)


def _baseline_points_from_context(
    *,
    team_context: Mapping[str, float],
    opponent_context: Mapping[str, float],
    is_home: bool,
    rest_days: int,
) -> float:
    home_factor = 1.025 if is_home else 0.985
    offense = (
        (0.52 * float(team_context["recent_points"]))
        + (0.23 * float(team_context["avg_points"]))
        + (0.25 * float(opponent_context["avg_allowed"]))
    )
    pace_factor = _historical_pace_factor(team_context, opponent_context, 78.0)
    return offense * pace_factor * home_factor * _rest_factor(rest_days)


def _historical_pace_factor(
    team_context: Mapping[str, float],
    opponent_context: Mapping[str, float],
    league_possessions: float,
) -> float:
    return _clamp(
        ((float(team_context["avg_possessions"]) + float(opponent_context["avg_possessions"])) / 2.0)
        / float(league_possessions),
        0.94,
        1.06,
    )


def _team_history_context(state: Mapping[str, Any] | None) -> dict[str, float]:
    if not state:
        return {
            "games": 0.0,
            "avg_points": 80.0,
            "avg_allowed": 80.0,
            "avg_possessions": 78.0,
            "recent_points": 80.0,
            "recent_allowed": 80.0,
            "recent_possessions": 78.0,
        }
    recent_points = state["recent_points"]
    recent_allowed = state["recent_allowed"]
    recent_possessions = state["recent_possessions"]
    return {
        "games": float(state["games"]),
        "avg_points": float(state["points_sum"]) / max(float(state["games"]), 1.0),
        "avg_allowed": float(state["allowed_sum"]) / max(float(state["games"]), 1.0),
        "avg_possessions": float(state["possessions_sum"]) / max(float(state["games"]), 1.0),
        "recent_points": sum(recent_points) / len(recent_points) if recent_points else float(state["points_sum"]) / max(float(state["games"]), 1.0),
        "recent_allowed": sum(recent_allowed) / len(recent_allowed) if recent_allowed else float(state["allowed_sum"]) / max(float(state["games"]), 1.0),
        "recent_possessions": (
            sum(recent_possessions) / len(recent_possessions)
            if recent_possessions
            else float(state["possessions_sum"]) / max(float(state["games"]), 1.0)
        ),
    }


def _historical_league_possessions(history: Mapping[int, Mapping[str, Any]]) -> float:
    total_games = 0.0
    total_possessions = 0.0
    for item in history.values():
        total_games += float(item["games"])
        total_possessions += float(item["possessions_sum"])
    if total_games <= 0:
        return 78.0
    return total_possessions / total_games


def _append_team_history(
    history: dict[int, dict[str, Any]],
    *,
    team_id: int,
    game_date: str,
    points: float,
    opponent_points: float,
    possessions: float,
) -> None:
    state = history.setdefault(
        team_id,
        {
            "games": 0,
            "points_sum": 0.0,
            "allowed_sum": 0.0,
            "possessions_sum": 0.0,
            "recent_points": [],
            "recent_allowed": [],
            "recent_possessions": [],
            "last_game_date": None,
        },
    )
    state["games"] += 1
    state["points_sum"] += points
    state["allowed_sum"] += opponent_points
    state["possessions_sum"] += possessions
    state["recent_points"].append(points)
    state["recent_allowed"].append(opponent_points)
    state["recent_possessions"].append(possessions)
    state["recent_points"] = state["recent_points"][-5:]
    state["recent_allowed"] = state["recent_allowed"][-5:]
    state["recent_possessions"] = state["recent_possessions"][-5:]
    state["last_game_date"] = game_date


def _fit_game_edge_model(
    edge_type: str,
    rows: list[tuple[list[float], float]],
) -> _GameEdgeModel:
    xs = [row[0] for row in rows]
    ys = [row[1] for row in rows]
    means, scales, standardized = _standardize_game_edge_rows(xs)
    coefficients = _ridge_regression(standardized, ys, penalty=1.0)
    return _GameEdgeModel(
        edge_type=edge_type,
        rows=len(rows),
        intercept=coefficients[0],
        coefficients=coefficients[1:],
        feature_means=means,
        feature_scales=scales,
    )


def _standardize_game_edge_rows(xs: list[list[float]]) -> tuple[list[float], list[float], list[list[float]]]:
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


def _predict_game_edge(model: _GameEdgeModel, features: list[float]) -> float:
    if np is not None:
        feature_array = np.asarray(features, dtype=float)
        means = np.asarray(model.feature_means, dtype=float)
        scales = np.asarray(model.feature_scales, dtype=float)
        standardized = (feature_array - means) / scales
        prediction = float(model.intercept + np.dot(np.asarray(model.coefficients, dtype=float), standardized))
        return _clamp(prediction, -GAME_EDGE_MODEL_MAX_ADJUSTMENT, GAME_EDGE_MODEL_MAX_ADJUSTMENT)

    standardized = [
        (value - model.feature_means[idx]) / model.feature_scales[idx]
        for idx, value in enumerate(features)
    ]
    prediction = model.intercept + sum(coef * value for coef, value in zip(model.coefficients, standardized))
    return _clamp(prediction, -GAME_EDGE_MODEL_MAX_ADJUSTMENT, GAME_EDGE_MODEL_MAX_ADJUSTMENT)


def _game_edge_blend_weight(rows: int, edge_type: str) -> float:
    if edge_type == "margin":
        if rows >= 40:
            return GAME_MARGIN_BLEND_WEIGHT
        return 0.10
    if rows >= 40:
        return GAME_TOTAL_BLEND_WEIGHT
    return 0.12


def _empty_game_metric() -> dict[str, float | int | None]:
    return {
        "rows": 0,
        "mae": None,
        "rmse": None,
        "bias": None,
        "directional_accuracy": None,
        "baseline_mae": None,
        "baseline_rmse": None,
        "baseline_bias": None,
        "baseline_directional_accuracy": None,
        "mae_improvement": None,
        "rmse_improvement": None,
        "directional_accuracy_improvement": None,
    }


def _game_metric_from_records(records: list[tuple[float, float, float]]) -> dict[str, float | int | None]:
    if not records:
        return _empty_game_metric()

    baseline_errors = [baseline - actual for baseline, _, actual in records]
    blended_errors = [blended - actual for _, blended, actual in records]
    baseline_direction_hits = 0
    blended_direction_hits = 0
    direction_rows = 0
    for baseline, blended, actual in records:
        actual_side = _edge_direction(actual)
        if actual_side == 0:
            continue
        direction_rows += 1
        if _edge_direction(baseline) == actual_side:
            baseline_direction_hits += 1
        if _edge_direction(blended) == actual_side:
            blended_direction_hits += 1

    row_count = len(records)
    baseline_mae = sum(abs(error) for error in baseline_errors) / row_count
    blended_mae = sum(abs(error) for error in blended_errors) / row_count
    baseline_rmse = math.sqrt(sum(error * error for error in baseline_errors) / row_count)
    blended_rmse = math.sqrt(sum(error * error for error in blended_errors) / row_count)
    baseline_bias = sum(baseline_errors) / row_count
    blended_bias = sum(blended_errors) / row_count
    baseline_direction = baseline_direction_hits / direction_rows if direction_rows else None
    blended_direction = blended_direction_hits / direction_rows if direction_rows else None
    return {
        "rows": row_count,
        "mae": round(blended_mae, 3),
        "rmse": round(blended_rmse, 3),
        "bias": round(blended_bias, 3),
        "directional_accuracy": round(blended_direction, 3) if blended_direction is not None else None,
        "baseline_mae": round(baseline_mae, 3),
        "baseline_rmse": round(baseline_rmse, 3),
        "baseline_bias": round(baseline_bias, 3),
        "baseline_directional_accuracy": round(baseline_direction, 3) if baseline_direction is not None else None,
        "mae_improvement": round(baseline_mae - blended_mae, 3),
        "rmse_improvement": round(baseline_rmse - blended_rmse, 3),
        "directional_accuracy_improvement": (
            round(blended_direction - baseline_direction, 3)
            if blended_direction is not None and baseline_direction is not None
            else None
        ),
    }


def _combine_game_metric_rows(metrics: list[dict[str, float | int | None]]) -> dict[str, float | int | None]:
    populated = [metric for metric in metrics if int(metric.get("rows") or 0) > 0]
    if not populated:
        return _empty_game_metric()
    total_rows = sum(int(metric["rows"]) for metric in populated)
    combined = {
        "rows": total_rows,
        "mae": 0.0,
        "rmse": 0.0,
        "bias": 0.0,
        "directional_accuracy": 0.0,
        "baseline_mae": 0.0,
        "baseline_rmse": 0.0,
        "baseline_bias": 0.0,
        "baseline_directional_accuracy": 0.0,
        "mae_improvement": 0.0,
        "rmse_improvement": 0.0,
        "directional_accuracy_improvement": 0.0,
    }
    direction_rows = 0
    baseline_direction_rows = 0
    for metric in populated:
        rows = int(metric["rows"])
        for key in ("mae", "rmse", "bias", "baseline_mae", "baseline_rmse", "baseline_bias", "mae_improvement", "rmse_improvement"):
            if metric.get(key) is not None:
                combined[key] += float(metric[key]) * rows
        if metric.get("directional_accuracy") is not None:
            combined["directional_accuracy"] += float(metric["directional_accuracy"]) * rows
            direction_rows += rows
        if metric.get("baseline_directional_accuracy") is not None:
            combined["baseline_directional_accuracy"] += float(metric["baseline_directional_accuracy"]) * rows
            baseline_direction_rows += rows
        if metric.get("directional_accuracy_improvement") is not None:
            combined["directional_accuracy_improvement"] += float(metric["directional_accuracy_improvement"]) * rows
    return {
        "rows": total_rows,
        "mae": round(combined["mae"] / total_rows, 3),
        "rmse": round(combined["rmse"] / total_rows, 3),
        "bias": round(combined["bias"] / total_rows, 3),
        "directional_accuracy": round(combined["directional_accuracy"] / direction_rows, 3) if direction_rows else None,
        "baseline_mae": round(combined["baseline_mae"] / total_rows, 3),
        "baseline_rmse": round(combined["baseline_rmse"] / total_rows, 3),
        "baseline_bias": round(combined["baseline_bias"] / total_rows, 3),
        "baseline_directional_accuracy": round(combined["baseline_directional_accuracy"] / baseline_direction_rows, 3) if baseline_direction_rows else None,
        "mae_improvement": round(combined["mae_improvement"] / total_rows, 3),
        "rmse_improvement": round(combined["rmse_improvement"] / total_rows, 3),
        "directional_accuracy_improvement": round(combined["directional_accuracy_improvement"] / total_rows, 3) if total_rows else None,
    }


def _edge_direction(value: float) -> int:
    if value > 0:
        return 1
    if value < 0:
        return -1
    return 0


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


def _ridge_regression(xs: list[list[float]], ys: list[float], penalty: float) -> list[float]:
    feature_count = len(xs[0]) + 1
    matrix = [[0.0 for _ in range(feature_count)] for _ in range(feature_count)]
    vector = [0.0 for _ in range(feature_count)]
    for features, target in zip(xs, ys):
        row = [1.0, *features]
        for i in range(feature_count):
            vector[i] += row[i] * target
            for j in range(feature_count):
                matrix[i][j] += row[i] * row[j]
    for i in range(1, feature_count):
        matrix[i][i] += penalty
    return _solve_linear_system(matrix, vector)


def _solve_linear_system(matrix: list[list[float]], vector: list[float]) -> list[float]:
    size = len(vector)
    augmented = [row[:] + [vector[idx]] for idx, row in enumerate(matrix)]
    for col in range(size):
        pivot = max(range(col, size), key=lambda row: abs(augmented[row][col]))
        if abs(augmented[pivot][col]) < 1e-9:
            continue
        augmented[col], augmented[pivot] = augmented[pivot], augmented[col]
        divisor = augmented[col][col]
        augmented[col] = [value / divisor for value in augmented[col]]
        for row in range(size):
            if row == col:
                continue
            factor = augmented[row][col]
            augmented[row] = [value - (factor * augmented[col][idx]) for idx, value in enumerate(augmented[row])]
    return [augmented[row][-1] for row in range(size)]
