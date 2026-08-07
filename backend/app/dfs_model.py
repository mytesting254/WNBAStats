from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import asdict
from dataclasses import dataclass
from functools import lru_cache

from .cache import read_json_cache, write_json_cache
from .odds import american_to_implied_probability
from .player_half_training_db import load_player_half_prop_examples
from .player_half_training_db import _estimated_minute_share as _half_minute_share
from .paths import get_player_half_training_db_path
from .player_prop_model import (
    FEATURE_NAMES,
    ModelTuningConfig,
    RidgeModel,
    TRAINING_MARKETS,
    _fit_model_from_rows,
    _player_sample_quality,
    _predict,
    feature_snapshot,
)


DFS_HALF_MODEL_VERSION = "dfs-first-half-ridge-v1"
DFS_HALF_MODEL_CACHE_PREFIX = "dfs_first_half_model"
DFS_HALF_EXTRA_FEATURES = [
    "line_value",
    "over_implied_probability",
    "under_implied_probability",
    "component_minus_line",
    "sample_count",
    "avg_minutes",
    "team_first_half_share",
    "estimated_first_half_minutes_share",
]
DFS_HALF_MARKETS = list(TRAINING_MARKETS)


@dataclass(frozen=True)
class DfsHalfEstimate:
    prop_line_id: int
    game_id: int
    player_id: int
    player: str
    position: str | None
    team: str
    team_logo_url: str | None
    sportsbook: str
    market: str
    line: float
    over_odds: int
    under_odds: int
    full_game_projection: float
    estimated_first_half_result: float
    expected_halfway_line: float
    pace_ratio: float | None
    halftime_margin_to_line: float | None
    on_track_probability: float | None
    recommended_side: str
    model_version: str
    model_rows: int
    team_first_half_share: float
    estimated_first_half_minutes_share: float
    projected_first_half_total: float | None
    game_total: float | None
    start_time: str | None
    confidence: str


def build_current_dfs_first_half_estimates(
    conn: sqlite3.Connection,
    *,
    recent_values_fn,
    recent_minutes_fn,
) -> list[dict]:
    rows = conn.execute(
        """
        WITH raw_props AS (
            SELECT
                pp.id AS prediction_id,
                pp.prop_line_id,
                pl.game_id,
                pl.player_id,
                p.full_name AS player,
                p.position,
                p.rotation_role,
                rt.abbreviation AS team,
                rt.logo_url AS team_logo_url,
                pl.sportsbook,
                pl.market,
                pl.line,
                pl.over_odds,
                pl.under_odds,
                pp.projection,
                g.start_time,
                g.game_total,
                (
                    SELECT gp.projected_first_half_total
                    FROM game_predictions gp
                    WHERE gp.game_id = g.id
                    ORDER BY gp.id DESC
                    LIMIT 1
                ) AS projected_first_half_total,
                ROW_NUMBER() OVER (
                    PARTITION BY pl.game_id, pl.player_id, pl.market, pl.line
                    ORDER BY
                        CASE
                            WHEN pp.recommended_side = 'over' THEN pl.over_odds
                            ELSE pl.under_odds
                        END DESC,
                        pp.expected_value DESC,
                        pp.id DESC
                ) AS rn
            FROM prop_predictions pp
            JOIN prop_lines pl ON pl.id = pp.prop_line_id
            JOIN players p ON p.id = pl.player_id
            JOIN games g ON g.id = pl.game_id
            LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
            JOIN teams rt ON rt.id = (
                SELECT COALESCE(
                    (
                        SELECT h.team_id
                        FROM player_team_history h
                        LEFT JOIN games hg ON hg.id = h.game_id
                        WHERE h.player_id = p.id
                          AND h.team_id IN (g.home_team_id, g.away_team_id)
                          AND h.game_id IS NOT NULL
                          AND hg.game_date IS NOT NULL
                          AND hg.game_date <= g.game_date
                        ORDER BY hg.game_date DESC, h.id DESC
                        LIMIT 1
                    ),
                    CASE
                        WHEN p.team_id IN (g.home_team_id, g.away_team_id) THEN p.team_id
                        ELSE g.home_team_id
                    END
                )
            )
            WHERE sp.id IS NULL
        )
        SELECT *
        FROM raw_props
        WHERE rn = 1
        ORDER BY start_time ASC, player ASC, market ASC
        """
    ).fetchall()
    payload: list[dict] = []
    runtime_cache: dict[str, dict[tuple, object]] = {}
    for row in rows:
        market = str(row["market"] or "").strip().lower()
        if market not in DFS_HALF_MARKETS:
            continue
        estimate = _predict_current_half_estimate(conn, row, runtime_cache=runtime_cache)
        if estimate is None:
            continue
        payload.append(
            {
                "prop_line_id": estimate.prop_line_id,
                "game_id": estimate.game_id,
                "player_id": estimate.player_id,
                "player": estimate.player,
                "position": estimate.position,
                "team": estimate.team,
                "team_logo_url": estimate.team_logo_url,
                "sportsbook": estimate.sportsbook,
                "market": estimate.market,
                "line": estimate.line,
                "over_odds": estimate.over_odds,
                "under_odds": estimate.under_odds,
                "full_game_projection": estimate.full_game_projection,
                "estimated_first_half_result": estimate.estimated_first_half_result,
                "expected_halfway_line": estimate.expected_halfway_line,
                "pace_ratio": estimate.pace_ratio,
                "halftime_margin_to_line": estimate.halftime_margin_to_line,
                "on_track_probability": estimate.on_track_probability,
                "recommended_side": estimate.recommended_side,
                "model_version": estimate.model_version,
                "model_rows": estimate.model_rows,
                "team_first_half_share": estimate.team_first_half_share,
                "estimated_first_half_minutes_share": estimate.estimated_first_half_minutes_share,
                "projected_first_half_total": estimate.projected_first_half_total,
                "game_total": estimate.game_total,
                "start_time": estimate.start_time,
                "confidence": estimate.confidence,
                "recent_values": recent_values_fn(
                    conn,
                    player_id=estimate.player_id,
                    market=estimate.market,
                    game_id=estimate.game_id,
                    limit=5,
                ),
                "recent_minutes": recent_minutes_fn(
                    conn,
                    player_id=estimate.player_id,
                    game_id=estimate.game_id,
                    limit=5,
                ),
            }
        )
    return payload


