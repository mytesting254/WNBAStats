from __future__ import annotations

import sqlite3
import math
from dataclasses import dataclass
from functools import lru_cache

from .odds import american_to_implied_probability


MODEL_VERSION = "adaptive-context-v1"
TRAINING_MARKETS = [
    "points",
    "rebounds",
    "assists",
    "points_rebounds",
    "points_assists",
    "rebounds_assists",
    "points_rebounds_assists",
    "threes",
    "steals",
    "blocks",
    "blocks_steals",
]
FEATURE_NAMES = [
    "component_projection",
    "weighted_recent",
    "ewma_value",
    "last_5_avg",
    "last_10_avg",
    "rate_projection",
    "ewma_minutes",
    "minutes_trend",
    "value_volatility",
    "consistency_score",
    "rest_days",
    "is_home",
    "pace_factor",
    "opponent_factor",
    "common_opponent_factor",
    "h2h_factor",
    "blowout_minutes_delta",
    "spread_abs",
    "game_total",
]
FEATURE_INDEX = {name: idx for idx, name in enumerate(FEATURE_NAMES)}

MARKET_VOLATILITY_FLOORS = {
    "points": 3.0,
    "rebounds": 2.2,
    "assists": 1.8,
    "points_rebounds": 4.2,
    "points_assists": 3.8,
    "rebounds_assists": 3.2,
    "threes": 1.1,
    "steals": 0.8,
    "blocks": 0.7,
    "blocks_steals": 1.1,
    "points_rebounds_assists": 5.0,
}

_CONNECTION_MODEL_CACHE: dict[tuple[int, str], RidgeModel | None] = {}
_CONNECTION_GAME_TOTAL_MEAN_CACHE: dict[int, float] = {}


@dataclass(frozen=True)
class FeatureSnapshot:
    values: list[float]
    component_projection: float
    reason: str
    injury_status: str = "available"
    hard_cap_zero: bool = False


@dataclass(frozen=True)
class RidgeModel:
    market: str
    rows: int
    intercept: float
    coefficients: list[float]
    feature_means: list[float]
    feature_scales: list[float]


def predict_player_prop(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    game_id: int,
    line: float | None = None,
    over_odds: int | None = None,
    under_odds: int | None = None,
) -> tuple[float, str, str]:
    snapshot = feature_snapshot(conn, player_id, market, game_id)
    sample_count, avg_minutes = _player_sample_quality(conn, player_id, game_id)
    model = train_market_model(conn, market)
    if not model:
        return snapshot.component_projection, snapshot.reason, "component"

    learned = _predict(model, snapshot.values)
    learned = max(0.0, learned)
    learned = _stabilize_combo_market_projection(learned, snapshot, market)
    learned, stabilization_note = _stabilize_learned_projection(learned, snapshot, market)
    projection = learned
    market_note = "no sportsbook line blend"

    if line is not None:
        market_weight = _market_weight(model.rows)
        market_weight = max(market_weight, _player_market_weight(sample_count, avg_minutes))
        projection = ((1 - market_weight) * learned) + (market_weight * float(line))
        if over_odds is not None and under_odds is not None:
            over_implied = american_to_implied_probability(int(over_odds))
            under_implied = american_to_implied_probability(int(under_odds))
            no_vig_mid = (over_implied / max(over_implied + under_implied, 0.01)) - 0.5
            projection += no_vig_mid * _market_price_nudge(market)
        market_note = (
            f"sportsbook line blend {market_weight:.0%} at {float(line):.1f} "
            f"(player sample {sample_count} games, {avg_minutes:.1f} avg minutes)"
        )

    if snapshot.hard_cap_zero:
        projection = 0.0
        market_note = f"{market_note}; player marked OUT"

    reason = (
        f"{snapshot.reason} Learned model {MODEL_VERSION} projected {learned:.1f} from "
        f"{model.rows} historical rows; {market_note}; {stabilization_note}. Final projection {projection:.1f}."
    )
    return round(projection, 2), reason, MODEL_VERSION


def _stabilize_combo_market_projection(learned: float, snapshot: FeatureSnapshot, market: str) -> float:
    """Limit extreme downside drift for combo markets when recent form is materially stronger."""
    if market not in {"points_assists", "points_rebounds", "rebounds_assists", "points_rebounds_assists"}:
        return learned
    if len(snapshot.values) < len(FEATURE_NAMES):
        return learned

    weighted_recent = snapshot.values[FEATURE_INDEX["weighted_recent"]]
    last_10_avg = snapshot.values[FEATURE_INDEX["last_10_avg"]]
    rate_projection = snapshot.values[FEATURE_INDEX["rate_projection"]]
    ewma_minutes = snapshot.values[FEATURE_INDEX["ewma_minutes"]]
    minutes_trend = snapshot.values[FEATURE_INDEX["minutes_trend"]]
    projected_minutes = max(ewma_minutes + (0.35 * minutes_trend), 0.0)
    minute_ratio = projected_minutes / max(ewma_minutes, 1.0)

    anchor = max(weighted_recent, last_10_avg, rate_projection)
    severe_downside_drift = learned < (anchor * 0.70)

    # If minutes are projected down or learned output drifts too far under recent anchors,
    # apply a partial floor for combo markets to avoid implausible collapses.
    if minute_ratio < 0.98 or severe_downside_drift:
        floor = anchor * 0.78
        return max(learned, floor)
    return learned


