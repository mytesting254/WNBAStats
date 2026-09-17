from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from collections import defaultdict
from dataclasses import asdict
from dataclasses import dataclass
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from .cache import read_json_cache, write_json_cache
from .odds import american_to_implied_probability
from .paths import get_cache_dir
from .player_half_training_db import (
    ensure_player_half_training_db,
    load_player_half_prop_examples,
    player_half_training_db_signature,
)
from .player_half_training_db import _estimated_minute_share as _half_minute_share
from .player_prop_model import (
    FEATURE_NAMES,
    ModelTuningConfig,
    RidgeModel,
    _fit_model_from_rows,
    _player_sample_quality,
    _predict,
    feature_snapshot,
)


DFS_HALF_MODEL_VERSION = "dfs-first-half-ridge-v5"
DFS_HALF_MODEL_CACHE_PREFIX = "dfs_first_half_model"
DFS_HALF_EVAL_CACHE_PREFIX = "dfs_first_half_eval"
DFS_HALF_EVAL_VERSION = "dfs-first-half-walk-forward-v3"
DFS_HALF_REQUIRE_OBSERVED_TARGETS = True
DFS_HALF_EVAL_MIN_HISTORY_ROWS = 200
DFS_HALF_EVAL_MIN_SEGMENT_ROWS = 10
DFS_HALF_MARKET_MIN_ROWS = 750
DFS_HALF_MARKET_MIN_SEGMENTS = 5
DFS_HALF_MARKET_MIN_SIDE_ACCURACY = 0.54
DFS_HALF_MARKET_MIN_MAE_EDGE = 0.005
DFS_HALF_MARKET_MIN_COMPONENT_EDGE = -0.02
DFS_HALF_EXTRA_FEATURES = [
    "line_value",
    "over_implied_probability",
    "under_implied_probability",
    "component_minus_line",
    "sample_count",
    "avg_minutes",
    "team_first_half_share",
    "estimated_first_half_minutes_share",
    "prior_observed_count",
    "prior_observed_last",
    "prior_observed_avg_3",
    "prior_observed_avg_5",
    "prior_observed_avg_10",
    "prior_observed_std_5",
    "prior_observed_fga_last",
    "prior_observed_fga_avg_3",
    "prior_observed_fga_avg_5",
    "prior_observed_fg_pct_avg_5",
    "prior_observed_rebound_share_avg_5",
    "prior_observed_assist_share_avg_5",
    "prior_observed_three_rate_avg_5",
    "prior_observed_assists_per_fga_avg_5",
]
DFS_HALF_MARKETS = [
    "points",
    "rebounds",
    "assists",
    "turnovers",
    "points_rebounds",
    "points_assists",
    "rebounds_assists",
    "points_rebounds_assists",
    "threes",
]
DFS_HALF_BLEND_WEIGHTS = {
    "points": 0.7,
    "rebounds": 0.7,
    "assists": 0.4,
    "turnovers": 1.0,
    "points_rebounds": 0.6,
    "points_assists": 0.5,
    "rebounds_assists": 0.4,
    "points_rebounds_assists": 0.6,
    "threes": 0.3,
}


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


@dataclass(frozen=True)
class DfsHalfEvalSample:
    segment: str
    game_date: str
    features: list[float]
    target: float
    line_value: float
    component_projection: float
    team_first_half_share: float