def _predict_current_half_estimate(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    runtime_cache: dict[str, dict[tuple, object]] | None,
) -> DfsHalfEstimate | None:
    market = str(row["market"] or "").strip().lower()
    model = train_dfs_half_market_model(conn, market)
    if model is None:
        return None
    player_id = int(row["player_id"])
    game_id = int(row["game_id"])
    snapshot = feature_snapshot(conn, player_id, market, game_id, runtime_cache=runtime_cache, allow_training=True)
    if len(snapshot.values) != len(FEATURE_NAMES):
        return None
    line_value = float(row["line"] or 0.0)
    over_odds = int(row["over_odds"] or 0)
    under_odds = int(row["under_odds"] or 0)
    projected_first_half_total = float(row["projected_first_half_total"]) if row["projected_first_half_total"] is not None else None
    game_total = float(row["game_total"]) if row["game_total"] is not None else None
    team_first_half_share = _projected_team_first_half_share(projected_first_half_total, game_total)
    estimated_first_half_minutes_share = _half_minute_share(str(row["rotation_role"] or "").strip().lower(), team_first_half_share)
    sample_count, avg_minutes = _player_sample_quality(conn, player_id, game_id)
    features = _dfs_half_feature_row(
        base_features=snapshot.values,
        component_projection=float(snapshot.component_projection),
        line_value=line_value,
        over_odds=over_odds,
        under_odds=under_odds,
        sample_count=int(sample_count),
        avg_minutes=float(avg_minutes),
        team_first_half_share=team_first_half_share,
        estimated_first_half_minutes_share=estimated_first_half_minutes_share,
    )
    estimate = max(0.0, _predict(model, features))
    expected_halfway_line = line_value * 0.5
    halftime_margin = estimate - expected_halfway_line
    pace_ratio = estimate / expected_halfway_line if abs(expected_halfway_line) > 1e-9 else None
    on_track_probability = None if pace_ratio is None else 1.0 / (1.0 + math.exp(-4.0 * (pace_ratio - 1.0)))
    return DfsHalfEstimate(
        prop_line_id=int(row["prop_line_id"]),
        game_id=game_id,
        player_id=player_id,
        player=str(row["player"]),
        position=str(row["position"]) if row["position"] is not None else None,
        team=str(row["team"]),
        team_logo_url=str(row["team_logo_url"]) if row["team_logo_url"] is not None else None,
        sportsbook=str(row["sportsbook"]),
        market=market,
        line=line_value,
        over_odds=over_odds,
        under_odds=under_odds,
        full_game_projection=float(row["projection"] or snapshot.component_projection),
        estimated_first_half_result=estimate,
        expected_halfway_line=expected_halfway_line,
        pace_ratio=pace_ratio,
        halftime_margin_to_line=halftime_margin,
        on_track_probability=on_track_probability,
        recommended_side="over" if halftime_margin >= 0 else "under",
        model_version=DFS_HALF_MODEL_VERSION,
        model_rows=int(model.rows),
        team_first_half_share=team_first_half_share,
        estimated_first_half_minutes_share=estimated_first_half_minutes_share,
        projected_first_half_total=projected_first_half_total,
        game_total=game_total,
        start_time=str(row["start_time"]) if row["start_time"] is not None else None,
        confidence="high" if abs(halftime_margin) >= 2.0 else "medium" if abs(halftime_margin) >= 1.0 else "low",
    )


def train_dfs_half_market_model(
    conn: sqlite3.Connection,
    market: str,
    *,
    config: ModelTuningConfig | None = None,
) -> RidgeModel | None:
    tuning = config or ModelTuningConfig()
    db_path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    return _train_dfs_half_market_model_cached(db_path, market, tuning)