def _stabilize_learned_projection(learned: float, snapshot: FeatureSnapshot, market: str) -> tuple[float, str]:
    """Global guardrail against implausible learned-vs-anchor drift."""
    if len(snapshot.values) < len(FEATURE_NAMES):
        return learned, "no stabilization"
    weighted_recent = snapshot.values[FEATURE_INDEX["weighted_recent"]]
    last_10_avg = snapshot.values[FEATURE_INDEX["last_10_avg"]]
    rate_projection = snapshot.values[FEATURE_INDEX["rate_projection"]]
    component_projection = snapshot.values[FEATURE_INDEX["component_projection"]]
    ewma_minutes = snapshot.values[FEATURE_INDEX["ewma_minutes"]]
    minutes_trend = snapshot.values[FEATURE_INDEX["minutes_trend"]]

    anchor = max(0.01, (0.45 * weighted_recent) + (0.30 * last_10_avg) + (0.25 * rate_projection))
    minute_ratio = max(ewma_minutes + (0.35 * minutes_trend), 0.0) / max(ewma_minutes, 1.0)
    ratio = learned / anchor

    low_guard = 0.82
    high_guard = 1.25
    if minute_ratio < 0.92:
        low_guard = 0.72
    if minute_ratio > 1.08:
        high_guard = 1.35

    adjusted = learned
    if ratio < low_guard:
        floor = anchor * low_guard
        adjusted = (0.65 * floor) + (0.35 * component_projection)
        adjusted = max(adjusted, floor)
        return max(0.0, adjusted), f"stabilized up ({ratio:.2f}x anchor)"
    if ratio > high_guard:
        cap = anchor * high_guard
        adjusted = (0.70 * cap) + (0.30 * component_projection)
        adjusted = min(adjusted, cap)
        return max(0.0, adjusted), f"stabilized down ({ratio:.2f}x anchor)"
    return learned, "no stabilization"