def build_current_dfs_first_half_estimates(
    conn: sqlite3.Connection,
    *,
    recent_values_fn,
    recent_minutes_fn,
    recent_h2h_fn,
) -> list[dict]:
    evaluation = evaluate_dfs_half_models(conn, allow_recompute=False)
    eligible_markets = {
        market
        for market, report in (evaluation.get("markets") or {}).items()
        if isinstance(report, dict) and bool(report.get("ships"))
    }
    rows = conn.execute(
        """
        WITH raw_props AS (
            SELECT
                pp.id AS prediction_id,
                pp.prop_line_id,
                pl.game_id,
                pl.player_id,
                (
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
                ) AS resolved_team_id,
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
                    PARTITION BY pl.game_id, pl.player_id, pl.market
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
    market_models = {
        market: train_dfs_half_market_model(conn, market, allow_training=False)
        for market in sorted(
            {
                str(row["market"] or "").strip().lower()
                for row in rows
                if str(row["market"] or "").strip().lower() in DFS_HALF_MARKETS
                and (not eligible_markets or str(row["market"] or "").strip().lower() in eligible_markets)
            }
        )
    }
    for row in rows:
        market = str(row["market"] or "").strip().lower()
        if market not in DFS_HALF_MARKETS:
            continue
        if eligible_markets and market not in eligible_markets:
            continue
        estimate = _predict_current_half_estimate(
            conn,
            row,
            runtime_cache=runtime_cache,
            model=market_models.get(market),
        )
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
                **recent_h2h_fn(
                    conn,
                    player_id=estimate.player_id,
                    market=estimate.market,
                    game_id=estimate.game_id,
                    resolved_team_id=int(row["resolved_team_id"]),
                    limit=5,
                ),
            }
        )
    return payload


def snapshot_current_dfs_first_half_estimates(
    conn: sqlite3.Connection,
    estimates: list[dict[str, object]],
) -> dict[str, int]:
    if not estimates:
        return {"inserted": 0, "updated": 0, "skipped": 0}
    captured_at = _utc_now_iso()
    inserted = 0
    updated = 0
    skipped = 0
    for estimate in estimates:
        prop_line_id = int(estimate.get("prop_line_id") or 0)
        model_version = str(estimate.get("model_version") or "").strip()
        if prop_line_id <= 0 or not model_version:
            skipped += 1
            continue
        existing = conn.execute(
            """
            SELECT *
            FROM dfs_first_half_projection_snapshots
            WHERE prop_line_id = ?
              AND model_version = ?
            LIMIT 1
            """,
            (prop_line_id, model_version),
        ).fetchone()
        payload = _dfs_snapshot_payload(estimate)
        if existing is None:
            conn.execute(
                """
                INSERT INTO dfs_first_half_projection_snapshots (
                    prop_line_id,
                    game_id,
                    player_id,
                    sportsbook,
                    market,
                    line,
                    over_odds,
                    under_odds,
                    full_game_projection,
                    estimated_first_half_result,
                    expected_halfway_line,
                    pace_ratio,
                    halftime_margin_to_line,
                    on_track_probability,
                    recommended_side,
                    confidence,
                    model_version,
                    model_rows,
                    team_first_half_share,
                    estimated_first_half_minutes_share,
                    projected_first_half_total,
                    game_total,
                    start_time,
                    first_captured_at,
                    last_captured_at,
                    capture_count
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)
                """,
                (
                    prop_line_id,
                    payload["game_id"],
                    payload["player_id"],
                    payload["sportsbook"],
                    payload["market"],
                    payload["line"],
                    payload["over_odds"],
                    payload["under_odds"],
                    payload["full_game_projection"],
                    payload["estimated_first_half_result"],
                    payload["expected_halfway_line"],
                    payload["pace_ratio"],
                    payload["halftime_margin_to_line"],
                    payload["on_track_probability"],
                    payload["recommended_side"],
                    payload["confidence"],
                    model_version,
                    payload["model_rows"],
                    payload["team_first_half_share"],
                    payload["estimated_first_half_minutes_share"],
                    payload["projected_first_half_total"],
                    payload["game_total"],
                    payload["start_time"],
                    captured_at,
                    captured_at,
                ),
            )
            inserted += 1
            continue
        conn.execute(
            """
            UPDATE dfs_first_half_projection_snapshots
            SET game_id = ?,
                player_id = ?,
                sportsbook = ?,
                market = ?,
                line = ?,
                over_odds = ?,
                under_odds = ?,
                full_game_projection = ?,
                estimated_first_half_result = ?,
                expected_halfway_line = ?,
                pace_ratio = ?,
                halftime_margin_to_line = ?,
                on_track_probability = ?,
                recommended_side = ?,
                confidence = ?,
                model_rows = ?,
                team_first_half_share = ?,
                estimated_first_half_minutes_share = ?,
                projected_first_half_total = ?,
                game_total = ?,
                start_time = ?,
                last_captured_at = ?,
                capture_count = capture_count + 1
            WHERE id = ?
            """,
            (
                payload["game_id"],
                payload["player_id"],
                payload["sportsbook"],
                payload["market"],
                payload["line"],
                payload["over_odds"],
                payload["under_odds"],
                payload["full_game_projection"],
                payload["estimated_first_half_result"],
                payload["expected_halfway_line"],
                payload["pace_ratio"],
                payload["halftime_margin_to_line"],
                payload["on_track_probability"],
                payload["recommended_side"],
                payload["confidence"],
                payload["model_rows"],
                payload["team_first_half_share"],
                payload["estimated_first_half_minutes_share"],
                payload["projected_first_half_total"],
                payload["game_total"],
                payload["start_time"],
                captured_at,
                int(existing["id"]),
            ),
        )
        updated += 1
    return {"inserted": inserted, "updated": updated, "skipped": skipped}


def settle_dfs_first_half_projection_snapshots(
    conn: sqlite3.Connection,
    *,
    selected_date: str | None = None,
    selected_dates: list[str] | None = None,
) -> dict[str, int]:
    date_values = [str(item).strip() for item in (selected_dates or []) if str(item).strip()]
    if selected_date:
        normalized = str(selected_date).strip()
        if normalized and normalized not in date_values:
            date_values.append(normalized)
    clauses = ["g.status = 'final'"]
    params: list[object] = []
    if date_values:
        placeholders = ",".join("?" for _ in date_values)
        clauses.append(f"g.game_date IN ({placeholders})")
        params.extend(date_values)
    rows = conn.execute(
        f"""
        SELECT
            snap.id,
            snap.market,
            snap.expected_halfway_line,
            snap.estimated_first_half_result,
            pfh.first_half_points,
            pfh.first_half_rebounds,
            pfh.first_half_assists,
            pfh.first_half_threes,
            pfh.first_half_steals,
            pfh.first_half_blocks,
            pfh.first_half_turnovers
        FROM dfs_first_half_projection_snapshots snap
        JOIN games g ON g.id = snap.game_id
        JOIN player_first_half_stats pfh
          ON pfh.game_id = snap.game_id
         AND pfh.player_id = snap.player_id
        LEFT JOIN dfs_first_half_projection_settlements settled
          ON settled.snapshot_id = snap.id
        WHERE settled.snapshot_id IS NULL
          AND {' AND '.join(clauses)}
        """,
        tuple(params),
    ).fetchall()
    settled = 0
    skipped = 0
    settled_at = _utc_now_iso()
    for row in rows:
        actual = _market_first_half_stat_value(row, str(row["market"] or ""))
        if actual is None:
            skipped += 1
            continue
        expected_halfway_line = float(row["expected_halfway_line"] or 0.0)
        projected = float(row["estimated_first_half_result"] or 0.0)
        winning_side = "over" if actual >= expected_halfway_line else "under"
        signed_error = projected - actual
        conn.execute(
            """
            INSERT INTO dfs_first_half_projection_settlements (
                snapshot_id,
                actual_first_half_result,
                winning_side,
                absolute_error,
                signed_error,
                correct_side,
                settled_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(row["id"]),
                float(actual),
                winning_side,
                abs(signed_error),
                signed_error,
                1 if winning_side == str("over" if projected >= expected_halfway_line else "under") else 0,
                settled_at,
            ),
        )
        settled += 1
    return {"settled": settled, "skipped": skipped}


