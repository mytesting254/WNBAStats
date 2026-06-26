from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict, dataclass
from datetime import datetime
from functools import lru_cache

from .odds import american_to_implied_probability


MODEL_VERSION = "adaptive-context-v1"
MODEL_CACHE_PREFIX = "learned_prop_model"
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
    "arch_usage_scorer",
    "arch_rebound_big",
    "arch_assist_guard",
    "arch_bench_gunner",
    "arch_stocks_specialist",
]
FEATURE_INDEX = {name: idx for idx, name in enumerate(FEATURE_NAMES)}
MINUTES_FEATURE_NAMES = [
    "ewma_minutes",
    "recent_minutes_avg",
    "last_10_minutes_avg",
    "minutes_trend",
    "minute_volatility",
    "rest_days",
    "is_home",
    "spread_abs",
    "injury_delta",
    "recent_absence_days",
    "role_core_starter",
    "role_starter_volatile",
    "role_rotation",
    "role_bench",
    "role_fringe",
]

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

@dataclass(frozen=True)
class ModelTuningConfig:
    ridge_penalty: float = 1.25
    market_weight_scale: float = 1.0
    player_weight_scale: float = 1.0
    stabilization_scale: float = 1.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


DEFAULT_TUNING_CONFIG = ModelTuningConfig()

LOCAL_COMBO_MARKETS: dict[str, tuple[str, ...]] = {
    "points_rebounds_assists": ("points", "rebounds", "assists"),
}

_CONNECTION_MODEL_CACHE: dict[tuple[int, str, ModelTuningConfig], RidgeModel | None] = {}
_CONNECTION_RESIDUAL_MODEL_CACHE: dict[tuple[int, str, ModelTuningConfig], RidgeModel | None] = {}
_CONNECTION_MINUTES_MODEL_CACHE: dict[tuple[int, str | None, ModelTuningConfig], RidgeModel | None] = {}
_CONNECTION_GAME_TOTAL_MEAN_CACHE: dict[int, float] = {}

MINUTES_ROLE_BUCKETS = ["core_starter", "starter_volatile", "rotation", "bench", "fringe"]


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


@dataclass(frozen=True)
class MinutesRoleState:
    bucket: str
    lower_bound: float
    upper_bound: float
    anchor_minutes: float
    recent_drop: bool
    recent_spike: bool
    recent_absence_days: float | None = None


@dataclass(frozen=True)
class PlayerArchetypeProfile:
    usage_scorer: bool
    rebound_big: bool
    assist_guard: bool
    bench_gunner: bool
    stocks_specialist: bool

    def feature_values(self) -> list[float]:
        return [
            1.0 if self.usage_scorer else 0.0,
            1.0 if self.rebound_big else 0.0,
            1.0 if self.assist_guard else 0.0,
            1.0 if self.bench_gunner else 0.0,
            1.0 if self.stocks_specialist else 0.0,
        ]

    def labels(self) -> list[str]:
        labels = []
        if self.usage_scorer:
            labels.append("usage scorer")
        if self.rebound_big:
            labels.append("rebound big")
        if self.assist_guard:
            labels.append("assist guard")
        if self.bench_gunner:
            labels.append("bench gunner")
        if self.stocks_specialist:
            labels.append("stocks specialist")
        return labels


def predict_player_prop(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    game_id: int,
    line: float | None = None,
    over_odds: int | None = None,
    under_odds: int | None = None,
    config: ModelTuningConfig | None = None,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
    allow_training: bool = True,
) -> tuple[float, str, str]:
    if market in LOCAL_COMBO_MARKETS:
        return _predict_local_combo_market(
            conn,
            player_id=player_id,
            market=market,
            game_id=game_id,
            config=config,
            runtime_cache=runtime_cache,
            allow_training=allow_training,
        )
    tuning = config or DEFAULT_TUNING_CONFIG
    snapshot_cache = runtime_cache.setdefault("feature_snapshot", {}) if runtime_cache is not None else None
    sample_cache = runtime_cache.setdefault("player_sample_quality", {}) if runtime_cache is not None else None

    snapshot_key = (int(player_id), str(market), int(game_id))
    snapshot = (
        snapshot_cache[snapshot_key]
        if snapshot_cache is not None and snapshot_key in snapshot_cache
        else feature_snapshot(conn, player_id, market, game_id, allow_training=allow_training)
    )
    if snapshot_cache is not None:
        snapshot_cache.setdefault(snapshot_key, snapshot)

    sample_key = (int(player_id), int(game_id))
    if sample_cache is not None and sample_key in sample_cache:
        sample_count, avg_minutes = sample_cache[sample_key]  # type: ignore[misc]
    else:
        sample_count, avg_minutes = _player_sample_quality(conn, player_id, game_id)
        if sample_cache is not None:
            sample_cache[sample_key] = (sample_count, avg_minutes)
    model = train_market_model(conn, market, config=tuning, allow_training=allow_training)
    if not model:
        return snapshot.component_projection, snapshot.reason, "component"

    learned = _predict(model, snapshot.values)
    learned = max(0.0, learned)
    learned = _stabilize_combo_market_projection(learned, snapshot, market)
    learned, stabilization_note = _stabilize_learned_projection(learned, snapshot, market, config=tuning)
    projection = learned
    market_note = "no sportsbook line blend"

    if line is not None:
        market_weight = _market_weight(model.rows, config=tuning)
        market_weight = max(market_weight, _player_market_weight(sample_count, avg_minutes, config=tuning))
        projection = ((1 - market_weight) * learned) + (market_weight * float(line))
        residual_model = train_market_residual_model(conn, market, config=tuning, allow_training=allow_training)
        if residual_model is not None:
            residual_prediction = _predict(residual_model, snapshot.values)
            residual_projection = float(line) + residual_prediction
            residual_weight = _residual_market_weight(residual_model.rows, sample_count, avg_minutes, config=tuning)
            projection = ((1 - residual_weight) * projection) + (residual_weight * residual_projection)
            market_note = (
                f"line blend {market_weight:.0%} at {float(line):.1f}; "
                f"residual blend {residual_weight:.0%} ({residual_model.rows} settled rows)"
            )
        if over_odds is not None and under_odds is not None:
            over_implied = american_to_implied_probability(int(over_odds))
            under_implied = american_to_implied_probability(int(under_odds))
            no_vig_mid = (over_implied / max(over_implied + under_implied, 0.01)) - 0.5
            projection += no_vig_mid * _market_price_nudge(market)
        market_note = (
            f"{market_note} (player sample {sample_count} games, {avg_minutes:.1f} avg minutes)"
        )

    if snapshot.hard_cap_zero:
        projection = 0.0
        market_note = f"{market_note}; player marked OUT"

    reason = (
        f"{snapshot.reason} Learned model {MODEL_VERSION} projected {learned:.1f} from "
        f"{model.rows} historical rows; {market_note}; {stabilization_note}. Final projection {projection:.1f}."
    )
    return round(projection, 2), reason, MODEL_VERSION