def feature_snapshot(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    game_id: int,
    before_game_date: str | None = None,
) -> FeatureSnapshot:
    context = _game_context(conn, player_id, game_id)
    reference_game_date = before_game_date or context.get("game_date")
    history = _player_history(conn, player_id, market, reference_game_date, exclude_game_id=game_id)
    if not history:
        return FeatureSnapshot([0.0 for _ in FEATURE_NAMES], 0.0, "No historical stats found; projection defaults to 0.")

    values = [row["value"] for row in history]
    minutes = [row["minutes"] for row in history]
    rates = [value / max(minute, 1.0) for value, minute in zip(values, minutes)]
    last_5 = values[:5]
    last_10 = values
    recent_avg = sum(last_5) / len(last_5)
    last_10_avg = sum(last_10) / len(last_10)
    weighted_recent = _weighted_average(values)
    weighted_rate = _weighted_average(rates)
    ewma_value = _ewma_newest_first(values, alpha=0.42)
    ewma_minutes = _ewma_newest_first(minutes, alpha=0.38)
    minutes_trend = _recent_trend(minutes)
    value_volatility = _ewma_volatility_newest_first(values, ewma_value, market)
    consistency_score = _consistency_score(ewma_value, value_volatility, market)

    blowout = _blowout_adjustment(conn, context, history[0]["rotation_role"] if history else "starter")
    injury = _injury_adjustment_for_prop(conn, player_id, context["team_id"], history[0]["rotation_role"] if history else "starter")
    projected_minutes, minutes_note = _project_minutes(
        conn,
        player_id=player_id,
        game_id=game_id,
        rotation_role=str(history[0]["rotation_role"] if history else "starter"),
        ewma_minutes=ewma_minutes,
        minutes_trend=minutes_trend,
        recent_minutes_avg=sum(minutes[:5]) / min(len(minutes), 5),
        last_10_minutes_avg=sum(minutes) / len(minutes),
        context=context,
        blowout_delta=float(blowout["minutes_delta"]),
        injury_delta=float(injury["minutes_delta"]),
        before_game_date=before_game_date,
    )
    rate_projection = weighted_rate * projected_minutes
    component_base = _adaptive_component_projection(
        weighted_recent=weighted_recent,
        ewma_value=ewma_value,
        recent_avg=recent_avg,
        last_10_avg=last_10_avg,
        rate_projection=rate_projection,
        consistency_score=consistency_score,
    )

    pace_factor = _pace_factor(conn, context["team_id"], context["opponent_id"]) if context else 1.0
    opponent_factor = _opponent_factor(conn, context["opponent_id"], market) if context else 1.0
    common_opponent_factor = _common_opponent_factor(conn, player_id, market, context, before_game_date) if context else 1.0
    h2h_factor = _h2h_factor(conn, player_id, market, context, before_game_date) if context else 1.0
    home_factor = 1.02 if context and context["is_home"] else 0.99
    rest_factor = _rest_factor(context["rest_days"]) if context else 1.0
    usage_multiplier, adjustment_note = _manual_adjustment(conn, player_id)
    usage_multiplier *= injury["usage_multiplier"]

    component_projection = (
        component_base
        * pace_factor
        * opponent_factor
        * common_opponent_factor
        * h2h_factor
        * home_factor
        * rest_factor
        * usage_multiplier
        * injury["availability_factor"]
    )
    spread_abs = abs(context["team_spread"]) if context and context["team_spread"] is not None else 0.0
    game_total = _game_total_or_neutral(conn, context["game_total"] if context else None)
    features = [
        component_projection,
        weighted_recent,
        ewma_value,
        recent_avg,
        last_10_avg,
        rate_projection,
        ewma_minutes,
        minutes_trend,
        value_volatility,
        consistency_score,
        float(context["rest_days"] if context else 2),
        1.0 if context and context["is_home"] else 0.0,
        pace_factor,
        opponent_factor,
        common_opponent_factor,
        h2h_factor,
        blowout["minutes_delta"],
        spread_abs,
        game_total,
    ]
    reason = (
        f"Weighted recent {weighted_recent:.1f}, EWMA {ewma_value:.1f}, last 5 {recent_avg:.1f}, "
        f"last 10 {last_10_avg:.1f}, rate x minutes {rate_projection:.1f} on {projected_minutes:.1f} projected minutes. "
        f"Minutes trend {minutes_trend:+.1f}, volatility {value_volatility:.1f}, consistency {consistency_score:.2f}. "
        f"Context: pace {pace_factor:.2f}, opponent {opponent_factor:.2f}, "
        f"common opponents {common_opponent_factor:.2f}, h2h {h2h_factor:.2f}, blowout {blowout['risk']} "
        f"({blowout['minutes_delta']:+.1f} min), "
        f"{'home' if context and context['is_home'] else 'away'} {home_factor:.2f}, "
        f"rest {rest_factor:.2f}, usage {usage_multiplier:.2f}; "
        f"injury {injury['status']} (avail {injury['availability_factor']:.2f}, "
        f"team usage {injury['usage_multiplier']:.2f}, min {injury['minutes_delta']:+.1f}); {adjustment_note}."
        f" Minutes projection: {minutes_note}."
    )
    return FeatureSnapshot(
        features,
        component_projection,
        reason,
        injury_status=str(injury["status"]),
        hard_cap_zero=bool(injury["hard_cap_zero"]),
    )


@lru_cache(maxsize=32)
def _train_market_model_cached(db_path: str, market: str) -> RidgeModel | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _train_market_model_uncached(conn, market)
    finally:
        conn.close()


def train_market_model(conn: sqlite3.Connection, market: str) -> RidgeModel | None:
    if not isinstance(conn, sqlite3.Connection):
        key = (id(conn), market)
        if key not in _CONNECTION_MODEL_CACHE:
            _CONNECTION_MODEL_CACHE[key] = _train_market_model_uncached(conn, market)
        return _CONNECTION_MODEL_CACHE[key]
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    return _train_market_model_cached(db_path, market)


def clear_model_cache() -> None:
    _train_market_model_cached.cache_clear()
    _CONNECTION_MODEL_CACHE.clear()
    _CONNECTION_GAME_TOTAL_MEAN_CACHE.clear()


def _train_market_model_uncached(conn: sqlite3.Connection, market: str) -> RidgeModel | None:
    rows = _training_rows(conn, market)
    return _fit_model_from_rows(market, rows)


def evaluate_market_model(conn: sqlite3.Connection, market: str) -> dict:
    rows = _training_rows(conn, market)
    if len(rows) < 20:
        return {"rows": 0, "mae": None, "rmse": None, "bias": None, "directional_accuracy": None}
    split = max(int(len(rows) * 0.8), 10)
    train_rows = rows[:split]
    test_rows = rows[split:]
    model = _fit_model_from_rows(market, train_rows)
    if not model or not test_rows:
        return {"rows": 0, "mae": None, "rmse": None, "bias": None, "directional_accuracy": None}

    errors = []
    absolute_errors = []
    squared_errors = []
    direction_hits = 0
    for features, actual in test_rows:
        prediction = max(0.0, _predict(model, features))
        error = prediction - actual
        errors.append(error)
        absolute_errors.append(abs(error))
        squared_errors.append(error * error)
        baseline = features[FEATURE_NAMES.index("last_10_avg")]
        if (prediction >= baseline and actual >= baseline) or (prediction < baseline and actual < baseline):
            direction_hits += 1
    row_count = len(test_rows)
    return {
        "rows": row_count,
        "mae": round(sum(absolute_errors) / row_count, 3),
        "rmse": round(math.sqrt(sum(squared_errors) / row_count), 3),
        "bias": round(sum(errors) / row_count, 3),
        "directional_accuracy": round(direction_hits / row_count, 3),
    }