def _predict_current_half_estimate(
    conn: sqlite3.Connection,
    row: sqlite3.Row,
    *,
    runtime_cache: dict[str, dict[tuple, object]] | None,
    model: RidgeModel | None = None,
) -> DfsHalfEstimate | None:
    market = str(row["market"] or "").strip().lower()
    if model is None:
        model = train_dfs_half_market_model(conn, market, allow_training=False)
    if model is None:
        return None
    player_id = int(row["player_id"])
    game_id = int(row["game_id"])
    snapshot = feature_snapshot(conn, player_id, market, game_id, runtime_cache=runtime_cache, allow_training=False)
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
    history_features = _current_player_first_half_history_features(
        conn,
        player_id=player_id,
        market=market,
        game_id=game_id,
        runtime_cache=runtime_cache,
    )
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
        prior_observed_count=history_features["count"],
        prior_observed_last=history_features["last"],
        prior_observed_avg_3=history_features["avg_3"],
        prior_observed_avg_5=history_features["avg_5"],
        prior_observed_avg_10=history_features["avg_10"],
        prior_observed_std_5=history_features["std_5"],
        prior_observed_fga_last=history_features["fga_last"],
        prior_observed_fga_avg_3=history_features["fga_avg_3"],
        prior_observed_fga_avg_5=history_features["fga_avg_5"],
        prior_observed_fg_pct_avg_5=history_features["fg_pct_avg_5"],
        prior_observed_rebound_share_avg_5=history_features["rebound_share_avg_5"],
        prior_observed_assist_share_avg_5=history_features["assist_share_avg_5"],
        prior_observed_three_rate_avg_5=history_features["three_rate_avg_5"],
        prior_observed_assists_per_fga_avg_5=history_features["assists_per_fga_avg_5"],
    )
    learned_estimate = max(0.0, _predict(model, features))
    expected_halfway_line = line_value * 0.5
    component_half = float(snapshot.component_projection) * team_first_half_share
    estimate = _dfs_half_blend_prediction(market, learned_estimate, component_half)
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


def _dfs_snapshot_payload(estimate: dict[str, object]) -> dict[str, object]:
    return {
        "game_id": int(estimate.get("game_id") or 0),
        "player_id": int(estimate.get("player_id") or 0),
        "sportsbook": str(estimate.get("sportsbook") or ""),
        "market": str(estimate.get("market") or "").strip().lower(),
        "line": float(estimate.get("line") or 0.0),
        "over_odds": int(estimate.get("over_odds") or 0),
        "under_odds": int(estimate.get("under_odds") or 0),
        "full_game_projection": float(estimate.get("full_game_projection") or 0.0),
        "estimated_first_half_result": float(estimate.get("estimated_first_half_result") or 0.0),
        "expected_halfway_line": float(estimate.get("expected_halfway_line") or 0.0),
        "pace_ratio": None if estimate.get("pace_ratio") is None else float(estimate.get("pace_ratio") or 0.0),
        "halftime_margin_to_line": None if estimate.get("halftime_margin_to_line") is None else float(estimate.get("halftime_margin_to_line") or 0.0),
        "on_track_probability": None if estimate.get("on_track_probability") is None else float(estimate.get("on_track_probability") or 0.0),
        "recommended_side": str(estimate.get("recommended_side") or ""),
        "confidence": str(estimate.get("confidence") or ""),
        "model_rows": int(estimate.get("model_rows") or 0),
        "team_first_half_share": float(estimate.get("team_first_half_share") or 0.5),
        "estimated_first_half_minutes_share": float(estimate.get("estimated_first_half_minutes_share") or 0.5),
        "projected_first_half_total": None if estimate.get("projected_first_half_total") is None else float(estimate.get("projected_first_half_total") or 0.0),
        "game_total": None if estimate.get("game_total") is None else float(estimate.get("game_total") or 0.0),
        "start_time": None if estimate.get("start_time") is None else str(estimate.get("start_time") or ""),
    }