@lru_cache(maxsize=64)
def _train_dfs_half_market_model_cached(
    db_path: str,
    market: str,
    config: ModelTuningConfig,
) -> RidgeModel | None:
    if not db_path:
        return None
    cache_key = _dfs_half_model_cache_key(db_path, market, config)
    cached = _load_cached_dfs_half_model(cache_key)
    if cached is not None:
        return cached
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        rows = _dfs_half_training_rows(conn, market)
    finally:
        conn.close()
    model = _fit_model_from_rows(f"dfs_1h:{market}", rows, config=config)
    if model is not None:
        _store_cached_dfs_half_model(cache_key, model, config=config)
    return model


def pretrain_dfs_half_models(
    conn: sqlite3.Connection,
    *,
    config: ModelTuningConfig | None = None,
) -> dict[str, object]:
    tuning = config or ModelTuningConfig()
    db_path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    per_market: dict[str, dict[str, object]] = {}
    trained = 0
    for market in DFS_HALF_MARKETS:
        model = _train_dfs_half_market_model_cached(db_path, market, tuning)
        per_market[market] = {
            "trained": model is not None,
            "rows": int(model.rows) if model is not None else 0,
            "cache_key": _dfs_half_model_cache_key(db_path, market, tuning),
        }
        if model is not None:
            trained += 1
    return {
        "model_version": DFS_HALF_MODEL_VERSION,
        "trained_markets": trained,
        "market_count": len(DFS_HALF_MARKETS),
        "markets": per_market,
    }


def _dfs_half_training_rows(conn: sqlite3.Connection, market: str) -> list[tuple[list[float], float]]:
    from .player_prop_training_db import load_player_prop_final_projection_samples

    final_samples, _diagnostics = load_player_prop_final_projection_samples(conn, market=market, force_rebuild=False)
    final_map = {
        int(sample.source_prop_line_id): sample
        for sample in final_samples
        if sample.source_prop_line_id is not None
    }
    half_rows, _info = load_player_half_prop_examples(conn, market=market, force_rebuild=False)
    rows: list[tuple[list[float], float]] = []
    for half_row in half_rows:
        sample = final_map.get(int(half_row["source_prop_line_id"]))
        if sample is None:
            continue
        rows.append(
            (
                _dfs_half_feature_row(
                    base_features=list(sample.features),
                    component_projection=float(sample.component_projection),
                    line_value=float(half_row["line_value"] or 0.0),
                    over_odds=int(half_row["over_odds"] or 0),
                    under_odds=int(half_row["under_odds"] or 0),
                    sample_count=int(sample.sample_count),
                    avg_minutes=float(sample.avg_minutes),
                    team_first_half_share=float(half_row["team_first_half_share"] or 0.5),
                    estimated_first_half_minutes_share=float(half_row["estimated_first_half_minutes_share"] or 0.5),
                ),
                float(half_row["estimated_first_half_result"] or 0.0),
            )
        )
    return rows


def _dfs_half_feature_row(
    *,
    base_features: list[float],
    component_projection: float,
    line_value: float,
    over_odds: int,
    under_odds: int,
    sample_count: int,
    avg_minutes: float,
    team_first_half_share: float,
    estimated_first_half_minutes_share: float,
) -> list[float]:
    return [
        *base_features,
        float(line_value),
        float(american_to_implied_probability(over_odds) if over_odds else 0.5),
        float(american_to_implied_probability(under_odds) if under_odds else 0.5),
        float(component_projection) - float(line_value),
        float(sample_count),
        float(avg_minutes),
        float(team_first_half_share),
        float(estimated_first_half_minutes_share),
    ]


def _projected_team_first_half_share(projected_first_half_total: float | None, game_total: float | None) -> float:
    if projected_first_half_total is None or game_total is None or abs(game_total) < 1e-9:
        return 0.5
    return max(0.35, min(0.65, float(projected_first_half_total) / float(game_total)))


def _dfs_half_model_cache_key(
    db_path: str,
    market: str,
    config: ModelTuningConfig,
) -> str:
    db_marker = hashlib.sha1(str(db_path).encode("utf-8")).hexdigest()[:12]
    half_training_path = str(get_player_half_training_db_path())
    half_marker = hashlib.sha1(half_training_path.encode("utf-8")).hexdigest()[:12]
    config_marker = hashlib.sha1(
        json.dumps(config.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    version_marker = hashlib.sha1(DFS_HALF_MODEL_VERSION.encode("utf-8")).hexdigest()[:8]
    return f"{DFS_HALF_MODEL_CACHE_PREFIX}-{market}-{db_marker}-{half_marker}-{config_marker}-{version_marker}.json"


def _load_cached_dfs_half_model(cache_key: str) -> RidgeModel | None:
    payload = read_json_cache(cache_key)
    if not isinstance(payload, dict):
        return None
    if str(payload.get("model_version") or "") != DFS_HALF_MODEL_VERSION:
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


def _store_cached_dfs_half_model(
    cache_key: str,
    model: RidgeModel,
    *,
    config: ModelTuningConfig,
) -> None:
    try:
        write_json_cache(
            cache_key,
            {
                "model_version": DFS_HALF_MODEL_VERSION,
                "config": config.to_dict(),
                "model": asdict(model),
            },
        )
    except OSError:
        return