def _fit_model_from_rows(market: str, rows: list[tuple[list[float], float]]) -> RidgeModel | None:
    if len(rows) < 20:
        return None
    xs = [row[0] for row in rows]
    ys = [row[1] for row in rows]
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

    coefs = _ridge_regression(standardized, ys, penalty=1.25)
    return RidgeModel(
        market=market,
        rows=len(rows),
        intercept=coefs[0],
        coefficients=coefs[1:],
        feature_means=means,
        feature_scales=scales,
    )


def _training_rows(conn: sqlite3.Connection, market: str) -> list[tuple[list[float], float]]:
    samples = []
    players = conn.execute("SELECT id FROM players ORDER BY id").fetchall()
    for player in players:
        rows = conn.execute(
            """
            SELECT
                s.*,
                g.game_date,
                g.home_team_id,
                g.away_team_id,
                g.rest_days_home,
                g.rest_days_away,
                g.spread_home,
                g.game_total,
                p.team_id
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            JOIN players p ON p.id = s.player_id
            WHERE s.player_id = ?
            ORDER BY g.game_date ASC, s.game_id ASC
            """,
            (player["id"],),
        ).fetchall()
        values = [_market_value(row, market) for row in rows]
        minutes = [float(row["minutes"]) for row in rows]
        for idx in range(5, len(rows)):
            history = values[max(0, idx - 10):idx]
            minute_history = minutes[max(0, idx - 10):idx]
            features = _historical_training_features(rows[idx], history, minute_history, market)
            samples.append((features, values[idx]))
    return samples


def _historical_training_features(row: sqlite3.Row, history: list[float], minutes: list[float], market: str) -> list[float]:
    rates = [value / max(minute, 1.0) for value, minute in zip(history, minutes)]
    recent_avg = sum(history[-5:]) / min(len(history), 5)
    last_10_avg = sum(history) / len(history)
    newest_values = list(reversed(history))
    newest_minutes = list(reversed(minutes))
    weighted_recent = _weighted_average(newest_values)
    weighted_rate = _weighted_average(list(reversed(rates)))
    ewma_value = _ewma_newest_first(newest_values, alpha=0.42)
    ewma_minutes = _ewma_newest_first(newest_minutes, alpha=0.38)
    minutes_trend = _recent_trend(newest_minutes)
    value_volatility = _ewma_volatility_newest_first(newest_values, ewma_value, market)
    consistency_score = _consistency_score(ewma_value, value_volatility, market)
    projected_minutes = max(ewma_minutes + (0.35 * minutes_trend), 4.0)
    rate_projection = weighted_rate * projected_minutes
    component_projection = _adaptive_component_projection(
        weighted_recent=weighted_recent,
        ewma_value=ewma_value,
        recent_avg=recent_avg,
        last_10_avg=last_10_avg,
        rate_projection=rate_projection,
        consistency_score=consistency_score,
    )
    is_home = int(row["team_id"]) == int(row["home_team_id"])
    rest_days = int(row["rest_days_home"] if is_home else row["rest_days_away"] or 2)
    spread_home = float(row["spread_home"]) if row["spread_home"] is not None else None
    team_spread = spread_home if is_home else -spread_home if spread_home is not None else None
    game_total = float(row["game_total"]) if row["game_total"] is not None and float(row["game_total"]) > 0 else 165.0
    return [
        component_projection,
        weighted_recent,
        ewma_value,
        recent_avg,
        last_10_avg,
        rate_projection,
        ewma_minutes,
        minutes_trend,
        value_volatility,
        consistency_score,
        float(rest_days),
        1.0 if is_home else 0.0,
        1.0,
        1.0,
        1.0,
        1.0,
        0.0,
        abs(team_spread) if team_spread is not None else 0.0,
        game_total,
    ]


def _project_minutes(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    game_id: int,
    rotation_role: str,
    ewma_minutes: float,
    minutes_trend: float,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
    context: dict,
    blowout_delta: float,
    injury_delta: float,
    before_game_date: str | None,
) -> tuple[float, str]:
    base_heuristic = max(ewma_minutes + (0.35 * minutes_trend), 4.0)
    projected = max(base_heuristic + blowout_delta + injury_delta, 0.0)

    # Keep minutes within plausible player-relative bounds.
    lower_bound = max(8.0, last_10_minutes_avg * 0.60)
    upper_bound = min(40.0, max(last_10_minutes_avg * 1.35, lower_bound + 4.0))
    projected = _clamp(projected, lower_bound, upper_bound)
    return projected, "heuristic minutes"