def _market_first_half_stat_value(row: sqlite3.Row, market: str) -> float | None:
    normalized = str(market or "").strip().lower()
    if normalized == "points":
        return float(row["first_half_points"] or 0.0)
    if normalized == "rebounds":
        return float(row["first_half_rebounds"] or 0.0)
    if normalized == "assists":
        return float(row["first_half_assists"] or 0.0)
    if normalized == "threes":
        return float(row["first_half_threes"] or 0.0)
    if normalized == "steals":
        return float(row["first_half_steals"] or 0.0)
    if normalized == "blocks":
        return float(row["first_half_blocks"] or 0.0)
    if normalized == "turnovers":
        return float(row["first_half_turnovers"] or 0.0)
    if normalized == "points_rebounds":
        return float(row["first_half_points"] or 0.0) + float(row["first_half_rebounds"] or 0.0)
    if normalized == "points_assists":
        return float(row["first_half_points"] or 0.0) + float(row["first_half_assists"] or 0.0)
    if normalized == "rebounds_assists":
        return float(row["first_half_rebounds"] or 0.0) + float(row["first_half_assists"] or 0.0)
    if normalized == "points_rebounds_assists":
        return (
            float(row["first_half_points"] or 0.0)
            + float(row["first_half_rebounds"] or 0.0)
            + float(row["first_half_assists"] or 0.0)
        )
    return None


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def train_dfs_half_market_model(
    conn: sqlite3.Connection,
    market: str,
    *,
    config: ModelTuningConfig | None = None,
    allow_training: bool = True,
) -> RidgeModel | None:
    tuning = config or ModelTuningConfig()
    db_path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    cache_key = _dfs_half_model_cache_key(db_path, market, tuning)
    cached = _load_cached_dfs_half_model(cache_key, market=market)
    if cached is not None:
        return cached
    if not allow_training:
        return None
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
        training_rows = _dfs_half_training_rows(conn, market)
        model = _train_dfs_half_market_model_cached(db_path, market, tuning)
        per_market[market] = {
            "trained": model is not None,
            "rows": int(model.rows) if model is not None else 0,
            "eligible_rows": len(training_rows),
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


def prewarm_dfs_half_cache(
    conn: sqlite3.Connection,
    *,
    config: ModelTuningConfig | None = None,
    min_history_rows: int = DFS_HALF_EVAL_MIN_HISTORY_ROWS,
    min_segment_rows: int = DFS_HALF_EVAL_MIN_SEGMENT_ROWS,
) -> dict[str, object]:
    ensure_player_half_training_db(conn, force=False, allow_rebuild=True)
    from .player_prop_training_db import ensure_player_prop_training_db

    ensure_player_prop_training_db(conn, force=False, allow_rebuild=True)
    training = pretrain_dfs_half_models(conn, config=config)
    evaluation = evaluate_dfs_half_models(
        conn,
        config=config,
        min_history_rows=min_history_rows,
        min_segment_rows=min_segment_rows,
        allow_recompute=True,
    )
    shipped_markets = list(((evaluation.get("gating") or {}).get("shipped_markets") or []))
    return {
        "model_version": DFS_HALF_MODEL_VERSION,
        "trained_markets": int(training.get("trained_markets") or 0),
        "market_count": int(training.get("market_count") or 0),
        "shipped_markets": shipped_markets,
        "shipped_market_count": len(shipped_markets),
        "overall_rows": int(((evaluation.get("overall") or {}).get("rows") or 0)),
    }


def evaluate_dfs_half_models(
    conn: sqlite3.Connection,
    *,
    config: ModelTuningConfig | None = None,
    min_history_rows: int = DFS_HALF_EVAL_MIN_HISTORY_ROWS,
    min_segment_rows: int = DFS_HALF_EVAL_MIN_SEGMENT_ROWS,
    allow_recompute: bool = True,
) -> dict[str, object]:
    tuning = config or ModelTuningConfig()
    db_path = str(conn.execute("PRAGMA database_list").fetchone()["file"] or "")
    cache_key = _dfs_half_eval_cache_key(
        db_path,
        tuning,
        min_history_rows=min_history_rows,
        min_segment_rows=min_segment_rows,
    )
    cached = _load_cached_dfs_half_eval(cache_key)
    if cached is not None:
        return cached
    if not allow_recompute:
        return {
            "model_family": DFS_HALF_EVAL_VERSION,
            "markets": {},
            "overall": {"rows": 0, "mae": None, "rmse": None, "side_accuracy": None},
            "min_history_rows": int(min_history_rows),
            "min_segment_rows": int(min_segment_rows),
            "gating": {
                "min_rows": DFS_HALF_MARKET_MIN_ROWS,
                "min_segments": DFS_HALF_MARKET_MIN_SEGMENTS,
                "min_side_accuracy": DFS_HALF_MARKET_MIN_SIDE_ACCURACY,
                "min_mae_vs_half_line": DFS_HALF_MARKET_MIN_MAE_EDGE,
                "min_mae_vs_component_share": DFS_HALF_MARKET_MIN_COMPONENT_EDGE,
                "shipped_markets": [],
                "cache_miss": True,
            },
        }

    metrics: dict[str, object] = {}
    overall_rows = 0
    overall_abs_error_sum = 0.0
    overall_squared_error_sum = 0.0
    overall_side_hits = 0.0
    shipped_markets: list[str] = []

    for market in DFS_HALF_MARKETS:
        report = _evaluate_dfs_half_market_walk_forward(
            conn,
            market=market,
            config=tuning,
            min_history_rows=min_history_rows,
            min_segment_rows=min_segment_rows,
        )
        ships = _dfs_half_market_passes_quality_gate(report)
        report["ships"] = ships
        metrics[market] = report
        if ships:
            shipped_markets.append(market)
        rows = int(report.get("rows") or 0)
        mae = report.get("mae")
        rmse = report.get("rmse")
        side_accuracy = report.get("side_accuracy")
        if rows > 0 and mae is not None and rmse is not None and side_accuracy is not None:
            overall_rows += rows
            overall_abs_error_sum += float(mae) * rows
            overall_squared_error_sum += (float(rmse) ** 2) * rows
            overall_side_hits += float(side_accuracy) * rows

    payload = {
        "model_family": DFS_HALF_EVAL_VERSION,
        "markets": metrics,
        "overall": {
            "rows": overall_rows,
            "mae": round(overall_abs_error_sum / overall_rows, 3) if overall_rows else None,
            "rmse": round(math.sqrt(overall_squared_error_sum / overall_rows), 3) if overall_rows else None,
            "side_accuracy": round(overall_side_hits / overall_rows, 3) if overall_rows else None,
        },
        "min_history_rows": int(min_history_rows),
        "min_segment_rows": int(min_segment_rows),
        "gating": {
            "min_rows": DFS_HALF_MARKET_MIN_ROWS,
            "min_segments": DFS_HALF_MARKET_MIN_SEGMENTS,
            "min_side_accuracy": DFS_HALF_MARKET_MIN_SIDE_ACCURACY,
            "min_mae_vs_half_line": DFS_HALF_MARKET_MIN_MAE_EDGE,
            "min_mae_vs_component_share": DFS_HALF_MARKET_MIN_COMPONENT_EDGE,
            "shipped_markets": shipped_markets,
        },
    }
    _store_cached_dfs_half_eval(cache_key, payload)
    return payload


def _evaluate_dfs_half_market_walk_forward(
    conn: sqlite3.Connection,
    *,
    market: str,
    config: ModelTuningConfig,
    min_history_rows: int,
    min_segment_rows: int,
) -> dict[str, object]:
    samples = _dfs_half_eval_samples(conn, market)
    if len(samples) < min_history_rows + min_segment_rows:
        return {
            "rows": 0,
            "segments_evaluated": 0,
            "segments_skipped": 0,
            "mae": None,
            "rmse": None,
            "bias": None,
            "side_accuracy": None,
            "half_line_baseline_mae": None,
            "component_share_baseline_mae": None,
            "notes": "not_enough_rows",
        }

    grouped: dict[str, list[DfsHalfEvalSample]] = defaultdict(list)
    for sample in sorted(samples, key=lambda item: (item.segment, item.game_date)):
        grouped[sample.segment].append(sample)

    history: list[DfsHalfEvalSample] = []
    errors: list[float] = []
    half_line_errors: list[float] = []
    component_errors: list[float] = []
    side_hits = 0
    segments_evaluated = 0
    segments_skipped = 0
    segment_reports: list[dict[str, object]] = []

    for segment in sorted(grouped):
        segment_samples = grouped[segment]
        if len(history) < min_history_rows:
            history.extend(segment_samples)
            segments_skipped += 1
            continue
        model = _fit_model_from_rows(
            f"dfs_1h_eval:{market}",
            [(row.features, row.target) for row in history],
            config=config,
        )
        if model is None:
            history.extend(segment_samples)
            segments_skipped += 1
            continue

        segment_errors: list[float] = []
        segment_side_hits = 0
        for sample in segment_samples:
            learned_prediction = max(0.0, _predict(model, sample.features))
            expected_halfway_line = sample.line_value * 0.5
            component_half = sample.component_projection * sample.team_first_half_share
            prediction = _dfs_half_blend_prediction(market, learned_prediction, component_half)
            actual_side_over = sample.target >= expected_halfway_line
            predicted_side_over = prediction >= expected_halfway_line
            error = prediction - sample.target
            errors.append(error)
            segment_errors.append(error)
            half_line_errors.append(expected_halfway_line - sample.target)
            component_errors.append(component_half - sample.target)
            if actual_side_over == predicted_side_over:
                side_hits += 1
                segment_side_hits += 1

        rows = len(segment_samples)
        segments_evaluated += 1
        segment_reports.append(
            {
                "segment": segment,
                "rows": rows,
                "mae": round(sum(abs(value) for value in segment_errors) / rows, 3),
                "bias": round(sum(segment_errors) / rows, 3),
                "side_accuracy": round(segment_side_hits / rows, 3),
            }
        )
        history.extend(segment_samples)

    rows = len(errors)
    if rows < min_segment_rows:
        return {
            "rows": 0,
            "segments_evaluated": segments_evaluated,
            "segments_skipped": segments_skipped,
            "mae": None,
            "rmse": None,
            "bias": None,
            "side_accuracy": None,
            "half_line_baseline_mae": None,
            "component_share_baseline_mae": None,
            "notes": "not_enough_evaluated_rows",
        }

    mae = sum(abs(value) for value in errors) / rows
    rmse = math.sqrt(sum(value * value for value in errors) / rows)
    bias = sum(errors) / rows
    half_line_mae = sum(abs(value) for value in half_line_errors) / rows
    component_mae = sum(abs(value) for value in component_errors) / rows
    return {
        "rows": rows,
        "segments_evaluated": segments_evaluated,
        "segments_skipped": segments_skipped,
        "mae": round(mae, 3),
        "rmse": round(rmse, 3),
        "bias": round(bias, 3),
        "side_accuracy": round(side_hits / rows, 3),
        "half_line_baseline_mae": round(half_line_mae, 3),
        "component_share_baseline_mae": round(component_mae, 3),
        "mae_vs_half_line": round(half_line_mae - mae, 3),
        "mae_vs_component_share": round(component_mae - mae, 3),
        "segments": segment_reports,
    }


def _dfs_half_market_passes_quality_gate(report: dict[str, object]) -> bool:
    rows = int(report.get("rows") or 0)
    segments = int(report.get("segments_evaluated") or 0)
    side_accuracy = report.get("side_accuracy")
    mae_vs_half_line = report.get("mae_vs_half_line")
    mae_vs_component_share = report.get("mae_vs_component_share")
    if rows < DFS_HALF_MARKET_MIN_ROWS or segments < DFS_HALF_MARKET_MIN_SEGMENTS:
        return False
    if side_accuracy is None or float(side_accuracy) < DFS_HALF_MARKET_MIN_SIDE_ACCURACY:
        return False
    if mae_vs_half_line is None or float(mae_vs_half_line) < DFS_HALF_MARKET_MIN_MAE_EDGE:
        return False
    if mae_vs_component_share is None or float(mae_vs_component_share) < DFS_HALF_MARKET_MIN_COMPONENT_EDGE:
        return False
    return True


def _dfs_half_eval_samples(conn: sqlite3.Connection, market: str) -> list[DfsHalfEvalSample]:
    from .player_prop_training_db import load_player_prop_final_projection_samples

    final_samples, _diagnostics = load_player_prop_final_projection_samples(
        conn,
        market=market,
        force_rebuild=False,
        allow_rebuild=False,
    )
    final_map = {
        int(sample.source_prop_line_id): sample
        for sample in final_samples
        if sample.source_prop_line_id is not None
    }
    half_rows, _info = load_player_half_prop_examples(
        conn,
        market=market,
        force_rebuild=False,
        allow_rebuild=False,
    )
    samples: list[DfsHalfEvalSample] = []
    for half_row in half_rows:
        if DFS_HALF_REQUIRE_OBSERVED_TARGETS and not _half_row_has_observed_target(half_row):
            continue
        sample = final_map.get(int(half_row["source_prop_line_id"]))
        if sample is None:
            continue
        team_first_half_share = float(half_row["team_first_half_share"] or 0.5)
        samples.append(
            DfsHalfEvalSample(
                segment=str(half_row["game_date"] or "")[:7],
                game_date=str(half_row["game_date"] or ""),
                features=_dfs_half_feature_row(
                    base_features=list(sample.features),
                    component_projection=float(sample.component_projection),
                    line_value=float(half_row["line_value"] or 0.0),
                    over_odds=int(half_row["over_odds"] or 0),
                    under_odds=int(half_row["under_odds"] or 0),
                    sample_count=int(sample.sample_count),
                    avg_minutes=float(sample.avg_minutes),
                    team_first_half_share=team_first_half_share,
                    estimated_first_half_minutes_share=float(half_row["estimated_first_half_minutes_share"] or 0.5),
                    prior_observed_count=int(half_row["prior_observed_count"] or 0),
                    prior_observed_last=_nullable_float(half_row["prior_observed_last"]),
                    prior_observed_avg_3=_nullable_float(half_row["prior_observed_avg_3"]),
                    prior_observed_avg_5=_nullable_float(half_row["prior_observed_avg_5"]),
                    prior_observed_avg_10=_nullable_float(half_row["prior_observed_avg_10"]),
                    prior_observed_std_5=_nullable_float(half_row["prior_observed_std_5"]),
                    prior_observed_fga_last=_nullable_float(half_row["prior_observed_fga_last"]),
                    prior_observed_fga_avg_3=_nullable_float(half_row["prior_observed_fga_avg_3"]),
                    prior_observed_fga_avg_5=_nullable_float(half_row["prior_observed_fga_avg_5"]),
                    prior_observed_fg_pct_avg_5=_nullable_float(half_row["prior_observed_fg_pct_avg_5"]),
                    prior_observed_rebound_share_avg_5=_nullable_float(half_row["prior_observed_rebound_share_avg_5"]),
                    prior_observed_assist_share_avg_5=_nullable_float(half_row["prior_observed_assist_share_avg_5"]),
                    prior_observed_three_rate_avg_5=_nullable_float(half_row["prior_observed_three_rate_avg_5"]),
                    prior_observed_assists_per_fga_avg_5=_nullable_float(half_row["prior_observed_assists_per_fga_avg_5"]),
                ),
                target=float(half_row["estimated_first_half_result"] or 0.0),
                line_value=float(half_row["line_value"] or 0.0),
                component_projection=float(sample.component_projection),
                team_first_half_share=team_first_half_share,
            )
        )
    return samples


def _dfs_half_training_rows(conn: sqlite3.Connection, market: str) -> list[tuple[list[float], float]]:
    from .player_prop_training_db import load_player_prop_final_projection_samples

    final_samples, _diagnostics = load_player_prop_final_projection_samples(
        conn,
        market=market,
        force_rebuild=False,
        allow_rebuild=False,
    )
    final_map = {
        int(sample.source_prop_line_id): sample
        for sample in final_samples
        if sample.source_prop_line_id is not None
    }
    half_rows, _info = load_player_half_prop_examples(
        conn,
        market=market,
        force_rebuild=False,
        allow_rebuild=False,
    )
    rows: list[tuple[list[float], float]] = []
    for half_row in half_rows:
        if DFS_HALF_REQUIRE_OBSERVED_TARGETS and not _half_row_has_observed_target(half_row):
            continue
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
                    prior_observed_count=int(half_row["prior_observed_count"] or 0),
                    prior_observed_last=_nullable_float(half_row["prior_observed_last"]),
                    prior_observed_avg_3=_nullable_float(half_row["prior_observed_avg_3"]),
                    prior_observed_avg_5=_nullable_float(half_row["prior_observed_avg_5"]),
                    prior_observed_avg_10=_nullable_float(half_row["prior_observed_avg_10"]),
                    prior_observed_std_5=_nullable_float(half_row["prior_observed_std_5"]),
                    prior_observed_fga_last=_nullable_float(half_row["prior_observed_fga_last"]),
                    prior_observed_fga_avg_3=_nullable_float(half_row["prior_observed_fga_avg_3"]),
                    prior_observed_fga_avg_5=_nullable_float(half_row["prior_observed_fga_avg_5"]),
                    prior_observed_fg_pct_avg_5=_nullable_float(half_row["prior_observed_fg_pct_avg_5"]),
                    prior_observed_rebound_share_avg_5=_nullable_float(half_row["prior_observed_rebound_share_avg_5"]),
                    prior_observed_assist_share_avg_5=_nullable_float(half_row["prior_observed_assist_share_avg_5"]),
                    prior_observed_three_rate_avg_5=_nullable_float(half_row["prior_observed_three_rate_avg_5"]),
                    prior_observed_assists_per_fga_avg_5=_nullable_float(half_row["prior_observed_assists_per_fga_avg_5"]),
                ),
                float(half_row["estimated_first_half_result"] or 0.0),
            )
        )
    return rows