def _predict_local_combo_market(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    market: str,
    game_id: int,
    config: ModelTuningConfig | None = None,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
    allow_training: bool = True,
) -> tuple[float, str, str]:
    components = LOCAL_COMBO_MARKETS[market]
    component_rows: list[tuple[str, float, str, str]] = []
    for component_market in components:
        projection, reason, model_version = predict_player_prop(
            conn,
            player_id=player_id,
            market=component_market,
            game_id=game_id,
            line=None,
            over_odds=None,
            under_odds=None,
            config=config,
            runtime_cache=runtime_cache,
            allow_training=allow_training,
        )
        component_rows.append((component_market, float(projection), reason, model_version))

    total_projection = round(sum(row[1] for row in component_rows), 2)
    component_summary = ", ".join(f"{market_name} {projection:.1f}" for market_name, projection, *_rest in component_rows)
    component_reasons = " ".join(
        f"{market_name}: {reason}"
        for market_name, _projection, reason, _model_version in component_rows
    )
    reason = (
        f"Local combo estimator for {market}: {component_summary}. "
        f"Combined projection {total_projection:.1f} from component models without direct combo-line blending. "
        f"{component_reasons}"
    )
    model_versions = {row[3] for row in component_rows}
    model_version = MODEL_VERSION if len(model_versions) != 1 else next(iter(model_versions))
    return total_projection, reason, model_version


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


def _stabilize_learned_projection(
    learned: float,
    snapshot: FeatureSnapshot,
    market: str,
    config: ModelTuningConfig | None = None,
) -> tuple[float, str]:
    """Global guardrail against implausible learned-vs-anchor drift."""
    tuning = config or DEFAULT_TUNING_CONFIG
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

    low_guard = _scaled_low_guard(0.82, tuning.stabilization_scale)
    high_guard = _scaled_high_guard(1.25, tuning.stabilization_scale)
    if minute_ratio < 0.92:
        low_guard = _scaled_low_guard(0.72, tuning.stabilization_scale)
    if minute_ratio > 1.08:
        high_guard = _scaled_high_guard(1.35, tuning.stabilization_scale)

    market_low_guard, market_high_guard = _market_stabilization_profile(market)
    low_guard = max(low_guard, market_low_guard)
    high_guard = min(high_guard, market_high_guard)
    if low_guard >= high_guard:
        midpoint = (low_guard + high_guard) / 2.0
        low_guard = midpoint - 0.02
        high_guard = midpoint + 0.02

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


def _market_stabilization_profile(market: str) -> tuple[float, float]:
    if market in {"points", "rebounds", "assists"}:
        return 0.80, 1.28
    if market == "threes":
        return 0.82, 1.22
    if market in {"points_rebounds", "points_assists", "rebounds_assists"}:
        return 0.85, 1.18
    if market == "points_rebounds_assists":
        return 0.88, 1.15
    if market in {"steals", "blocks", "blocks_steals"}:
        return 0.90, 1.12
    return 0.84, 1.20