def _game_total_or_neutral(conn: sqlite3.Connection, game_total: float | None) -> float:
    if game_total is not None and float(game_total) > 0:
        return float(game_total)
    conn_key = id(conn)
    if conn_key in _CONNECTION_GAME_TOTAL_MEAN_CACHE:
        return _CONNECTION_GAME_TOTAL_MEAN_CACHE[conn_key]
    row = conn.execute(
        """
        SELECT AVG(game_total) AS avg_total
        FROM games
        WHERE game_total IS NOT NULL
          AND game_total > 0
        """
    ).fetchone()
    neutral = float(row["avg_total"]) if row and row["avg_total"] is not None else 165.0
    _CONNECTION_GAME_TOTAL_MEAN_CACHE[conn_key] = neutral
    return neutral


def _training_rows_slow(conn: sqlite3.Connection, market: str) -> list[tuple[list[float], float]]:
    samples = []
    rows = conn.execute(
        """
        SELECT s.player_id, s.game_id, g.game_date
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        ORDER BY g.game_date ASC, s.game_id ASC
        """
    ).fetchall()
    for row in rows:
        prior_count = conn.execute(
            """
            SELECT COUNT(*)
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            WHERE s.player_id = ?
              AND (g.game_date < ? OR (g.game_date = ? AND s.game_id < ?))
            """,
            (row["player_id"], row["game_date"], row["game_date"], row["game_id"]),
        ).fetchone()[0]
        if prior_count < 5:
            continue
        snapshot = feature_snapshot(conn, row["player_id"], market, row["game_id"], before_game_date=row["game_date"])
        actual_row = conn.execute("SELECT * FROM player_game_stats WHERE game_id = ? AND player_id = ?", (row["game_id"], row["player_id"])).fetchone()
        samples.append((snapshot.values, _market_value(actual_row, market)))
    return samples


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


def _predict(model: RidgeModel, features: list[float]) -> float:
    standardized = [
        (value - model.feature_means[idx]) / model.feature_scales[idx]
        for idx, value in enumerate(features)
    ]
    return model.intercept + sum(coef * value for coef, value in zip(model.coefficients, standardized))


def _market_weight(rows: int) -> float:
    if rows >= 500:
        return 0.25
    if rows >= 150:
        return 0.18
    return 0.12


def _player_market_weight(sample_count: int, avg_minutes: float) -> float:
    if sample_count < 4:
        return 0.70
    if sample_count < 7:
        return 0.55
    if avg_minutes < 16:
        return 0.48
    if sample_count < 10:
        return 0.38
    return 0.25


def _player_sample_quality(conn: sqlite3.Connection, player_id: int, game_id: int) -> tuple[int, float]:
    rows = conn.execute(
        """
        SELECT s.minutes
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN games target ON target.id = ?
        WHERE s.player_id = ?
          AND (g.game_date < target.game_date OR (g.game_date = target.game_date AND s.game_id < target.id))
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT 10
        """,
        (game_id, player_id),
    ).fetchall()
    if not rows:
        return 0, 0.0
    minutes = [float(row["minutes"]) for row in rows]
    return len(minutes), (sum(minutes) / len(minutes))


def _market_price_nudge(market: str) -> float:
    if market == "points_rebounds_assists":
        return 1.8
    if market in {"points_rebounds", "points_assists", "rebounds_assists"}:
        return 1.4
    return 1.0


def _adaptive_component_projection(
    *,
    weighted_recent: float,
    ewma_value: float,
    recent_avg: float,
    last_10_avg: float,
    rate_projection: float,
    consistency_score: float,
) -> float:
    volatility_weight = 1 - consistency_score
    weights = {
        "weighted_recent": 0.24,
        "ewma_value": 0.20 + (0.12 * volatility_weight),
        "recent_avg": 0.20 + (0.08 * volatility_weight),
        "rate_projection": 0.22 + (0.06 * consistency_score),
        "last_10_avg": max(0.14 - (0.08 * volatility_weight), 0.04),
    }
    total = sum(weights.values())
    return (
        (weighted_recent * weights["weighted_recent"])
        + (ewma_value * weights["ewma_value"])
        + (recent_avg * weights["recent_avg"])
        + (rate_projection * weights["rate_projection"])
        + (last_10_avg * weights["last_10_avg"])
    ) / total


def _ewma_newest_first(values: list[float], alpha: float) -> float:
    if not values:
        return 0.0
    estimate = values[-1]
    for value in reversed(values[:-1]):
        estimate = (alpha * value) + ((1 - alpha) * estimate)
    return estimate


def _ewma_volatility_newest_first(values: list[float], center: float, market: str) -> float:
    if len(values) < 2:
        return _market_volatility_floor(market)
    alpha = 0.42
    variance = (values[-1] - center) ** 2
    for value in reversed(values[:-1]):
        variance = (alpha * ((value - center) ** 2)) + ((1 - alpha) * variance)
    return max(math.sqrt(max(variance, 0.0)), _market_volatility_floor(market))