def _half_row_has_observed_target(row: sqlite3.Row) -> bool:
    raw_details = row["share_details_json"] if "share_details_json" in row.keys() else None
    if raw_details is None:
        return False
    try:
        payload = json.loads(str(raw_details))
    except (TypeError, ValueError, json.JSONDecodeError):
        return False
    return not bool(payload.get("derived"))


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
    prior_observed_count: int,
    prior_observed_last: float | None,
    prior_observed_avg_3: float | None,
    prior_observed_avg_5: float | None,
    prior_observed_avg_10: float | None,
    prior_observed_std_5: float | None,
    prior_observed_fga_last: float | None,
    prior_observed_fga_avg_3: float | None,
    prior_observed_fga_avg_5: float | None,
    prior_observed_fg_pct_avg_5: float | None,
    prior_observed_rebound_share_avg_5: float | None,
    prior_observed_assist_share_avg_5: float | None,
    prior_observed_three_rate_avg_5: float | None,
    prior_observed_assists_per_fga_avg_5: float | None,
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
        float(prior_observed_count),
        float(prior_observed_last if prior_observed_last is not None else 0.0),
        float(prior_observed_avg_3 if prior_observed_avg_3 is not None else 0.0),
        float(prior_observed_avg_5 if prior_observed_avg_5 is not None else 0.0),
        float(prior_observed_avg_10 if prior_observed_avg_10 is not None else 0.0),
        float(prior_observed_std_5 if prior_observed_std_5 is not None else 0.0),
        float(prior_observed_fga_last if prior_observed_fga_last is not None else 0.0),
        float(prior_observed_fga_avg_3 if prior_observed_fga_avg_3 is not None else 0.0),
        float(prior_observed_fga_avg_5 if prior_observed_fga_avg_5 is not None else 0.0),
        float(prior_observed_fg_pct_avg_5 if prior_observed_fg_pct_avg_5 is not None else 0.0),
        float(prior_observed_rebound_share_avg_5 if prior_observed_rebound_share_avg_5 is not None else 0.0),
        float(prior_observed_assist_share_avg_5 if prior_observed_assist_share_avg_5 is not None else 0.0),
        float(prior_observed_three_rate_avg_5 if prior_observed_three_rate_avg_5 is not None else 0.0),
        float(prior_observed_assists_per_fga_avg_5 if prior_observed_assists_per_fga_avg_5 is not None else 0.0),
    ]


