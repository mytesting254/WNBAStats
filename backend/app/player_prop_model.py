from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
from dataclasses import asdict, dataclass
from datetime import date, datetime
from functools import lru_cache

try:
    import numpy as np
except ImportError:  # pragma: no cover - fallback remains exercised without numpy installed
    np = None

from .odds import american_to_implied_probability
from .team_form_regime import build_team_form_regime
from .timezone_utils import APP_TIMEZONE


MODEL_VERSION = "adaptive-context-v12-team-form-regime"
COMPONENT_MODEL_VERSION = "component-pregame-v2"
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
    "games_since_joining_team",
    "new_team_minutes_trend",
    "teammate_minutes_redistribution",
    "rotation_stability",
    "recent_team_minute_share",
    "recent_minute_rank",
    "recent_position_minute_share",
    "same_position_unavailable_minutes",
    "same_position_key_out_count",
    "same_position_opportunity_persistence",
    "same_position_competition_minutes",
    "recent_opponent_factor",
    "team_off_rating_factor",
    "opponent_def_rating_factor",
    "recent_opponent_def_rating_factor",
    "matchup_net_rating_diff",
    "team_off_form_delta",
    "opponent_off_form_delta",
    "team_def_form_delta",
    "opponent_def_form_delta",
    "team_pace_form_delta",
    "opponent_pace_form_delta",
    "team_fga_form_delta",
    "opponent_fga_form_delta",
    "team_fga_allowed_form_delta",
    "opponent_fga_allowed_form_delta",
    "team_turnover_rate_form_delta",
    "opponent_turnover_rate_form_delta",
    "team_forced_turnover_rate_form_delta",
    "opponent_forced_turnover_rate_form_delta",
    "team_off_form_volatility",
    "opponent_off_form_volatility",
    "team_def_form_volatility",
    "opponent_def_form_volatility",
    "off_form_delta_diff",
    "def_form_delta_diff",
    "pace_form_delta_diff",
    "fga_form_delta_diff",
    "fga_allowed_form_delta_diff",
    "turnover_rate_form_delta_diff",
    "forced_turnover_rate_form_delta_diff",
    "team_hot_offense_flag",
    "opponent_hot_offense_flag",
    "team_slump_offense_flag",
    "opponent_slump_offense_flag",
    "team_hot_defense_flag",
    "opponent_hot_defense_flag",
    "team_slump_defense_flag",
    "opponent_slump_defense_flag",
]
FEATURE_INDEX = {name: idx for idx, name in enumerate(FEATURE_NAMES)}
MINUTES_FEATURE_NAMES = [
    "ewma_minutes",
    "recent_minutes_avg",
    "last_10_minutes_avg",
    "minutes_trend",
    "minute_volatility",
    "recent_change_ratio",
    "recent_vs_ewma_gap",
    "absence_return_flag",
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
    "games_since_joining_team",
    "new_team_minutes_trend",
    "teammate_minutes_redistribution",
    "rotation_stability",
    "recent_team_minute_share",
    "recent_minute_rank",
    "recent_position_minute_share",
    "same_position_unavailable_minutes",
    "same_position_key_out_count",
    "same_position_opportunity_persistence",
    "same_position_competition_minutes",
    "same_position_opportunity_trend",
    "same_position_competition_trend",
    "same_position_returner_pressure",
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
MARKET_WINSORIZATION_SCALES = {
    "points": 1.75,
    "rebounds": 1.6,
    "assists": 1.6,
    "points_rebounds": 1.8,
    "points_assists": 1.8,
    "rebounds_assists": 1.7,
    "points_rebounds_assists": 1.9,
    "threes": 1.45,
    "steals": 1.35,
    "blocks": 1.35,
    "blocks_steals": 1.4,
}

MARKET_CONTEXT_FEATURE_POLICY = {
    "points": {
        "lineup": True,
        "opportunity": True,
        "recent_opponent": True,
    },
    "points_rebounds": {
        "lineup": True,
        "opportunity": True,
        "recent_opponent": True,
    },
    "threes": {
        "lineup": False,
        "opportunity": False,
        "recent_opponent": True,
    },
}
RESIDUAL_PROMOTION_MIN_ROWS = 100
RESIDUAL_PROMOTION_MIN_MAE_IMPROVEMENT = 0.0
RESIDUAL_PROMOTION_MIN_RMSE_IMPROVEMENT = 0.0
DEFAULT_MARKET_OVERLAY_POLICY = {
    "mode": "component_only",
    "blend_weight": 0.0,
    "min_rows": 1500,
    "min_mae_improvement": 0.02,
    "min_rmse_improvement": 0.0,
    "max_abs_bias": 0.2,
    "recent_transfer_min_rows": 100,
    "recent_transfer_min_mae_improvement": 0.0,
    "recent_transfer_blend_weight": 0.15,
}
MARKET_OVERLAY_POLICY = {
    "points": {
        "mode": "blend",
        "blend_weight": 0.20,
        "min_rows": 2000,
        "min_mae_improvement": 0.10,
        "min_rmse_improvement": 0.05,
        "max_abs_bias": 0.15,
        "recent_transfer_min_rows": 100,
        "recent_transfer_min_mae_improvement": 0.0,
        "recent_transfer_blend_weight": 0.10,
    },
    "rebounds": {
        "mode": "blend",
        "blend_weight": 0.20,
        "min_rows": 2000,
        "min_mae_improvement": 0.05,
        "min_rmse_improvement": 0.02,
        "max_abs_bias": 0.15,
        "recent_transfer_blend_weight": 0.10,
    },
    "assists": {
        "mode": "blend",
        "blend_weight": 0.20,
        "min_rows": 2000,
        "min_mae_improvement": 0.05,
        "min_rmse_improvement": 0.02,
        "max_abs_bias": 0.15,
        "recent_transfer_blend_weight": 0.10,
    },
    "points_rebounds": {
        "mode": "component_only",
        "min_rows": 1800,
        "min_mae_improvement": 0.12,
        "min_rmse_improvement": 0.06,
        "max_abs_bias": 0.15,
        "recent_transfer_min_rows": 100,
        "recent_transfer_min_mae_improvement": 0.0,
        "recent_transfer_blend_weight": 0.08,
    },
    "points_assists": {
        "mode": "component_only",
        "min_rows": 1800,
        "min_mae_improvement": 0.12,
        "min_rmse_improvement": 0.06,
        "max_abs_bias": 0.15,
        "recent_transfer_min_rows": 100,
        "recent_transfer_min_mae_improvement": 0.0,
        "recent_transfer_blend_weight": 0.08,
    },
    "rebounds_assists": {
        "mode": "component_only",
        "min_rows": 1800,
        "min_mae_improvement": 0.08,
        "min_rmse_improvement": 0.04,
        "max_abs_bias": 0.15,
        "recent_transfer_blend_weight": 0.08,
    },
    "points_rebounds_assists": {
        "mode": "blend",
        "blend_weight": 0.10,
        "min_rows": 1800,
        "min_mae_improvement": 0.15,
        "min_rmse_improvement": 0.08,
        "max_abs_bias": 0.15,
        "recent_transfer_min_rows": 100,
        "recent_transfer_min_mae_improvement": 0.0,
        "recent_transfer_blend_weight": 0.05,
    },
    "threes": {
        "mode": "component_only",
        "min_rows": 1500,
        "min_mae_improvement": 0.04,
        "min_rmse_improvement": 0.0,
        "max_abs_bias": 0.10,
        "recent_transfer_blend_weight": 0.08,
    },
    "steals": {
        "mode": "blend",
        "blend_weight": 0.10,
        "min_rows": 1500,
        "min_mae_improvement": 0.02,
        "min_rmse_improvement": 0.0,
        "max_abs_bias": 0.08,
        "recent_transfer_blend_weight": 0.05,
    },
    "blocks": {
        "mode": "blend",
        "blend_weight": 0.10,
        "min_rows": 1500,
        "min_mae_improvement": 0.03,
        "min_rmse_improvement": 0.0,
        "max_abs_bias": 0.08,
        "recent_transfer_blend_weight": 0.05,
    },
    "blocks_steals": {
        "mode": "blend",
        "blend_weight": 0.10,
        "min_rows": 1500,
        "min_mae_improvement": 0.03,
        "min_rmse_improvement": 0.0,
        "max_abs_bias": 0.10,
        "recent_transfer_blend_weight": 0.05,
    },
}

@dataclass(frozen=True)
class ModelTuningConfig:
    ridge_penalty: float = 1.25
    market_weight_scale: float = 1.0
    player_weight_scale: float = 1.0
    stabilization_scale: float = 1.0
    recency_weight_scale: float = 0.0

    def to_dict(self) -> dict[str, float]:
        return asdict(self)


DEFAULT_TUNING_CONFIG = ModelTuningConfig()
TRAINING_START_DATE_ENV = "WNBA_TRAINING_START_DATE"

LOCAL_COMBO_MARKETS: dict[str, tuple[str, ...]] = {
    "points_rebounds_assists": ("points", "rebounds", "assists"),
}

_CONNECTION_MODEL_CACHE: dict[tuple[int, str, ModelTuningConfig], RidgeModel | None] = {}
_CONNECTION_RESIDUAL_MODEL_CACHE: dict[tuple[int, str, ModelTuningConfig], RidgeModel | None] = {}
_CONNECTION_MINUTES_MODEL_CACHE: dict[tuple[int, str | None, ModelTuningConfig], RidgeModel | None] = {}
_CONNECTION_GAME_TOTAL_MEAN_CACHE: dict[int, float] = {}
_CONNECTION_TRAINING_CACHE: dict[tuple[int, str], dict[str, dict[tuple, object]]] = {}

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
class TrainingSample:
    features: list[float]
    target: float
    game_date: str
    season: str
    segment: str
    baseline: float
    source_prop_line_id: int | None = None
    source_player_id: int = 0
    source_game_id: int = 0
    is_recent_transfer: bool = False
    sample_count: int = 0
    avg_minutes: float = 0.0


@dataclass(frozen=True)
class FinalProjectionSample:
    features: list[float]
    target: float
    game_date: str
    season: str
    segment: str
    component_projection: float
    line: float | None
    over_odds: int | None
    under_odds: int | None
    sample_count: int
    avg_minutes: float
    source_prop_line_id: int | None = None
    source_player_id: int = 0
    source_game_id: int = 0


@dataclass(frozen=True)
class TrainingSampleDiagnostics:
    candidate_rows: int = 0
    included_rows: int = 0
    skipped_before_training_start: int = 0
    skipped_missing_history_window: int = 0
    skipped_incomplete_context: int = 0
    skipped_ambiguous_team_identity: int = 0
    skipped_missing_snapshot: int = 0
    skipped_market_curation: int = 0

    def to_dict(self) -> dict[str, int]:
        return {
            "candidate_rows": int(self.candidate_rows),
            "included_rows": int(self.included_rows),
            "skipped_before_training_start": int(self.skipped_before_training_start),
            "skipped_missing_history_window": int(self.skipped_missing_history_window),
            "skipped_incomplete_context": int(self.skipped_incomplete_context),
            "skipped_ambiguous_team_identity": int(self.skipped_ambiguous_team_identity),
            "skipped_missing_snapshot": int(self.skipped_missing_snapshot),
            "skipped_market_curation": int(self.skipped_market_curation),
        }


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
        else feature_snapshot(
            conn,
            player_id,
            market,
            game_id,
            allow_training=allow_training,
            runtime_cache=runtime_cache,
        )
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
    overlay_policy = _market_overlay_decision(conn, market)
    overlay_mode = str(overlay_policy.get("mode") or "component_only")
    overlay_note = str(overlay_policy.get("note") or "market policy defaulted to component")
    model_version = MODEL_VERSION
    if overlay_mode == "component_only":
        projection = snapshot.component_projection
        market_note = f"{overlay_note}; learned projection suppressed"
        model_version = COMPONENT_MODEL_VERSION
    elif overlay_mode == "blend":
        blend_weight = float(overlay_policy.get("blend_weight") or 0.0)
        projection = ((blend_weight * learned) + ((1.0 - blend_weight) * snapshot.component_projection))
        market_note = f"{overlay_note}; learned/component blend {blend_weight:.0%}"
    elif overlay_mode == "transfer_blend":
        blend_weight = float(overlay_policy.get("blend_weight") or 0.0)
        projection = ((blend_weight * learned) + ((1.0 - blend_weight) * snapshot.component_projection))
        market_note = f"{overlay_note}; reduced learned/component blend {blend_weight:.0%}"
    else:
        projection = learned
        market_note = f"{overlay_note}; no sportsbook line blend"

    if line is not None:
        if overlay_mode == "component_only":
            market_weight = _component_market_line_weight(
                market,
                sample_count,
                avg_minutes,
            )
            projection = ((1 - market_weight) * projection) + (market_weight * float(line))
            market_note = f"{market_note}; component line anchor {market_weight:.0%} at {float(line):.1f}"
        else:
            market_weight = _market_line_weight(
                market,
                model.rows,
                sample_count,
                avg_minutes,
                config=tuning,
            )
            projection = ((1 - market_weight) * learned) + (market_weight * float(line))
            residual_model = train_market_residual_model(conn, market, config=tuning, allow_training=allow_training)
            if residual_model is not None:
                residual_prediction = _predict(residual_model, snapshot.values)
                residual_projection = float(line) + residual_prediction
                residual_weight = _residual_market_weight(
                    market,
                    residual_model.rows,
                    sample_count,
                    avg_minutes,
                    config=tuning,
                )
                projection = ((1 - residual_weight) * projection) + (residual_weight * residual_projection)
                market_note = (
                    f"line blend {market_weight:.0%} at {float(line):.1f}; "
                    f"residual blend {residual_weight:.0%} ({residual_model.rows} settled rows)"
                )
        if over_odds is not None and under_odds is not None:
            over_implied = american_to_implied_probability(int(over_odds))
            under_implied = american_to_implied_probability(int(under_odds))
            no_vig_mid = (over_implied / max(over_implied + under_implied, 0.01)) - 0.5
            price_nudge_scale = 0.55 if overlay_mode == "component_only" else 1.0
            projection += no_vig_mid * _market_price_nudge(market) * price_nudge_scale
        market_note = (
            f"{market_note} (player sample {sample_count} games, {avg_minutes:.1f} avg minutes)"
        )

    if snapshot.hard_cap_zero:
        projection = 0.0
        market_note = f"{market_note}; player marked OUT"

    if overlay_mode == "component_only":
        reason = (
            f"{snapshot.reason} Learned model {MODEL_VERSION} projected {learned:.1f} from "
            f"{model.rows} historical rows, but live market policy held {market} on the "
            f"{COMPONENT_MODEL_VERSION} baseline; {market_note}; {stabilization_note}. "
            f"Final projection {projection:.1f}."
        )
    else:
        reason = (
            f"{snapshot.reason} Learned model {MODEL_VERSION} projected {learned:.1f} from "
            f"{model.rows} historical rows; {market_note}; {stabilization_note}. Final projection {projection:.1f}."
        )
    return round(projection, 2), reason, model_version


def _market_overlay_metrics(conn: sqlite3.Connection) -> dict[str, dict]:
    cache = _connection_training_cache_bucket(conn, "market_overlay_metrics")
    cache_key = ("latest", MODEL_VERSION, "walk_forward_segments")
    if cache_key in cache:
        return cache[cache_key]  # type: ignore[return-value]
    row = conn.execute(
        """
        SELECT metrics_json
        FROM model_runs
        WHERE model_version = ?
          AND run_type = 'walk_forward_segments'
          AND status = 'completed'
        ORDER BY started_at DESC, id DESC
        LIMIT 1
        """,
        (MODEL_VERSION,),
    ).fetchone()
    if row is None:
        cache[cache_key] = {}
        return {}
    metrics = json.loads(str(row["metrics_json"] if isinstance(row, sqlite3.Row) else row[0]))
    cache[cache_key] = metrics
    return metrics


def _market_overlay_decision(conn: sqlite3.Connection, market: str) -> dict[str, object]:
    policy = {**DEFAULT_MARKET_OVERLAY_POLICY, **MARKET_OVERLAY_POLICY.get(str(market), {})}
    metrics = _market_overlay_metrics(conn).get(str(market))
    if not isinstance(metrics, dict):
        return {"mode": "component_only", "note": "no saved walk-forward metrics"}
    rows = int(metrics.get("rows") or 0)
    mae_improvement = metrics.get("mae_improvement")
    rmse_improvement = metrics.get("rmse_improvement")
    bias = metrics.get("bias")
    if rows < int(policy["min_rows"]):
        return {"mode": "component_only", "note": f"walk-forward support {rows} below {int(policy['min_rows'])}"}
    if mae_improvement is None or float(mae_improvement) < float(policy["min_mae_improvement"]):
        return {
            "mode": "component_only",
            "note": f"walk-forward MAE improvement {float(mae_improvement or 0.0):+.3f} below {float(policy['min_mae_improvement']):+.3f}",
        }
    if rmse_improvement is None or float(rmse_improvement) < float(policy["min_rmse_improvement"]):
        return {
            "mode": "component_only",
            "note": f"walk-forward RMSE improvement {float(rmse_improvement or 0.0):+.3f} below {float(policy['min_rmse_improvement']):+.3f}",
        }
    if bias is not None and abs(float(bias)) > float(policy["max_abs_bias"]):
        return {
            "mode": "component_only",
            "note": f"walk-forward bias {float(bias):+.3f} exceeded {float(policy['max_abs_bias']):.3f}",
        }
    transfer_metrics = metrics.get("recent_transfer_evaluation")
    if isinstance(transfer_metrics, dict):
        transfer_rows = int(transfer_metrics.get("rows") or 0)
        transfer_mae_improvement = transfer_metrics.get("mae_improvement")
        if (
            transfer_rows >= int(policy["recent_transfer_min_rows"])
            and (
                transfer_mae_improvement is None
                or float(transfer_mae_improvement) < float(policy["recent_transfer_min_mae_improvement"])
            )
        ):
            if str(policy["mode"]) == "blend":
                return {
                    "mode": "transfer_blend",
                    "blend_weight": float(policy.get("recent_transfer_blend_weight") or 0.0),
                    "note": (
                        f"recent-transfer MAE improvement {float(transfer_mae_improvement or 0.0):+.3f} "
                        f"below {float(policy['recent_transfer_min_mae_improvement']):+.3f}"
                    ),
                }
            return {
                "mode": "component_only",
                "note": (
                    f"recent-transfer MAE improvement {float(transfer_mae_improvement or 0.0):+.3f} "
                    f"below {float(policy['recent_transfer_min_mae_improvement']):+.3f}"
                ),
            }
    mode = str(policy["mode"])
    if mode == "component_only":
        return {
            "mode": "component_only",
            "note": (
                f"market policy holds component baseline live ({rows} rows, MAE {float(mae_improvement):+.3f}, "
                f"RMSE {float(rmse_improvement):+.3f})"
            ),
        }
    if mode == "blend":
        return {
            "mode": "blend",
            "blend_weight": float(policy["blend_weight"]),
            "note": (
                f"walk-forward gate passed ({rows} rows, MAE {float(mae_improvement):+.3f}, "
                f"RMSE {float(rmse_improvement):+.3f})"
            ),
        }
    return {
        "mode": "full_learned",
        "note": (
            f"walk-forward gate passed ({rows} rows, MAE {float(mae_improvement):+.3f}, "
            f"RMSE {float(rmse_improvement):+.3f})"
        ),
    }


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


def _market_context_features(
    market: str,
    *,
    lineup_context: list[float] | None,
    opportunity_context: list[float] | None,
    recent_opponent_factor: float,
) -> list[float]:
    policy = MARKET_CONTEXT_FEATURE_POLICY.get(str(market), {})
    lineup_enabled = bool(policy.get("lineup"))
    opportunity_enabled = bool(policy.get("opportunity"))
    recent_opponent_enabled = bool(policy.get("recent_opponent"))
    normalized_lineup = list(lineup_context or [0.0, 0.5, 0.0])
    if len(normalized_lineup) < 3:
        normalized_lineup = (normalized_lineup + [0.0, 0.5, 0.0])[:3]
    normalized_opportunity = list(opportunity_context or [0.0, 0.0, 0.0, 0.0])
    if len(normalized_opportunity) < 4:
        normalized_opportunity = (normalized_opportunity + [0.0, 0.0, 0.0, 0.0])[:4]
    return [
        float(normalized_lineup[0]) if lineup_enabled else 0.0,
        float(normalized_lineup[1]) if lineup_enabled else 0.0,
        float(normalized_lineup[2]) if lineup_enabled else 0.0,
        float(normalized_opportunity[0]) if opportunity_enabled else 0.0,
        float(normalized_opportunity[1]) if opportunity_enabled else 0.0,
        float(normalized_opportunity[2]) if opportunity_enabled else 0.0,
        float(normalized_opportunity[3]) if opportunity_enabled else 0.0,
        float(recent_opponent_factor) if recent_opponent_enabled else 1.0,
    ]


def _team_form_regime_feature_values(
    team_form: dict[str, float] | None,
    opponent_form: dict[str, float] | None,
) -> list[float]:
    team_payload = dict(team_form or {})
    opponent_payload = dict(opponent_form or {})
    team_off_form_delta = float(team_payload.get("off_form_delta") or 0.0)
    opponent_off_form_delta = float(opponent_payload.get("off_form_delta") or 0.0)
    team_def_form_delta = float(team_payload.get("def_form_delta") or 0.0)
    opponent_def_form_delta = float(opponent_payload.get("def_form_delta") or 0.0)
    team_pace_form_delta = float(team_payload.get("pace_form_delta") or 0.0)
    opponent_pace_form_delta = float(opponent_payload.get("pace_form_delta") or 0.0)
    team_fga_form_delta = float(team_payload.get("fga_form_delta") or 0.0)
    opponent_fga_form_delta = float(opponent_payload.get("fga_form_delta") or 0.0)
    team_fga_allowed_form_delta = float(team_payload.get("fga_allowed_form_delta") or 0.0)
    opponent_fga_allowed_form_delta = float(opponent_payload.get("fga_allowed_form_delta") or 0.0)
    team_turnover_rate_form_delta = float(team_payload.get("turnover_rate_form_delta") or 0.0)
    opponent_turnover_rate_form_delta = float(opponent_payload.get("turnover_rate_form_delta") or 0.0)
    team_forced_turnover_rate_form_delta = float(team_payload.get("forced_turnover_rate_form_delta") or 0.0)
    opponent_forced_turnover_rate_form_delta = float(opponent_payload.get("forced_turnover_rate_form_delta") or 0.0)
    return [
        team_off_form_delta,
        opponent_off_form_delta,
        team_def_form_delta,
        opponent_def_form_delta,
        team_pace_form_delta,
        opponent_pace_form_delta,
        team_fga_form_delta,
        opponent_fga_form_delta,
        team_fga_allowed_form_delta,
        opponent_fga_allowed_form_delta,
        team_turnover_rate_form_delta,
        opponent_turnover_rate_form_delta,
        team_forced_turnover_rate_form_delta,
        opponent_forced_turnover_rate_form_delta,
        float(team_payload.get("off_form_volatility") or 0.0),
        float(opponent_payload.get("off_form_volatility") or 0.0),
        float(team_payload.get("def_form_volatility") or 0.0),
        float(opponent_payload.get("def_form_volatility") or 0.0),
        team_off_form_delta - opponent_off_form_delta,
        team_def_form_delta - opponent_def_form_delta,
        team_pace_form_delta - opponent_pace_form_delta,
        team_fga_form_delta - opponent_fga_form_delta,
        team_fga_allowed_form_delta - opponent_fga_allowed_form_delta,
        team_turnover_rate_form_delta - opponent_turnover_rate_form_delta,
        team_forced_turnover_rate_form_delta - opponent_forced_turnover_rate_form_delta,
        float(team_payload.get("hot_offense_flag") or 0.0),
        float(opponent_payload.get("hot_offense_flag") or 0.0),
        float(team_payload.get("slump_offense_flag") or 0.0),
        float(opponent_payload.get("slump_offense_flag") or 0.0),
        float(team_payload.get("hot_defense_flag") or 0.0),
        float(opponent_payload.get("hot_defense_flag") or 0.0),
        float(team_payload.get("slump_defense_flag") or 0.0),
        float(opponent_payload.get("slump_defense_flag") or 0.0),
    ]


def feature_snapshot(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    game_id: int,
    before_game_date: str | None = None,
    allow_training: bool = True,
    use_injury_context: bool = True,
    use_live_minutes_context: bool = True,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
) -> FeatureSnapshot:
    shared = _shared_projection_context(
        conn,
        player_id=player_id,
        game_id=game_id,
        before_game_date=before_game_date,
        allow_training=allow_training,
        use_injury_context=use_injury_context,
        use_live_minutes_context=use_live_minutes_context,
        runtime_cache=runtime_cache,
    )
    context = shared["context"]
    reference_game_date = shared["reference_game_date"]
    history_rows = shared["history_rows"]
    history = shared["history_dates"]
    if not history:
        return FeatureSnapshot([0.0 for _ in FEATURE_NAMES], 0.0, "No historical stats found; projection defaults to 0.")

    values = _winsorize_history_values([_market_value(row, market) for row in history_rows], market)
    minutes = [float(row["minutes"]) for row in history_rows]
    sample_weights = [
        _historical_blowout_weight(row, str(row["rotation_role"] or rotation_role))
        for row in history_rows
    ]
    rates = [value / max(minute, 1.0) for value, minute in zip(values, minutes)]
    last_5 = values[:5]
    last_10 = values
    recent_weights = sample_weights[: len(last_5)]
    recent_avg = sum(value * weight for value, weight in zip(last_5, recent_weights)) / max(sum(recent_weights), 1e-9)
    last_10_avg = sum(value * weight for value, weight in zip(last_10, sample_weights)) / max(sum(sample_weights), 1e-9)
    weighted_recent = _weighted_average([value * weight for value, weight in zip(values, sample_weights)])
    weighted_rate = _weighted_average([value * weight for value, weight in zip(rates, sample_weights)])
    ewma_value = _ewma_newest_first(values, alpha=0.42)
    ewma_minutes = _ewma_newest_first(minutes, alpha=0.38)
    minutes_trend = _recent_trend(minutes)
    value_volatility = _ewma_volatility_newest_first(values, ewma_value, market)
    consistency_score = _consistency_score(ewma_value, value_volatility, market)

    rotation_role = str(shared["rotation_role"])
    minute_volatility = float(shared["minute_volatility"])
    blowout = shared["blowout"]
    injury = shared["injury"]
    projected_minutes = float(shared["projected_minutes"])
    minutes_note = str(shared["minutes_note"])
    rate_projection = weighted_rate * projected_minutes
    component_base = _adaptive_component_projection(
        weighted_recent=weighted_recent,
        ewma_value=ewma_value,
        recent_avg=recent_avg,
        last_10_avg=last_10_avg,
        rate_projection=rate_projection,
        consistency_score=consistency_score,
    )
    component_base = _market_specific_component_projection(
        market=market,
        component_base=component_base,
        rate_projection=rate_projection,
        last_10_avg=last_10_avg,
        consistency_score=consistency_score,
        value_volatility=value_volatility,
    )

    pace_factor = _cached_pace_factor(conn, context["team_id"], context["opponent_id"]) if context else 1.0
    opponent_factor = _cached_opponent_factor(conn, context["opponent_id"], market) if context else 1.0
    recent_opponent_factor = _cached_recent_opponent_factor(conn, context["opponent_id"], market) if context else 1.0
    team_rating_context = _cached_team_rating_context(conn, int(context["team_id"]), reference_game_date) if context else None
    opponent_rating_context = _cached_team_rating_context(conn, int(context["opponent_id"]), reference_game_date) if context else None
    league_rating_context = _cached_league_rating_context(conn, reference_game_date) if context else None
    team_form = (
        build_team_form_regime(
            conn,
            team_id=int(context["team_id"]),
            before_game_date=reference_game_date,
            runtime_cache=runtime_cache,
        )
        if context
        else None
    )
    opponent_form = (
        build_team_form_regime(
            conn,
            team_id=int(context["opponent_id"]),
            before_game_date=reference_game_date,
            runtime_cache=runtime_cache,
        )
        if context
        else None
    )
    team_form_features = _team_form_regime_feature_values(team_form, opponent_form)
    team_off_rating_factor = (
        _clamp(
            float(team_rating_context["off_rating"]) / max(float(league_rating_context["off_rating"]), 1.0),
            0.94,
            1.06,
        )
        if isinstance(team_rating_context, dict) and isinstance(league_rating_context, dict)
        else 1.0
    )
    opponent_def_rating_factor = (
        _clamp(
            max(float(league_rating_context["def_rating"]), 1.0) / max(float(opponent_rating_context["def_rating"]), 1.0),
            0.94,
            1.06,
        )
        if isinstance(opponent_rating_context, dict) and isinstance(league_rating_context, dict)
        else 1.0
    )
    recent_opponent_def_rating_factor = (
        _clamp(
            max(float(league_rating_context["recent_def_rating"]), 1.0) / max(float(opponent_rating_context["recent_def_rating"]), 1.0),
            0.93,
            1.07,
        )
        if isinstance(opponent_rating_context, dict) and isinstance(league_rating_context, dict)
        else 1.0
    )
    matchup_net_rating_diff = (
        float(team_rating_context["net_rating"]) - float(opponent_rating_context["net_rating"])
        if isinstance(team_rating_context, dict) and isinstance(opponent_rating_context, dict)
        else 0.0
    )
    rating_context_factor = _clamp(
        (0.45 * team_off_rating_factor)
        + (0.35 * opponent_def_rating_factor)
        + (0.20 * recent_opponent_def_rating_factor),
        0.92,
        1.08,
    )
    common_opponent_factor = _cached_common_opponent_factor(
        conn,
        player_id,
        market,
        context,
        before_game_date,
        baseline_avg=last_10_avg,
    ) if context else 1.0
    h2h_factor = _cached_h2h_factor(
        conn,
        player_id,
        market,
        context,
        before_game_date,
        baseline_avg=last_10_avg,
    ) if context else 1.0
    home_factor = 1.02 if context and context["is_home"] else 0.99
    rest_factor = _rest_factor(context["rest_days"]) if context else 1.0
    usage_multiplier = float(shared["usage_multiplier"])
    adjustment_note = str(shared["adjustment_note"])
    usage_multiplier *= injury["usage_multiplier"]
    archetype = shared["archetype"]
    team_transition = shared["team_transition"]
    lineup_context = list(shared.get("lineup_context") or [0.0, 0.5, 0.0])
    opportunity_context = list(shared.get("opportunity_context") or [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    market_context_features = _market_context_features(
        market,
        lineup_context=lineup_context,
        opportunity_context=opportunity_context,
        recent_opponent_factor=recent_opponent_factor,
    )

    component_projection = (
        component_base
        * pace_factor
        * opponent_factor
        * rating_context_factor
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
        *team_transition,
        *market_context_features,
        team_off_rating_factor,
        opponent_def_rating_factor,
        recent_opponent_def_rating_factor,
        matchup_net_rating_diff,
        *team_form_features,
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
        f" Team transition: {int(team_transition[0])} games with team, minutes trend {team_transition[1]:+.1f}, "
        f"teammate minutes shift {team_transition[2]:+.2f}, rotation stability {team_transition[3]:.2f}."
        f" Minutes projection: {minutes_note}."
        f" Lineup share {float(lineup_context[0]):.2f}, lineup rank {float(lineup_context[1]):.2f}, position share {float(lineup_context[2]):.2f}."
        f" Opportunity unavailable {float(opportunity_context[0]):.1f}, key outs {float(opportunity_context[1]):.1f}, persistence {float(opportunity_context[2]):.2f}, competition {float(opportunity_context[3]):.1f}."
        f" Recent opponent {recent_opponent_factor:.2f}. Ratings: team off {team_off_rating_factor:.2f}, opp def {opponent_def_rating_factor:.2f}, recent opp def {recent_opponent_def_rating_factor:.2f}, net diff {matchup_net_rating_diff:+.1f}."
        f" Team form: off {team_form_features[0]:+.1f}, def {team_form_features[2]:+.1f}, pace {team_form_features[4]:+.1f};"
        f" opponent form: off {team_form_features[1]:+.1f}, def {team_form_features[3]:+.1f}, pace {team_form_features[5]:+.1f}."
    )
    return FeatureSnapshot(
        features,
        component_projection,
        reason,
        injury_status=str(injury["status"]),
        hard_cap_zero=bool(injury["hard_cap_zero"]),
    )


def _shared_projection_context(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    game_id: int,
    before_game_date: str | None,
    allow_training: bool,
    use_injury_context: bool = True,
    use_live_minutes_context: bool = True,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
) -> dict[str, object]:
    cache = runtime_cache.setdefault("shared_projection_context", {}) if runtime_cache is not None else None
    cache_key = (
        int(player_id),
        int(game_id),
        str(before_game_date or ""),
        bool(allow_training),
        bool(use_injury_context),
        bool(use_live_minutes_context),
    )
    if cache is not None and cache_key in cache:
        return cache[cache_key]  # type: ignore[return-value]

    context = _game_context(conn, player_id, game_id)
    reference_game_date = before_game_date or context.get("game_date")
    history_rows = _player_recent_feature_rows(conn, player_id, reference_game_date, exclude_game_id=game_id)
    if not history_rows:
        shared = {
            "context": context,
            "reference_game_date": reference_game_date,
            "history_rows": [],
            "history_dates": [],
            "rotation_role": "starter",
            "minute_volatility": 0.0,
            "blowout": {"risk": "low", "probability": 0.0, "minutes_delta": 0.0, "factor": 1.0},
            "injury": {
                "status": "available",
                "availability_factor": 1.0,
                "usage_multiplier": 1.0,
                "minutes_delta": 0.0,
                "hard_cap_zero": False,
            },
            "projected_minutes": 0.0,
            "minutes_note": "No historical stats found.",
            "usage_multiplier": 1.0,
            "adjustment_note": "no manual adjustment",
            "archetype": PlayerArchetypeProfile(False, False, False, False, False),
        }
        if cache is not None:
            cache[cache_key] = shared
        return shared

    rotation_role = str(history_rows[0]["rotation_role"] or "starter")
    history_rows = _prune_extreme_blowout_history_rows(history_rows, rotation_role)
    minutes = [float(row["minutes"]) for row in history_rows]
    ewma_minutes = _ewma_newest_first(minutes, alpha=0.38)
    minutes_trend = _recent_trend(minutes)
    recent_minutes_avg = sum(minutes[:5]) / min(len(minutes), 5)
    last_10_minutes_avg = sum(minutes) / len(minutes)
    minute_volatility = _minute_volatility(minutes)
    lineup_context = (
        _minutes_lineup_context(
            conn,
            player_id=player_id,
            team_id=int(context["team_id"]),
            history_rows=history_rows,
        )
        if use_live_minutes_context
        else None
    )
    blowout = _blowout_adjustment(conn, context, rotation_role)
    if use_injury_context:
        injury = _injury_adjustment_for_prop(
            conn,
            player_id,
            int(context["team_id"]),
            rotation_role,
            as_of_date=reference_game_date,
        )
    else:
        injury = {
            "status": "available",
            "availability_factor": 1.0,
            "usage_multiplier": 1.0,
            "minutes_delta": 0.0,
            "hard_cap_zero": False,
        }
    projected_minutes, minutes_note = _project_minutes(
        conn if use_live_minutes_context else None,
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
        recent_absence_days=_recent_absence_days(
            [{"game_date": str(row["game_date"])} for row in history_rows],
            reference_game_date,
        ),
        team_transition=(
            _team_transition_features(
                conn,
                player_id=player_id,
                team_id=int(context["team_id"]),
                history_rows=history_rows,
            )
            if use_live_minutes_context
            else None
        ),
        lineup_context=lineup_context,
        opportunity_context=(
            _live_minutes_opportunity_context(
                conn,
                player_id=player_id,
                team_id=int(context["team_id"]),
                position=str(history_rows[0]["position"] or ""),
                as_of_date=reference_game_date,
            )
            if use_live_minutes_context
            else None
        ),
        before_game_date=before_game_date,
        allow_training=allow_training and use_live_minutes_context,
    )
    usage_multiplier, adjustment_note = _manual_adjustment(conn, player_id)
    team_transition = _team_transition_features(
        conn,
        player_id=player_id,
        team_id=int(context["team_id"]),
        history_rows=history_rows,
    )
    shared = {
        "context": context,
        "reference_game_date": reference_game_date,
        "history_rows": history_rows,
        "history_dates": [{"game_date": str(row["game_date"])} for row in history_rows],
        "rotation_role": rotation_role,
        "minute_volatility": minute_volatility,
        "blowout": blowout,
        "injury": injury,
        "projected_minutes": projected_minutes,
        "minutes_note": minutes_note,
        "usage_multiplier": usage_multiplier,
        "adjustment_note": adjustment_note,
        "archetype": _player_archetype_profile_from_rows(history_rows, fallback_rotation_role=rotation_role),
        "team_transition": team_transition,
        "lineup_context": lineup_context,
        "opportunity_context": (
            _live_minutes_opportunity_context(
                conn,
                player_id=player_id,
                team_id=int(context["team_id"]),
                position=str(history_rows[0]["position"] or ""),
                as_of_date=reference_game_date,
            )
            if use_live_minutes_context
            else None
        ),
    }
    if cache is not None:
        cache[cache_key] = shared
    return shared


@lru_cache(maxsize=32)
def _train_market_model_cached(db_path: str, market: str, config: ModelTuningConfig, training_start: str) -> RidgeModel | None:
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
    training_start = _training_start_date()
    if not isinstance(conn, sqlite3.Connection):
        key = (id(conn), market, tuning, training_start)
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
    return _train_market_model_cached(db_path, market, tuning, training_start)


@lru_cache(maxsize=32)
def _train_market_residual_model_cached(db_path: str, market: str, config: ModelTuningConfig, training_start: str) -> RidgeModel | None:
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
    training_start = _training_start_date()
    cache_market = f"residual:{market}"
    if not isinstance(conn, sqlite3.Connection):
        key = (id(conn), market, tuning, training_start)
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
    return _train_market_residual_model_cached(db_path, market, tuning, training_start)


def clear_model_cache() -> None:
    _train_market_model_cached.cache_clear()
    _CONNECTION_MODEL_CACHE.clear()
    _train_market_residual_model_cached.cache_clear()
    _CONNECTION_RESIDUAL_MODEL_CACHE.clear()
    _train_minutes_model_cached.cache_clear()
    _CONNECTION_MINUTES_MODEL_CACHE.clear()
    _CONNECTION_GAME_TOTAL_MEAN_CACHE.clear()
    _CONNECTION_TRAINING_CACHE.clear()


def _train_market_model_uncached(
    conn: sqlite3.Connection,
    market: str,
    config: ModelTuningConfig | None = None,
) -> RidgeModel | None:
    samples, _diagnostics = _training_samples(conn, market)
    model = _fit_model_from_samples(market, samples, config=config)
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
    samples, _diagnostics = _residual_training_samples(conn, market)
    tuning = config or DEFAULT_TUNING_CONFIG
    model = _fit_model_from_samples(f"residual:{market}", samples, config=tuning)
    if model is not None:
        evaluation = _evaluate_walk_forward_samples(f"residual:{market}", samples, config=tuning)
        if not _residual_model_passes_promotion_gate(evaluation):
            return None
        db_path = conn.execute("PRAGMA database_list").fetchone()["file"]
        cache_key = _model_cache_key(conn, db_path, f"residual:{market}", tuning, kind="residual")
        _store_cached_model(cache_key, model, tuning)
    return model


def _residual_model_passes_promotion_gate(metrics: dict[str, object] | None) -> bool:
    if not metrics:
        return False
    rows = int(metrics.get("rows") or 0)
    mae_improvement = metrics.get("mae_improvement")
    rmse_improvement = metrics.get("rmse_improvement")
    if rows < RESIDUAL_PROMOTION_MIN_ROWS:
        return False
    if mae_improvement is None or rmse_improvement is None:
        return False
    return (
        float(mae_improvement) > RESIDUAL_PROMOTION_MIN_MAE_IMPROVEMENT
        and float(rmse_improvement) >= RESIDUAL_PROMOTION_MIN_RMSE_IMPROVEMENT
    )


def evaluate_market_model(
    conn: sqlite3.Connection,
    market: str,
    config: ModelTuningConfig | None = None,
) -> dict:
    tuning = config or DEFAULT_TUNING_CONFIG
    samples, diagnostics = _training_samples(conn, market)
    if len(samples) < 20:
        return {
            "rows": 0,
            "mae": None,
            "rmse": None,
            "bias": None,
            "directional_accuracy": None,
            "training_sample_diagnostics": diagnostics.to_dict(),
        }
    metrics = _evaluate_walk_forward_samples(market, samples, config=tuning)
    metrics["training_sample_diagnostics"] = diagnostics.to_dict()
    metrics.update(_evaluate_final_projection_samples(conn, market, samples, config=tuning))
    transfer_samples = [sample for sample in samples if sample.is_recent_transfer]
    metrics["recent_transfer_evaluation"] = (
        _evaluate_walk_forward_samples(market, transfer_samples, config=tuning)
        if len(transfer_samples) >= 20
        else {"rows": len(transfer_samples), "mae": None, "status": "insufficient_transfer_samples"}
    )
    return metrics


def evaluate_market_residual_model(
    conn: sqlite3.Connection,
    market: str,
    config: ModelTuningConfig | None = None,
) -> dict:
    tuning = config or DEFAULT_TUNING_CONFIG
    samples, diagnostics = _residual_training_samples(conn, market)
    if len(samples) < 20:
        return {
            "residual_rows": 0,
            "residual_mae": None,
            "residual_rmse": None,
            "residual_bias": None,
            "residual_directional_accuracy": None,
            "residual_training_sample_diagnostics": diagnostics.to_dict(),
        }
    metrics = _evaluate_walk_forward_samples(f"residual:{market}", samples, config=tuning)
    return {
        "residual_rows": metrics["rows"],
        "residual_mae": metrics["mae"],
        "residual_rmse": metrics["rmse"],
        "residual_bias": metrics["bias"],
        "residual_directional_accuracy": metrics["directional_accuracy"],
        "residual_baseline_mae": metrics.get("baseline_mae"),
        "residual_baseline_rmse": metrics.get("baseline_rmse"),
        "residual_baseline_directional_accuracy": metrics.get("baseline_directional_accuracy"),
        "residual_mae_improvement": metrics.get("mae_improvement"),
        "residual_rmse_improvement": metrics.get("rmse_improvement"),
        "residual_directional_accuracy_improvement": metrics.get("directional_accuracy_improvement"),
        "residual_segment_count": metrics.get("segment_count"),
        "residual_skipped_segments": metrics.get("skipped_segments"),
        "residual_seasons": metrics.get("seasons"),
        "residual_segments": metrics.get("segments"),
        "residual_training_sample_diagnostics": diagnostics.to_dict(),
    }


def _fit_model_from_rows(
    market: str,
    rows: list[tuple[list[float], float]],
    config: ModelTuningConfig | None = None,
    weights: list[float] | None = None,
) -> RidgeModel | None:
    tuning = config or DEFAULT_TUNING_CONFIG
    if len(rows) < 20:
        return None
    xs = [row[0] for row in rows]
    ys = [row[1] for row in rows]
    means, scales, standardized = _standardize_feature_rows(xs, weights=weights)

    coefs = _ridge_regression(standardized, ys, penalty=tuning.ridge_penalty, weights=weights)
    return RidgeModel(
        market=market,
        rows=len(rows),
        intercept=coefs[0],
        coefficients=coefs[1:],
        feature_means=means,
        feature_scales=scales,
    )


def _fit_model_from_samples(
    market: str,
    samples: list[TrainingSample],
    config: ModelTuningConfig | None = None,
) -> RidgeModel | None:
    rows = [(sample.features, sample.target) for sample in samples]
    return _fit_model_from_rows(market, rows, config=config, weights=_training_sample_weights(samples, config=config))


def _training_sample_weights(
    samples: list[TrainingSample],
    *,
    config: ModelTuningConfig | None = None,
) -> list[float]:
    if not samples:
        return []
    tuning = config or DEFAULT_TUNING_CONFIG
    ordinals = [_training_sample_ordinal(sample) for sample in samples]
    min_ordinal = min(ordinals)
    span = max(max(ordinals) - min_ordinal, 1)
    weights = []
    for ordinal in ordinals:
        recency = (ordinal - min_ordinal) / span
        weights.append(1.0 + (recency * 1.5 * max(tuning.recency_weight_scale, 0.0)))
    return weights


def _training_sample_ordinal(sample: TrainingSample) -> int:
    try:
        return date.fromisoformat(sample.game_date).toordinal()
    except ValueError:
        return 0


def _standardize_feature_rows(
    xs: list[list[float]],
    *,
    weights: list[float] | None = None,
) -> tuple[list[float], list[float], list[list[float]]]:
    if np is not None:
        design = np.asarray(xs, dtype=float)
        if weights is not None:
            weight_array = np.asarray(weights, dtype=float)
            weight_array = weight_array / max(weight_array.sum(), 1e-9)
            means = np.average(design, axis=0, weights=weight_array)
            centered = design - means
            scales = np.sqrt(np.average(centered * centered, axis=0, weights=weight_array))
        else:
            means = design.mean(axis=0)
            if design.shape[0] > 1:
                scales = design.std(axis=0, ddof=1)
            else:
                scales = np.ones(design.shape[1], dtype=float)
        scales = np.where(scales < 1.0, 1.0, scales)
        standardized = (design - means) / scales
        return means.tolist(), scales.tolist(), standardized.tolist()

    if weights is not None:
        total_weight = max(sum(weights), 1e-9)
        normalized = [float(weight) / total_weight for weight in weights]
        means = [
            sum(feature_row[idx] * normalized[row_idx] for row_idx, feature_row in enumerate(xs))
            for idx in range(len(xs[0]))
        ]
    else:
        means = [sum(values) / len(values) for values in zip(*xs)]
    scales = []
    standardized = []
    for features in xs:
        standardized.append([])
        for idx, value in enumerate(features):
            if len(scales) <= idx:
                if weights is not None:
                    variance = sum(
                        normalized[row_idx] * ((item[idx] - means[idx]) ** 2)
                        for row_idx, item in enumerate(xs)
                    )
                else:
                    variance = sum((item[idx] - means[idx]) ** 2 for item in xs) / max(len(xs) - 1, 1)
                scales.append(max(variance ** 0.5, 1.0))
            standardized[-1].append((value - means[idx]) / scales[idx])
    return means, scales, standardized


def _build_training_samples_inline(conn: sqlite3.Connection, market: str) -> tuple[list[TrainingSample], TrainingSampleDiagnostics]:
    cache = _connection_training_cache_bucket(conn, "training_samples")
    cache_key = ("base", str(market))
    if cache_key in cache:
        return cache[cache_key]  # type: ignore[return-value]
    samples: list[TrainingSample] = []
    candidate_rows = 0
    skipped_before_training_start = 0
    skipped_missing_history_window = 0
    skipped_incomplete_context = 0
    skipped_ambiguous_team_identity = 0
    training_start = _training_start_date()
    players = conn.execute("SELECT id FROM players ORDER BY id").fetchall()
    for player in players:
        rows = _player_training_rows(conn, int(player["id"]))
        values = [_market_value(row, market) for row in rows]
        minutes = [float(row["minutes"]) for row in rows]
        for idx in range(5, len(rows)):
            candidate_rows += 1
            game_date = str(rows[idx]["game_date"])
            if _before_training_start(game_date, training_start):
                skipped_before_training_start += 1
                continue
            if not _has_unambiguous_historical_team(rows[idx]):
                skipped_ambiguous_team_identity += 1
                continue
            if not _has_complete_training_context(rows[idx]):
                skipped_incomplete_context += 1
                continue
            history = values[max(0, idx - 10):idx]
            minute_history = minutes[max(0, idx - 10):idx]
            recent_rows = rows[max(0, idx - 10):idx]
            if not _has_strong_training_history_window(history, minute_history):
                skipped_missing_history_window += 1
                continue
            previous_game_date = str(rows[idx - 1]["game_date"]) if idx > 0 else None
            features = _historical_training_features(
                conn,
                rows[idx],
                history,
                minute_history,
                market,
                previous_game_date,
                recent_rows=recent_rows,
                player_rows=rows,
                row_index=idx,
            )
            samples.append(
                TrainingSample(
                    source_prop_line_id=None,
                    source_player_id=int(rows[idx]["player_id"]),
                    source_game_id=int(rows[idx]["game_id"]),
                    features=features,
                    target=values[idx],
                    game_date=game_date,
                    season=game_date[:4],
                    segment=game_date[:7],
                    # Compare learned walk-forward results to the same
                    # component anchor that live inference builds on top of,
                    # not to a simpler last-10 average proxy.
                    baseline=float(features[FEATURE_NAMES.index("component_projection")]),
                    is_recent_transfer=_is_recent_transfer_training_row(rows, idx),
                    sample_count=len(recent_rows),
                    avg_minutes=(sum(minute_history) / len(minute_history)) if minute_history else 0.0,
                )
            )
    diagnostics = TrainingSampleDiagnostics(
        candidate_rows=candidate_rows,
        included_rows=len(samples),
        skipped_before_training_start=skipped_before_training_start,
        skipped_missing_history_window=skipped_missing_history_window,
        skipped_incomplete_context=skipped_incomplete_context,
        skipped_ambiguous_team_identity=skipped_ambiguous_team_identity,
    )
    cache[cache_key] = (samples, diagnostics)
    return samples, diagnostics


def _is_recent_transfer_training_row(rows: list[sqlite3.Row], row_index: int) -> bool:
    if row_index <= 0:
        return False
    current_team_id = _historical_row_team_id(rows[row_index])
    return _is_recent_transfer_team_history(reversed(rows[:row_index]), current_team_id)


def _is_recent_transfer_feature_rows(history_rows: list[sqlite3.Row], target_team_id: int) -> bool:
    return _is_recent_transfer_team_history(history_rows, target_team_id)


def _is_recent_transfer_team_history(rows: object, target_team_id: int) -> bool:
    games_with_current_team = 0
    prior_team_ids: set[int] = set()
    for row in rows:
        team_id = _historical_row_team_id(row)
        if team_id == target_team_id and not prior_team_ids:
            games_with_current_team += 1
            continue
        prior_team_ids.add(team_id)
    return 0 < games_with_current_team <= 12 and any(team_id != target_team_id for team_id in prior_team_ids)


def _training_samples(conn: sqlite3.Connection, market: str) -> tuple[list[TrainingSample], TrainingSampleDiagnostics]:
    from .player_prop_training_db import load_player_prop_training_samples

    return load_player_prop_training_samples(conn, market=market, sample_kind="raw", force_rebuild=False)


def _training_rows(conn: sqlite3.Connection, market: str) -> list[tuple[list[float], float]]:
    samples, _diagnostics = _training_samples(conn, market)
    return [(sample.features, sample.target) for sample in samples]


def _build_residual_training_samples_inline(conn: sqlite3.Connection, market: str) -> tuple[list[TrainingSample], TrainingSampleDiagnostics]:
    cache = _connection_training_cache_bucket(conn, "residual_training_samples")
    cache_key = ("residual", str(market))
    if cache_key in cache:
        return cache[cache_key]  # type: ignore[return-value]
    samples: list[TrainingSample] = []
    candidate_rows = 0
    skipped_before_training_start = 0
    skipped_missing_history_window = 0
    skipped_incomplete_context = 0
    skipped_ambiguous_team_identity = 0
    skipped_missing_snapshot = 0
    training_start = _training_start_date()
    rows = conn.execute(
        """
        SELECT
            pl.id AS prop_line_id,
            pl.player_id,
            pl.game_id,
            pl.market,
            pl.line,
            sp.actual_result,
            g.game_date,
            g.spread_home,
            g.game_total
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
        candidate_rows += 1
        game_date = str(row["game_date"])
        if _before_training_start(game_date, training_start):
            skipped_before_training_start += 1
            continue
        training_row = _player_training_row_for_game(conn, int(row["player_id"]), int(row["game_id"]))
        if training_row is None or not _has_unambiguous_historical_team(training_row):
            skipped_ambiguous_team_identity += 1
            continue
        if not _has_complete_training_context(row):
            skipped_incomplete_context += 1
            continue
        recent_rows = _player_recent_feature_rows(
            conn,
            int(row["player_id"]),
            str(row["game_date"]) if row["game_date"] is not None else None,
            exclude_game_id=int(row["game_id"]),
        )
        recent_values = [_market_value(prior_row, market) for prior_row in recent_rows]
        recent_minutes = [float(prior_row["minutes"]) for prior_row in recent_rows]
        if not _has_strong_training_history_window(recent_values, recent_minutes):
            skipped_missing_history_window += 1
            continue
        snapshot = feature_snapshot(
            conn,
            int(row["player_id"]),
            market,
            int(row["game_id"]),
            before_game_date=str(row["game_date"]) if row["game_date"] is not None else None,
            use_injury_context=False,
            use_live_minutes_context=False,
        )
        if not snapshot.values:
            skipped_missing_snapshot += 1
            continue
        target = float(row["actual_result"]) - float(row["line"])
        sample_count, avg_minutes = _player_sample_quality(conn, int(row["player_id"]), int(row["game_id"]))
        samples.append(
            TrainingSample(
                source_prop_line_id=int(row["prop_line_id"]),
                source_player_id=int(row["player_id"]),
                source_game_id=int(row["game_id"]),
                features=snapshot.values,
                target=target,
                game_date=game_date,
                season=game_date[:4],
                segment=game_date[:7],
                baseline=0.0,
                is_recent_transfer=_is_recent_transfer_feature_rows(
                    recent_rows,
                    _historical_row_team_id(training_row),
                ),
                sample_count=int(sample_count),
                avg_minutes=float(avg_minutes),
            )
        )
    diagnostics = TrainingSampleDiagnostics(
        candidate_rows=candidate_rows,
        included_rows=len(samples),
        skipped_before_training_start=skipped_before_training_start,
        skipped_missing_history_window=skipped_missing_history_window,
        skipped_incomplete_context=skipped_incomplete_context,
        skipped_ambiguous_team_identity=skipped_ambiguous_team_identity,
        skipped_missing_snapshot=skipped_missing_snapshot,
    )
    cache[cache_key] = (samples, diagnostics)
    return samples, diagnostics


def _residual_training_samples(conn: sqlite3.Connection, market: str) -> tuple[list[TrainingSample], TrainingSampleDiagnostics]:
    from .player_prop_training_db import load_player_prop_training_samples

    return load_player_prop_training_samples(conn, market=market, sample_kind="residual", force_rebuild=False)


def _residual_training_rows(conn: sqlite3.Connection, market: str) -> list[tuple[list[float], float]]:
    samples, _diagnostics = _residual_training_samples(conn, market)
    return [(sample.features, sample.target) for sample in samples]


def _build_final_projection_samples_inline(conn: sqlite3.Connection, market: str) -> tuple[list[FinalProjectionSample], TrainingSampleDiagnostics]:
    cache = _connection_training_cache_bucket(conn, "final_projection_samples")
    cache_key = ("final", str(market))
    if cache_key in cache:
        return cache[cache_key]  # type: ignore[return-value]

    samples: list[FinalProjectionSample] = []
    candidate_rows = 0
    skipped_before_training_start = 0
    skipped_missing_history_window = 0
    skipped_incomplete_context = 0
    skipped_ambiguous_team_identity = 0
    skipped_missing_snapshot = 0
    training_start = _training_start_date()
    rows = conn.execute(
        """
        SELECT
            pl.id AS prop_line_id,
            pl.player_id,
            pl.game_id,
            pl.market,
            pl.line,
            pl.over_odds,
            pl.under_odds,
            sp.actual_result,
            g.game_date,
            g.spread_home,
            g.game_total
        FROM settled_props sp
        JOIN prop_lines pl ON pl.id = sp.prop_line_id
        JOIN games g ON g.id = pl.game_id
        WHERE pl.market = ?
        ORDER BY g.game_date ASC, pl.id ASC
        """,
        (market,),
    ).fetchall()

    for row in rows:
        candidate_rows += 1
        game_date = str(row["game_date"])
        if _before_training_start(game_date, training_start):
            skipped_before_training_start += 1
            continue
        training_row = _player_training_row_for_game(conn, int(row["player_id"]), int(row["game_id"]))
        if training_row is None or not _has_unambiguous_historical_team(training_row):
            skipped_ambiguous_team_identity += 1
            continue
        if not _has_complete_training_context(row):
            skipped_incomplete_context += 1
            continue
        recent_rows = _player_recent_feature_rows(
            conn,
            int(row["player_id"]),
            game_date,
            exclude_game_id=int(row["game_id"]),
        )
        recent_values = [_market_value(prior_row, market) for prior_row in recent_rows]
        recent_minutes = [float(prior_row["minutes"]) for prior_row in recent_rows]
        if not _has_strong_training_history_window(recent_values, recent_minutes):
            skipped_missing_history_window += 1
            continue
        snapshot = feature_snapshot(
            conn,
            int(row["player_id"]),
            market,
            int(row["game_id"]),
            before_game_date=game_date,
            use_injury_context=False,
            use_live_minutes_context=False,
        )
        if not snapshot.values:
            skipped_missing_snapshot += 1
            continue
        sample_count, avg_minutes = _player_sample_quality(conn, int(row["player_id"]), int(row["game_id"]))
        samples.append(
            FinalProjectionSample(
                source_prop_line_id=int(row["prop_line_id"]) if "prop_line_id" in row.keys() and row["prop_line_id"] is not None else None,
                source_player_id=int(row["player_id"]),
                source_game_id=int(row["game_id"]),
                features=snapshot.values,
                target=float(row["actual_result"]),
                game_date=game_date,
                season=game_date[:4],
                segment=game_date[:7],
                component_projection=float(snapshot.component_projection),
                line=float(row["line"]) if row["line"] is not None else None,
                over_odds=int(row["over_odds"]) if row["over_odds"] is not None else None,
                under_odds=int(row["under_odds"]) if row["under_odds"] is not None else None,
                sample_count=int(sample_count),
                avg_minutes=float(avg_minutes),
            )
        )

    diagnostics = TrainingSampleDiagnostics(
        candidate_rows=candidate_rows,
        included_rows=len(samples),
        skipped_before_training_start=skipped_before_training_start,
        skipped_missing_history_window=skipped_missing_history_window,
        skipped_incomplete_context=skipped_incomplete_context,
        skipped_ambiguous_team_identity=skipped_ambiguous_team_identity,
        skipped_missing_snapshot=skipped_missing_snapshot,
    )
    cache[cache_key] = (samples, diagnostics)
    return samples, diagnostics


def _final_projection_samples(conn: sqlite3.Connection, market: str) -> tuple[list[FinalProjectionSample], TrainingSampleDiagnostics]:
    from .player_prop_training_db import load_player_prop_final_projection_samples

    return load_player_prop_final_projection_samples(conn, market=market, force_rebuild=False)


def _has_complete_training_context(row: sqlite3.Row) -> bool:
    spread_home = row["spread_home"] if "spread_home" in row.keys() else None
    game_total = row["game_total"] if "game_total" in row.keys() else None
    if spread_home is None:
        return False
    if game_total is None:
        return False
    try:
        return float(game_total) > 0
    except (TypeError, ValueError):
        return False


def _has_strong_training_history_window(history: list[float], minutes: list[float]) -> bool:
    if len(history) < 7 or len(minutes) < 7:
        return False
    recent_minutes = list(minutes[-7:])
    recent_active_games = sum(1 for value in recent_minutes if float(value) >= 8.0)
    return recent_active_games >= 4


def _evaluate_walk_forward_samples(
    market: str,
    samples: list[TrainingSample],
    *,
    config: ModelTuningConfig,
) -> dict:
    ordered = sorted(samples, key=lambda sample: (sample.segment, sample.game_date))
    if len(ordered) < 20:
        return {"rows": 0, "mae": None, "rmse": None, "bias": None, "directional_accuracy": None}

    grouped_segments: dict[str, list[TrainingSample]] = {}
    for sample in ordered:
        grouped_segments.setdefault(sample.segment, []).append(sample)

    history_samples: list[TrainingSample] = []
    evaluated_segments: list[dict[str, object]] = []
    season_rollup: dict[str, dict[str, float | int]] = {}
    errors: list[float] = []
    squared_errors: list[float] = []
    absolute_errors: list[float] = []
    baseline_errors: list[float] = []
    baseline_squared_errors: list[float] = []
    baseline_absolute_errors: list[float] = []
    direction_hits = 0
    skipped_segments = 0

    for segment, segment_samples in grouped_segments.items():
        if len(history_samples) < 20:
            history_samples.extend(segment_samples)
            skipped_segments += 1
            continue
        model = _fit_model_from_samples(market, history_samples, config=config)
        if model is None:
            history_samples.extend(segment_samples)
            skipped_segments += 1
            continue
        segment_errors: list[float] = []
        segment_squared_errors: list[float] = []
        segment_absolute_errors: list[float] = []
        segment_baseline_errors: list[float] = []
        segment_baseline_squared_errors: list[float] = []
        segment_baseline_absolute_errors: list[float] = []
        segment_direction_hits = 0
        season = segment_samples[0].season
        for sample in segment_samples:
            prediction = max(0.0, _predict(model, sample.features)) if not market.startswith("residual:") else _predict(model, sample.features)
            error = prediction - sample.target
            baseline_error = sample.baseline - sample.target
            errors.append(error)
            squared_errors.append(error * error)
            absolute_errors.append(abs(error))
            baseline_errors.append(baseline_error)
            baseline_squared_errors.append(baseline_error * baseline_error)
            baseline_absolute_errors.append(abs(baseline_error))
            segment_errors.append(error)
            segment_squared_errors.append(error * error)
            segment_absolute_errors.append(abs(error))
            segment_baseline_errors.append(baseline_error)
            segment_baseline_squared_errors.append(baseline_error * baseline_error)
            segment_baseline_absolute_errors.append(abs(baseline_error))
            if (prediction >= sample.baseline and sample.target >= sample.baseline) or (prediction < sample.baseline and sample.target < sample.baseline):
                direction_hits += 1
                segment_direction_hits += 1
        row_count = len(segment_samples)
        segment_metric = {
            "segment": segment,
            "season": season,
            "rows": row_count,
            "mae": round(sum(segment_absolute_errors) / row_count, 3),
            "rmse": round(math.sqrt(sum(segment_squared_errors) / row_count), 3),
            "bias": round(sum(segment_errors) / row_count, 3),
            "directional_accuracy": round(segment_direction_hits / row_count, 3),
            "baseline_mae": round(sum(segment_baseline_absolute_errors) / row_count, 3),
            "baseline_rmse": round(math.sqrt(sum(segment_baseline_squared_errors) / row_count), 3),
            "baseline_bias": round(sum(segment_baseline_errors) / row_count, 3),
            "baseline_directional_accuracy": None,
        }
        segment_metric["mae_improvement"] = round(float(segment_metric["baseline_mae"]) - float(segment_metric["mae"]), 3)
        segment_metric["rmse_improvement"] = round(float(segment_metric["baseline_rmse"]) - float(segment_metric["rmse"]), 3)
        segment_metric["directional_accuracy_improvement"] = None
        evaluated_segments.append(segment_metric)
        season_bucket = season_rollup.setdefault(
            season,
            {
                "rows": 0,
                "mae_sum": 0.0,
                "rmse_sum": 0.0,
                "bias_sum": 0.0,
                "direction_sum": 0.0,
                "baseline_mae_sum": 0.0,
                "baseline_rmse_sum": 0.0,
                "baseline_bias_sum": 0.0,
            },
        )
        season_bucket["rows"] = int(season_bucket["rows"]) + row_count
        season_bucket["mae_sum"] = float(season_bucket["mae_sum"]) + float(segment_metric["mae"]) * row_count
        season_bucket["rmse_sum"] = float(season_bucket["rmse_sum"]) + float(segment_metric["rmse"]) * row_count
        season_bucket["bias_sum"] = float(season_bucket["bias_sum"]) + float(segment_metric["bias"]) * row_count
        season_bucket["direction_sum"] = float(season_bucket["direction_sum"]) + float(segment_metric["directional_accuracy"]) * row_count
        season_bucket["baseline_mae_sum"] = float(season_bucket["baseline_mae_sum"]) + float(segment_metric["baseline_mae"]) * row_count
        season_bucket["baseline_rmse_sum"] = float(season_bucket["baseline_rmse_sum"]) + float(segment_metric["baseline_rmse"]) * row_count
        season_bucket["baseline_bias_sum"] = float(season_bucket["baseline_bias_sum"]) + float(segment_metric["baseline_bias"]) * row_count
        history_samples.extend(segment_samples)

    row_count = len(errors)
    if row_count == 0:
        return _evaluate_holdout_samples(market, ordered, config=config)

    seasons = []
    for season, bucket in sorted(season_rollup.items()):
        season_rows = int(bucket["rows"])
        season_metric = {
            "season": season,
            "rows": season_rows,
            "mae": round(float(bucket["mae_sum"]) / season_rows, 3),
            "rmse": round(float(bucket["rmse_sum"]) / season_rows, 3),
            "bias": round(float(bucket["bias_sum"]) / season_rows, 3),
            "directional_accuracy": round(float(bucket["direction_sum"]) / season_rows, 3),
            "baseline_mae": round(float(bucket["baseline_mae_sum"]) / season_rows, 3),
            "baseline_rmse": round(float(bucket["baseline_rmse_sum"]) / season_rows, 3),
            "baseline_bias": round(float(bucket["baseline_bias_sum"]) / season_rows, 3),
            "baseline_directional_accuracy": None,
        }
        season_metric["mae_improvement"] = round(float(season_metric["baseline_mae"]) - float(season_metric["mae"]), 3)
        season_metric["rmse_improvement"] = round(float(season_metric["baseline_rmse"]) - float(season_metric["rmse"]), 3)
        season_metric["directional_accuracy_improvement"] = None
        seasons.append(season_metric)

    baseline_mae = sum(baseline_absolute_errors) / row_count
    baseline_rmse = math.sqrt(sum(baseline_squared_errors) / row_count)
    baseline_bias = sum(baseline_errors) / row_count
    mae = sum(absolute_errors) / row_count
    rmse = math.sqrt(sum(squared_errors) / row_count)
    directional_accuracy = direction_hits / row_count
    return {
        "rows": row_count,
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "bias": round(sum(errors) / row_count, 3),
        "directional_accuracy": round(directional_accuracy, 3),
        "baseline_mae": round(baseline_mae, 3),
        "baseline_rmse": round(baseline_rmse, 3),
        "baseline_bias": round(baseline_bias, 3),
        "baseline_directional_accuracy": None,
        "mae_improvement": round(baseline_mae - mae, 3),
        "rmse_improvement": round(baseline_rmse - rmse, 3),
        "directional_accuracy_improvement": None,
        "segment_count": len(evaluated_segments),
        "skipped_segments": skipped_segments,
        "segments": evaluated_segments,
        "seasons": seasons,
    }


def _evaluate_holdout_samples(
    market: str,
    samples: list[TrainingSample],
    *,
    config: ModelTuningConfig,
) -> dict:
    if len(samples) < 20:
        return {"rows": 0, "mae": None, "rmse": None, "bias": None, "directional_accuracy": None}
    split = max(int(len(samples) * 0.8), 10)
    train_samples = samples[:split]
    test_samples = samples[split:]
    model = _fit_model_from_samples(market, train_samples, config=config)
    if model is None or not test_samples:
        return {"rows": 0, "mae": None, "rmse": None, "bias": None, "directional_accuracy": None}

    errors: list[float] = []
    absolute_errors: list[float] = []
    squared_errors: list[float] = []
    baseline_errors: list[float] = []
    baseline_absolute_errors: list[float] = []
    baseline_squared_errors: list[float] = []
    direction_hits = 0
    for sample in test_samples:
        prediction = max(0.0, _predict(model, sample.features)) if not market.startswith("residual:") else _predict(model, sample.features)
        error = prediction - sample.target
        baseline_error = sample.baseline - sample.target
        errors.append(error)
        absolute_errors.append(abs(error))
        squared_errors.append(error * error)
        baseline_errors.append(baseline_error)
        baseline_absolute_errors.append(abs(baseline_error))
        baseline_squared_errors.append(baseline_error * baseline_error)
        if (prediction >= sample.baseline and sample.target >= sample.baseline) or (prediction < sample.baseline and sample.target < sample.baseline):
            direction_hits += 1
    row_count = len(test_samples)
    baseline_mae = sum(baseline_absolute_errors) / row_count
    baseline_rmse = math.sqrt(sum(baseline_squared_errors) / row_count)
    mae = sum(absolute_errors) / row_count
    rmse = math.sqrt(sum(squared_errors) / row_count)
    segment = test_samples[0].segment if test_samples else None
    season = test_samples[0].season if test_samples else None
    return {
        "rows": row_count,
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "bias": round(sum(errors) / row_count, 3),
        "directional_accuracy": round(direction_hits / row_count, 3),
        "baseline_mae": round(baseline_mae, 3),
        "baseline_rmse": round(baseline_rmse, 3),
        "baseline_bias": round(sum(baseline_errors) / row_count, 3),
        "baseline_directional_accuracy": None,
        "mae_improvement": round(baseline_mae - mae, 3),
        "rmse_improvement": round(baseline_rmse - rmse, 3),
        "directional_accuracy_improvement": None,
        "segment_count": 1 if segment else 0,
        "skipped_segments": 0,
        "segments": [
            {
                "segment": segment,
                "season": season,
                "rows": row_count,
                "mae": round(mae, 3),
                "rmse": round(rmse, 3),
                "bias": round(sum(errors) / row_count, 3),
                "directional_accuracy": round(direction_hits / row_count, 3),
                "baseline_mae": round(baseline_mae, 3),
                "baseline_rmse": round(baseline_rmse, 3),
                "baseline_bias": round(sum(baseline_errors) / row_count, 3),
                "baseline_directional_accuracy": None,
                "mae_improvement": round(baseline_mae - mae, 3),
                "rmse_improvement": round(baseline_rmse - rmse, 3),
                "directional_accuracy_improvement": None,
            }
        ]
        if segment
        else [],
        "seasons": [
            {
                "season": season,
                "rows": row_count,
                "mae": round(mae, 3),
                "rmse": round(rmse, 3),
                "bias": round(sum(errors) / row_count, 3),
                "directional_accuracy": round(direction_hits / row_count, 3),
                "baseline_mae": round(baseline_mae, 3),
                "baseline_rmse": round(baseline_rmse, 3),
                "baseline_bias": round(sum(baseline_errors) / row_count, 3),
                "baseline_directional_accuracy": None,
                "mae_improvement": round(baseline_mae - mae, 3),
                "rmse_improvement": round(baseline_rmse - rmse, 3),
                "directional_accuracy_improvement": None,
            }
        ]
        if season
        else [],
    }


def _apply_market_context_projection(
    base_projection: float,
    *,
    market: str,
    line: float | None,
    over_odds: int | None,
    under_odds: int | None,
    market_rows: int,
    sample_count: int,
    avg_minutes: float,
    config: ModelTuningConfig | None = None,
) -> float:
    projection = float(base_projection)
    if line is None:
        return projection
    market_weight = _market_line_weight(
        market,
        market_rows,
        sample_count,
        avg_minutes,
        config=config,
    )
    projection = ((1 - market_weight) * projection) + (market_weight * float(line))
    if over_odds is not None and under_odds is not None:
        over_implied = american_to_implied_probability(int(over_odds))
        under_implied = american_to_implied_probability(int(under_odds))
        no_vig_mid = (over_implied / max(over_implied + under_implied, 0.01)) - 0.5
        projection += no_vig_mid * _market_price_nudge(market)
    return projection


def _evaluate_final_projection_samples(
    conn: sqlite3.Connection,
    market: str,
    training_samples: list[TrainingSample],
    *,
    config: ModelTuningConfig,
) -> dict[str, object]:
    final_samples, diagnostics = _final_projection_samples(conn, market)
    if len(final_samples) < 20:
        return {
            "final_rows": 0,
            "final_mae": None,
            "final_rmse": None,
            "final_bias": None,
            "final_directional_accuracy": None,
            "final_baseline_mae": None,
            "final_baseline_rmse": None,
            "final_baseline_bias": None,
            "final_baseline_directional_accuracy": None,
            "final_mae_improvement": None,
            "final_rmse_improvement": None,
            "final_directional_accuracy_improvement": None,
            "final_segment_count": 0,
            "final_skipped_segments": 0,
            "final_segments": [],
            "final_seasons": [],
            "final_projection_sample_diagnostics": diagnostics.to_dict(),
        }

    residual_samples, _residual_diagnostics = _residual_training_samples(conn, market)
    raw_by_segment: dict[str, list[TrainingSample]] = {}
    for sample in training_samples:
        raw_by_segment.setdefault(sample.segment, []).append(sample)
    residual_by_segment: dict[str, list[TrainingSample]] = {}
    for sample in residual_samples:
        residual_by_segment.setdefault(sample.segment, []).append(sample)
    final_by_segment: dict[str, list[FinalProjectionSample]] = {}
    for sample in final_samples:
        final_by_segment.setdefault(sample.segment, []).append(sample)

    raw_segments = sorted(raw_by_segment)
    residual_segments = sorted(residual_by_segment)
    final_segments = sorted(final_by_segment)

    raw_cursor = 0
    residual_cursor = 0
    raw_history: list[TrainingSample] = []
    residual_history: list[TrainingSample] = []
    evaluated_segments: list[dict[str, object]] = []
    season_rollup: dict[str, dict[str, float | int]] = {}
    errors: list[float] = []
    squared_errors: list[float] = []
    absolute_errors: list[float] = []
    baseline_errors: list[float] = []
    baseline_squared_errors: list[float] = []
    baseline_absolute_errors: list[float] = []
    direction_hits = 0
    skipped_segments = 0

    for segment in final_segments:
        while raw_cursor < len(raw_segments) and raw_segments[raw_cursor] < segment:
            raw_history.extend(raw_by_segment[raw_segments[raw_cursor]])
            raw_cursor += 1
        while residual_cursor < len(residual_segments) and residual_segments[residual_cursor] < segment:
            residual_history.extend(residual_by_segment[residual_segments[residual_cursor]])
            residual_cursor += 1

        if len(raw_history) < 20:
            skipped_segments += 1
            continue
        raw_model = _fit_model_from_samples(market, raw_history, config=config)
        if raw_model is None:
            skipped_segments += 1
            continue
        residual_model = (
            _fit_model_from_samples(f"residual:{market}", residual_history, config=config)
            if len(residual_history) >= 20
            else None
        )

        segment_samples = final_by_segment[segment]
        segment_errors: list[float] = []
        segment_squared_errors: list[float] = []
        segment_absolute_errors: list[float] = []
        segment_baseline_errors: list[float] = []
        segment_baseline_squared_errors: list[float] = []
        segment_baseline_absolute_errors: list[float] = []
        segment_direction_hits = 0
        season = segment_samples[0].season

        for sample in segment_samples:
            learned = max(0.0, _predict(raw_model, sample.features))
            snapshot = FeatureSnapshot(sample.features, sample.component_projection, "evaluation")
            learned = _stabilize_combo_market_projection(learned, snapshot, market)
            learned, _ = _stabilize_learned_projection(learned, snapshot, market, config=config)
            prediction = _apply_market_context_projection(
                learned,
                market=market,
                line=sample.line,
                over_odds=sample.over_odds,
                under_odds=sample.under_odds,
                market_rows=raw_model.rows,
                sample_count=sample.sample_count,
                avg_minutes=sample.avg_minutes,
                config=config,
            )
            if residual_model is not None and sample.line is not None:
                residual_prediction = _predict(residual_model, sample.features)
                residual_projection = float(sample.line) + residual_prediction
                residual_weight = _residual_market_weight(
                    market,
                    residual_model.rows,
                    sample.sample_count,
                    sample.avg_minutes,
                    config=config,
                )
                prediction = ((1 - residual_weight) * prediction) + (residual_weight * residual_projection)

            baseline_projection = _apply_market_context_projection(
                sample.component_projection,
                market=market,
                line=sample.line,
                over_odds=sample.over_odds,
                under_odds=sample.under_odds,
                market_rows=raw_model.rows,
                sample_count=sample.sample_count,
                avg_minutes=sample.avg_minutes,
                config=config,
            )

            error = prediction - sample.target
            baseline_error = baseline_projection - sample.target
            errors.append(error)
            squared_errors.append(error * error)
            absolute_errors.append(abs(error))
            baseline_errors.append(baseline_error)
            baseline_squared_errors.append(baseline_error * baseline_error)
            baseline_absolute_errors.append(abs(baseline_error))
            segment_errors.append(error)
            segment_squared_errors.append(error * error)
            segment_absolute_errors.append(abs(error))
            segment_baseline_errors.append(baseline_error)
            segment_baseline_squared_errors.append(baseline_error * baseline_error)
            segment_baseline_absolute_errors.append(abs(baseline_error))
            if (prediction >= baseline_projection and sample.target >= baseline_projection) or (
                prediction < baseline_projection and sample.target < baseline_projection
            ):
                direction_hits += 1
                segment_direction_hits += 1

        row_count = len(segment_samples)
        segment_metric = {
            "segment": segment,
            "season": season,
            "rows": row_count,
            "mae": round(sum(segment_absolute_errors) / row_count, 3),
            "rmse": round(math.sqrt(sum(segment_squared_errors) / row_count), 3),
            "bias": round(sum(segment_errors) / row_count, 3),
            "directional_accuracy": round(segment_direction_hits / row_count, 3),
            "baseline_mae": round(sum(segment_baseline_absolute_errors) / row_count, 3),
            "baseline_rmse": round(math.sqrt(sum(segment_baseline_squared_errors) / row_count), 3),
            "baseline_bias": round(sum(segment_baseline_errors) / row_count, 3),
            "baseline_directional_accuracy": None,
        }
        segment_metric["mae_improvement"] = round(float(segment_metric["baseline_mae"]) - float(segment_metric["mae"]), 3)
        segment_metric["rmse_improvement"] = round(float(segment_metric["baseline_rmse"]) - float(segment_metric["rmse"]), 3)
        segment_metric["directional_accuracy_improvement"] = None
        evaluated_segments.append(segment_metric)

        season_bucket = season_rollup.setdefault(
            season,
            {
                "rows": 0,
                "mae_sum": 0.0,
                "rmse_sum": 0.0,
                "bias_sum": 0.0,
                "direction_sum": 0.0,
                "baseline_mae_sum": 0.0,
                "baseline_rmse_sum": 0.0,
                "baseline_bias_sum": 0.0,
            },
        )
        season_bucket["rows"] = int(season_bucket["rows"]) + row_count
        season_bucket["mae_sum"] = float(season_bucket["mae_sum"]) + float(segment_metric["mae"]) * row_count
        season_bucket["rmse_sum"] = float(season_bucket["rmse_sum"]) + float(segment_metric["rmse"]) * row_count
        season_bucket["bias_sum"] = float(season_bucket["bias_sum"]) + float(segment_metric["bias"]) * row_count
        season_bucket["direction_sum"] = float(season_bucket["direction_sum"]) + float(segment_metric["directional_accuracy"]) * row_count
        season_bucket["baseline_mae_sum"] = float(season_bucket["baseline_mae_sum"]) + float(segment_metric["baseline_mae"]) * row_count
        season_bucket["baseline_rmse_sum"] = float(season_bucket["baseline_rmse_sum"]) + float(segment_metric["baseline_rmse"]) * row_count
        season_bucket["baseline_bias_sum"] = float(season_bucket["baseline_bias_sum"]) + float(segment_metric["baseline_bias"]) * row_count

    row_count = len(errors)
    if row_count == 0:
        return {
            "final_rows": 0,
            "final_mae": None,
            "final_rmse": None,
            "final_bias": None,
            "final_directional_accuracy": None,
            "final_baseline_mae": None,
            "final_baseline_rmse": None,
            "final_baseline_bias": None,
            "final_baseline_directional_accuracy": None,
            "final_mae_improvement": None,
            "final_rmse_improvement": None,
            "final_directional_accuracy_improvement": None,
            "final_segment_count": len(evaluated_segments),
            "final_skipped_segments": skipped_segments,
            "final_segments": evaluated_segments,
            "final_seasons": [],
            "final_projection_sample_diagnostics": diagnostics.to_dict(),
        }

    seasons = []
    for season, bucket in sorted(season_rollup.items()):
        season_rows = int(bucket["rows"])
        season_metric = {
            "season": season,
            "rows": season_rows,
            "mae": round(float(bucket["mae_sum"]) / season_rows, 3),
            "rmse": round(float(bucket["rmse_sum"]) / season_rows, 3),
            "bias": round(float(bucket["bias_sum"]) / season_rows, 3),
            "directional_accuracy": round(float(bucket["direction_sum"]) / season_rows, 3),
            "baseline_mae": round(float(bucket["baseline_mae_sum"]) / season_rows, 3),
            "baseline_rmse": round(float(bucket["baseline_rmse_sum"]) / season_rows, 3),
            "baseline_bias": round(float(bucket["baseline_bias_sum"]) / season_rows, 3),
            "baseline_directional_accuracy": None,
        }
        season_metric["mae_improvement"] = round(float(season_metric["baseline_mae"]) - float(season_metric["mae"]), 3)
        season_metric["rmse_improvement"] = round(float(season_metric["baseline_rmse"]) - float(season_metric["rmse"]), 3)
        season_metric["directional_accuracy_improvement"] = None
        seasons.append(season_metric)

    baseline_mae = sum(baseline_absolute_errors) / row_count
    baseline_rmse = math.sqrt(sum(baseline_squared_errors) / row_count)
    baseline_bias = sum(baseline_errors) / row_count
    mae = sum(absolute_errors) / row_count
    rmse = math.sqrt(sum(squared_errors) / row_count)
    directional_accuracy = direction_hits / row_count
    return {
        "final_rows": row_count,
        "final_mae": round(mae, 3),
        "final_rmse": round(rmse, 3),
        "final_bias": round(sum(errors) / row_count, 3),
        "final_directional_accuracy": round(directional_accuracy, 3),
        "final_baseline_mae": round(baseline_mae, 3),
        "final_baseline_rmse": round(baseline_rmse, 3),
        "final_baseline_bias": round(baseline_bias, 3),
        "final_baseline_directional_accuracy": None,
        "final_mae_improvement": round(baseline_mae - mae, 3),
        "final_rmse_improvement": round(baseline_rmse - rmse, 3),
        "final_directional_accuracy_improvement": None,
        "final_segment_count": len(evaluated_segments),
        "final_skipped_segments": skipped_segments,
        "final_segments": evaluated_segments,
        "final_seasons": seasons,
        "final_projection_sample_diagnostics": diagnostics.to_dict(),
    }


def _historical_training_features(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    history: list[float],
    minutes: list[float],
    market: str,
    previous_game_date: str | None,
    recent_rows: list[sqlite3.Row] | None = None,
    player_rows: list[sqlite3.Row] | None = None,
    row_index: int | None = None,
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
    pace_factor = _cached_pace_factor(conn, int(context["team_id"]), int(context["opponent_id"]))
    opponent_factor = _cached_opponent_factor(conn, int(context["opponent_id"]), market)
    recent_opponent_factor = _cached_recent_opponent_factor(conn, int(context["opponent_id"]), market)
    team_rating_context = _cached_team_rating_context(conn, int(context["team_id"]), current_game_date)
    opponent_rating_context = _cached_team_rating_context(conn, int(context["opponent_id"]), current_game_date)
    league_rating_context = _cached_league_rating_context(conn, current_game_date)
    training_runtime_cache = {"team_form_regime": _connection_training_cache_bucket(conn, "team_form_regime")}
    team_form = build_team_form_regime(
        conn,
        team_id=int(context["team_id"]),
        before_game_date=current_game_date,
        runtime_cache=training_runtime_cache,
    )
    opponent_form = build_team_form_regime(
        conn,
        team_id=int(context["opponent_id"]),
        before_game_date=current_game_date,
        runtime_cache=training_runtime_cache,
    )
    team_form_features = _team_form_regime_feature_values(team_form, opponent_form)
    team_off_rating_factor = _clamp(
        float(team_rating_context["off_rating"]) / max(float(league_rating_context["off_rating"]), 1.0),
        0.94,
        1.06,
    )
    opponent_def_rating_factor = _clamp(
        max(float(league_rating_context["def_rating"]), 1.0) / max(float(opponent_rating_context["def_rating"]), 1.0),
        0.94,
        1.06,
    )
    recent_opponent_def_rating_factor = _clamp(
        max(float(league_rating_context["recent_def_rating"]), 1.0) / max(float(opponent_rating_context["recent_def_rating"]), 1.0),
        0.93,
        1.07,
    )
    matchup_net_rating_diff = float(team_rating_context["net_rating"]) - float(opponent_rating_context["net_rating"])
    if player_rows is not None and row_index is not None:
        common_opponent_factor = _common_opponent_factor_from_rows(
            conn,
            player_rows=player_rows,
            row_index=row_index,
            market=market,
            context=context,
            before_game_date=current_game_date,
            baseline_avg=last_10_avg,
        )
        h2h_factor = _h2h_factor_from_rows(
            player_rows=player_rows,
            row_index=row_index,
            market=market,
            opponent_id=int(context["opponent_id"]),
            baseline_avg=last_10_avg,
        )
    else:
        common_opponent_factor = _cached_common_opponent_factor(
            conn,
            int(row["player_id"]),
            market,
            context,
            current_game_date,
            baseline_avg=last_10_avg,
        )
        h2h_factor = _cached_h2h_factor(
            conn,
            int(row["player_id"]),
            market,
            context,
            current_game_date,
            baseline_avg=last_10_avg,
        )
    blowout = _blowout_adjustment(None, context, str(row["rotation_role"] or "starter"))
    lineup_context = (
        _minutes_lineup_context_from_player_rows(
            conn,
            player_id=int(row["player_id"]),
            team_id=int(context["team_id"]),
            player_rows=player_rows,
            row_index=row_index,
        )
        if player_rows is not None and row_index is not None
        else _minutes_lineup_context(
            conn,
            player_id=int(row["player_id"]),
            team_id=int(context["team_id"]),
            history_rows=recent_rows,
        ) if recent_rows else None
    )
    opportunity_context = _historical_minutes_opportunity_context(
        conn,
        player_id=int(row["player_id"]),
        game_id=int(row["game_id"]),
        team_id=int(context["team_id"]),
        position=str(_row_mapping_value(row, "position") or ""),
        before_game_date=current_game_date,
    )
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
        lineup_context=lineup_context,
        opportunity_context=opportunity_context,
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
    component_projection = _market_specific_component_projection(
        market=market,
        component_base=component_projection,
        rate_projection=rate_projection,
        last_10_avg=last_10_avg,
        consistency_score=consistency_score,
        value_volatility=value_volatility,
    )
    component_projection *= _clamp(
        (0.45 * team_off_rating_factor)
        + (0.35 * opponent_def_rating_factor)
        + (0.20 * recent_opponent_def_rating_factor),
        0.92,
        1.08,
    )
    is_home = bool(context["is_home"])
    rest_days = int(context["rest_days"])
    team_spread = context["team_spread"]
    game_total = float(context["game_total"]) if context["game_total"] is not None and float(context["game_total"]) > 0 else 165.0
    if recent_rows:
        archetype = _player_archetype_profile_from_rows(
            recent_rows,
            fallback_rotation_role=str(row["rotation_role"] or "starter"),
        )
    else:
        archetype = _cached_player_archetype_profile(
            conn,
            player_id=int(row["player_id"]),
            reference_game_date=str(row["game_date"]),
            exclude_game_id=int(row["game_id"]),
            fallback_rotation_role=str(row["rotation_role"] or "starter"),
        )
    team_transition = _team_transition_features_from_player_rows(
        conn,
        player_id=int(row["player_id"]),
        team_id=int(context["team_id"]),
        player_rows=player_rows or [],
        row_index=row_index,
    )
    market_context_features = _market_context_features(
        market,
        lineup_context=lineup_context,
        opportunity_context=opportunity_context,
        recent_opponent_factor=recent_opponent_factor,
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
        *team_transition,
        *market_context_features,
        team_off_rating_factor,
        opponent_def_rating_factor,
        recent_opponent_def_rating_factor,
        matchup_net_rating_diff,
        *team_form_features,
    ]


@lru_cache(maxsize=32)
def _train_minutes_model_cached(
    db_path: str,
    config: ModelTuningConfig,
    role_bucket: str | None = None,
    training_start: str = "",
    data_signature: str = "",
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
    training_start = _training_start_date()
    cache_market = f"minutes:{bucket or 'all'}"
    if not isinstance(conn, sqlite3.Connection):
        key = (id(conn), bucket, tuning, training_start)
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
    from .minutes_training_db import minutes_training_db_signature

    return _train_minutes_model_cached(
        db_path,
        tuning,
        bucket,
        training_start,
        minutes_training_db_signature(conn),
    )


def prewarm_model_cache(
    conn: sqlite3.Connection,
    config: ModelTuningConfig | None = None,
    *,
    allow_training: bool = True,
) -> dict[str, int]:
    tuning = config or DEFAULT_TUNING_CONFIG
    warmed = {"minutes": 0, "markets": 0, "residuals": 0}
    if train_minutes_model(conn, config=tuning, allow_training=allow_training) is not None:
        warmed["minutes"] = 1
    for bucket in MINUTES_ROLE_BUCKETS:
        if train_minutes_model(conn, config=tuning, role_bucket=bucket, allow_training=allow_training) is not None:
            warmed["minutes"] += 1
    for market in TRAINING_MARKETS:
        if train_market_model(conn, market, config=tuning, allow_training=allow_training) is not None:
            warmed["markets"] += 1
        if train_market_residual_model(conn, market, config=tuning, allow_training=allow_training) is not None:
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
    from .minutes_training_db import load_minutes_training_examples

    samples, _info = load_minutes_training_examples(conn, role_bucket=role_bucket, force_rebuild=False)
    if samples:
        return samples

    samples = []
    training_start = _training_start_date()
    bucket_filter = role_bucket if role_bucket in set(MINUTES_ROLE_BUCKETS) else None
    players = conn.execute("SELECT id FROM players ORDER BY id").fetchall()
    for player in players:
        rows = _player_training_rows(conn, int(player["id"]))
        minutes = [float(row["minutes"]) for row in rows]
        for idx in range(5, len(rows)):
            current = rows[idx]
            if _before_training_start(str(current["game_date"]), training_start):
                continue
            newest_minutes = list(reversed(minutes[max(0, idx - 10):idx]))
            if len(newest_minutes) < 5:
                continue
            previous_game_date = str(rows[idx - 1]["game_date"]) if idx > 0 else None
            context = _historical_game_context(current)
            minute_volatility = _minute_volatility(newest_minutes)
            ewma_minutes = _ewma_newest_first(newest_minutes, alpha=0.38)
            minutes_trend = _recent_trend(newest_minutes)
            recent_minutes_avg = sum(newest_minutes[:5]) / min(len(newest_minutes), 5)
            last_10_minutes_avg = sum(newest_minutes) / len(newest_minutes)
            recent_absence_days = _days_between_game_dates(previous_game_date, str(current["game_date"]))
            # Historical minutes-model training already uses completed boxscore rows.
            # Replaying live teammate-injury context across every historical sample is
            # extremely expensive and not required to classify the observed row.
            injury = {
                "status": "available",
                "availability_factor": 1.0,
                "usage_multiplier": 1.0,
                "minutes_delta": 0.0,
                "hard_cap_zero": False,
            }
            lineup_context = _minutes_lineup_context_from_player_rows(
                conn,
                player_id=int(player["id"]),
                team_id=int(context["team_id"]),
                player_rows=rows,
                row_index=idx,
            )
            opportunity_context = _historical_minutes_opportunity_context(
                conn,
                player_id=int(player["id"]),
                game_id=int(current["game_id"]),
                team_id=int(context["team_id"]),
                position=str(current["position"] or ""),
                before_game_date=str(current["game_date"]),
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
                lineup_context=lineup_context,
                opportunity_context=opportunity_context,
            )
            if bucket_filter is not None and role_state.bucket != bucket_filter:
                continue
            team_transition = _team_transition_features_from_player_rows(
                conn,
                player_id=int(player["id"]),
                team_id=int(context["team_id"]),
                player_rows=rows,
                row_index=idx,
            )
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
                team_transition=team_transition,
                lineup_context=lineup_context,
                opportunity_context=opportunity_context,
            )
            recent_blend = _minutes_recent_blend(
                recent_minutes_avg=recent_minutes_avg,
                last_10_minutes_avg=last_10_minutes_avg,
            )
            samples.append((features, float(current["minutes"]) - recent_blend))
    return samples


def _training_start_date(today: date | None = None) -> str:
    configured = os.getenv(TRAINING_START_DATE_ENV, "").strip()
    if configured:
        try:
            return date.fromisoformat(configured).isoformat()
        except ValueError:
            pass
    current = today or datetime.now(APP_TIMEZONE).date()
    return date(current.year - 1, 1, 1).isoformat()


def _before_training_start(game_date: str | None, training_start: str) -> bool:
    if not game_date:
        return True
    return str(game_date)[:10] < training_start


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
    team_transition: list[float] | None = None,
    lineup_context: list[float] | None = None,
    opportunity_context: list[float] | None = None,
    before_game_date: str | None = None,
    allow_training: bool = True,
    include_stage_details: bool = False,
) -> tuple[float, str] | tuple[float, str, dict[str, float | str | None]]:
    stage_details: dict[str, float | str | None] = {}
    base_heuristic = max(ewma_minutes + (0.35 * minutes_trend), 4.0)
    recent_blend = _minutes_recent_blend(
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
    )
    recency_anchor = _minutes_recency_anchor(
        ewma_minutes=ewma_minutes,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
    )
    hard_statuses = {"out", "inactive", "suspended", "unavailable"}
    if str(injury_status or "").strip().lower() in hard_statuses:
        if include_stage_details:
            return 0.0, "hard rule out", {"role_bucket": None, "base_heuristic": base_heuristic, "recent_blend": recent_blend, "recency_anchor": recency_anchor}
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
        lineup_context=lineup_context,
        opportunity_context=opportunity_context,
    )
    learned_minutes = None
    blend_note = "heuristic only"
    stage_details["base_heuristic"] = base_heuristic
    stage_details["recent_blend"] = recent_blend
    stage_details["recency_anchor"] = recency_anchor
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
                team_transition=team_transition,
                lineup_context=lineup_context,
                opportunity_context=opportunity_context,
            )
            learned_delta = _predict(minutes_model, minutes_features)
            learned_minutes = max(0.0, recent_blend + learned_delta)
            base_blend_weight = _minutes_model_weight(
                rows=minutes_model.rows,
                role_bucket=role_state.bucket,
                minute_volatility=minute_volatility,
                recent_absence_days=recent_absence_days,
            )
            blend_weight = _minutes_earned_blend_weight(
                base_weight=base_blend_weight,
                learned_minutes=learned_minutes,
                recent_blend=recent_blend,
                recency_anchor=recency_anchor,
                role_bucket=role_state.bucket,
                minute_volatility=minute_volatility,
                recent_absence_days=recent_absence_days,
                recent_drop=role_state.recent_drop,
                recent_spike=role_state.recent_spike,
                injury_delta=injury_delta,
                opportunity_context=opportunity_context,
            )
            projected = ((1.0 - blend_weight) * projected) + (blend_weight * learned_minutes)
            blend_note = f"learned blend {blend_weight:.0%}/{base_blend_weight:.0%} ({model_scope}, delta {learned_delta:+.1f})"
            stage_details["learned_minutes"] = learned_minutes
            stage_details["base_blend_weight"] = base_blend_weight
            stage_details["blend_weight"] = blend_weight
    stage_details["post_blend_projection"] = projected
    stage_details["role_bucket"] = role_state.bucket
    anchor_weight = _minutes_recency_anchor_weight(
        role_bucket=role_state.bucket,
        minute_volatility=minute_volatility,
        recent_absence_days=recent_absence_days,
        recent_drop=role_state.recent_drop,
        recent_spike=role_state.recent_spike,
        injury_delta=injury_delta,
    )
    projected = ((1.0 - anchor_weight) * projected) + (anchor_weight * recency_anchor)
    stage_details["anchor_weight"] = anchor_weight
    stage_details["post_anchor_projection"] = projected
    reference_baseline = _minutes_reference_baseline(
        role_state=role_state,
        base_heuristic=base_heuristic,
        recent_blend=recent_blend,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minutes_trend=minutes_trend,
        minute_volatility=minute_volatility,
        injury_status=injury_status,
        injury_delta=injury_delta,
        recent_absence_days=recent_absence_days,
    )
    baseline_guard_weight = _minutes_baseline_guard_weight(
        learned_minutes=learned_minutes,
        recency_anchor=recency_anchor,
        reference_baseline=reference_baseline,
        role_bucket=role_state.bucket,
        minute_volatility=minute_volatility,
        recent_absence_days=recent_absence_days,
        recent_drop=role_state.recent_drop,
        recent_spike=role_state.recent_spike,
        injury_delta=injury_delta,
    )
    projected = ((1.0 - baseline_guard_weight) * projected) + (baseline_guard_weight * reference_baseline)
    stage_details["reference_baseline"] = reference_baseline
    stage_details["baseline_guard_weight"] = baseline_guard_weight
    stage_details["post_baseline_projection"] = projected
    projected = _apply_minutes_recency_floor(
        projected=projected,
        role_state=role_state,
        recent_blend=recent_blend,
        recency_anchor=recency_anchor,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        minutes_trend=minutes_trend,
        injury_status=injury_status,
        injury_delta=injury_delta,
        recent_absence_days=recent_absence_days,
        team_transition=team_transition,
    )
    stage_details["post_recency_floor_projection"] = projected
    lower_bound, upper_bound = _role_aware_minutes_bounds(
        role_state,
        last_10_minutes_avg=last_10_minutes_avg,
        recent_minutes_avg=recent_minutes_avg,
        minutes_trend=minutes_trend,
        injury_status=injury_status,
        injury_delta=injury_delta,
    )
    projected = _clamp(projected, lower_bound, upper_bound)
    stage_details["post_bounds_projection"] = projected
    projected, hard_rule_notes = _apply_minutes_hard_rules(
        projected=projected,
        role_state=role_state,
        rotation_role=rotation_role,
        injury_status=injury_status,
        injury_delta=injury_delta,
        blowout_delta=blowout_delta,
        recent_blend=recent_blend,
        recent_minutes_avg=recent_minutes_avg,
        last_10_minutes_avg=last_10_minutes_avg,
        opportunity_context=opportunity_context,
    )
    stage_details["post_hard_rules_projection"] = projected
    projected, rebound_guard_notes = _apply_minutes_rebound_guard(
        projected=projected,
        role_state=role_state,
        recent_blend=recent_blend,
        last_10_minutes_avg=last_10_minutes_avg,
        minutes_trend=minutes_trend,
        minute_volatility=minute_volatility,
        injury_status=injury_status,
        recent_absence_days=recent_absence_days,
        lineup_context=lineup_context,
        team_transition=team_transition,
        blowout_delta=blowout_delta,
    )
    stage_details["post_rebound_guard_projection"] = projected
    projected, stable_context_notes = _apply_minutes_stable_context_cap(
        projected=projected,
        role_state=role_state,
        recent_blend=recent_blend,
        recency_anchor=recency_anchor,
        last_10_minutes_avg=last_10_minutes_avg,
        minutes_trend=minutes_trend,
        minute_volatility=minute_volatility,
        injury_status=injury_status,
        injury_delta=injury_delta,
        recent_absence_days=recent_absence_days,
        team_transition=team_transition,
        opportunity_context=opportunity_context,
    )
    projected = _clamp(projected, lower_bound, upper_bound)
    stage_details["final_projection"] = projected
    if abs(venue_delta) >= 0.05:
        venue_note = f", venue {venue_delta:+.1f}"
    else:
        venue_note = ""
    learned_note = f", learned {learned_minutes:.1f}" if learned_minutes is not None else ""
    anchor_note = f", recency anchor {recency_anchor:.1f} @ {anchor_weight:.0%}"
    baseline_note = f", baseline {reference_baseline:.1f} @ {baseline_guard_weight:.0%}"
    all_rule_notes = [*hard_rule_notes, *rebound_guard_notes, *stable_context_notes]
    hard_rule_suffix = f", {'; '.join(all_rule_notes)}" if all_rule_notes else ""
    note = f"{role_state.bucket} bounds {lower_bound:.1f}-{upper_bound:.1f}{venue_note}, {blend_note}{learned_note}{anchor_note}{baseline_note}{hard_rule_suffix}"
    if include_stage_details:
        return projected, note, stage_details
    return projected, note


def _minutes_recency_anchor(
    *,
    ewma_minutes: float,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
) -> float:
    return (0.65 * recent_minutes_avg) + (0.25 * last_10_minutes_avg) + (0.10 * ewma_minutes)


def _minutes_recent_blend(
    *,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
) -> float:
    return (0.65 * recent_minutes_avg) + (0.35 * last_10_minutes_avg)


def _minutes_recency_anchor_weight(
    *,
    role_bucket: str,
    minute_volatility: float,
    recent_absence_days: float | None,
    recent_drop: bool,
    recent_spike: bool,
    injury_delta: float,
) -> float:
    if role_bucket == "core_starter":
        weight = 0.16
    elif role_bucket in {"starter_volatile", "rotation"}:
        weight = 0.22
    else:
        weight = 0.30
    if minute_volatility >= 8.0:
        weight += 0.06
    if recent_absence_days is not None and recent_absence_days >= 7:
        weight += 0.04
    if recent_drop:
        weight -= 0.05
    if recent_spike and injury_delta > 0:
        weight -= 0.08
    elif role_bucket == "starter_volatile" and recent_spike and injury_delta <= 0:
        weight += 0.06
    return max(0.10, min(0.42, weight))


def _minutes_reference_baseline(
    *,
    role_state: MinutesRoleState,
    base_heuristic: float,
    recent_blend: float,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
    minutes_trend: float,
    minute_volatility: float,
    injury_status: str,
    injury_delta: float,
    recent_absence_days: float | None,
) -> float:
    status = str(injury_status or "").strip().lower()
    baseline = recent_blend
    if role_state.recent_drop or minutes_trend <= -4.0:
        blend_recent_weight = 0.55
        if role_state.bucket in {"bench", "fringe"} and (
            recent_absence_days is None or recent_absence_days < 7.0
        ):
            blend_recent_weight = 0.72
        if minute_volatility < 5.5 and (recent_absence_days is None or recent_absence_days < 7.0):
            blend_recent_weight = max(blend_recent_weight, 0.78 if role_state.bucket in {"bench", "fringe"} else 0.68)
        baseline = (blend_recent_weight * recent_blend) + ((1.0 - blend_recent_weight) * base_heuristic)
    if recent_absence_days is not None and recent_absence_days >= 7:
        baseline = (0.50 * baseline) + (0.50 * base_heuristic)
    if status in {"questionable", "gtd", "doubtful"}:
        baseline = min(baseline, max(base_heuristic, recent_minutes_avg * 0.9))
    if role_state.recent_spike and injury_delta > 0:
        baseline = max(baseline, recent_minutes_avg - 0.5)
    if role_state.bucket == "core_starter":
        baseline = max(baseline, last_10_minutes_avg - 1.5)
    return baseline


def _minutes_baseline_guard_weight(
    *,
    learned_minutes: float | None,
    recency_anchor: float,
    reference_baseline: float,
    role_bucket: str,
    minute_volatility: float,
    recent_absence_days: float | None,
    recent_drop: bool,
    recent_spike: bool,
    injury_delta: float,
) -> float:
    if role_bucket == "core_starter":
        weight = 0.10
    elif role_bucket in {"starter_volatile", "rotation"}:
        weight = 0.16
    else:
        weight = 0.24
    if minute_volatility >= 8.0:
        weight += 0.04
    if recent_absence_days is not None and recent_absence_days >= 7:
        weight += 0.05
    if recent_drop:
        weight -= 0.04
    if recent_spike and injury_delta > 0:
        weight -= 0.08
    elif role_bucket == "starter_volatile" and recent_spike and injury_delta <= 0:
        weight += 0.06
    if learned_minutes is not None:
        deviation = abs(learned_minutes - recency_anchor)
        baseline_gap = abs(learned_minutes - reference_baseline)
        if baseline_gap >= 4.0:
            weight += 0.10
        elif baseline_gap >= 2.5:
            weight += 0.05
        if deviation >= 6.0:
            weight += 0.05
    return max(0.06, min(0.34, weight))


def _apply_minutes_recency_floor(
    *,
    projected: float,
    role_state: MinutesRoleState,
    recent_blend: float,
    recency_anchor: float,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
    minutes_trend: float,
    injury_status: str,
    injury_delta: float,
    recent_absence_days: float | None,
    team_transition: list[float] | None,
) -> float:
    status = str(injury_status or "").strip().lower()
    if status in {"questionable", "gtd", "doubtful"}:
        return projected
    if recent_absence_days is not None and recent_absence_days >= 7:
        return projected
    floor = recency_anchor - (2.0 if role_state.bucket in {"bench", "fringe"} else 2.5)
    if role_state.bucket == "core_starter":
        floor = max(floor, last_10_minutes_avg - 3.0)
    if role_state.recent_spike and injury_delta > 0:
        floor = max(floor, recent_minutes_avg - 1.0)
    games_since_joining_team = float((team_transition or [0.0])[0] if team_transition else 0.0)
    if 0.0 < games_since_joining_team <= 10.0:
        floor = max(floor, recent_minutes_avg - 1.5)
    if role_state.recent_drop and minutes_trend <= -4.0:
        floor -= 1.5
    floor = max(projected, max(role_state.lower_bound, floor))
    if role_state.bucket == "core_starter" and not role_state.recent_drop:
        floor = max(floor, recent_blend - 2.0)
    return floor


def _apply_minutes_stable_context_cap(
    *,
    projected: float,
    role_state: MinutesRoleState,
    recent_blend: float,
    recency_anchor: float,
    last_10_minutes_avg: float,
    minutes_trend: float,
    minute_volatility: float,
    injury_status: str,
    injury_delta: float,
    recent_absence_days: float | None,
    team_transition: list[float] | None,
    opportunity_context: list[float] | None,
) -> tuple[float, list[str]]:
    notes: list[str] = []
    status = str(injury_status or "").strip().lower()
    if status in {"questionable", "gtd", "doubtful"}:
        return projected, notes
    games_since_joining_team = float((team_transition or [0.0])[0] if team_transition else 0.0)
    transitional = 0.0 < games_since_joining_team <= 10.0
    normalized_opportunity = list(opportunity_context or [0.0, 0.0])
    if len(normalized_opportunity) < 2:
        normalized_opportunity = (normalized_opportunity + [0.0, 0.0])[:2]
    same_position_unavailable_minutes = float(normalized_opportunity[0])
    same_position_key_out_count = float(normalized_opportunity[1])
    strong_vacancy = same_position_unavailable_minutes >= 18.0 and same_position_key_out_count >= 1.0
    soft_vacancy = (
        not strong_vacancy
        and (same_position_unavailable_minutes >= 10.0 or same_position_key_out_count >= 1.0)
    )
    if (
        role_state.bucket == "core_starter"
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and not role_state.recent_drop
        and not role_state.recent_spike
        and not soft_vacancy
        and not strong_vacancy
        and minute_volatility < 5.8
        and abs(minutes_trend) < 2.5
    ):
        core_starter_floor = min(
            role_state.upper_bound,
            max(
                role_state.lower_bound,
                recent_blend - 1.3,
                recency_anchor - 1.0,
                last_10_minutes_avg - 1.9,
            ),
        )
        if projected < core_starter_floor:
            projected = core_starter_floor
            notes.append("stable-context core-starter floor")
    if (
        role_state.bucket == "starter_volatile"
        and role_state.recent_spike
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and soft_vacancy
    ):
        soft_vacancy_cap = max(
            role_state.lower_bound,
            min(
                role_state.upper_bound,
                max(
                    recent_blend + 1.4,
                    recency_anchor + 0.9,
                    last_10_minutes_avg + 0.6,
                ),
            ),
        )
        if projected > soft_vacancy_cap:
            projected = soft_vacancy_cap
            notes.append("soft-vacancy rise cap")
    if (
        role_state.bucket == "starter_volatile"
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and soft_vacancy
        and not role_state.recent_spike
        and minute_volatility < 5.8
    ):
        soft_vacancy_floor = min(
            role_state.upper_bound,
            max(
                role_state.lower_bound,
                recent_blend - 0.9,
                recency_anchor - 0.7,
            ),
        )
        if projected < soft_vacancy_floor:
            projected = soft_vacancy_floor
            notes.append("soft-vacancy starter floor")
    if (
        role_state.bucket == "starter_volatile"
        and role_state.recent_spike
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and not soft_vacancy
        and not strong_vacancy
        and minute_volatility < 8.5
    ):
        rise_buffer = 1.9
        anchor_buffer = 1.3
        last10_buffer = 1.1
        if minute_volatility < 7.6:
            rise_buffer = 1.5
            anchor_buffer = 1.0
            last10_buffer = 0.8
        if minute_volatility < 5.8 and abs(minutes_trend) < 4.2:
            rise_buffer = 1.2
            anchor_buffer = 0.9
            last10_buffer = 0.7
        stable_rise_cap = max(
            role_state.lower_bound,
            min(
                role_state.upper_bound,
                max(
                    recent_blend + rise_buffer,
                    recency_anchor + anchor_buffer,
                    last_10_minutes_avg + last10_buffer,
                ),
            ),
        )
        if projected > stable_rise_cap:
            projected = stable_rise_cap
            notes.append("stable-context starter rise cap")
    if (
        role_state.bucket == "starter_volatile"
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and not soft_vacancy
        and not strong_vacancy
        and not role_state.recent_spike
        and minute_volatility < 6.6
    ):
        stable_starter_floor = min(
            role_state.upper_bound,
            max(
                role_state.lower_bound,
                recent_blend - 2.1,
                recency_anchor - 1.7,
            ),
        )
        if minute_volatility < 4.9 and abs(minutes_trend) < 1.6:
            stable_starter_floor = min(
                role_state.upper_bound,
                max(
                    stable_starter_floor,
                    recent_blend - 1.4,
                    recency_anchor - 1.2,
                ),
            )
        if minute_volatility < 6.2 and abs(minutes_trend) < 0.8:
            stable_starter_floor = min(
                role_state.upper_bound,
                max(
                    stable_starter_floor,
                    recent_blend - 1.5,
                    recency_anchor - 1.0,
                ),
            )
        if projected < stable_starter_floor:
            projected = stable_starter_floor
            notes.append("stable-context starter floor")
    if (
        role_state.bucket == "rotation"
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and soft_vacancy
        and role_state.recent_spike
        and minute_volatility < 6.4
    ):
        soft_vacancy_rotation_rise_cap = max(
            role_state.lower_bound,
            min(
                role_state.upper_bound,
                max(
                    recent_blend + 1.4,
                    recency_anchor + 0.9,
                    last_10_minutes_avg + 0.7,
                ),
            ),
        )
        if projected > soft_vacancy_rotation_rise_cap:
            projected = soft_vacancy_rotation_rise_cap
            notes.append("soft-vacancy rotation rise cap")
    if (
        role_state.bucket == "rotation"
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and soft_vacancy
    ):
        soft_vacancy_floor = min(
            role_state.upper_bound,
            max(
                role_state.lower_bound,
                recent_blend - 0.6,
                last_10_minutes_avg - 1.2,
            ),
        )
        if projected < soft_vacancy_floor:
            projected = soft_vacancy_floor
            notes.append("soft-vacancy rotation floor")
    if (
        role_state.bucket == "rotation"
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and not soft_vacancy
        and not strong_vacancy
        and minute_volatility < 7.4
        and abs(minutes_trend) < 4.0
    ):
        stable_rotation_floor = min(
            role_state.upper_bound,
            max(
                role_state.lower_bound,
                recent_blend - 1.6,
                recency_anchor - 1.4,
            ),
        )
        if minute_volatility < 7.3 and abs(minutes_trend) >= 3.0:
            stable_rotation_floor = min(
                role_state.upper_bound,
                max(
                    stable_rotation_floor,
                    recent_blend - 1.3,
                    recency_anchor - 1.1,
                ),
            )
        if minute_volatility < 6.6 and abs(minutes_trend) < 3.0:
            stable_rotation_floor = min(
                role_state.upper_bound,
                max(
                    stable_rotation_floor,
                    recent_blend - 1.2,
                    recency_anchor - 0.9,
                ),
            )
        if minute_volatility < 4.9 and abs(minutes_trend) < 1.6:
            stable_rotation_floor = min(
                role_state.upper_bound,
                max(
                    stable_rotation_floor,
                    recent_blend - 1.1,
                    recency_anchor - 1.0,
                ),
            )
        if projected < stable_rotation_floor:
            projected = stable_rotation_floor
            notes.append("stable-context rotation floor")
    if (
        role_state.bucket == "rotation"
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and not soft_vacancy
        and not strong_vacancy
        and minute_volatility < 7.6
        and abs(minutes_trend) < 2.5
    ):
        rise_buffer = 1.0
        last10_buffer = 0.6
        if minute_volatility >= 6.8:
            rise_buffer = 0.8
            last10_buffer = 0.5
        elif minute_volatility < 5.6 and abs(minutes_trend) < 2.0:
            rise_buffer = 0.8
            last10_buffer = 0.45
        stable_rotation_rise_cap = max(
            role_state.lower_bound,
            min(
                role_state.upper_bound,
                max(
                    recent_blend + rise_buffer,
                    last_10_minutes_avg + last10_buffer,
                ),
            ),
        )
        if projected > stable_rotation_rise_cap:
            projected = stable_rotation_rise_cap
            notes.append("stable-context rotation rise cap")
    if (
        role_state.bucket == "rotation"
        and role_state.recent_spike
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and strong_vacancy
        and minute_volatility < 7.4
    ):
        vacancy_rotation_rise_cap = max(
            role_state.lower_bound,
            min(
                role_state.upper_bound,
                max(
                    recent_blend + 1.5,
                    recency_anchor + 0.9,
                    last_10_minutes_avg + 0.5,
                ),
            ),
        )
        if projected > vacancy_rotation_rise_cap:
            projected = vacancy_rotation_rise_cap
            notes.append("vacancy rotation rise cap")
    if (
        role_state.bucket == "rotation"
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and strong_vacancy
        and minute_volatility < 6.6
        and abs(minutes_trend) < 3.0
    ):
        stable_vacancy_rotation_cap = max(
            role_state.lower_bound,
            min(
                role_state.upper_bound,
                max(
                    recent_blend + 1.25,
                    recency_anchor + 0.75,
                    last_10_minutes_avg - 0.1,
                ),
            ),
        )
        if projected > stable_vacancy_rotation_cap:
            projected = stable_vacancy_rotation_cap
            notes.append("stable-vacancy rotation rise cap")
    if (
        role_state.bucket == "starter_volatile"
        and role_state.recent_spike
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and strong_vacancy
        and minute_volatility < 6.8
    ):
        vacancy_starter_rise_cap = max(
            role_state.lower_bound,
            min(
                role_state.upper_bound,
                max(
                    recent_blend + 1.9,
                    recency_anchor + 1.1,
                    last_10_minutes_avg + 0.7,
                ),
            ),
        )
        if projected > vacancy_starter_rise_cap:
            projected = vacancy_starter_rise_cap
            notes.append("vacancy starter rise cap")
    if (
        role_state.bucket in {"starter_volatile", "rotation"}
        and injury_delta <= 0.0
        and recent_absence_days is not None
        and 7.0 <= recent_absence_days < 14.0
        and minute_volatility < 6.8
        and minutes_trend > -4.5
        and not strong_vacancy
    ):
        absence_return_floor = min(
            role_state.upper_bound,
            max(
                role_state.lower_bound,
                recency_anchor - 1.1,
                recent_blend - 1.4,
            ),
        )
        if projected < absence_return_floor:
            projected = absence_return_floor
            notes.append("absence-return floor")
    dynamic_context = any(
        (
            abs(injury_delta) >= 1.0,
            recent_absence_days is not None and recent_absence_days >= 7.0,
            role_state.recent_drop,
            role_state.recent_spike,
            minute_volatility >= 8.0,
            abs(minutes_trend) >= 3.5,
            transitional,
            strong_vacancy,
        )
    )
    if dynamic_context:
        return projected, notes
    tolerance = 2.2 if role_state.bucket == "core_starter" else 2.8 if role_state.bucket in {"starter_volatile", "rotation"} else 3.2
    if abs(recency_anchor - recent_blend) <= 0.75:
        tolerance -= 0.4
    tolerance = max(1.8, tolerance)
    lower_cap = recent_blend - tolerance
    upper_cap = recent_blend + tolerance
    capped = _clamp(projected, lower_cap, upper_cap)
    if abs(capped - projected) >= 0.05:
        notes.append("stable-context recent blend cap")
    stable_upside_tolerance = None
    if role_state.bucket == "core_starter":
        stable_upside_tolerance = 1.4
    elif role_state.bucket == "starter_volatile":
        stable_upside_tolerance = 1.8
    elif role_state.bucket == "rotation":
        stable_upside_tolerance = 2.0
    if stable_upside_tolerance is not None:
        stable_upside_cap = max(
            role_state.lower_bound,
            min(
                role_state.upper_bound,
                max(recent_blend + stable_upside_tolerance, last_10_minutes_avg + 0.8),
            ),
        )
        if capped > stable_upside_cap:
            capped = stable_upside_cap
            notes.append("stable-context upside cap")
    return capped, notes


def _apply_minutes_rebound_guard(
    *,
    projected: float,
    role_state: MinutesRoleState,
    recent_blend: float,
    last_10_minutes_avg: float,
    minutes_trend: float,
    minute_volatility: float,
    injury_status: str,
    recent_absence_days: float | None,
    lineup_context: list[float] | None,
    team_transition: list[float] | None,
    blowout_delta: float,
) -> tuple[float, list[str]]:
    notes: list[str] = []
    status = str(injury_status or "").strip().lower()
    if status in {"questionable", "gtd", "doubtful"}:
        return projected, notes
    if role_state.bucket not in {"starter_volatile", "rotation"}:
        return projected, notes
    if minutes_trend > -5.0:
        return projected, notes
    normalized_lineup_context = list(lineup_context or [0.0, 0.5, 0.0])
    if len(normalized_lineup_context) < 3:
        normalized_lineup_context = (normalized_lineup_context + [0.0, 0.5, 0.0])[:3]
    recent_team_minute_share = float(normalized_lineup_context[0])
    recent_minute_rank = float(normalized_lineup_context[1])
    recent_position_minute_share = float(normalized_lineup_context[2])
    if recent_team_minute_share < 0.12 or recent_minute_rank < 0.72:
        return projected, notes
    if recent_position_minute_share < 0.22:
        return projected, notes
    if blowout_delta <= -1.75:
        return projected, notes
    games_since_joining_team = float((team_transition or [0.0])[0] if team_transition else 0.0)
    if 0.0 < games_since_joining_team <= 2.0:
        return projected, notes
    if recent_absence_days is not None and recent_absence_days >= 28.0:
        return projected, notes
    if recent_blend - projected < 2.25:
        return projected, notes

    allowance = 1.6
    if role_state.bucket == "rotation":
        allowance = 1.9
    if minute_volatility >= 8.0:
        allowance += 0.5
    if recent_absence_days is not None and recent_absence_days >= 7.0:
        allowance += 0.5
    rebound_floor = max(role_state.lower_bound, max(last_10_minutes_avg - 3.0, recent_blend - allowance))
    floored = max(projected, rebound_floor)
    if floored - projected >= 0.05:
        notes.append("rebound guard strong-role drop")
    return floored, notes


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
    team_id = _historical_row_team_id(row)
    is_home = team_id == int(row["home_team_id"])
    spread_home = float(row["spread_home"]) if row["spread_home"] is not None else None
    return {
        "team_id": team_id,
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
    lineup_context: list[float] | None = None,
    opportunity_context: list[float] | None = None,
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
    normalized_lineup_context = list(lineup_context or [0.0, 0.5, 0.0])
    if len(normalized_lineup_context) < 3:
        normalized_lineup_context = (normalized_lineup_context + [0.0, 0.5, 0.0])[:3]
    recent_team_minute_share = float(normalized_lineup_context[0])
    recent_minute_rank = float(normalized_lineup_context[1])
    recent_position_minute_share = float(normalized_lineup_context[2])
    if recent_team_minute_share > 0.0:
        anchor += _clamp((recent_team_minute_share - 0.14) * 14.0, -1.4, 2.2)
    if recent_minute_rank >= 0.78:
        anchor += 1.0
    elif recent_minute_rank <= 0.42:
        anchor -= 1.0
    if recent_position_minute_share >= 0.58:
        anchor += 0.6
    elif 0.0 < recent_position_minute_share <= 0.34:
        anchor -= 0.4
    normalized_opportunity = list(opportunity_context or [0.0, 0.0])
    if len(normalized_opportunity) < 2:
        normalized_opportunity = (normalized_opportunity + [0.0, 0.0])[:2]
    same_position_unavailable_minutes = float(normalized_opportunity[0])
    same_position_key_out_count = float(normalized_opportunity[1])
    if same_position_unavailable_minutes >= 12.0:
        anchor += min(2.4, same_position_unavailable_minutes / 12.0)
    if same_position_key_out_count >= 1.0 and role_text in {"rotation", "bench"}:
        anchor += 0.8
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
    recent_blend: float,
    recent_minutes_avg: float,
    last_10_minutes_avg: float,
    opportunity_context: list[float] | None = None,
) -> tuple[float, list[str]]:
    notes: list[str] = []
    status = str(injury_status or "").strip().lower()
    role = str(rotation_role or "").strip().lower()
    normalized_opportunity = list(opportunity_context or [0.0, 0.0])
    if len(normalized_opportunity) < 2:
        normalized_opportunity = (normalized_opportunity + [0.0, 0.0])[:2]
    same_position_unavailable_minutes = float(normalized_opportunity[0])
    same_position_key_out_count = float(normalized_opportunity[1])

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
        blowout_cap = max(
            role_state.lower_bound,
            min(role_state.upper_bound, last_10_minutes_avg + (blowout_delta * 0.85)),
        )
        if (
            role_state.bucket in {"core_starter", "starter_volatile"}
            and same_position_unavailable_minutes >= 10.0
            and same_position_key_out_count < 1.0
            and blowout_delta > -1.6
        ):
            blowout_cap = max(
                blowout_cap,
                min(
                    role_state.upper_bound,
                    max(
                        last_10_minutes_avg + (blowout_delta * 0.45),
                        recent_blend - 0.8,
                    ),
                ),
            )
        if projected > blowout_cap:
            projected = blowout_cap
            notes.append("hard rule blowout star cap")

    if injury_delta >= 1.5 and role_state.recent_spike:
        floor = min(role_state.upper_bound, max(role_state.lower_bound, recent_minutes_avg + min(2.5, injury_delta * 1.25)))
        if projected < floor:
            projected = floor
            notes.append("hard rule injury replacement floor")

    if (
        same_position_unavailable_minutes >= 18.0
        and same_position_key_out_count >= 1.0
        and role_state.bucket in {"starter_volatile", "rotation", "bench"}
    ):
        vacancy_lift = min(2.0, same_position_unavailable_minutes / 18.0)
        last_10_floor = last_10_minutes_avg - 0.5
        recent_blend_floor = recent_blend + 0.25
        if role_state.bucket == "starter_volatile":
            vacancy_lift = min(1.35, same_position_unavailable_minutes / 28.0)
            last_10_floor = last_10_minutes_avg - 0.9
            recent_blend_floor = recent_blend + 0.05
            if not role_state.recent_spike:
                vacancy_lift = min(0.45, same_position_unavailable_minutes / 80.0)
                last_10_floor = last_10_minutes_avg - 2.1
                recent_blend_floor = recent_blend - 0.3
        elif role_state.bucket == "rotation":
            vacancy_lift = min(0.65, same_position_unavailable_minutes / 36.0)
            last_10_floor = last_10_minutes_avg - 2.0
            recent_blend_floor = recent_blend - 0.1
            if not role_state.recent_spike:
                vacancy_lift = min(0.3, same_position_unavailable_minutes / 90.0)
                last_10_floor = last_10_minutes_avg - 2.6
                recent_blend_floor = recent_blend - 0.45
        elif role_state.bucket == "bench":
            vacancy_lift = min(1.4, same_position_unavailable_minutes / 22.0)
            last_10_floor = last_10_minutes_avg - 1.0
            recent_blend_floor = recent_blend + 0.15
        opportunity_floor = min(
            role_state.upper_bound,
            max(
                role_state.lower_bound,
                recent_minutes_avg + vacancy_lift,
                last_10_floor,
                recent_blend_floor,
            ),
        )
        if projected < opportunity_floor:
            projected = opportunity_floor
            notes.append("hard rule same-position vacancy floor")

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
    team_transition: list[float] | None = None,
    lineup_context: list[float] | None = None,
    opportunity_context: list[float] | None = None,
) -> list[float]:
    role_flags = {
        "core_starter": 0.0,
        "starter_volatile": 0.0,
        "rotation": 0.0,
        "bench": 0.0,
        "fringe": 0.0,
    }
    role_flags[role_state.bucket] = 1.0
    recent_change_ratio = recent_minutes_avg / max(last_10_minutes_avg, 1.0)
    recent_vs_ewma_gap = recent_minutes_avg - ewma_minutes
    absence_return_flag = 1.0 if recent_absence_days is not None and recent_absence_days >= 7 else 0.0
    normalized_team_transition = list(team_transition or [0.0, 0.0, 0.0, 0.5])
    if len(normalized_team_transition) < 4:
        normalized_team_transition = (normalized_team_transition + [0.0, 0.0, 0.0, 0.5])[:4]
    normalized_lineup_context = list(lineup_context or [0.0, 0.5, 0.0])
    if len(normalized_lineup_context) < 3:
        normalized_lineup_context = (normalized_lineup_context + [0.0, 0.5, 0.0])[:3]
    normalized_opportunity_context = list(opportunity_context or [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])
    if len(normalized_opportunity_context) < 7:
        normalized_opportunity_context = (normalized_opportunity_context + [0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0])[:7]
    return [
        ewma_minutes,
        recent_minutes_avg,
        last_10_minutes_avg,
        minutes_trend,
        minute_volatility,
        float(recent_change_ratio),
        float(recent_vs_ewma_gap),
        absence_return_flag,
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
        float(normalized_team_transition[0]),
        float(normalized_team_transition[1]),
        float(normalized_team_transition[2]),
        float(normalized_team_transition[3]),
        float(normalized_lineup_context[0]),
        float(normalized_lineup_context[1]),
        float(normalized_lineup_context[2]),
        float(normalized_opportunity_context[0]),
        float(normalized_opportunity_context[1]),
        float(normalized_opportunity_context[2]),
        float(normalized_opportunity_context[3]),
        float(normalized_opportunity_context[4]),
        float(normalized_opportunity_context[5]),
        float(normalized_opportunity_context[6]),
    ]


def _minutes_model_weight(
    *,
    rows: int,
    role_bucket: str,
    minute_volatility: float,
    recent_absence_days: float | None,
) -> float:
    if rows >= 400:
        weight = 0.36
    elif rows >= 150:
        weight = 0.27
    else:
        weight = 0.18
    if role_bucket in {"starter_volatile", "rotation"}:
        weight -= 0.03
    if role_bucket in {"bench", "fringe"}:
        weight -= 0.08
    if minute_volatility >= 8.0:
        weight -= 0.06
    if recent_absence_days is not None and recent_absence_days >= 7:
        weight -= 0.08
    return max(0.08, min(0.42, weight))


def _minutes_earned_blend_weight(
    *,
    base_weight: float,
    learned_minutes: float,
    recent_blend: float,
    recency_anchor: float,
    role_bucket: str,
    minute_volatility: float,
    recent_absence_days: float | None,
    recent_drop: bool,
    recent_spike: bool,
    injury_delta: float,
    opportunity_context: list[float] | None = None,
) -> float:
    tolerance = 2.2 if role_bucket == "core_starter" else 2.8 if role_bucket in {"starter_volatile", "rotation"} else 3.2
    if minute_volatility >= 8.0:
        tolerance += 0.5
    if recent_absence_days is not None and recent_absence_days >= 7:
        tolerance += 0.5
    downside_gap = max(recency_anchor - learned_minutes, 0.0)
    upside_gap = max(learned_minutes - recency_anchor, 0.0)
    downside_penalty = min(0.72, downside_gap / max(tolerance, 0.5))
    upside_penalty = min(0.45, upside_gap / max(tolerance * 1.5, 0.5))
    penalty = max(downside_penalty, upside_penalty)
    if recent_drop:
        penalty *= 0.82
    if recent_spike and injury_delta > 0:
        penalty *= 0.78
    if (
        role_bucket in {"starter_volatile", "rotation"}
        and not recent_spike
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
    ):
        unsupported_upside_gap = max(learned_minutes - recent_blend, 0.0)
        if unsupported_upside_gap >= 1.5:
            penalty = max(
                penalty,
                min(0.72, 0.18 + (unsupported_upside_gap - 1.5) / 4.0),
            )
    if (
        role_bucket == "starter_volatile"
        and recent_spike
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
    ):
        unsupported_spike_gap = max(learned_minutes - recency_anchor, 0.0)
        normalized_opportunity = list(opportunity_context or [0.0, 0.0])
        if len(normalized_opportunity) < 2:
            normalized_opportunity = (normalized_opportunity + [0.0, 0.0])[:2]
        same_position_unavailable_minutes = float(normalized_opportunity[0])
        same_position_key_out_count = float(normalized_opportunity[1])
        strong_vacancy = same_position_unavailable_minutes >= 18.0 and same_position_key_out_count >= 1.0
        soft_vacancy = same_position_unavailable_minutes >= 10.0 or same_position_key_out_count >= 1.0
        if unsupported_spike_gap >= 1.0 and not strong_vacancy:
            base_penalty = 0.22 if soft_vacancy else 0.28
            penalty = max(
                penalty,
                min(0.72, base_penalty + (unsupported_spike_gap - 1.0) / 4.5),
            )
    if (
        role_bucket in {"starter_volatile", "rotation"}
        and minute_volatility < 5.0
        and not recent_drop
        and not recent_spike
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
    ):
        stable_low_vol_penalty = 0.24 if role_bucket == "starter_volatile" else 0.30
        stable_low_vol_gap = abs(learned_minutes - recent_blend)
        if stable_low_vol_gap >= 1.0:
            stable_low_vol_penalty += min(0.14, (stable_low_vol_gap - 1.0) / 6.0)
        penalty = max(penalty, min(0.72, stable_low_vol_penalty))
    normalized_opportunity = list(opportunity_context or [0.0, 0.0])
    if len(normalized_opportunity) < 2:
        normalized_opportunity = (normalized_opportunity + [0.0, 0.0])[:2]
    same_position_unavailable_minutes = float(normalized_opportunity[0])
    same_position_key_out_count = float(normalized_opportunity[1])
    if (
        role_bucket in {"starter_volatile", "rotation"}
        and minute_volatility < 5.0
        and not recent_drop
        and not recent_spike
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and same_position_unavailable_minutes < 18.0
        and same_position_key_out_count < 1.0
    ):
        quiet_context_penalty = 0.42 if role_bucket == "starter_volatile" else 0.48
        quiet_context_gap = abs(learned_minutes - recent_blend)
        if quiet_context_gap >= 0.75:
            quiet_context_penalty += min(0.12, (quiet_context_gap - 0.75) / 5.0)
        penalty = max(penalty, min(0.78, quiet_context_penalty))
    if (
        role_bucket == "rotation"
        and minute_volatility < 7.5
        and not recent_drop
        and not recent_spike
        and injury_delta <= 0.0
        and (recent_absence_days is None or recent_absence_days < 7.0)
        and same_position_unavailable_minutes < 10.0
        and same_position_key_out_count < 1.0
    ):
        quiet_rotation_upside_gap = max(learned_minutes - recent_blend, 0.0)
        if quiet_rotation_upside_gap >= 1.5:
            quiet_rotation_penalty = 0.44 + min(0.18, (quiet_rotation_upside_gap - 1.5) / 4.0)
            if minute_volatility < 6.0:
                quiet_rotation_penalty += 0.04
            penalty = max(penalty, min(0.78, quiet_rotation_penalty))
    earned_weight = base_weight * (1.0 - penalty)
    return max(0.03, min(base_weight, earned_weight))


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


def _ridge_regression(
    xs: list[list[float]],
    ys: list[float],
    penalty: float,
    weights: list[float] | None = None,
) -> list[float]:
    if np is not None:
        return _ridge_regression_numpy(xs, ys, penalty, weights=weights)
    feature_count = len(xs[0]) + 1
    matrix = [[0.0 for _ in range(feature_count)] for _ in range(feature_count)]
    vector = [0.0 for _ in range(feature_count)]
    if weights is None:
        row_weights = [1.0 for _ in xs]
    else:
        row_weights = [max(float(weight), 1e-9) for weight in weights]
    for features, target, row_weight in zip(xs, ys, row_weights):
        row = [1.0, *features]
        for i in range(feature_count):
            vector[i] += row_weight * row[i] * target
            for j in range(feature_count):
                matrix[i][j] += row_weight * row[i] * row[j]
    for i in range(1, feature_count):
        matrix[i][i] += penalty
    return _solve_linear_system(matrix, vector)


def _ridge_regression_numpy(
    xs: list[list[float]],
    ys: list[float],
    penalty: float,
    weights: list[float] | None = None,
) -> list[float]:
    design = np.asarray(xs, dtype=float)
    targets = np.asarray(ys, dtype=float)
    intercept = np.ones((design.shape[0], 1), dtype=float)
    design = np.concatenate((intercept, design), axis=1)
    if weights is not None:
        sqrt_weights = np.sqrt(np.asarray(weights, dtype=float)).reshape(-1, 1)
        design = design * sqrt_weights
        targets = targets * sqrt_weights[:, 0]

    xtx = design.T @ design
    regularization = np.eye(xtx.shape[0], dtype=float)
    regularization[0, 0] = 0.0
    xtx = xtx + (regularization * float(penalty))
    xty = design.T @ targets
    try:
        solution = np.linalg.solve(xtx, xty)
    except np.linalg.LinAlgError:
        solution = np.linalg.lstsq(xtx, xty, rcond=None)[0]
    return solution.tolist()


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


def _market_depth_scale(market: str) -> float:
    if market in {"points", "rebounds"}:
        return 1.0
    if market == "assists":
        return 0.95
    if market == "threes":
        return 0.88
    if market in {"points_rebounds", "points_assists", "rebounds_assists"}:
        return 0.78
    if market == "points_rebounds_assists":
        return 0.68
    if market in {"steals", "blocks", "blocks_steals"}:
        return 0.72
    return 0.85


def _market_line_weight(
    market: str,
    rows: int,
    sample_count: int,
    avg_minutes: float,
    config: ModelTuningConfig | None = None,
) -> float:
    depth_scale = _market_depth_scale(market)
    market_weight = _market_weight(rows, config=config) * depth_scale
    player_weight = _player_market_weight(sample_count, avg_minutes, config=config) * depth_scale
    return max(market_weight, player_weight)


def _component_market_line_weight(
    market: str,
    sample_count: int,
    avg_minutes: float,
) -> float:
    depth_scale = _market_depth_scale(market)
    player_weight = _player_market_weight(sample_count, avg_minutes) * depth_scale
    cap_by_market = {
        "points": 0.18,
        "rebounds": 0.16,
        "assists": 0.16,
        "points_rebounds": 0.14,
        "points_assists": 0.14,
        "rebounds_assists": 0.14,
        "points_rebounds_assists": 0.12,
        "threes": 0.12,
        "steals": 0.10,
        "blocks": 0.10,
        "blocks_steals": 0.09,
    }
    cap = cap_by_market.get(market, 0.15)
    return max(0.05, min(cap, player_weight * 0.42))


def _residual_market_weight(
    market: str,
    rows: int,
    sample_count: int,
    avg_minutes: float,
    config: ModelTuningConfig | None = None,
) -> float:
    # Three-season walk-forward evaluation found negative residual MAE deltas
    # for every live market except points+assists. Keep that one as a bounded
    # experiment only after it has meaningful line-relative support.
    if market != "points_assists" or rows < 200:
        return 0.0
    tuning = config or DEFAULT_TUNING_CONFIG
    player_floor = 0.06 if sample_count < 8 or avg_minutes < 20.0 else 0.09 if sample_count < 15 else 0.12
    return _scaled_market_weight(max(0.17, player_floor) * _market_depth_scale(market), tuning.market_weight_scale)


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
    cache = _connection_training_cache_bucket(conn, "model_fingerprint")
    cached = cache.get(("current",))
    if isinstance(cached, str) and cached:
        return cached
    parts = [
        _table_signature(
            conn,
            "games",
            (
                "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, "
                "COALESCE(MAX(game_date), '') AS max_game_date, "
                "SUM(CASE WHEN spread_home IS NOT NULL THEN 1 ELSE 0 END) AS spread_rows, "
                "ROUND(COALESCE(SUM(spread_home), 0), 3) AS spread_sum, "
                "SUM(CASE WHEN game_total IS NOT NULL THEN 1 ELSE 0 END) AS total_rows, "
                "ROUND(COALESCE(SUM(game_total), 0), 3) AS total_sum"
            ),
        ),
        _table_signature(
            conn,
            "player_game_stats",
            (
                "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, "
                "ROUND(COALESCE(SUM(minutes), 0), 3) AS minutes_sum, "
                "ROUND(COALESCE(SUM(points + rebounds + assists + steals + blocks + turnovers), 0), 3) AS stat_sum"
            ),
        ),
        _table_signature(
            conn,
            "player_team_history",
            "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, COALESCE(MAX(game_id), 0) AS max_game_id",
        ),
        _table_signature(
            conn,
            "team_game_results",
            (
                "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, "
                "ROUND(COALESCE(SUM(points + opponent_points), 0), 3) AS scoring_sum, "
                "ROUND(COALESCE(SUM(possessions), 0), 3) AS possessions_sum, "
                "SUM(CASE WHEN COALESCE(possessions_source, 'fallback') != 'fallback' THEN 1 ELSE 0 END) AS sourced_rows"
            ),
        ),
        _table_signature(
            conn,
            "team_game_boxscores",
            (
                "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, "
                "ROUND(COALESCE(SUM(field_goals_attempted + free_throws_attempted + offensive_rebounds), 0), 3) AS volume_sum, "
                "ROUND(COALESCE(SUM(COALESCE(total_turnovers, turnovers + team_turnovers, turnovers, team_turnovers, 0)), 0), 3) AS turnover_sum, "
                "ROUND(COALESCE(SUM(possessions), 0), 3) AS possessions_sum"
            ),
        ),
        _table_signature(
            conn,
            "settled_props",
            (
                "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, "
                "ROUND(COALESCE(SUM(actual_result + COALESCE(team_possessions, 0) + COALESCE(net_rating, 0)), 0), 3) AS outcome_sum"
            ),
        ),
        _table_signature(
            conn,
            "players",
            "COUNT(*) AS row_count, COALESCE(MAX(id), 0) AS max_id, COALESCE(MAX(team_id), 0) AS max_team_id",
        ),
    ]
    fingerprint = hashlib.sha1("|".join(parts).encode("utf-8")).hexdigest()[:16]
    cache[("current",)] = fingerprint
    return fingerprint


def _table_signature(conn: sqlite3.Connection, table: str, select_sql: str) -> str:
    row = conn.execute(f"SELECT {select_sql} FROM {table}").fetchone()
    values = [f"{key}={row[key]}" for key in row.keys()]
    return f"{table}:" + ",".join(values)


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
    cache = _connection_training_cache_bucket(conn, "model_cache_key")
    db_marker = hashlib.sha1(str(db_path).encode("utf-8")).hexdigest()[:12]
    safe_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name)
    training_marker = hashlib.sha1(_training_start_date().encode("utf-8")).hexdigest()[:8]
    version_marker = hashlib.sha1(MODEL_VERSION.encode("utf-8")).hexdigest()[:8]
    config_marker = _config_fingerprint(config)
    cache_key = (str(db_path), str(name), str(kind), config_marker, training_marker, version_marker)
    cached = cache.get(cache_key)
    if isinstance(cached, str) and cached:
        return cached
    resolved = (
        f"{MODEL_CACHE_PREFIX}-{kind}-{safe_name}-{db_marker}-{_model_fingerprint(conn)}-"
        f"{config_marker}-{training_marker}-{version_marker}.json"
    )
    cache[cache_key] = resolved
    return resolved


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


def _market_specific_component_projection(
    *,
    market: str,
    component_base: float,
    rate_projection: float,
    last_10_avg: float,
    consistency_score: float,
    value_volatility: float,
) -> float:
    stable_anchor = (0.55 * rate_projection) + (0.45 * last_10_avg)
    low_consistency = consistency_score < 0.45
    high_volatility = value_volatility > max(2.5, abs(component_base) * 0.38)
    if market == "points":
        if low_consistency or high_volatility:
            return (0.90 * component_base) + (0.10 * stable_anchor)
        return component_base
    if market in {"points_rebounds", "points_assists", "points_rebounds_assists"}:
        if low_consistency or high_volatility:
            return (0.86 * component_base) + (0.14 * stable_anchor)
        return component_base
    if market in {"steals", "blocks", "blocks_steals"}:
        return (0.92 * component_base) + (0.08 * stable_anchor)
    return component_base


def _ewma_newest_first(values: list[float], alpha: float) -> float:
    if not values:
        return 0.0
    estimate = values[-1]
    for value in reversed(values[:-1]):
        estimate = (alpha * value) + ((1 - alpha) * estimate)
    return estimate


def _quantile(sorted_values: list[float], q: float) -> float:
    if not sorted_values:
        return 0.0
    if len(sorted_values) == 1:
        return float(sorted_values[0])
    position = max(0.0, min(float(len(sorted_values) - 1), q * float(len(sorted_values) - 1)))
    lower = int(math.floor(position))
    upper = int(math.ceil(position))
    if lower == upper:
        return float(sorted_values[lower])
    weight = position - lower
    return ((1.0 - weight) * float(sorted_values[lower])) + (weight * float(sorted_values[upper]))


def _winsorize_history_values(values: list[float], market: str) -> list[float]:
    if len(values) < 4:
        return [max(0.0, float(value)) for value in values]
    sorted_values = sorted(float(value) for value in values)
    q1 = _quantile(sorted_values, 0.25)
    q3 = _quantile(sorted_values, 0.75)
    iqr = max(q3 - q1, _market_volatility_floor(market) * 0.5)
    scale = MARKET_WINSORIZATION_SCALES.get(market, 1.6)
    lower = max(0.0, q1 - (scale * iqr))
    upper = q3 + (scale * iqr)
    return [_clamp(float(value), lower, upper) for value in values]


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
            SELECT s.*, g.game_date, p.rotation_role,
                (
                    SELECT ABS(tgr.points - tgr.opponent_points)
                    FROM team_game_results tgr
                    WHERE tgr.game_id = s.game_id
                      AND tgr.team_id = COALESCE((
                        SELECT h.team_id FROM player_team_history h
                        WHERE h.player_id = s.player_id AND h.game_id = s.game_id
                        ORDER BY h.id DESC LIMIT 1
                      ), p.team_id)
                    LIMIT 1
                ) AS team_margin
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
            SELECT s.*, g.game_date, p.rotation_role,
                (
                    SELECT ABS(tgr.points - tgr.opponent_points)
                    FROM team_game_results tgr
                    WHERE tgr.game_id = s.game_id
                      AND tgr.team_id = COALESCE((
                        SELECT h.team_id FROM player_team_history h
                        WHERE h.player_id = s.player_id AND h.game_id = s.game_id
                        ORDER BY h.id DESC LIMIT 1
                      ), p.team_id)
                    LIMIT 1
                ) AS team_margin
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
            "is_blowout": bool(float(row["team_margin"] or 0.0) >= 15.0),
        }
        for row in rows
    ]


def _player_recent_feature_rows(
    conn: sqlite3.Connection,
    player_id: int,
    before_game_date: str | None,
    exclude_game_id: int | None,
) -> list[sqlite3.Row]:
    filters = ["s.player_id = ?"]
    params: list[object] = [player_id]
    if before_game_date is not None:
        filters.append("g.game_date < ?")
        params.append(before_game_date)
    if exclude_game_id is not None:
        filters.append("s.game_id != ?")
        params.append(exclude_game_id)
    if before_game_date is not None:
        return conn.execute(
            f"""
            SELECT s.*, g.game_date, p.rotation_role, p.position,
                COALESCE((
                    SELECT h.team_id FROM player_team_history h
                    WHERE h.player_id = s.player_id AND h.game_id = s.game_id
                    ORDER BY h.id DESC LIMIT 1
                ), p.team_id) AS resolved_team_id,
                (
                    SELECT ABS(tgr.points - tgr.opponent_points)
                    FROM team_game_results tgr
                    WHERE tgr.game_id = s.game_id
                      AND tgr.team_id = COALESCE((
                        SELECT h.team_id FROM player_team_history h
                        WHERE h.player_id = s.player_id AND h.game_id = s.game_id
                        ORDER BY h.id DESC LIMIT 1
                      ), p.team_id)
                    LIMIT 1
                ) AS team_margin
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
    return conn.execute(
        f"""
        SELECT s.*, g.game_date, p.rotation_role, p.position,
            COALESCE((
                SELECT h.team_id FROM player_team_history h
                WHERE h.player_id = s.player_id AND h.game_id = s.game_id
                ORDER BY h.id DESC LIMIT 1
            ), p.team_id) AS resolved_team_id,
            (
                SELECT ABS(tgr.points - tgr.opponent_points)
                FROM team_game_results tgr
                WHERE tgr.game_id = s.game_id
                  AND tgr.team_id = COALESCE((
                    SELECT h.team_id FROM player_team_history h
                    WHERE h.player_id = s.player_id AND h.game_id = s.game_id
                    ORDER BY h.id DESC LIMIT 1
                  ), p.team_id)
                LIMIT 1
            ) AS team_margin
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN players p ON p.id = s.player_id
        WHERE {' AND '.join(filters)}
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT 10
        """,
        params,
    ).fetchall()


def _team_transition_features(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    team_id: int,
    history_rows: list[sqlite3.Row],
) -> list[float]:
    """Describe the player's current-team tenure using only completed games."""
    current_team_rows: list[sqlite3.Row] = []
    for row in history_rows:
        if _historical_row_team_id(row) != team_id:
            break
        current_team_rows.append(row)
    return _team_transition_features_from_games(conn, player_id, current_team_rows)


def _team_transition_features_from_player_rows(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    team_id: int,
    player_rows: list[sqlite3.Row],
    row_index: int | None,
) -> list[float]:
    if row_index is None:
        return [0.0, 0.0, 0.0, 0.5]
    current_team_rows: list[sqlite3.Row] = []
    for row in reversed(player_rows[:row_index]):
        if _historical_row_team_id(row) != team_id:
            break
        current_team_rows.append(row)
    return _team_transition_features_from_games(conn, player_id, current_team_rows)


def _minutes_lineup_context(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    team_id: int,
    history_rows: list[sqlite3.Row],
) -> list[float]:
    current_team_rows: list[sqlite3.Row] = []
    for row in history_rows:
        if _historical_row_team_id(row) != team_id:
            break
        current_team_rows.append(row)
    return _minutes_lineup_context_from_games(conn, player_id, current_team_rows)


def _minutes_lineup_context_from_player_rows(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    team_id: int,
    player_rows: list[sqlite3.Row],
    row_index: int | None,
) -> list[float]:
    if row_index is None:
        return [0.0, 0.5, 0.0]
    current_team_rows: list[sqlite3.Row] = []
    for row in reversed(player_rows[:row_index]):
        if _historical_row_team_id(row) != team_id:
            break
        current_team_rows.append(row)
    return _minutes_lineup_context_from_games(conn, player_id, current_team_rows)


def _minutes_lineup_context_from_games(
    conn: sqlite3.Connection,
    player_id: int,
    current_team_rows: list[sqlite3.Row],
) -> list[float]:
    if not current_team_rows:
        return [0.0, 0.5, 0.0]

    recent_rows = current_team_rows[:8]
    game_ids = tuple(int(row["game_id"]) for row in recent_rows)
    team_id = int(_historical_row_team_id(recent_rows[0]) or 0)
    position_prefix = str(recent_rows[0]["position"] or "").strip().upper()[:1]
    cache = _connection_training_cache_bucket(conn, "minutes_lineup_context")
    cache_key = (int(player_id), team_id, position_prefix, game_ids)
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)  # type: ignore[arg-type]

    placeholders = ",".join("?" for _ in game_ids)
    roster_rows = conn.execute(
        f"""
        SELECT DISTINCT h.game_id, h.player_id, s.minutes, p.position
        FROM player_team_history h
        JOIN player_game_stats s
          ON s.player_id = h.player_id AND s.game_id = h.game_id
        JOIN players p ON p.id = h.player_id
        WHERE h.game_id IN ({placeholders})
          AND h.team_id = ?
        """,
        (*game_ids, team_id),
    ).fetchall()

    roster_minutes: dict[int, list[tuple[int, float, str]]] = {game_id: [] for game_id in game_ids}
    for row in roster_rows:
        roster_minutes[int(row["game_id"])].append(
            (
                int(row["player_id"]),
                float(row["minutes"] or 0.0),
                str(row["position"] or "").strip().upper()[:1],
            )
        )

    team_shares: list[float] = []
    minute_ranks: list[float] = []
    position_shares: list[float] = []
    for row in recent_rows:
        game_id = int(row["game_id"])
        player_minutes = float(row["minutes"] or 0.0)
        game_roster = roster_minutes.get(game_id) or []
        if not game_roster:
            continue
        total_minutes = sum(item[1] for item in game_roster)
        if total_minutes > 0.0:
            team_shares.append(player_minutes / total_minutes)
        ordered_minutes = sorted((item[1] for item in game_roster), reverse=True)
        if ordered_minutes:
            rank_index = next((idx for idx, value in enumerate(ordered_minutes) if abs(value - player_minutes) < 0.01), len(ordered_minutes) - 1)
            minute_ranks.append(1.0 if len(ordered_minutes) == 1 else 1.0 - (rank_index / max(len(ordered_minutes) - 1, 1)))
        if position_prefix:
            position_total = sum(item[1] for item in game_roster if item[2] == position_prefix)
            if position_total > 0.0:
                position_shares.append(player_minutes / position_total)

    result = [
        float(sum(team_shares) / len(team_shares)) if team_shares else 0.0,
        float(sum(minute_ranks) / len(minute_ranks)) if minute_ranks else 0.5,
        float(sum(position_shares) / len(position_shares)) if position_shares else 0.0,
    ]
    cache[cache_key] = tuple(result)
    return result


def _team_transition_features_from_games(
    conn: sqlite3.Connection,
    player_id: int,
    current_team_rows: list[sqlite3.Row],
) -> list[float]:
    if not current_team_rows:
        return [0.0, 0.0, 0.0, 0.5]

    recent_rows = current_team_rows[:8]
    minutes = [float(row["minutes"]) for row in recent_rows]
    new_team_minutes_trend = _recent_trend(minutes)
    game_ids = tuple(int(row["game_id"]) for row in recent_rows)
    cache = _connection_training_cache_bucket(conn, "team_transition_features")
    cache_key = (int(player_id), game_ids)
    cached = cache.get(cache_key)
    if cached is not None:
        teammate_shift, rotation_stability = cached  # type: ignore[misc]
    else:
        placeholders = ",".join("?" for _ in game_ids)
        roster_rows = conn.execute(
            f"""
            SELECT DISTINCT h.game_id, h.player_id, s.minutes
            FROM player_team_history h
            JOIN player_game_stats s
              ON s.player_id = h.player_id AND s.game_id = h.game_id
            WHERE h.game_id IN ({placeholders})
            """,
            game_ids,
        ).fetchall()
        teammate_minutes: dict[int, float] = {game_id: 0.0 for game_id in game_ids}
        rosters: dict[int, set[int]] = {game_id: set() for game_id in game_ids}
        for row in roster_rows:
            game_id = int(row["game_id"])
            teammate_id = int(row["player_id"])
            if teammate_id == player_id:
                continue
            teammate_minutes[game_id] += float(row["minutes"])
            rosters[game_id].add(teammate_id)
        minute_totals = [teammate_minutes[game_id] for game_id in game_ids]
        recent_avg = sum(minute_totals[:3]) / min(len(minute_totals), 3)
        prior_values = minute_totals[3:6]
        prior_avg = sum(prior_values) / len(prior_values) if prior_values else recent_avg
        teammate_shift = _clamp((prior_avg - recent_avg) / 30.0, -1.0, 1.0)
        stability_scores = []
        for current_game_id, previous_game_id in zip(game_ids, game_ids[1:]):
            current_roster = rosters[current_game_id]
            previous_roster = rosters[previous_game_id]
            union = current_roster | previous_roster
            if union:
                stability_scores.append(len(current_roster & previous_roster) / len(union))
        rotation_stability = sum(stability_scores) / len(stability_scores) if stability_scores else 0.5
        cache[cache_key] = (teammate_shift, rotation_stability)

    return [
        float(min(len(current_team_rows), 20)),
        float(new_team_minutes_trend),
        float(teammate_shift),
        float(rotation_stability),
    ]


def _live_minutes_opportunity_context(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    team_id: int,
    position: str | None,
    as_of_date: str | None,
) -> list[float]:
    position_prefix = str(position or "").strip().upper()[:1]
    cache = _connection_training_cache_bucket(conn, "live_minutes_opportunity_context")
    cache_key = (int(player_id), int(team_id), position_prefix, str(as_of_date or ""))
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)  # type: ignore[arg-type]

    date_filter = "AND DATE(i.captured_at) <= DATE(?)" if as_of_date else ""
    params: list[object] = []
    if as_of_date:
        params.append(as_of_date)
    params.append(team_id)
    if as_of_date:
        params.append(as_of_date)
        params.append(as_of_date)
    teammate_rows = conn.execute(
        f"""
        SELECT
            p.id AS player_id,
            p.position,
            p.rotation_role,
            lower(trim(i.status)) AS status,
            COALESCE(
                (
                    SELECT AVG(sample.minutes)
                    FROM (
                        SELECT s.minutes
                        FROM player_game_stats s
                        JOIN games g ON g.id = s.game_id
                        WHERE s.player_id = p.id
                          {"AND DATE(g.game_date) <= DATE(?)" if as_of_date else ""}
                        ORDER BY g.game_date DESC, s.game_id DESC
                        LIMIT 8
                    ) sample
                ),
                0.0
            ) AS recent_minutes
        FROM injuries i
        JOIN players p ON p.id = i.player_id
        WHERE p.team_id = ?
          {date_filter}
          AND i.captured_at = (
              SELECT MAX(i2.captured_at)
              FROM injuries i2
              WHERE i2.player_id = i.player_id
              {date_filter.replace("i.", "i2.")}
          )
        """,
        params,
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
    same_position_unavailable_minutes = 0.0
    same_position_key_out_count = 0.0
    for row in teammate_rows:
        teammate_id = int(row["player_id"])
        if teammate_id == int(player_id):
            continue
        status_weight = miss_weight.get(str(row["status"] or "").strip().lower())
        if status_weight is None:
            continue
        teammate_position = str(row["position"] or "").strip().upper()[:1]
        if position_prefix and teammate_position and teammate_position != position_prefix:
            continue
        recent_minutes = float(row["recent_minutes"] or 0.0)
        if recent_minutes <= 0.0:
            continue
        weighted_minutes = recent_minutes * status_weight
        same_position_unavailable_minutes += weighted_minutes
        if weighted_minutes >= 14.0 or str(row["rotation_role"] or "").strip().lower() in {"star", "starter"}:
            same_position_key_out_count += 1.0
    opportunity_persistence, competition_minutes, opportunity_trend, competition_trend, returner_pressure = _same_position_context_history_features(
        conn,
        player_id=player_id,
        team_id=team_id,
        position=position,
        before_game_date=as_of_date,
    )
    result = [
        float(same_position_unavailable_minutes),
        float(same_position_key_out_count),
        float(opportunity_persistence),
        float(competition_minutes),
        float(opportunity_trend),
        float(competition_trend),
        float(returner_pressure),
    ]
    cache[cache_key] = tuple(result)
    return result


def _historical_minutes_opportunity_context(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    game_id: int,
    team_id: int,
    position: str | None,
    before_game_date: str | None,
) -> list[float]:
    position_prefix = str(position or "").strip().upper()[:1]
    cache = _connection_training_cache_bucket(conn, "historical_minutes_opportunity_context")
    cache_key = (int(player_id), int(game_id), int(team_id), position_prefix, str(before_game_date or ""))
    cached = cache.get(cache_key)
    if cached is not None:
        return list(cached)  # type: ignore[arg-type]

    teammate_rows = conn.execute(
        """
        SELECT
            pga.player_id,
            p.position,
            p.rotation_role,
            pga.did_not_play,
            COALESCE(
                (
                    SELECT AVG(sample.minutes)
                    FROM (
                        SELECT s.minutes
                        FROM player_game_stats s
                        JOIN games g ON g.id = s.game_id
                        WHERE s.player_id = pga.player_id
                          AND DATE(g.game_date) < DATE(?)
                        ORDER BY g.game_date DESC, s.game_id DESC
                        LIMIT 8
                    ) sample
                ),
                0.0
            ) AS recent_minutes
        FROM player_game_availability pga
        JOIN players p ON p.id = pga.player_id
        WHERE pga.game_id = ?
          AND pga.team_id = ?
          AND pga.did_not_play = 1
        """,
        (before_game_date, int(game_id), int(team_id)),
    ).fetchall()

    same_position_unavailable_minutes = 0.0
    same_position_key_out_count = 0.0
    for row in teammate_rows:
        teammate_id = int(row["player_id"])
        if teammate_id == int(player_id):
            continue
        teammate_position = str(row["position"] or "").strip().upper()[:1]
        if position_prefix and teammate_position and teammate_position != position_prefix:
            continue
        recent_minutes = float(row["recent_minutes"] or 0.0)
        if recent_minutes <= 0.0:
            continue
        same_position_unavailable_minutes += recent_minutes
        if recent_minutes >= 14.0 or str(row["rotation_role"] or "").strip().lower() in {"star", "starter"}:
            same_position_key_out_count += 1.0
    opportunity_persistence, competition_minutes, opportunity_trend, competition_trend, returner_pressure = _same_position_context_history_features(
        conn,
        player_id=player_id,
        team_id=team_id,
        position=position,
        before_game_date=before_game_date,
    )
    result = [
        float(same_position_unavailable_minutes),
        float(same_position_key_out_count),
        float(opportunity_persistence),
        float(competition_minutes),
        float(opportunity_trend),
        float(competition_trend),
        float(returner_pressure),
    ]
    cache[cache_key] = tuple(result)
    return result


def _same_position_context_history_features(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    team_id: int,
    position: str | None,
    before_game_date: str | None,
) -> tuple[float, float, float, float, float]:
    position_prefix = str(position or "").strip().upper()[:1]
    cache = _connection_training_cache_bucket(conn, "same_position_context_history_features")
    cache_key = (int(player_id), int(team_id), position_prefix, str(before_game_date or ""))
    cached = cache.get(cache_key)
    if cached is not None:
        return tuple(cached)  # type: ignore[return-value]

    if not before_game_date:
        cache[cache_key] = (0.0, 0.0, 0.0, 0.0, 0.0)
        return 0.0, 0.0, 0.0, 0.0, 0.0

    recent_games = conn.execute(
        """
        SELECT id, game_date
        FROM games
        WHERE DATE(game_date) < DATE(?)
          AND (home_team_id = ? OR away_team_id = ?)
        ORDER BY game_date DESC, id DESC
        LIMIT 4
        """,
        (before_game_date, int(team_id), int(team_id)),
    ).fetchall()
    if not recent_games:
        cache[cache_key] = (0.0, 0.0, 0.0, 0.0, 0.0)
        return 0.0, 0.0, 0.0, 0.0, 0.0

    opportunity_totals: list[float] = []
    competition_totals: list[float] = []
    for game in recent_games:
        historical_context = _historical_minutes_opportunity_context_for_game(
            conn,
            player_id=player_id,
            game_id=int(game["id"]),
            team_id=team_id,
            position=position_prefix,
            game_date=str(game["game_date"] or ""),
        )
        opportunity_totals.append(float(historical_context[0]))
        competition_totals.append(float(historical_context[1]))

    opportunity_persistence = sum(opportunity_totals) / len(opportunity_totals) if opportunity_totals else 0.0
    competition_minutes = sum(competition_totals) / len(competition_totals) if competition_totals else 0.0
    recent_opportunity = sum(opportunity_totals[:2]) / min(len(opportunity_totals), 2) if opportunity_totals else 0.0
    prior_opportunity_values = opportunity_totals[2:4]
    prior_opportunity = (
        sum(prior_opportunity_values) / len(prior_opportunity_values)
        if prior_opportunity_values
        else recent_opportunity
    )
    recent_competition = sum(competition_totals[:2]) / min(len(competition_totals), 2) if competition_totals else 0.0
    prior_competition_values = competition_totals[2:4]
    prior_competition = (
        sum(prior_competition_values) / len(prior_competition_values)
        if prior_competition_values
        else recent_competition
    )
    opportunity_trend = recent_opportunity - prior_opportunity
    competition_trend = recent_competition - prior_competition
    returner_pressure = max(competition_trend - opportunity_trend, 0.0)
    result = (
        float(opportunity_persistence),
        float(competition_minutes),
        float(opportunity_trend),
        float(competition_trend),
        float(returner_pressure),
    )
    cache[cache_key] = result
    return result


def _historical_minutes_opportunity_context_for_game(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    game_id: int,
    team_id: int,
    position: str,
    game_date: str,
) -> tuple[float, float]:
    opportunity_total = 0.0
    competition_total = 0.0

    unavailable_rows = conn.execute(
        """
        SELECT
            pga.player_id,
            p.position,
            p.rotation_role,
            COALESCE(
                (
                    SELECT AVG(sample.minutes)
                    FROM (
                        SELECT s.minutes
                        FROM player_game_stats s
                        JOIN games g ON g.id = s.game_id
                        WHERE s.player_id = pga.player_id
                          AND DATE(g.game_date) < DATE(?)
                        ORDER BY g.game_date DESC, s.game_id DESC
                        LIMIT 8
                    ) sample
                ),
                0.0
            ) AS recent_minutes
        FROM player_game_availability pga
        JOIN players p ON p.id = pga.player_id
        WHERE pga.game_id = ?
          AND pga.team_id = ?
          AND pga.did_not_play = 1
        """,
        (game_date, int(game_id), int(team_id)),
    ).fetchall()
    for row in unavailable_rows:
        teammate_id = int(row["player_id"])
        if teammate_id == int(player_id):
            continue
        teammate_position = str(row["position"] or "").strip().upper()[:1]
        if position and teammate_position and teammate_position != position:
            continue
        recent_minutes = float(row["recent_minutes"] or 0.0)
        if recent_minutes > 0.0:
            opportunity_total += recent_minutes

    competition_rows = conn.execute(
        """
        SELECT s.player_id, p.position, s.minutes
        FROM player_game_stats s
        JOIN players p ON p.id = s.player_id
        WHERE s.game_id = ?
          AND p.team_id = ?
        """,
        (int(game_id), int(team_id)),
    ).fetchall()
    for row in competition_rows:
        teammate_id = int(row["player_id"])
        if teammate_id == int(player_id):
            continue
        teammate_position = str(row["position"] or "").strip().upper()[:1]
        if position and teammate_position and teammate_position != position:
            continue
        competition_total += float(row["minutes"] or 0.0)

    return float(opportunity_total), float(competition_total)


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


def _player_archetype_profile_from_rows(
    rows: list[sqlite3.Row],
    *,
    fallback_rotation_role: str,
) -> PlayerArchetypeProfile:
    if not rows:
        return PlayerArchetypeProfile(False, False, False, False, False)
    sample = rows[-10:]
    sample_count = len(sample)
    avg_minutes = sum(float(row["minutes"] or 0.0) for row in sample) / sample_count
    avg_points = sum(float(row["points"] or 0.0) for row in sample) / sample_count
    avg_rebounds = sum(float(row["rebounds"] or 0.0) for row in sample) / sample_count
    avg_assists = sum(float(row["assists"] or 0.0) for row in sample) / sample_count
    avg_threes = sum(float(row["threes"] or 0.0) for row in sample) / sample_count
    avg_stocks = sum(float((row["steals"] or 0.0) + (row["blocks"] or 0.0)) for row in sample) / sample_count
    position = str(sample[-1]["position"] or "").upper() if "position" in sample[-1].keys() else ""
    rotation_role = str(sample[-1]["rotation_role"] or fallback_rotation_role or "").lower()

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


def _connection_training_cache_bucket(conn: sqlite3.Connection, name: str) -> dict[tuple, object]:
    conn_key = (id(conn), _connection_training_cache_db_marker(conn))
    buckets = _CONNECTION_TRAINING_CACHE.setdefault(conn_key, {})
    bucket = buckets.get(name)
    if bucket is None:
        bucket = {}
        buckets[name] = bucket
    return bucket  # type: ignore[return-value]


def _connection_training_cache_db_marker(conn: sqlite3.Connection) -> str:
    try:
        row = conn.execute("PRAGMA database_list").fetchone()
    except sqlite3.Error:
        return ""
    if row is None:
        return ""
    if "file" in row.keys():
        return str(row["file"] or "")
    return str(row[2] or "") if len(row) > 2 else ""


def _player_training_rows(conn: sqlite3.Connection, player_id: int) -> list[sqlite3.Row]:
    cache = _connection_training_cache_bucket(conn, "player_rows")
    key = (int(player_id),)
    if key not in cache:
        cache[key] = conn.execute(
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
                p.position,
                p.team_id,
                p.rotation_role,
                COALESCE(
                    (
                        SELECT h.team_id
                        FROM player_team_history h
                        LEFT JOIN games hg ON hg.id = h.game_id
                        WHERE h.player_id = s.player_id
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
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            JOIN players p ON p.id = s.player_id
            WHERE s.player_id = ?
            ORDER BY g.game_date ASC, s.game_id ASC
            """,
            (int(player_id),),
        ).fetchall()
    return cache[key]  # type: ignore[return-value]


def _player_training_row_for_game(
    conn: sqlite3.Connection,
    player_id: int,
    game_id: int,
) -> sqlite3.Row | None:
    for row in _player_training_rows(conn, player_id):
        if int(row["game_id"]) == int(game_id):
            return row
    return None


def _cached_pace_factor(conn: sqlite3.Connection, team_id: int, opponent_id: int) -> float:
    cache = _connection_training_cache_bucket(conn, "pace_factor")
    key = (int(team_id), int(opponent_id))
    if key not in cache:
        cache[key] = _pace_factor(conn, int(team_id), int(opponent_id))
    return float(cache[key])


def _cached_opponent_factor(conn: sqlite3.Connection, opponent_id: int, market: str) -> float:
    cache = _connection_training_cache_bucket(conn, "opponent_factor")
    key = (int(opponent_id), str(market))
    if key not in cache:
        cache[key] = _opponent_factor(conn, int(opponent_id), str(market))
    return float(cache[key])


def _cached_common_opponent_factor(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    context: dict,
    before_game_date: str | None,
    baseline_avg: float | None = None,
) -> float:
    cache = _connection_training_cache_bucket(conn, "common_opponent_factor")
    key = (
        int(player_id),
        str(market),
        int(context["team_id"]),
        int(context["opponent_id"]),
        str(before_game_date or ""),
        round(float(baseline_avg), 6) if baseline_avg is not None else None,
    )
    if key not in cache:
        cache[key] = _common_opponent_factor(
            conn,
            int(player_id),
            str(market),
            context,
            before_game_date,
            baseline_avg=baseline_avg,
        )
    return float(cache[key])


def _cached_h2h_factor(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    context: dict,
    before_game_date: str | None,
    baseline_avg: float | None = None,
) -> float:
    cache = _connection_training_cache_bucket(conn, "h2h_factor")
    key = (
        int(player_id),
        str(market),
        int(context["opponent_id"]),
        str(before_game_date or ""),
        round(float(baseline_avg), 6) if baseline_avg is not None else None,
    )
    if key not in cache:
        cache[key] = _h2h_factor(
            conn,
            int(player_id),
            str(market),
            context,
            before_game_date,
            baseline_avg=baseline_avg,
        )
    return float(cache[key])


def _cached_player_archetype_profile(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    reference_game_date: str | None,
    exclude_game_id: int | None,
    fallback_rotation_role: str,
) -> PlayerArchetypeProfile:
    cache = _connection_training_cache_bucket(conn, "player_archetype")
    key = (
        int(player_id),
        str(reference_game_date or ""),
        int(exclude_game_id) if exclude_game_id is not None else None,
        str(fallback_rotation_role or ""),
    )
    if key not in cache:
        cache[key] = _player_archetype_profile(
            conn,
            player_id=int(player_id),
            reference_game_date=reference_game_date,
            exclude_game_id=exclude_game_id,
            fallback_rotation_role=fallback_rotation_role,
        )
    return cache[key]  # type: ignore[return-value]


def _cached_recent_opponent_factor(conn: sqlite3.Connection, opponent_id: int, market: str) -> float:
    cache = _connection_training_cache_bucket(conn, "recent_opponent_factor")
    key = (int(opponent_id), str(market))
    if key not in cache:
        cache[key] = _recent_opponent_factor(conn, opponent_id, market)
    return cache[key]  # type: ignore[return-value]


def _cached_team_rating_context(
    conn: sqlite3.Connection,
    team_id: int,
    before_game_date: str | None,
) -> dict[str, float]:
    cache = _connection_training_cache_bucket(conn, "team_rating_context")
    key = (int(team_id), str(before_game_date or ""))
    if key not in cache:
        cache[key] = _team_rating_context(conn, int(team_id), before_game_date)
    return cache[key]  # type: ignore[return-value]


def _cached_league_rating_context(
    conn: sqlite3.Connection,
    before_game_date: str | None,
) -> dict[str, float]:
    cache = _connection_training_cache_bucket(conn, "league_rating_context")
    key = str(before_game_date or "")
    if key not in cache:
        cache[key] = _league_rating_context(conn, before_game_date)
    return cache[key]  # type: ignore[return-value]


def _cached_player_common_opponent_rows(
    conn: sqlite3.Connection,
    player_id: int,
    common_opponents: tuple[int, ...],
    before_game_date: str | None,
) -> list[sqlite3.Row]:
    cache = _connection_training_cache_bucket(conn, "player_common_rows")
    key = (int(player_id), common_opponents, str(before_game_date or ""))
    if key not in cache:
        placeholders = ",".join("?" for _ in common_opponents)
        date_filter = "AND g.game_date < ?" if before_game_date is not None else ""
        params: list[object] = [int(player_id), *common_opponents]
        if before_game_date is not None:
            params.append(before_game_date)
        cache[key] = conn.execute(
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
    return cache[key]  # type: ignore[return-value]


def _cached_player_h2h_rows(
    conn: sqlite3.Connection,
    player_id: int,
    opponent_id: int,
    before_game_date: str | None,
) -> list[sqlite3.Row]:
    cache = _connection_training_cache_bucket(conn, "player_h2h_rows")
    key = (int(player_id), int(opponent_id), str(before_game_date or ""))
    if key not in cache:
        date_filter = "AND g.game_date < ?" if before_game_date is not None else ""
        params: list[object] = [int(player_id), int(opponent_id)]
        if before_game_date is not None:
            params.append(before_game_date)
        cache[key] = conn.execute(
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
    return cache[key]  # type: ignore[return-value]


def _row_mapping_value(row: sqlite3.Row, key: str, default: object = None) -> object:
    try:
        return row[key]
    except (IndexError, KeyError):
        return default


def _historical_row_opponent_id(row: sqlite3.Row) -> int:
    team_id = _historical_row_team_id(row)
    is_home = team_id == int(row["home_team_id"])
    return int(row["away_team_id"] if is_home else row["home_team_id"])


def _historical_row_team_id(row: sqlite3.Row) -> int:
    if "resolved_team_id" in row.keys() and row["resolved_team_id"] is not None:
        return int(row["resolved_team_id"])
    return int(row["team_id"])


def _has_unambiguous_historical_team(row: sqlite3.Row) -> bool:
    team_id = _historical_row_team_id(row)
    return team_id in {int(row["home_team_id"]), int(row["away_team_id"])}


def _common_opponent_factor_from_rows(
    conn: sqlite3.Connection,
    *,
    player_rows: list[sqlite3.Row],
    row_index: int,
    market: str,
    context: dict,
    before_game_date: str | None,
    baseline_avg: float,
) -> float:
    common_opponents = _common_opponent_ids(conn, int(context["team_id"]), int(context["opponent_id"]), before_game_date)
    if not common_opponents:
        return 1.0
    common_set = set(common_opponents)
    matching_rows: list[sqlite3.Row] = []
    for prior_row in reversed(player_rows[:row_index]):
        if _historical_row_opponent_id(prior_row) in common_set:
            matching_rows.append(prior_row)
            if len(matching_rows) >= 10:
                break
    if len(matching_rows) < 2 or baseline_avg <= 0:
        return 1.0
    common_avg = sum(_market_value(row, market) for row in matching_rows) / len(matching_rows)
    sample_weight = min(len(matching_rows) / 5, 1.0)
    return _clamp(1 + (((common_avg / baseline_avg) - 1) * sample_weight * 0.5), 0.94, 1.06)


def _h2h_factor_from_rows(
    *,
    player_rows: list[sqlite3.Row],
    row_index: int,
    market: str,
    opponent_id: int,
    baseline_avg: float,
) -> float:
    h2h_rows: list[sqlite3.Row] = []
    for prior_row in reversed(player_rows[:row_index]):
        if _historical_row_opponent_id(prior_row) == int(opponent_id):
            h2h_rows.append(prior_row)
            if len(h2h_rows) >= 6:
                break
    if len(h2h_rows) < 2 or baseline_avg <= 0:
        return 1.0
    h2h_avg = sum(_market_value(row, market) for row in h2h_rows) / len(h2h_rows)
    sample_weight = min(1.0, len(h2h_rows) / 6.0)
    ratio = _clamp(h2h_avg / baseline_avg, 0.82, 1.18)
    return _clamp(1 + ((ratio - 1) * sample_weight * 0.55), 0.92, 1.08)


def _weighted_average(values: list[float]) -> float:
    weights = list(range(len(values), 0, -1))
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _historical_row_margin(row: sqlite3.Row | dict[str, object]) -> float:
    if isinstance(row, sqlite3.Row):
        margin = row["team_margin"] if "team_margin" in row.keys() else None
    elif isinstance(row, dict):
        margin = row.get("team_margin")
        if margin is None:
            margin = 15.0 if row.get("is_blowout") else 0.0
    else:
        try:
            margin = row["team_margin"]  # type: ignore[index]
        except Exception:
            margin = None
    return float(margin or 0.0)


def _historical_row_is_blowout(row: sqlite3.Row | dict[str, object]) -> bool:
    return _historical_row_margin(row) >= 15.0


def _prune_extreme_blowout_history_rows(
    rows: list[sqlite3.Row] | list[dict[str, object]],
    rotation_role: str | None,
    *,
    extreme_margin: float = 20.0,
    min_competitive_rows: int = 5,
) -> list[sqlite3.Row] | list[dict[str, object]]:
    role = str(rotation_role or "starter").strip().lower()
    if role not in {"star", "starter"} or len(rows) <= min_competitive_rows:
        return rows
    competitive_rows = [row for row in rows if _historical_row_margin(row) < 15.0]
    if len(competitive_rows) < min_competitive_rows:
        return rows
    filtered = [row for row in rows if _historical_row_margin(row) < extreme_margin]
    return filtered or rows


def _historical_blowout_weight(row: sqlite3.Row | dict[str, object], rotation_role: str | None) -> float:
    if not _historical_row_is_blowout(row):
        return 1.0
    role = str(rotation_role or "starter").strip().lower()
    if role in {"star", "starter"}:
        return 0.72
    if role == "rotation":
        return 0.82
    return 0.92


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

    teammate_cache = _connection_training_cache_bucket(conn, "injury_teammates")
    teammate_cache_key = (int(team_id), str(as_of_date or ""))
    injury_cutoff_clause = "AND DATE(i.captured_at) <= DATE(?)" if as_of_date else ""
    injury_cutoff_params: tuple[object, ...] = (as_of_date,) if as_of_date else ()
    teammate_rows = teammate_cache.get(teammate_cache_key)
    if teammate_rows is None:
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
              {injury_cutoff_clause}
              AND i.captured_at = (
                  SELECT MAX(i2.captured_at)
                  FROM injuries i2
                  WHERE i2.player_id = i.player_id
                    {"AND DATE(i2.captured_at) <= DATE(?)" if as_of_date else ""}
              )
            """,
            (*injury_cutoff_params, team_id, *injury_cutoff_params, *injury_cutoff_params),
        ).fetchall()
        teammate_cache[teammate_cache_key] = teammate_rows
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
        if int(row["player_id"]) == int(player_id):
            continue
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

    role_key = (rotation_role or "starter").lower()
    usage_cap_by_role = {"star": 0.18, "starter": 0.15, "rotation": 0.10, "bench": 0.06}
    usage_boost = _clamp(
        (teammate_penalty / 105.0) + (missing_key * 0.01),
        0.0,
        usage_cap_by_role.get(role_key, 0.10),
    )
    base_minutes_by_role = {"star": 1.25, "starter": 1.0, "rotation": 0.65, "bench": 0.35}
    role_minutes = base_minutes_by_role.get(role_key, 0.75)
    minutes_cap_by_role = {"star": 4.0, "starter": 3.2, "rotation": 2.2, "bench": 1.2}
    minutes_delta = min(((teammate_penalty / 30.0) * role_minutes) + (missing_key * 0.35), minutes_cap_by_role.get(role_key, 2.5))
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


def _team_rating_context(
    conn: sqlite3.Connection,
    team_id: int,
    before_game_date: str | None,
) -> dict[str, float]:
    date_filter = "AND g.game_date < ?" if before_game_date else ""
    params: list[object] = [int(team_id)]
    if before_game_date:
        params.append(str(before_game_date))
    row = conn.execute(
        f"""
        WITH ranked AS (
            SELECT
                r.points,
                r.opponent_points,
                r.possessions,
                ROW_NUMBER() OVER (
                    ORDER BY g.game_date DESC, g.start_time DESC, r.game_id DESC, r.id DESC
                ) AS rn
            FROM team_game_results r
            JOIN games g ON g.id = r.game_id
            WHERE r.team_id = ?
              {date_filter}
        )
        SELECT
            COUNT(*) AS games,
            COALESCE(SUM(points), 0.0) AS total_points,
            COALESCE(SUM(opponent_points), 0.0) AS total_allowed,
            COALESCE(SUM(possessions), 0.0) AS total_possessions,
            COALESCE(SUM(CASE WHEN rn <= 10 THEN points ELSE 0 END), 0.0) AS recent_points,
            COALESCE(SUM(CASE WHEN rn <= 10 THEN opponent_points ELSE 0 END), 0.0) AS recent_allowed,
            COALESCE(SUM(CASE WHEN rn <= 10 THEN possessions ELSE 0 END), 0.0) AS recent_possessions,
            COALESCE(SUM(CASE WHEN rn <= 10 THEN 1 ELSE 0 END), 0) AS recent_games
        FROM ranked
        """,
        tuple(params),
    ).fetchone()
    if row is None:
        return {
            "games": 0.0,
            "off_rating": 100.0,
            "def_rating": 100.0,
            "net_rating": 0.0,
            "recent_off_rating": 100.0,
            "recent_def_rating": 100.0,
            "recent_net_rating": 0.0,
        }
    total_possessions = float(row["total_possessions"] or 0.0)
    recent_possessions = float(row["recent_possessions"] or 0.0)
    off_rating = (100.0 * float(row["total_points"] or 0.0)) / max(total_possessions, 1.0)
    def_rating = (100.0 * float(row["total_allowed"] or 0.0)) / max(total_possessions, 1.0)
    recent_off_rating = (100.0 * float(row["recent_points"] or 0.0)) / max(recent_possessions, 1.0)
    recent_def_rating = (100.0 * float(row["recent_allowed"] or 0.0)) / max(recent_possessions, 1.0)
    return {
        "games": float(row["games"] or 0.0),
        "off_rating": off_rating,
        "def_rating": def_rating,
        "net_rating": off_rating - def_rating,
        "recent_off_rating": recent_off_rating,
        "recent_def_rating": recent_def_rating,
        "recent_net_rating": recent_off_rating - recent_def_rating,
    }


def _league_rating_context(
    conn: sqlite3.Connection,
    before_game_date: str | None,
) -> dict[str, float]:
    date_filter = "WHERE g.game_date < ?" if before_game_date else ""
    params: tuple[object, ...] = (str(before_game_date),) if before_game_date else ()
    row = conn.execute(
        f"""
        WITH ranked AS (
            SELECT
                r.points,
                r.opponent_points,
                r.possessions,
                ROW_NUMBER() OVER (
                    ORDER BY g.game_date DESC, g.start_time DESC, r.game_id DESC, r.id DESC
                ) AS rn
            FROM team_game_results r
            JOIN games g ON g.id = r.game_id
            {date_filter}
        )
        SELECT
            COALESCE(SUM(points), 0.0) AS total_points,
            COALESCE(SUM(opponent_points), 0.0) AS total_allowed,
            COALESCE(SUM(possessions), 0.0) AS total_possessions,
            COALESCE(SUM(CASE WHEN rn <= 40 THEN points ELSE 0 END), 0.0) AS recent_points,
            COALESCE(SUM(CASE WHEN rn <= 40 THEN opponent_points ELSE 0 END), 0.0) AS recent_allowed,
            COALESCE(SUM(CASE WHEN rn <= 40 THEN possessions ELSE 0 END), 0.0) AS recent_possessions
        FROM ranked
        """,
        params,
    ).fetchone()
    total_possessions = float(row["total_possessions"] or 0.0) if row is not None else 0.0
    recent_possessions = float(row["recent_possessions"] or 0.0) if row is not None else 0.0
    off_rating = (100.0 * float(row["total_points"] or 0.0)) / max(total_possessions, 1.0) if row is not None else 100.0
    def_rating = (100.0 * float(row["total_allowed"] or 0.0)) / max(total_possessions, 1.0) if row is not None else 100.0
    recent_off_rating = (100.0 * float(row["recent_points"] or 0.0)) / max(recent_possessions, 1.0) if row is not None else 100.0
    recent_def_rating = (100.0 * float(row["recent_allowed"] or 0.0)) / max(recent_possessions, 1.0) if row is not None else 100.0
    return {
        "off_rating": off_rating,
        "def_rating": def_rating,
        "net_rating": off_rating - def_rating,
        "recent_off_rating": recent_off_rating,
        "recent_def_rating": recent_def_rating,
        "recent_net_rating": recent_off_rating - recent_def_rating,
    }


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


def _recent_opponent_factor(conn: sqlite3.Connection, opponent_id: int, market: str) -> float:
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
        LIMIT 60
        """,
        (opponent_id,),
    ).fetchall()
    league_rows = conn.execute(
        """
        SELECT s.*
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT 300
        """
    ).fetchall()
    if not opponent_rows or not league_rows:
        return 1.0
    opponent_allowed = sum(_market_value(row, market) for row in opponent_rows) / len(opponent_rows)
    league_allowed = sum(_market_value(row, market) for row in league_rows) / len(league_rows)
    return _clamp(opponent_allowed / league_allowed, 0.88, 1.12) if league_allowed > 0 else 1.0


def _common_opponent_factor(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    context: dict,
    before_game_date: str | None,
    baseline_avg: float | None = None,
) -> float:
    common_opponents = _common_opponent_ids(conn, context["team_id"], context["opponent_id"], before_game_date)
    if not common_opponents:
        return 1.0
    player_rows = _cached_player_common_opponent_rows(conn, player_id, tuple(common_opponents), before_game_date)
    if len(player_rows) < 2:
        return 1.0
    player_avg = float(baseline_avg) if baseline_avg is not None else None
    if player_avg is None:
        all_rows = _player_history(conn, player_id, market, before_game_date, exclude_game_id=None)
        if not all_rows:
            return 1.0
        player_avg = sum(row["value"] for row in all_rows) / len(all_rows)
    if player_avg <= 0:
        return 1.0
    common_avg = sum(_market_value(row, market) for row in player_rows) / len(player_rows)
    sample_weight = min(len(player_rows) / 5, 1.0)
    return _clamp(1 + (((common_avg / player_avg) - 1) * sample_weight * 0.5), 0.94, 1.06)


def _h2h_factor(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    context: dict,
    before_game_date: str | None,
    baseline_avg: float | None = None,
) -> float:
    opponent_id = int(context["opponent_id"])
    h2h_rows = _cached_player_h2h_rows(conn, player_id, opponent_id, before_game_date)
    if len(h2h_rows) < 2:
        return 1.0
    baseline = float(baseline_avg) if baseline_avg is not None else None
    if baseline is None:
        baseline_rows = _player_history(conn, player_id, market, before_game_date, exclude_game_id=None)
        if not baseline_rows:
            return 1.0
        baseline = sum(row["value"] for row in baseline_rows) / len(baseline_rows)
    if baseline <= 0:
        return 1.0
    h2h_avg = sum(_market_value(row, market) for row in h2h_rows) / len(h2h_rows)
    sample_weight = min(1.0, len(h2h_rows) / 6.0)
    ratio = _clamp(h2h_avg / baseline, 0.82, 1.18)
    return _clamp(1 + ((ratio - 1) * sample_weight * 0.55), 0.92, 1.08)


def _common_opponent_ids(conn: sqlite3.Connection, team_id: int, opponent_id: int, before_game_date: str | None) -> list[int]:
    cache = _connection_training_cache_bucket(conn, "common_opponent_ids")
    key = (int(team_id), int(opponent_id), str(before_game_date or ""))
    if key not in cache:
        cache[key] = sorted(
            _recent_opponent_ids(conn, team_id, before_game_date).intersection(
                _recent_opponent_ids(conn, opponent_id, before_game_date)
            )
        )
    return cache[key]  # type: ignore[return-value]


def _recent_opponent_ids(conn: sqlite3.Connection, team_id: int, before_game_date: str | None) -> set[int]:
    cache = _connection_training_cache_bucket(conn, "recent_opponent_ids")
    key = (int(team_id), str(before_game_date or ""))
    if key in cache:
        return cache[key]  # type: ignore[return-value]
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
    cache[key] = {int(row["opponent_id"]) for row in rows if row["opponent_id"] is not None}
    return cache[key]  # type: ignore[return-value]


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