def _recent_trend(values: list[float]) -> float:
    if len(values) < 5:
        return 0.0
    recent = values[:3]
    prior = values[3:8] or values[3:]
    if not prior:
        return 0.0
    return (sum(recent) / len(recent)) - (sum(prior) / len(prior))


def _consistency_score(center: float, volatility: float, market: str) -> float:
    denominator = max(abs(center), _market_volatility_floor(market) * 2)
    return _clamp(1 - (volatility / denominator), 0.0, 1.0)


def _market_volatility_floor(market: str) -> float:
    return MARKET_VOLATILITY_FLOORS.get(market, 2.0)


def _player_history(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    before_game_date: str | None,
    exclude_game_id: int | None,
) -> list[dict]:
    filters = ["s.player_id = ?"]
    params: list[object] = [player_id]
    if before_game_date is not None:
        filters.append("g.game_date < ?")
        params.append(before_game_date)
    if exclude_game_id is not None:
        filters.append("s.game_id != ?")
        params.append(exclude_game_id)
    if before_game_date is not None:
        rows = conn.execute(
            f"""
            SELECT s.*, g.game_date, p.rotation_role
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            JOIN players p ON p.id = s.player_id
            WHERE {' AND '.join(filters)}
            ORDER BY
                CASE
                    WHEN substr(g.game_date, 1, 4) = substr(?, 1, 4) THEN 0
                    ELSE 1
                END,
                g.game_date DESC,
                s.game_id DESC
            LIMIT 10
            """,
            (*params, before_game_date),
        ).fetchall()
    else:
        rows = conn.execute(
            f"""
            SELECT s.*, g.game_date, p.rotation_role
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            JOIN players p ON p.id = s.player_id
            WHERE {' AND '.join(filters)}
            ORDER BY g.game_date DESC, s.game_id DESC
            LIMIT 10
            """,
            params,
        ).fetchall()
    return [
        {
            "value": _market_value(row, market),
            "minutes": float(row["minutes"]),
            "rotation_role": row["rotation_role"],
        }
        for row in rows
    ]


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


def _weighted_average(values: list[float]) -> float:
    weights = list(range(len(values), 0, -1))
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _game_context(conn: sqlite3.Connection, player_id: int, game_id: int | None) -> dict:
    row = conn.execute(
        """
        SELECT
            g.game_date,
            g.home_team_id,
            g.away_team_id,
            g.rest_days_home,
            g.rest_days_away,
            g.spread_home,
            g.game_total,
            COALESCE(
                (
                    SELECT h.team_id
                    FROM player_team_history h
                    LEFT JOIN games hg ON hg.id = h.game_id
                    WHERE h.player_id = p.id
                      AND (
                        h.game_id IS NULL
                        OR hg.game_date IS NULL
                        OR hg.game_date <= g.game_date
                      )
                    ORDER BY hg.game_date DESC, h.id DESC
                    LIMIT 1
                ),
                p.team_id
            ) AS resolved_team_id
        FROM players p
        JOIN games g ON g.id = ?
        WHERE p.id = ?
        """,
        (game_id, player_id),
    ).fetchone()
    if not row:
        return {"team_id": 0, "opponent_id": 0, "is_home": False, "rest_days": 2, "team_spread": None, "game_total": None, "game_date": None}
    team_id = int(row["resolved_team_id"])
    is_home = team_id == int(row["home_team_id"])
    opponent_id = int(row["away_team_id"] if is_home else row["home_team_id"])
    spread_home = float(row["spread_home"]) if row["spread_home"] is not None else None
    return {
        "team_id": team_id,
        "opponent_id": opponent_id,
        "is_home": is_home,
        "rest_days": int(row["rest_days_home"] if is_home else row["rest_days_away"] or 2),
        "team_spread": spread_home if is_home else -spread_home if spread_home is not None else None,
        "game_total": float(row["game_total"]) if row["game_total"] is not None else None,
        "game_date": str(row["game_date"]) if row["game_date"] is not None else None,
    }