def _dfs_half_blend_prediction(market: str, learned_prediction: float, component_half: float) -> float:
    weight = float(DFS_HALF_BLEND_WEIGHTS.get(str(market or "").strip().lower(), 1.0))
    return max(0.0, (weight * float(learned_prediction)) + ((1.0 - weight) * float(component_half)))


def _current_player_first_half_history_features(
    conn: sqlite3.Connection,
    *,
    player_id: int,
    market: str,
    game_id: int,
    runtime_cache: dict[str, dict[tuple, object]] | None,
) -> dict[str, float | int | None]:
    bucket: dict[tuple, object] | None = None
    if runtime_cache is not None:
        bucket = runtime_cache.setdefault("dfs_half_history", {})
    game_date = _current_game_date(conn, game_id, runtime_cache)
    cache_key = (int(player_id), str(market), int(game_id), str(game_date or ""))
    if bucket is not None and cache_key in bucket:
        return dict(bucket[cache_key])  # type: ignore[arg-type]

    values: list[float] = []
    fga_values: list[float] = []
    fg_pct_values: list[float] = []
    rebound_share_values: list[float] = []
    assist_share_values: list[float] = []
    three_rate_values: list[float] = []
    assists_per_fga_values: list[float] = []
    if game_date:
        rows = conn.execute(
            """
            SELECT
                pfh.*,
                (
                    SELECT SUM(pfh2.first_half_field_goals_made)
                    FROM player_first_half_stats pfh2
                    WHERE pfh2.game_id = pfh.game_id
                      AND pfh2.team_id = pfh.team_id
                ) AS team_first_half_fgm,
                (
                    SELECT SUM(pfh2.first_half_rebounds)
                    FROM player_first_half_stats pfh2
                    WHERE pfh2.game_id = pfh.game_id
                      AND pfh2.team_id = pfh.team_id
                ) AS team_first_half_rebounds
            FROM player_first_half_stats pfh
            JOIN games g ON g.id = pfh.game_id
            WHERE pfh.player_id = ?
              AND g.status = 'final'
              AND g.game_date < ?
            ORDER BY g.game_date ASC, pfh.game_id ASC
            """,
            (int(player_id), str(game_date)),
        ).fetchall()
        for row in rows:
            value = _first_half_market_value_from_stat_row(row, market)
            if value is not None:
                values.append(float(value))
            fga = float(row["first_half_field_goals_attempted"] or 0.0)
            fgm = float(row["first_half_field_goals_made"] or 0.0)
            rebounds = float(row["first_half_rebounds"] or 0.0)
            assists = float(row["first_half_assists"] or 0.0)
            team_fgm = float(row["team_first_half_fgm"] or 0.0)
            team_rebounds = float(row["team_first_half_rebounds"] or 0.0)
            fga_values.append(fga)
            fg_pct_values.append((fgm / fga) if fga > 0 else 0.0)
            rebound_share_values.append((rebounds / team_rebounds) if team_rebounds > 0 else 0.0)
            assist_share_values.append((assists / team_fgm) if team_fgm > 0 else 0.0)
            three_rate_values.append((float(row["first_half_threes"] or 0.0) / fga) if fga > 0 else 0.0)
            assists_per_fga_values.append((assists / fga) if fga > 0 else 0.0)
    summary = {
        "count": len(values),
        "last": values[-1] if values else None,
        "avg_3": _window_average(values, 3),
        "avg_5": _window_average(values, 5),
        "avg_10": _window_average(values, 10),
        "std_5": _window_std(values, 5),
        "fga_last": _history_last(fga_values),
        "fga_avg_3": _window_average(fga_values, 3),
        "fga_avg_5": _window_average(fga_values, 5),
        "fg_pct_avg_5": _window_average(fg_pct_values, 5),
        "rebound_share_avg_5": _window_average(rebound_share_values, 5),
        "assist_share_avg_5": _window_average(assist_share_values, 5),
        "three_rate_avg_5": _window_average(three_rate_values, 5),
        "assists_per_fga_avg_5": _window_average(assists_per_fga_values, 5),
    }
    if bucket is not None:
        bucket[cache_key] = dict(summary)
    return summary