def feature_snapshot(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    game_id: int,
    before_game_date: str | None = None,
    allow_training: bool = True,
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

    rotation_role = str(history[0]["rotation_role"] if history else "starter")
    recent_minutes_avg = sum(minutes[:5]) / min(len(minutes), 5)
    last_10_minutes_avg = sum(minutes) / len(minutes)
    minute_volatility = _minute_volatility(minutes)
    blowout = _blowout_adjustment(conn, context, rotation_role)
    injury = _injury_adjustment_for_prop(
        conn,
        player_id,
        context["team_id"],
        rotation_role,
        as_of_date=before_game_date or context.get("game_date"),
    )
    projected_minutes, minutes_note = _project_minutes(
        conn,
        player_id=player_id,
        game_id=game_id,
        rotation_role=rotation_role,
        ewma_minutes=ewma_minutes,
        minutes_trend=minutes_trend,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minute_volatility=minute_volatility,
        context=context,
        blowout_delta=float(blowout["minutes_delta"]),
        injury_delta=float(injury["minutes_delta"]),
        injury_status=str(injury["status"]),
        recent_absence_days=_recent_absence_days(history, before_game_date or context.get("game_date")),
        before_game_date=before_game_date,
        allow_training=allow_training,
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
    archetype = _player_archetype_profile(
        conn,
        player_id=player_id,
        reference_game_date=reference_game_date,
        exclude_game_id=game_id,
        fallback_rotation_role=rotation_role,
    )

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
        *archetype.feature_values(),
    ]
    archetype_note = ", ".join(archetype.labels()) or "balanced"
    reason = (
        f"Weighted recent {weighted_recent:.1f}, EWMA {ewma_value:.1f}, last 5 {recent_avg:.1f}, "
        f"last 10 {last_10_avg:.1f}, rate x minutes {rate_projection:.1f} on {projected_minutes:.1f} projected minutes. "
        f"Minutes trend {minutes_trend:+.1f}, minute volatility {minute_volatility:.1f}, value volatility {value_volatility:.1f}, consistency {consistency_score:.2f}. "
        f"Context: pace {pace_factor:.2f}, opponent {opponent_factor:.2f}, "
        f"common opponents {common_opponent_factor:.2f}, h2h {h2h_factor:.2f}, blowout {blowout['risk']} "
        f"({blowout['minutes_delta']:+.1f} min), "
        f"{'home' if context and context['is_home'] else 'away'} {home_factor:.2f}, "
        f"rest {rest_factor:.2f}, usage {usage_multiplier:.2f}; "
        f"injury {injury['status']} (avail {injury['availability_factor']:.2f}, "
        f"team usage {injury['usage_multiplier']:.2f}, min {injury['minutes_delta']:+.1f}); {adjustment_note}."
        f" Archetype: {archetype_note}."
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
def _train_market_model_cached(db_path: str, market: str, config: ModelTuningConfig) -> RidgeModel | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _train_market_model_uncached(conn, market, config=config)
    finally:
        conn.close()


def train_market_model(
    conn: sqlite3.Connection,
    market: str,
    config: ModelTuningConfig | None = None,
    allow_training: bool = True,
) -> RidgeModel | None:
    tuning = config or DEFAULT_TUNING_CONFIG
    if not isinstance(conn, sqlite3.Connection):
        key = (id(conn), market, tuning)
        if key not in _CONNECTION_MODEL_CACHE:
            if not allow_training:
                return None
            _CONNECTION_MODEL_CACHE[key] = _train_market_model_uncached(conn, market, config=tuning)
        return _CONNECTION_MODEL_CACHE[key]
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    cache_key = _model_cache_key(conn, db_path, market, tuning, kind="market")
    cached_model = _load_cached_model(cache_key)
    if cached_model is not None:
        return cached_model
    if not allow_training:
        return None
    return _train_market_model_cached(db_path, market, tuning)


@lru_cache(maxsize=32)
def _train_market_residual_model_cached(db_path: str, market: str, config: ModelTuningConfig) -> RidgeModel | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _train_market_residual_model_uncached(conn, market, config=config)
    finally:
        conn.close()


def train_market_residual_model(
    conn: sqlite3.Connection,
    market: str,
    config: ModelTuningConfig | None = None,
    allow_training: bool = True,
) -> RidgeModel | None:
    tuning = config or DEFAULT_TUNING_CONFIG
    cache_market = f"residual:{market}"
    if not isinstance(conn, sqlite3.Connection):
        key = (id(conn), market, tuning)
        if key not in _CONNECTION_RESIDUAL_MODEL_CACHE:
            if not allow_training:
                return None
            _CONNECTION_RESIDUAL_MODEL_CACHE[key] = _train_market_residual_model_uncached(conn, market, config=tuning)
        return _CONNECTION_RESIDUAL_MODEL_CACHE[key]
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    cache_key = _model_cache_key(conn, db_path, cache_market, tuning, kind="residual")
    cached_model = _load_cached_model(cache_key)
    if cached_model is not None:
        return cached_model
    if not allow_training:
        return None
    return _train_market_residual_model_cached(db_path, market, tuning)


def clear_model_cache() -> None:
    _train_market_model_cached.cache_clear()
    _CONNECTION_MODEL_CACHE.clear()
    _train_market_residual_model_cached.cache_clear()
    _CONNECTION_RESIDUAL_MODEL_CACHE.clear()
    _train_minutes_model_cached.cache_clear()
    _CONNECTION_MINUTES_MODEL_CACHE.clear()
    _CONNECTION_GAME_TOTAL_MEAN_CACHE.clear()


def _train_market_model_uncached(
    conn: sqlite3.Connection,
    market: str,
    config: ModelTuningConfig | None = None,
) -> RidgeModel | None:
    rows = _training_rows(conn, market)
    model = _fit_model_from_rows(market, rows, config=config)
    if model is not None:
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        cache_key = _model_cache_key(conn, db_path, market, config or DEFAULT_TUNING_CONFIG, kind="market")
        _store_cached_model(cache_key, model, config or DEFAULT_TUNING_CONFIG)
    return model


def _train_market_residual_model_uncached(
    conn: sqlite3.Connection,
    market: str,
    config: ModelTuningConfig | None = None,
) -> RidgeModel | None:
    rows = _residual_training_rows(conn, market)
    model = _fit_model_from_rows(f"residual:{market}", rows, config=config)
    if model is not None:
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        cache_key = _model_cache_key(conn, db_path, f"residual:{market}", config or DEFAULT_TUNING_CONFIG, kind="residual")
        _store_cached_model(cache_key, model, config or DEFAULT_TUNING_CONFIG)
    return model


def evaluate_market_model(
    conn: sqlite3.Connection,
    market: str,
    config: ModelTuningConfig | None = None,
) -> dict:
    tuning = config or DEFAULT_TUNING_CONFIG
    rows = _training_rows(conn, market)
    if len(rows) < 20:
        return {"rows": 0, "mae": None, "rmse": None, "bias": None, "directional_accuracy": None}
    split = max(int(len(rows) * 0.8), 10)
    train_rows = rows[:split]
    test_rows = rows[split:]
    model = _fit_model_from_rows(market, train_rows, config=tuning)
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


def evaluate_market_residual_model(
    conn: sqlite3.Connection,
    market: str,
    config: ModelTuningConfig | None = None,
) -> dict:
    tuning = config or DEFAULT_TUNING_CONFIG
    rows = _residual_training_rows(conn, market)
    if len(rows) < 20:
        return {
            "residual_rows": 0,
            "residual_mae": None,
            "residual_rmse": None,
            "residual_bias": None,
            "residual_directional_accuracy": None,
        }
    split = max(int(len(rows) * 0.8), 10)
    train_rows = rows[:split]
    test_rows = rows[split:]
    model = _fit_model_from_rows(f"residual:{market}", train_rows, config=tuning)
    if not model or not test_rows:
        return {
            "residual_rows": 0,
            "residual_mae": None,
            "residual_rmse": None,
            "residual_bias": None,
            "residual_directional_accuracy": None,
        }

    errors = []
    absolute_errors = []
    squared_errors = []
    direction_hits = 0
    for features, actual_residual in test_rows:
        prediction = _predict(model, features)
        error = prediction - actual_residual
        errors.append(error)
        absolute_errors.append(abs(error))
        squared_errors.append(error * error)
        if (prediction >= 0 and actual_residual >= 0) or (prediction < 0 and actual_residual < 0):
            direction_hits += 1
    row_count = len(test_rows)
    return {
        "residual_rows": row_count,
        "residual_mae": round(sum(absolute_errors) / row_count, 3),
        "residual_rmse": round(math.sqrt(sum(squared_errors) / row_count), 3),
        "residual_bias": round(sum(errors) / row_count, 3),
        "residual_directional_accuracy": round(direction_hits / row_count, 3),
    }


def _fit_model_from_rows(
    market: str,
    rows: list[tuple[list[float], float]],
    config: ModelTuningConfig | None = None,
) -> RidgeModel | None:
    tuning = config or DEFAULT_TUNING_CONFIG
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

    coefs = _ridge_regression(standardized, ys, penalty=tuning.ridge_penalty)
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
                p.team_id,
                p.rotation_role
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
            previous_game_date = str(rows[idx - 1]["game_date"]) if idx > 0 else None
            features = _historical_training_features(conn, rows[idx], history, minute_history, market, previous_game_date)
            samples.append((features, values[idx]))
    return samples


def _residual_training_rows(conn: sqlite3.Connection, market: str) -> list[tuple[list[float], float]]:
    samples = []
    rows = conn.execute(
        """
        SELECT
            pl.id AS prop_line_id,
            pl.player_id,
            pl.game_id,
            pl.market,
            pl.line,
            sp.actual_result,
            g.game_date
        FROM settled_props sp
        JOIN prop_lines pl ON pl.id = sp.prop_line_id
        JOIN games g ON g.id = pl.game_id
        WHERE pl.market = ?
          AND pl.line IS NOT NULL
        ORDER BY g.game_date ASC, pl.id ASC
        """,
        (market,),
    ).fetchall()
    for row in rows:
        snapshot = feature_snapshot(
            conn,
            int(row["player_id"]),
            market,
            int(row["game_id"]),
            before_game_date=str(row["game_date"]) if row["game_date"] is not None else None,
        )
        if not snapshot.values:
            continue
        target = float(row["actual_result"]) - float(row["line"])
        samples.append((snapshot.values, target))
    return samples


def _historical_training_features(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    history: list[float],
    minutes: list[float],
    market: str,
    previous_game_date: str | None,
) -> list[float]:
    current_game_date = str(row["game_date"]) if row["game_date"] is not None else None
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
    recent_minutes_avg = sum(newest_minutes[:5]) / min(len(newest_minutes), 5)
    last_10_minutes_avg = sum(newest_minutes) / len(newest_minutes)
    minute_volatility = _minute_volatility(newest_minutes)
    value_volatility = _ewma_volatility_newest_first(newest_values, ewma_value, market)
    consistency_score = _consistency_score(ewma_value, value_volatility, market)
    context = _historical_game_context(row)
    pace_factor = _pace_factor(conn, int(context["team_id"]), int(context["opponent_id"]))
    opponent_factor = _opponent_factor(conn, int(context["opponent_id"]), market)
    common_opponent_factor = _common_opponent_factor(
        conn,
        int(row["player_id"]),
        market,
        context,
        current_game_date,
    )
    h2h_factor = _h2h_factor(
        conn,
        int(row["player_id"]),
        market,
        context,
        current_game_date,
    )
    blowout = _blowout_adjustment(None, context, str(row["rotation_role"] or "starter"))
    projected_minutes, _minutes_note = _project_minutes(
        None,
        player_id=int(row["player_id"]),
        game_id=int(row["game_id"]),
        rotation_role=str(row["rotation_role"] or "starter"),
        ewma_minutes=ewma_minutes,
        minutes_trend=minutes_trend,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minute_volatility=minute_volatility,
        context=context,
        blowout_delta=float(blowout["minutes_delta"]),
        injury_delta=0.0,
        injury_status="available",
        recent_absence_days=_days_between_game_dates(previous_game_date, current_game_date),
        before_game_date=current_game_date,
    )
    rate_projection = weighted_rate * projected_minutes
    component_projection = _adaptive_component_projection(
        weighted_recent=weighted_recent,
        ewma_value=ewma_value,
        recent_avg=recent_avg,
        last_10_avg=last_10_avg,
        rate_projection=rate_projection,
        consistency_score=consistency_score,
    )
    is_home = bool(context["is_home"])
    rest_days = int(context["rest_days"])
    team_spread = context["team_spread"]
    game_total = float(context["game_total"]) if context["game_total"] is not None and float(context["game_total"]) > 0 else 165.0
    archetype = _player_archetype_profile(
        conn,
        player_id=int(row["player_id"]),
        reference_game_date=str(row["game_date"]),
        exclude_game_id=int(row["game_id"]),
        fallback_rotation_role=str(row["rotation_role"] or "starter"),
    )
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
        pace_factor,
        opponent_factor,
        common_opponent_factor,
        h2h_factor,
        float(blowout["minutes_delta"]),
        abs(team_spread) if team_spread is not None else 0.0,
        game_total,
        *archetype.feature_values(),
    ]


@lru_cache(maxsize=32)
def _train_minutes_model_cached(
    db_path: str,
    config: ModelTuningConfig,
    role_bucket: str | None = None,
) -> RidgeModel | None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return _train_minutes_model_uncached(conn, config=config, role_bucket=role_bucket)
    finally:
        conn.close()


def train_minutes_model(
    conn: sqlite3.Connection,
    config: ModelTuningConfig | None = None,
    role_bucket: str | None = None,
    allow_training: bool = True,
) -> RidgeModel | None:
    tuning = config or DEFAULT_TUNING_CONFIG
    bucket = role_bucket if role_bucket in set(MINUTES_ROLE_BUCKETS) else None
    cache_market = f"minutes:{bucket or 'all'}"
    if not isinstance(conn, sqlite3.Connection):
        key = (id(conn), bucket, tuning)
        if key not in _CONNECTION_MINUTES_MODEL_CACHE:
            if not allow_training:
                return None
            _CONNECTION_MINUTES_MODEL_CACHE[key] = _train_minutes_model_uncached(conn, config=tuning, role_bucket=bucket)
        return _CONNECTION_MINUTES_MODEL_CACHE[key]
    db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
    cache_key = _model_cache_key(conn, db_path, cache_market, tuning, kind="minutes")
    cached_model = _load_cached_model(cache_key)
    if cached_model is not None:
        return cached_model
    if not allow_training:
        return None
    return _train_minutes_model_cached(db_path, tuning, bucket)


def prewarm_model_cache(conn: sqlite3.Connection, config: ModelTuningConfig | None = None) -> dict[str, int]:
    tuning = config or DEFAULT_TUNING_CONFIG
    warmed = {"minutes": 0, "markets": 0, "residuals": 0}
    if train_minutes_model(conn, config=tuning) is not None:
        warmed["minutes"] = 1
    for bucket in MINUTES_ROLE_BUCKETS:
        if train_minutes_model(conn, config=tuning, role_bucket=bucket) is not None:
            warmed["minutes"] += 1
    for market in TRAINING_MARKETS:
        if train_market_model(conn, market, config=tuning) is not None:
            warmed["markets"] += 1
        if train_market_residual_model(conn, market, config=tuning) is not None:
            warmed["residuals"] += 1
    return warmed


def _train_minutes_model_uncached(
    conn: sqlite3.Connection,
    config: ModelTuningConfig | None = None,
    role_bucket: str | None = None,
) -> RidgeModel | None:
    bucket = role_bucket if role_bucket in set(MINUTES_ROLE_BUCKETS) else None
    rows = _minutes_training_rows(conn, role_bucket=bucket)
    model = _fit_model_from_rows(f"minutes:{bucket or 'all'}", rows, config=config)
    if model is not None:
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        cache_market = f"minutes:{bucket or 'all'}"
        cache_key = _model_cache_key(conn, db_path, cache_market, config or DEFAULT_TUNING_CONFIG, kind="minutes")
        _store_cached_model(cache_key, model, config or DEFAULT_TUNING_CONFIG)
    return model


def _minutes_training_rows(
    conn: sqlite3.Connection,
    role_bucket: str | None = None,
) -> list[tuple[list[float], float]]:
    samples = []
    bucket_filter = role_bucket if role_bucket in set(MINUTES_ROLE_BUCKETS) else None
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
                p.team_id,
                p.rotation_role
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            JOIN players p ON p.id = s.player_id
            WHERE s.player_id = ?
            ORDER BY g.game_date ASC, s.game_id ASC
            """,
            (player["id"],),
        ).fetchall()
        minutes = [float(row["minutes"]) for row in rows]
        for idx in range(5, len(rows)):
            newest_minutes = list(reversed(minutes[max(0, idx - 10):idx]))
            if len(newest_minutes) < 5:
                continue
            current = rows[idx]
            previous_game_date = str(rows[idx - 1]["game_date"]) if idx > 0 else None
            context = _historical_game_context(current)
            minute_volatility = _minute_volatility(newest_minutes)
            ewma_minutes = _ewma_newest_first(newest_minutes, alpha=0.38)
            minutes_trend = _recent_trend(newest_minutes)
            recent_minutes_avg = sum(newest_minutes[:5]) / min(len(newest_minutes), 5)
            last_10_minutes_avg = sum(newest_minutes) / len(newest_minutes)
            recent_absence_days = _days_between_game_dates(previous_game_date, str(current["game_date"]))
            injury = _injury_adjustment_for_prop(
                conn,
                player_id=int(current["player_id"]),
                team_id=int(current["team_id"]),
                rotation_role=str(current["rotation_role"] or "starter"),
                as_of_date=str(current["game_date"]),
            )
            role_state = _classify_minutes_role(
                rotation_role=str(current["rotation_role"] or "starter"),
                recent_minutes_avg=recent_minutes_avg,
                last_10_minutes_avg=last_10_minutes_avg,
                ewma_minutes=ewma_minutes,
                minutes_trend=minutes_trend,
                minute_volatility=minute_volatility,
                injury_status=str(injury["status"]),
                injury_delta=float(injury["minutes_delta"]),
                recent_absence_days=recent_absence_days,
            )
            if bucket_filter is not None and role_state.bucket != bucket_filter:
                continue
            features = _minutes_feature_values(
                role_state=role_state,
                ewma_minutes=ewma_minutes,
                recent_minutes_avg=recent_minutes_avg,
                last_10_minutes_avg=last_10_minutes_avg,
                minutes_trend=minutes_trend,
                minute_volatility=minute_volatility,
                rest_days=int(context["rest_days"]),
                is_home=bool(context["is_home"]),
                spread_abs=abs(float(context["team_spread"])) if context["team_spread"] is not None else 0.0,
                injury_delta=float(injury["minutes_delta"]),
                recent_absence_days=recent_absence_days,
            )
            samples.append((features, float(current["minutes"])))
    return samples


def _project_minutes(
    conn: sqlite3.Connection | None,
    *,
    player_id: int,
    game_id: int,
    rotation_role: str,
    ewma_minutes: float,
    minutes_trend: float,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
    minute_volatility: float,
    context: dict,
    blowout_delta: float,
    injury_delta: float,
    injury_status: str,
    recent_absence_days: float | None,
    before_game_date: str | None,
    allow_training: bool = True,
) -> tuple[float, str]:
    base_heuristic = max(ewma_minutes + (0.35 * minutes_trend), 4.0)
    hard_statuses = {"out", "inactive", "suspended", "unavailable"}
    if str(injury_status or "").strip().lower() in hard_statuses:
        return 0.0, "hard rule out"
    venue_delta = 0.0
    if conn is not None:
        venue_delta = _venue_minutes_adjustment(
            conn,
            player_id=player_id,
            is_home=bool(context.get("is_home")) if isinstance(context, dict) else False,
            before_game_date=before_game_date,
            exclude_game_id=game_id,
        )
    projected = max(base_heuristic + venue_delta + blowout_delta + injury_delta, 0.0)
    role_state = _classify_minutes_role(
        rotation_role=rotation_role,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        ewma_minutes=ewma_minutes,
        minutes_trend=minutes_trend,
        minute_volatility=minute_volatility,
        injury_status=injury_status,
        injury_delta=injury_delta,
        recent_absence_days=recent_absence_days,
    )
    learned_minutes = None
    blend_note = "heuristic only"
    if conn is not None:
        minutes_model = train_minutes_model(conn, role_bucket=role_state.bucket, allow_training=allow_training)
        model_scope = f"role {role_state.bucket}"
        if minutes_model is None:
            minutes_model = train_minutes_model(conn, allow_training=allow_training)
            model_scope = "global"
        if minutes_model is not None:
            minutes_features = _minutes_feature_values(
                role_state=role_state,
                ewma_minutes=ewma_minutes,
                recent_minutes_avg=recent_minutes_avg,
                last_10_minutes_avg=last_10_minutes_avg,
                minutes_trend=minutes_trend,
                minute_volatility=minute_volatility,
                rest_days=int(context.get("rest_days") or 2),
                is_home=bool(context.get("is_home")),
                spread_abs=abs(float(context.get("team_spread"))) if context.get("team_spread") is not None else 0.0,
                injury_delta=injury_delta,
                recent_absence_days=recent_absence_days,
            )
            learned_minutes = max(0.0, _predict(minutes_model, minutes_features))
            blend_weight = _minutes_model_weight(
                rows=minutes_model.rows,
                role_bucket=role_state.bucket,
                minute_volatility=minute_volatility,
                recent_absence_days=recent_absence_days,
            )
            projected = ((1.0 - blend_weight) * projected) + (blend_weight * learned_minutes)
            blend_note = f"learned blend {blend_weight:.0%} ({model_scope})"
    lower_bound, upper_bound = _role_aware_minutes_bounds(
        role_state,
        last_10_minutes_avg=last_10_minutes_avg,
        recent_minutes_avg=recent_minutes_avg,
        minutes_trend=minutes_trend,
        injury_status=injury_status,
        injury_delta=injury_delta,
    )
    projected = _clamp(projected, lower_bound, upper_bound)
    projected, hard_rule_notes = _apply_minutes_hard_rules(
        projected=projected,
        role_state=role_state,
        rotation_role=rotation_role,
        injury_status=injury_status,
        injury_delta=injury_delta,
        blowout_delta=blowout_delta,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
    )
    projected = _clamp(projected, lower_bound, upper_bound)
    if abs(venue_delta) >= 0.05:
        venue_note = f", venue {venue_delta:+.1f}"
    else:
        venue_note = ""
    learned_note = f", learned {learned_minutes:.1f}" if learned_minutes is not None else ""
    hard_rule_suffix = f", {'; '.join(hard_rule_notes)}" if hard_rule_notes else ""
    return projected, f"{role_state.bucket} bounds {lower_bound:.1f}-{upper_bound:.1f}{venue_note}, {blend_note}{learned_note}{hard_rule_suffix}"


def _venue_minutes_adjustment(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    is_home: bool,
    before_game_date: str | None,
    exclude_game_id: int | None,
) -> float:
    filters = ["s.player_id = ?"]
    params: list[object] = [int(player_id)]
    if before_game_date is not None:
        filters.append("g.game_date < ?")
        params.append(before_game_date)
    if exclude_game_id is not None:
        filters.append("s.game_id != ?")
        params.append(int(exclude_game_id))
    rows = conn.execute(
        f"""
        SELECT
            s.minutes,
            CASE WHEN p.team_id = g.home_team_id THEN 1 ELSE 0 END AS sample_is_home
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN players p ON p.id = s.player_id
        WHERE {' AND '.join(filters)}
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT 12
        """,
        params,
    ).fetchall()
    if len(rows) < 4:
        return 0.0
    same_venue = [float(row["minutes"]) for row in rows if bool(int(row["sample_is_home"])) == bool(is_home)]
    opposite_venue = [float(row["minutes"]) for row in rows if bool(int(row["sample_is_home"])) != bool(is_home)]
    if not same_venue or not opposite_venue:
        return 0.0
    same_avg = sum(same_venue) / len(same_venue)
    opp_avg = sum(opposite_venue) / len(opposite_venue)
    # Keep venue correction small so it nudges, not dominates, the recency heuristic.
    return _clamp((same_avg - opp_avg) * 0.35, -2.0, 2.0)


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


def _historical_game_context(row: sqlite3.Row) -> dict[str, float | int | bool | str | None]:
    is_home = int(row["team_id"]) == int(row["home_team_id"])
    spread_home = float(row["spread_home"]) if row["spread_home"] is not None else None
    return {
        "team_id": int(row["team_id"]),
        "opponent_id": int(row["away_team_id"] if is_home else row["home_team_id"]),
        "is_home": is_home,
        "rest_days": int(row["rest_days_home"] if is_home else row["rest_days_away"] or 2),
        "team_spread": spread_home if is_home else -spread_home if spread_home is not None else None,
        "game_total": float(row["game_total"]) if row["game_total"] is not None else None,
        "game_date": str(row["game_date"]) if row["game_date"] is not None else None,
    }


def _recent_absence_days(history: list[dict], target_game_date: str | None) -> float | None:
    if not history or not target_game_date:
        return None
    latest_game_date = str(history[0].get("game_date") or "").strip()
    if not latest_game_date:
        return None
    try:
        latest = datetime.fromisoformat(latest_game_date[:10]).date()
        target = datetime.fromisoformat(str(target_game_date)[:10]).date()
    except ValueError:
        return None
    gap = (target - latest).days
    return float(gap) if gap >= 0 else None


def _days_between_game_dates(previous_game_date: str | None, current_game_date: str | None) -> float | None:
    if not previous_game_date or not current_game_date:
        return None
    try:
        previous = datetime.fromisoformat(str(previous_game_date)[:10]).date()
        current = datetime.fromisoformat(str(current_game_date)[:10]).date()
    except ValueError:
        return None
    gap = (current - previous).days
    return float(gap) if gap >= 0 else None


def _classify_minutes_role(
    *,
    rotation_role: str,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
    ewma_minutes: float,
    minutes_trend: float,
    minute_volatility: float,
    injury_status: str,
    injury_delta: float,
    recent_absence_days: float | None,
) -> MinutesRoleState:
    recent_median = recent_minutes_avg if last_10_minutes_avg <= 0 else ((recent_minutes_avg * 0.65) + (last_10_minutes_avg * 0.35))
    anchor = (0.45 * recent_minutes_avg) + (0.30 * recent_median) + (0.25 * ewma_minutes)
    recent_drop = recent_minutes_avg < (last_10_minutes_avg * 0.78) or minutes_trend <= -4.0
    recent_spike = recent_minutes_avg > (last_10_minutes_avg * 1.12) or minutes_trend >= 3.0
    role_text = str(rotation_role or "starter").strip().lower()

    if recent_drop:
        anchor -= 2.5
    if recent_spike:
        anchor += 1.5
    if minute_volatility >= 8.0:
        anchor -= 1.0
    if injury_delta > 0.0:
        anchor += min(2.5, injury_delta * 1.2)
    if recent_absence_days is not None and recent_absence_days >= 7:
        anchor -= 3.0 if recent_absence_days >= 14 else 1.8
    if role_text == "star":
        anchor = max(anchor, 29.0)
    elif role_text == "bench":
        anchor = min(anchor, 20.0)

    if anchor >= 30.0 and minute_volatility < 7.0 and role_text in {"star", "starter"}:
        bucket = "core_starter"
    elif anchor >= 24.0:
        bucket = "starter_volatile"
    elif anchor >= 18.0:
        bucket = "rotation"
    elif anchor >= 10.0:
        bucket = "bench"
    else:
        bucket = "fringe"

    if injury_status in {"doubtful", "questionable", "gtd"} and bucket == "core_starter":
        bucket = "starter_volatile"

    lower_bound, upper_bound = _minutes_bucket_bounds(bucket)
    return MinutesRoleState(
        bucket=bucket,
        lower_bound=lower_bound,
        upper_bound=upper_bound,
        anchor_minutes=anchor,
        recent_drop=recent_drop,
        recent_spike=recent_spike,
        recent_absence_days=recent_absence_days,
    )


def _minutes_bucket_bounds(bucket: str) -> tuple[float, float]:
    bounds = {
        "core_starter": (28.0, 37.0),
        "starter_volatile": (22.0, 34.0),
        "rotation": (16.0, 28.0),
        "bench": (8.0, 22.0),
        "fringe": (0.0, 14.0),
    }
    return bounds.get(bucket, (12.0, 30.0))


def _role_aware_minutes_bounds(
    role_state: MinutesRoleState,
    *,
    last_10_minutes_avg: float,
    recent_minutes_avg: float,
    minutes_trend: float,
    injury_status: str,
    injury_delta: float,
) -> tuple[float, float]:
    lower_bound = max(role_state.lower_bound, last_10_minutes_avg * 0.72)
    upper_bound = min(role_state.upper_bound, max(last_10_minutes_avg * 1.18, lower_bound + 4.0))

    if role_state.bucket in {"bench", "fringe"}:
        lower_bound = role_state.lower_bound
    if role_state.recent_drop:
        lower_bound = max(role_state.lower_bound, lower_bound - 4.0)
        upper_bound = min(upper_bound, max(recent_minutes_avg + 2.0, lower_bound + 3.0))
    if role_state.recent_spike and injury_delta > 0:
        upper_bound = min(40.0, upper_bound + min(4.0, injury_delta * 2.0))
        lower_bound = min(upper_bound - 2.0, lower_bound + min(2.0, injury_delta))
    if injury_status in {"doubtful", "questionable", "gtd"}:
        upper_bound = max(lower_bound + 2.0, upper_bound - 2.0)
    if role_state.recent_absence_days is not None:
        if role_state.recent_absence_days >= 14:
            upper_bound = min(upper_bound, max(lower_bound + 2.0, recent_minutes_avg + 3.0))
            lower_bound = max(role_state.lower_bound, lower_bound - 4.0)
        elif role_state.recent_absence_days >= 7:
            upper_bound = min(upper_bound, max(lower_bound + 2.5, recent_minutes_avg + 4.0))
    if minutes_trend <= -5.0:
        lower_bound = max(role_state.lower_bound, lower_bound - 2.0)
    return max(0.0, lower_bound), min(40.0, upper_bound)


def _apply_minutes_hard_rules(
    *,
    projected: float,
    role_state: MinutesRoleState,
    rotation_role: str,
    injury_status: str,
    injury_delta: float,
    blowout_delta: float,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
) -> tuple[float, list[str]]:
    notes: list[str] = []
    status = str(injury_status or "").strip().lower()
    role = str(rotation_role or "").strip().lower()

    if status in {"questionable", "gtd"}:
        cap = max(role_state.lower_bound + 2.0, min(role_state.upper_bound, recent_minutes_avg * 0.92))
        if projected > cap:
            projected = cap
            notes.append("hard rule gtd cap")
    elif status == "doubtful":
        cap = max(role_state.lower_bound + 1.5, min(role_state.upper_bound, recent_minutes_avg * 0.82))
        if projected > cap:
            projected = cap
            notes.append("hard rule doubtful cap")

    if role in {"star", "starter"} and blowout_delta <= -1.0:
        blowout_cap = max(role_state.lower_bound, min(role_state.upper_bound, last_10_minutes_avg + (blowout_delta * 0.85)))
        if projected > blowout_cap:
            projected = blowout_cap
            notes.append("hard rule blowout star cap")

    if injury_delta >= 1.5 and role_state.recent_spike:
        floor = min(role_state.upper_bound, max(role_state.lower_bound, recent_minutes_avg + min(2.5, injury_delta * 1.25)))
        if projected < floor:
            projected = floor
            notes.append("hard rule injury replacement floor")

    return projected, notes


def _minute_volatility(minutes: list[float]) -> float:
    if len(minutes) < 2:
        return 0.0
    mean = sum(minutes) / len(minutes)
    variance = sum((value - mean) ** 2 for value in minutes) / (len(minutes) - 1)
    return math.sqrt(max(variance, 0.0))


def _minutes_feature_values(
    *,
    role_state: MinutesRoleState,
    ewma_minutes: float,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
    minutes_trend: float,
    minute_volatility: float,
    rest_days: int,
    is_home: bool,
    spread_abs: float,
    injury_delta: float,
    recent_absence_days: float | None,
) -> list[float]:
    role_flags = {
        "core_starter": 0.0,
        "starter_volatile": 0.0,
        "rotation": 0.0,
        "bench": 0.0,
        "fringe": 0.0,
    }
    role_flags[role_state.bucket] = 1.0
    return [
        ewma_minutes,
        recent_minutes_avg,
        last_10_minutes_avg,
        minutes_trend,
        minute_volatility,
        float(rest_days),
        1.0 if is_home else 0.0,
        float(spread_abs),
        float(injury_delta),
        float(recent_absence_days or 0.0),
        role_flags["core_starter"],
        role_flags["starter_volatile"],
        role_flags["rotation"],
        role_flags["bench"],
        role_flags["fringe"],
    ]


def _minutes_model_weight(
    *,
    rows: int,
    role_bucket: str,
    minute_volatility: float,
    recent_absence_days: float | None,
) -> float:
    if rows >= 400:
        weight = 0.42
    elif rows >= 150:
        weight = 0.32
    else:
        weight = 0.22
    if role_bucket in {"bench", "fringe"}:
        weight -= 0.06
    if minute_volatility >= 8.0:
        weight -= 0.05
    if recent_absence_days is not None and recent_absence_days >= 7:
        weight -= 0.08
    return max(0.12, min(0.50, weight))


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


def _market_weight(rows: int, config: ModelTuningConfig | None = None) -> float:
    tuning = config or DEFAULT_TUNING_CONFIG
    if rows >= 500:
        return _scaled_market_weight(0.18, tuning.market_weight_scale)
    if rows >= 150:
        return _scaled_market_weight(0.13, tuning.market_weight_scale)
    return _scaled_market_weight(0.09, tuning.market_weight_scale)


def _player_market_weight(
    sample_count: int,
    avg_minutes: float,
    config: ModelTuningConfig | None = None,
) -> float:
    tuning = config or DEFAULT_TUNING_CONFIG
    if sample_count < 4:
        return _scaled_market_weight(0.55, tuning.player_weight_scale)
    if sample_count < 7:
        return _scaled_market_weight(0.42, tuning.player_weight_scale)
    if avg_minutes < 16:
        return _scaled_market_weight(0.34, tuning.player_weight_scale)
    if sample_count < 10:
        return _scaled_market_weight(0.26, tuning.player_weight_scale)
    return _scaled_market_weight(0.18, tuning.player_weight_scale)


def _residual_market_weight(
    rows: int,
    sample_count: int,
    avg_minutes: float,
    config: ModelTuningConfig | None = None,
) -> float:
    tuning = config or DEFAULT_TUNING_CONFIG
    base = 0.12
    if rows >= 60:
        base = 0.17
    if rows >= 120:
        base = 0.22
    if rows >= 220:
        base = 0.28
    player_floor = 0.06 if sample_count < 8 or avg_minutes < 20.0 else 0.09 if sample_count < 15 else 0.12
    return _scaled_market_weight(max(base, player_floor), tuning.market_weight_scale)


def _scaled_market_weight(weight: float, scale: float) -> float:
    return max(0.05, min(0.85, weight * scale))


def _scaled_low_guard(base: float, scale: float) -> float:
    return max(0.55, min(0.95, 1.0 - ((1.0 - base) * scale)))


def _scaled_high_guard(base: float, scale: float) -> float:
    return max(1.05, min(1.6, 1.0 + ((base - 1.0) * scale)))


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


def _model_fingerprint(conn: sqlite3.Connection) -> str:
    tables = [
        "player_game_stats",
        "games",
        "injuries",
        "manual_adjustments",
        "player_team_history",
        "team_game_results",
        "players",
    ]
    parts: list[str] = []
    for table in tables:
        row = conn.execute(
            f"SELECT COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id FROM {table}"
        ).fetchone()
        parts.append(f"{table}:{int(row['row_count'] or 0)}:{int(row['max_id'] or 0)}")
    return hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]


def _config_fingerprint(config: ModelTuningConfig) -> str:
    payload = json.dumps(config.to_dict(), sort_keys=True, separators=(",", ":"))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]


def _model_cache_key(
    conn: sqlite3.Connection,
    db_path: str,
    name: str,
    config: ModelTuningConfig,
    *,
    kind: str,
) -> str:
    db_marker = hashlib.sha1(str(db_path).encode("utf-8")).hexdigest()[:12]
    return f"{MODEL_CACHE_PREFIX}-{kind}-{name}-{db_marker}-{_model_fingerprint(conn)}-{_config_fingerprint(config)}.json"


def _load_cached_model(cache_key: str) -> RidgeModel | None:
    from .cache import read_json_cache

    payload = read_json_cache(cache_key)
    if not isinstance(payload, dict):
        return None
    model = payload.get("model")
    if not isinstance(model, dict):
        return None
    try:
        return RidgeModel(
            market=str(model["market"]),
            rows=int(model["rows"]),
            intercept=float(model["intercept"]),
            coefficients=[float(value) for value in model["coefficients"]],
            feature_means=[float(value) for value in model["feature_means"]],
            feature_scales=[float(value) for value in model["feature_scales"]],
        )
    except (KeyError, TypeError, ValueError):
        return None


def _store_cached_model(cache_key: str, model: RidgeModel, config: ModelTuningConfig) -> None:
    from .cache import write_json_cache

    try:
        write_json_cache(
            cache_key,
            {
                "model_version": MODEL_VERSION,
                "config": config.to_dict(),
                "model": asdict(model),
            },
        )
    except OSError:
        # Cache is a performance optimization; keep recalc working if the
        # configured cache directory is unavailable or read-only.
        return


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
            "game_date": str(row["game_date"]),
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


def _player_archetype_profile(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    reference_game_date: str | None,
    exclude_game_id: int | None,
    fallback_rotation_role: str,
) -> PlayerArchetypeProfile:
    params: list[object] = [player_id]
    date_filter = ""
    if reference_game_date:
        date_filter = " AND g.game_date < ?"
        params.append(reference_game_date)
    exclude_filter = ""
    if exclude_game_id is not None:
        exclude_filter = " AND s.game_id <> ?"
        params.append(exclude_game_id)
    rows = conn.execute(
        """
        SELECT
            s.minutes,
            s.points,
            s.rebounds,
            s.assists,
            s.threes,
            s.steals,
            s.blocks,
            p.position,
            p.rotation_role
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN players p ON p.id = s.player_id
        WHERE s.player_id = ?
        """
        + date_filter
        + exclude_filter
        + """
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT 10
        """,
        tuple(params),
    ).fetchall()
    if not rows:
        return PlayerArchetypeProfile(False, False, False, False, False)

    sample_count = len(rows)
    avg_minutes = sum(float(row["minutes"] or 0.0) for row in rows) / sample_count
    avg_points = sum(float(row["points"] or 0.0) for row in rows) / sample_count
    avg_rebounds = sum(float(row["rebounds"] or 0.0) for row in rows) / sample_count
    avg_assists = sum(float(row["assists"] or 0.0) for row in rows) / sample_count
    avg_threes = sum(float(row["threes"] or 0.0) for row in rows) / sample_count
    avg_stocks = sum(float((row["steals"] or 0.0) + (row["blocks"] or 0.0)) for row in rows) / sample_count
    position = str(rows[0]["position"] or "").upper()
    rotation_role = str(rows[0]["rotation_role"] or fallback_rotation_role or "").lower()

    usage_scorer = avg_points >= 16.5 and avg_assists <= 6.5 and avg_minutes >= 22.0
    rebound_big = avg_rebounds >= 7.5 and position in {"F", "C"} and avg_minutes >= 20.0
    assist_guard = avg_assists >= 5.5 and position in {"G", "PG", "SG"} and avg_minutes >= 22.0
    bench_gunner = rotation_role in {"bench", "rotation"} and avg_points >= 11.5 and avg_threes >= 1.5 and avg_minutes <= 26.0
    stocks_specialist = avg_stocks >= 2.0 and avg_minutes >= 18.0

    return PlayerArchetypeProfile(
        usage_scorer=usage_scorer,
        rebound_big=rebound_big,
        assist_guard=assist_guard,
        bench_gunner=bench_gunner,
        stocks_specialist=stocks_specialist,
    )


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
    as_of_date: str | None = None,
) -> dict[str, float | str | bool]:
    status = _latest_player_injury_status(conn, player_id, as_of_date=as_of_date)
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

    injury_cutoff_clause = "AND DATE(i.captured_at) <= DATE(?)" if as_of_date else ""
    injury_cutoff_params: tuple[object, ...] = (as_of_date,) if as_of_date else ()
    teammate_rows = conn.execute(
        f"""
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
                          {"AND DATE(g.game_date) <= DATE(?)" if as_of_date else ""}
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
          {injury_cutoff_clause}
          AND i.captured_at = (
              SELECT MAX(i2.captured_at)
              FROM injuries i2
              WHERE i2.player_id = i.player_id
                {"AND DATE(i2.captured_at) <= DATE(?)" if as_of_date else ""}
          )
        """,
        (*injury_cutoff_params, team_id, player_id, *injury_cutoff_params, *injury_cutoff_params),
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


def _latest_player_injury_status(conn: sqlite3.Connection, player_id: int, as_of_date: str | None = None) -> str:
    if as_of_date:
        row = conn.execute(
            """
            SELECT lower(trim(status)) AS status
            FROM injuries
            WHERE player_id = ?
              AND DATE(captured_at) <= DATE(?)
            ORDER BY captured_at DESC
            LIMIT 1
            """,
            (player_id, as_of_date),
        ).fetchone()
    else:
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