def _manual_adjustment(conn: sqlite3.Connection, player_id: int) -> tuple[float, str]:
    row = conn.execute(
        """
        SELECT usage_multiplier, note
        FROM manual_adjustments
        WHERE player_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (player_id,),
    ).fetchone()
    if not row:
        return 1.0, "no manual adjustment"
    return float(row["usage_multiplier"] or 1.0), row["note"] or "manual adjustment applied"


def _injury_adjustment_for_prop(
    conn: sqlite3.Connection,
    player_id: int,
    team_id: int,
    rotation_role: str | None,
) -> dict[str, float | str | bool]:
    status = _latest_player_injury_status(conn, player_id)
    status_weight = {
        "out": 0.0,
        "inactive": 0.0,
        "suspended": 0.0,
        "unavailable": 0.0,
        "doubtful": 0.55,
        "questionable": 0.82,
        "gtd": 0.82,
        "probable": 0.96,
    }
    availability_factor = status_weight.get(status, 1.0)
    hard_cap_zero = availability_factor == 0.0

    teammate_rows = conn.execute(
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
          AND p.id != ?
          AND i.captured_at = (
              SELECT MAX(i2.captured_at)
              FROM injuries i2
              WHERE i2.player_id = i.player_id
          )
        """,
        (team_id, player_id),
    ).fetchall()
    miss_weight = {
        "out": 1.0,
        "inactive": 1.0,
        "suspended": 1.0,
        "unavailable": 1.0,
        "doubtful": 0.75,
        "questionable": 0.35,
        "gtd": 0.35,
    }
    role_weight = {"star": 1.25, "starter": 1.0, "rotation": 0.75, "bench": 0.5}
    teammate_penalty = 0.0
    missing_key = 0
    for row in teammate_rows:
        s = str(row["status"] or "").strip()
        s_weight = miss_weight.get(s)
        if s_weight is None:
            continue
        contribution = float(row["contribution"] or 0.0)
        if contribution <= 0:
            continue
        r_weight = role_weight.get(str(row["rotation_role"] or "starter").strip().lower(), 0.9)
        teammate_penalty += contribution * s_weight * r_weight
        if s_weight >= 0.75 and r_weight >= 1.0:
            missing_key += 1

    if hard_cap_zero:
        return {
            "status": status,
            "availability_factor": 0.0,
            "usage_multiplier": 1.0,
            "minutes_delta": -40.0,
            "hard_cap_zero": True,
        }

    usage_boost = _clamp(teammate_penalty / 120.0, 0.0, 0.12)
    base_minutes_by_role = {"star": 1.0, "starter": 0.8, "rotation": 0.5, "bench": 0.25}
    role_minutes = base_minutes_by_role.get((rotation_role or "starter").lower(), 0.6)
    minutes_delta = min(missing_key * role_minutes, 3.0)
    return {
        "status": status,
        "availability_factor": availability_factor,
        "usage_multiplier": 1.0 + usage_boost,
        "minutes_delta": minutes_delta,
        "hard_cap_zero": False,
    }


def _latest_player_injury_status(conn: sqlite3.Connection, player_id: int) -> str:
    row = conn.execute(
        """
        SELECT lower(trim(status)) AS status
        FROM injuries
        WHERE player_id = ?
        ORDER BY captured_at DESC
        LIMIT 1
        """,
        (player_id,),
    ).fetchone()
    if not row or not row["status"]:
        return "available"
    return str(row["status"])


def _pace_factor(conn: sqlite3.Connection, team_id: int, opponent_id: int) -> float:
    league_pace = _avg_scalar(conn, "SELECT AVG(possessions) FROM team_game_results") or 78.0
    team_pace = _avg_scalar(conn, "SELECT AVG(possessions) FROM team_game_results WHERE team_id = ?", (team_id,)) or league_pace
    opponent_pace = _avg_scalar(conn, "SELECT AVG(possessions) FROM team_game_results WHERE team_id = ?", (opponent_id,)) or league_pace
    return _clamp(((team_pace + opponent_pace) / 2) / league_pace, 0.94, 1.06)


def _opponent_factor(conn: sqlite3.Connection, opponent_id: int, market: str) -> float:
    opponent_rows = conn.execute(
        """
        SELECT s.*
        FROM player_game_stats s
        JOIN players p ON p.id = s.player_id
        JOIN games g ON g.id = s.game_id
        WHERE CASE
            WHEN p.team_id = g.home_team_id THEN g.away_team_id
            ELSE g.home_team_id
        END = ?
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT 160
        """,
        (opponent_id,),
    ).fetchall()
    league_rows = conn.execute(
        """
        SELECT s.*
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT 800
        """
    ).fetchall()
    if not opponent_rows or not league_rows:
        return 1.0
    opponent_allowed = sum(_market_value(row, market) for row in opponent_rows) / len(opponent_rows)
    league_allowed = sum(_market_value(row, market) for row in league_rows) / len(league_rows)
    return _clamp(opponent_allowed / league_allowed, 0.90, 1.10) if league_allowed > 0 else 1.0