def _current_game_date(
    conn: sqlite3.Connection,
    game_id: int,
    runtime_cache: dict[str, dict[tuple, object]] | None,
) -> str | None:
    bucket: dict[tuple, object] | None = None
    if runtime_cache is not None:
        bucket = runtime_cache.setdefault("dfs_half_game_date", {})
        cache_key = (int(game_id),)
        cached = bucket.get(cache_key)
        if cached is not None:
            return str(cached) if cached else None
    row = conn.execute("SELECT game_date FROM games WHERE id = ?", (int(game_id),)).fetchone()
    game_date = str(row["game_date"]) if row and row["game_date"] is not None else None
    if bucket is not None:
        bucket[(int(game_id),)] = game_date
    return game_date


def _first_half_market_value_from_stat_row(row: sqlite3.Row, market: str) -> float | None:
    points = float(row["first_half_points"] or 0.0)
    rebounds = float(row["first_half_rebounds"] or 0.0)
    assists = float(row["first_half_assists"] or 0.0)
    threes = float(row["first_half_threes"] or 0.0)
    steals = float(row["first_half_steals"] or 0.0)
    blocks = float(row["first_half_blocks"] or 0.0)
    turnovers = float(row["first_half_turnovers"] or 0.0)
    mapping = {
        "points": points,
        "rebounds": rebounds,
        "assists": assists,
        "turnovers": turnovers,
        "threes": threes,
        "steals": steals,
        "blocks": blocks,
        "points_rebounds": points + rebounds,
        "points_assists": points + assists,
        "rebounds_assists": rebounds + assists,
        "points_rebounds_assists": points + rebounds + assists,
        "blocks_steals": blocks + steals,
    }
    value = mapping.get(str(market or "").strip().lower())
    return float(value) if value is not None else None


def _window_average(values: list[float], window: int) -> float | None:
    if not values:
        return None
    subset = values[-window:]
    return round(sum(subset) / len(subset), 3)


def _window_std(values: list[float], window: int) -> float | None:
    if not values:
        return None
    subset = values[-window:]
    if len(subset) < 2:
        return 0.0
    mean = sum(subset) / len(subset)
    variance = sum((value - mean) * (value - mean) for value in subset) / len(subset)
    return round(variance ** 0.5, 3)


def _history_last(values: list[float]) -> float | None:
    if not values:
        return None
    return round(float(values[-1]), 3)


def _nullable_float(value: object) -> float | None:
    if value is None:
        return None
    return float(value)


def _projected_team_first_half_share(projected_first_half_total: float | None, game_total: float | None) -> float:
    if projected_first_half_total is None or game_total is None or abs(game_total) < 1e-9:
        return 0.5
    return max(0.35, min(0.65, float(projected_first_half_total) / float(game_total)))


def _dfs_half_model_cache_key(
    db_path: str,
    market: str,
    config: ModelTuningConfig,
) -> str:
    with sqlite3.connect(db_path) as signature_conn:
        signature_conn.row_factory = sqlite3.Row
        runtime_marker = _path_fingerprint(db_path)
        half_marker = player_half_training_db_signature(signature_conn, allow_rebuild=False)
        from .player_prop_training_db import player_prop_training_db_signature
        prop_marker = player_prop_training_db_signature(signature_conn, allow_rebuild=False)
    config_marker = hashlib.sha1(
        json.dumps(config.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    version_marker = hashlib.sha1(DFS_HALF_MODEL_VERSION.encode("utf-8")).hexdigest()[:8]
    return (
        f"{DFS_HALF_MODEL_CACHE_PREFIX}-{market}-{runtime_marker}-"
        f"{half_marker}-{prop_marker}-{config_marker}-{version_marker}.json"
    )


def _dfs_half_eval_cache_key(
    db_path: str,
    config: ModelTuningConfig,
    *,
    min_history_rows: int,
    min_segment_rows: int,
) -> str:
    with sqlite3.connect(db_path) as signature_conn:
        signature_conn.row_factory = sqlite3.Row
        db_marker = _path_fingerprint(db_path)
        half_marker = player_half_training_db_signature(signature_conn, allow_rebuild=False)
        from .player_prop_training_db import player_prop_training_db_signature
        prop_marker = player_prop_training_db_signature(signature_conn, allow_rebuild=False)
    config_marker = hashlib.sha1(
        json.dumps(config.to_dict(), sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()[:12]
    threshold_marker = hashlib.sha1(
        json.dumps(
            {
                "min_history_rows": int(min_history_rows),
                "min_segment_rows": int(min_segment_rows),
                "min_rows": DFS_HALF_MARKET_MIN_ROWS,
                "min_segments": DFS_HALF_MARKET_MIN_SEGMENTS,
                "min_side_accuracy": DFS_HALF_MARKET_MIN_SIDE_ACCURACY,
                "min_mae_edge": DFS_HALF_MARKET_MIN_MAE_EDGE,
                "min_component_edge": DFS_HALF_MARKET_MIN_COMPONENT_EDGE,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    ).hexdigest()[:12]
    version_marker = hashlib.sha1(DFS_HALF_EVAL_VERSION.encode("utf-8")).hexdigest()[:8]
    return (
        f"{DFS_HALF_EVAL_CACHE_PREFIX}-{db_marker}-{half_marker}-{prop_marker}-"
        f"{config_marker}-{threshold_marker}-{version_marker}.json"
    )


def _deserialize_cached_dfs_half_model(payload: dict[str, object]) -> RidgeModel | None:
    if str(payload.get("model_version") or "") != DFS_HALF_MODEL_VERSION:
        return None
    model = payload.get("model")
    if not isinstance(model, dict):
        return None
    try:
        coefficients = [float(value) for value in model["coefficients"]]
        feature_means = [float(value) for value in model["feature_means"]]
        feature_scales = [float(value) for value in model["feature_scales"]]
        expected_feature_count = len(FEATURE_NAMES) + len(DFS_HALF_EXTRA_FEATURES)
        if (
            len(coefficients) != expected_feature_count
            or len(feature_means) != expected_feature_count
            or len(feature_scales) != expected_feature_count
        ):
            return None
        return RidgeModel(
            market=str(model["market"]),
            rows=int(model["rows"]),
            intercept=float(model["intercept"]),
            coefficients=coefficients,
            feature_means=feature_means,
            feature_scales=feature_scales,
        )
    except (KeyError, TypeError, ValueError):
        return None


def _load_cached_dfs_half_model(cache_key: str, *, market: str | None = None) -> RidgeModel | None:
    payload = read_json_cache(cache_key)
    if isinstance(payload, dict):
        loaded = _deserialize_cached_dfs_half_model(payload)
        if loaded is not None:
            return loaded
    if not market:
        return None
    cache_dir = get_cache_dir()
    prefix = f"{DFS_HALF_MODEL_CACHE_PREFIX}-{market}-"
    try:
        candidates = sorted(cache_dir.glob(f"{prefix}*.json"), key=lambda path: path.stat().st_mtime_ns, reverse=True)
    except OSError:
        return None
    for path in candidates:
        loaded_payload = read_json_cache(path.name)
        if not isinstance(loaded_payload, dict):
            continue
        loaded = _deserialize_cached_dfs_half_model(loaded_payload)
        if loaded is not None:
            return loaded
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


def _load_cached_dfs_half_eval(cache_key: str) -> dict[str, object] | None:
    payload = read_json_cache(cache_key)
    if isinstance(payload, dict) and str(payload.get("model_family") or "") == DFS_HALF_EVAL_VERSION:
        return payload
    return None


def _store_cached_dfs_half_eval(cache_key: str, payload: dict[str, object]) -> None:
    try:
        write_json_cache(cache_key, payload)
    except OSError:
        return


def _path_fingerprint(raw_path: str) -> str:
    try:
        payload = str(Path(raw_path).resolve())
    except OSError:
        payload = str(Path(raw_path))
    return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:12]