def _common_opponent_factor(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    context: dict,
    before_game_date: str | None,
) -> float:
    common_opponents = _common_opponent_ids(conn, context["team_id"], context["opponent_id"], before_game_date)
    if not common_opponents:
        return 1.0
    placeholders = ",".join("?" for _ in common_opponents)
    date_filter = "AND g.game_date < ?" if before_game_date is not None else ""
    params: list[object] = [player_id, *common_opponents]
    if before_game_date is not None:
        params.append(before_game_date)
    player_rows = conn.execute(
        f"""
        SELECT s.*
        FROM player_game_stats s
        JOIN players p ON p.id = s.player_id
        JOIN games g ON g.id = s.game_id
        WHERE s.player_id = ?
          AND CASE
            WHEN p.team_id = g.home_team_id THEN g.away_team_id
            ELSE g.home_team_id
          END IN ({placeholders})
          {date_filter}
        ORDER BY g.game_date DESC
        LIMIT 10
        """,
        params,
    ).fetchall()
    if len(player_rows) < 2:
        return 1.0
    all_rows = _player_history(conn, player_id, market, before_game_date, exclude_game_id=None)
    if not all_rows:
        return 1.0
    common_avg = sum(_market_value(row, market) for row in player_rows) / len(player_rows)
    player_avg = sum(row["value"] for row in all_rows) / len(all_rows)
    if player_avg <= 0:
        return 1.0
    sample_weight = min(len(player_rows) / 5, 1.0)
    return _clamp(1 + (((common_avg / player_avg) - 1) * sample_weight * 0.5), 0.94, 1.06)


def _h2h_factor(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    context: dict,
    before_game_date: str | None,
) -> float:
    opponent_id = int(context["opponent_id"])
    date_filter = "AND g.game_date < ?" if before_game_date is not None else ""
    params: list[object] = [player_id, opponent_id]
    if before_game_date is not None:
        params.append(before_game_date)
    h2h_rows = conn.execute(
        f"""
        SELECT s.*
        FROM player_game_stats s
        JOIN players p ON p.id = s.player_id
        JOIN games g ON g.id = s.game_id
        WHERE s.player_id = ?
          AND CASE
            WHEN p.team_id = g.home_team_id THEN g.away_team_id
            ELSE g.home_team_id
          END = ?
          {date_filter}
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT 6
        """,
        params,
    ).fetchall()
    if len(h2h_rows) < 2:
        return 1.0
    baseline_rows = _player_history(conn, player_id, market, before_game_date, exclude_game_id=None)
    if not baseline_rows:
        return 1.0
    h2h_avg = sum(_market_value(row, market) for row in h2h_rows) / len(h2h_rows)
    baseline_avg = sum(row["value"] for row in baseline_rows) / len(baseline_rows)
    if baseline_avg <= 0:
        return 1.0
    sample_weight = min(1.0, len(h2h_rows) / 6.0)
    ratio = _clamp(h2h_avg / baseline_avg, 0.82, 1.18)
    return _clamp(1 + ((ratio - 1) * sample_weight * 0.55), 0.92, 1.08)


def _common_opponent_ids(conn: sqlite3.Connection, team_id: int, opponent_id: int, before_game_date: str | None) -> list[int]:
    return sorted(_recent_opponent_ids(conn, team_id, before_game_date).intersection(_recent_opponent_ids(conn, opponent_id, before_game_date)))


def _recent_opponent_ids(conn: sqlite3.Connection, team_id: int, before_game_date: str | None) -> set[int]:
    date_filter = "AND g.game_date < ?" if before_game_date is not None else ""
    params: list[object] = [team_id, team_id]
    if before_game_date is not None:
        params.append(before_game_date)
    rows = conn.execute(
        f"""
        SELECT
            CASE
                WHEN g.home_team_id = ? THEN g.away_team_id
                ELSE g.home_team_id
            END AS opponent_id
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        WHERE r.team_id = ?
          {date_filter}
        ORDER BY g.game_date DESC
        LIMIT 10
        """,
        params,
    ).fetchall()
    return {int(row["opponent_id"]) for row in rows if row["opponent_id"] is not None}


def _blowout_adjustment(conn: sqlite3.Connection, context: dict, rotation_role: str | None) -> dict:
    team_spread = context.get("team_spread")
    if team_spread is None:
        return {"risk": "unknown", "minutes_delta": 0.0}
    probability = _blowout_probability(abs(team_spread))
    role_delta = {"star": -4.0, "starter": -3.0, "rotation": -1.5, "bench": 2.0}.get(rotation_role or "starter", -2.0)
    minutes_delta = probability * role_delta
    risk = "very high" if probability >= 0.45 else "high" if probability >= 0.30 else "medium" if probability >= 0.15 else "low"
    return {"risk": risk, "minutes_delta": minutes_delta}


def _blowout_probability(spread_abs: float) -> float:
    if spread_abs < 4.5:
        return 0.08
    if spread_abs < 8.5:
        return 0.18
    if spread_abs < 12.5:
        return 0.34
    if spread_abs < 16.5:
        return 0.48
    return 0.60


def _rest_factor(rest_days: int) -> float:
    if rest_days <= 0:
        return 0.95
    if rest_days == 1:
        return 0.98
    if rest_days >= 4:
        return 1.01
    return 1.0


def _avg_scalar(conn: sqlite3.Connection, query: str, params: tuple = ()) -> float | None:
    value = conn.execute(query, params).fetchone()[0]
    return float(value) if value is not None else None


def _clamp(value: float, low: float, high: float) -> float:
    return max(low, min(high, value))
