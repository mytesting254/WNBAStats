from __future__ import annotations

import math
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .odds import american_to_implied_probability, expected_value
from .player_prop_model import MODEL_VERSION as LEARNED_MODEL_VERSION
from .player_prop_model import clear_model_cache, predict_player_prop


MODEL_VERSION = LEARNED_MODEL_VERSION
LOCAL_TZ = timezone(timedelta(hours=-4))

MARKET_COLUMNS = {
    "points": "points",
    "rebounds": "rebounds",
    "assists": "assists",
    "points_rebounds": "points + rebounds",
    "points_assists": "points + assists",
    "rebounds_assists": "rebounds + assists",
    "threes": "threes",
    "steals": "steals",
    "blocks": "blocks",
    "points_rebounds_assists": "points + rebounds + assists",
}

MARKET_SIGMA_FLOORS = {
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

CALIBRATION_MIN_SAMPLES = 120
CALIBRATION_BIN_WIDTH = 0.05
CALIBRATION_SHRINKAGE_K = 20.0


@dataclass(frozen=True)
class PropProjection:
    prop_line_id: int
    model_version: str
    prediction_time: str
    projection: float
    recommended_side: str
    model_probability: float
    implied_probability: float
    edge: float
    expected_value: float
    confidence: str
    reason: str


def project_player_market(
    conn: sqlite3.Connection,
    player_id: int,
    market: str,
    game_id: int | None = None,
) -> tuple[float, str]:
    rows = conn.execute(
        """
        SELECT
            s.*,
            g.game_date,
            g.home_team_id,
            g.away_team_id,
            g.rest_days_home,
            g.rest_days_away,
            p.team_id,
            p.rotation_role
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN players p ON p.id = s.player_id
        WHERE s.player_id = ?
        ORDER BY g.game_date DESC
        LIMIT 10
        """,
        (player_id,),
    ).fetchall()
    if not rows:
        return 0.0, "No historical stats found; projection defaults to 0."

    values = [_market_value(row, market) for row in rows]
    minutes = [float(row["minutes"]) for row in rows]
    rates = [value / max(minute, 1.0) for value, minute in zip(values, minutes)]
    last_5 = values[:5]
    last_10 = values
    recent_avg = sum(last_5) / len(last_5)
    last_10_avg = sum(last_10) / len(last_10)
    weighted_recent = _weighted_average(values)
    weighted_rate = _weighted_average(rates)
    avg_minutes = sum(minutes[:5]) / min(len(minutes), 5)

    adjustment = conn.execute(
        """
        SELECT projected_minutes, usage_multiplier, note
        FROM manual_adjustments
        WHERE player_id = ?
        ORDER BY id DESC
        LIMIT 1
        """,
        (player_id,),
    ).fetchone()

    projected_minutes = avg_minutes
    usage_multiplier = 1.0
    adjustment_note = "no manual adjustment"
    if adjustment:
        usage_multiplier = float(adjustment["usage_multiplier"] or 1.0)
        if adjustment["projected_minutes"]:
            projected_minutes = float(adjustment["projected_minutes"])
        adjustment_note = adjustment["note"] or "manual adjustment applied"

    context = _game_context(conn, player_id, game_id) if game_id else None
    blowout = _blowout_adjustment(conn, context, rows[0]["rotation_role"] if rows else "starter") if context else {
        "risk": "low",
        "probability": 0.0,
        "minutes_delta": 0.0,
        "factor": 1.0,
    }
    projected_minutes = max(projected_minutes + blowout["minutes_delta"], 4.0)
    rate_projection = weighted_rate * projected_minutes
    base_projection = (
        (0.40 * weighted_recent)
        + (0.25 * recent_avg)
        + (0.20 * rate_projection)
        + (0.15 * last_10_avg)
    )

    pace_factor = _pace_factor(conn, context["team_id"], context["opponent_id"]) if context else 1.0
    opponent_factor = _opponent_factor(conn, context["opponent_id"], market) if context else 1.0
    common_opponent_factor = _common_opponent_factor(conn, player_id, market, context) if context else 1.0
    home_factor = 1.02 if context and context["is_home"] else 0.99 if context else 1.0
    rest_factor = _rest_factor(context["rest_days"]) if context else 1.0

    projection = (
        base_projection
        * pace_factor
        * opponent_factor
        * common_opponent_factor
        * home_factor
        * rest_factor
        * usage_multiplier
    )
    reason = (
        f"Weighted recent {weighted_recent:.1f}, last 5 {recent_avg:.1f}, last 10 {last_10_avg:.1f}, "
        f"rate x minutes {rate_projection:.1f} on {projected_minutes:.1f} projected minutes. "
        f"Adjustments: pace {pace_factor:.2f}, opponent {opponent_factor:.2f}, "
        f"common opponents {common_opponent_factor:.2f}, "
        f"blowout {blowout['risk']} ({blowout['minutes_delta']:+.1f} min), "
        f"{'home' if context and context['is_home'] else 'away' if context else 'neutral'} {home_factor:.2f}, "
        f"rest {rest_factor:.2f}, usage {usage_multiplier:.2f}; {adjustment_note}."
    )
    return round(projection, 2), reason


def build_prop_projection(conn: sqlite3.Connection, prop_line_id: int) -> PropProjection:
    prop = conn.execute(
        """
        SELECT pl.*, p.full_name, g.start_time
        FROM prop_lines pl
        JOIN players p ON p.id = pl.player_id
        JOIN games g ON g.id = pl.game_id
        WHERE pl.id = ?
        """,
        (prop_line_id,),
    ).fetchone()
    if not prop:
        raise ValueError(f"Prop line {prop_line_id} was not found")

    projection, reason, model_version = predict_player_prop(
        conn,
        prop["player_id"],
        prop["market"],
        prop["game_id"],
        line=float(prop["line"]),
        over_odds=int(prop["over_odds"]),
        under_odds=int(prop["under_odds"]),
    )
    line = float(prop["line"])
    stat_sigma = _estimated_sigma(conn, prop["player_id"], prop["market"], projection, prop["game_id"])
    over_probability = 1 - _normal_cdf(line, projection, stat_sigma)
    under_probability = 1 - over_probability
    over_model_probability = _calibrated_probability(
        conn,
        over_probability,
        prop["market"],
        model_version,
        "over",
    )
    under_model_probability = _calibrated_probability(
        conn,
        under_probability,
        prop["market"],
        model_version,
        "under",
    )
    over_odds = int(prop["over_odds"])
    under_odds = int(prop["under_odds"])
    over_implied = american_to_implied_probability(over_odds)
    under_implied = american_to_implied_probability(under_odds)
    edge_over = over_model_probability - over_implied
    edge_under = under_model_probability - under_implied

    # Conservative over gating: avoid thin-margin over recommendations,
    # especially in markets where overs have underperformed historically.
    if projection > line and (projection - line) < _over_min_margin(prop["market"]):
        edge_over -= 0.03
    # Conservative under gating for points/rebounds: avoid medium-quality
    # under calls that have historically been less stable.
    if projection < line and (line - projection) < _under_min_margin(prop["market"]):
        edge_under -= 0.03

    if edge_over >= edge_under:
        side = "over"
        model_probability = over_model_probability
        implied = over_implied
        edge = edge_over
        odds = over_odds
    else:
        side = "under"
        model_probability = under_model_probability
        implied = under_implied
        edge = edge_under
        odds = under_odds
    ev = expected_value(model_probability, odds)

    return PropProjection(
        prop_line_id=prop_line_id,
        model_version=model_version,
        prediction_time=datetime.now(timezone.utc).isoformat(),
        projection=projection,
        recommended_side=side,
        model_probability=round(model_probability, 4),
        implied_probability=round(implied, 4),
        edge=round(edge, 4),
        expected_value=round(ev, 4),
        confidence=_confidence(
            edge,
            abs(projection - line),
            stat_sigma,
            prop["player_id"],
            prop["game_id"],
            conn,
            prop["market"],
            side,
        ),
        reason=reason,
    )


def rebuild_predictions(
    conn: sqlite3.Connection,
    game_ids: list[int] | None = None,
    refresh_models: bool = True,
) -> list[PropProjection]:
    if refresh_models:
        clear_model_cache()
    target_game_ids = sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0})
    _refresh_scheduled_game_rest_days(conn, game_ids=target_game_ids or None)
    # Defensive cleanup for legacy partial-import states.
    conn.execute(
        """
        DELETE FROM watchlist_snapshot_items
        WHERE NOT EXISTS (
            SELECT 1
            FROM prop_predictions pp
            WHERE pp.id = watchlist_snapshot_items.prediction_id
        )
        """
    )
    # Defensive cleanup in case legacy/orphaned rows exist from prior partial imports.
    conn.execute(
        """
        DELETE FROM prop_predictions
        WHERE NOT EXISTS (
            SELECT 1
            FROM prop_lines pl
            WHERE pl.id = prop_predictions.prop_line_id
        )
        """
    )
    game_filter = ""
    game_filter_params: tuple[int, ...] = ()
    if target_game_ids:
        placeholders = ",".join("?" for _ in target_game_ids)
        game_filter = f" AND pl.game_id IN ({placeholders})"
        game_filter_params = tuple(target_game_ids)
    props = conn.execute(
        """
        SELECT pl.id
        FROM prop_lines pl
        JOIN games g ON g.id = pl.game_id
        WHERE g.status = 'scheduled'
        """ + game_filter + """
        ORDER BY pl.captured_at DESC
        """,
        game_filter_params,
    ).fetchall()
    projections = [build_prop_projection(conn, int(row["id"])) for row in props]
    prop_ids = [p.prop_line_id for p in projections]
    if prop_ids:
        placeholders = ",".join("?" for _ in prop_ids)
        conn.execute(
            f"""
            DELETE FROM watchlist_snapshot_items
            WHERE prediction_id IN (
                SELECT id
                FROM prop_predictions
                WHERE model_version = ?
                  AND prop_line_id IN ({placeholders})
            )
            """,
            (MODEL_VERSION, *prop_ids),
        )
        conn.execute(
            f"""
            DELETE FROM prop_predictions
            WHERE model_version = ?
              AND prop_line_id IN ({placeholders})
            """,
            (MODEL_VERSION, *prop_ids),
        )
    conn.executemany(
        """
        INSERT INTO prop_predictions (
            prop_line_id, model_version, prediction_time, projection, recommended_side,
            model_probability, implied_probability, edge, expected_value, confidence, reason
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        [
            (
                p.prop_line_id,
                p.model_version,
                p.prediction_time,
                p.projection,
                p.recommended_side,
                p.model_probability,
                p.implied_probability,
                p.edge,
                p.expected_value,
                p.confidence,
                p.reason,
            )
            for p in projections
        ],
    )
    conn.commit()
    return projections


def _refresh_scheduled_game_rest_days(conn: sqlite3.Connection, game_ids: list[int] | None = None) -> None:
    params: tuple[int, ...] = ()
    game_filter = ""
    if game_ids:
        placeholders = ",".join("?" for _ in game_ids)
        game_filter = f" AND id IN ({placeholders})"
        params = tuple(game_ids)
    games = conn.execute(
        """
        SELECT id, home_team_id, away_team_id, start_time, game_date
        FROM games
        WHERE status = 'scheduled'
        """ + game_filter,
        params,
    ).fetchall()
    updates = []
    for game in games:
        home_rest = _rest_days_before_game(conn, int(game["home_team_id"]), str(game["start_time"]), game["game_date"])
        away_rest = _rest_days_before_game(conn, int(game["away_team_id"]), str(game["start_time"]), game["game_date"])
        if home_rest is None and away_rest is None:
            continue
        updates.append(
            (
                int(home_rest) if home_rest is not None else 2,
                int(away_rest) if away_rest is not None else 2,
                int(game["id"]),
            )
        )
    if updates:
        conn.executemany(
            """
            UPDATE games
            SET rest_days_home = ?, rest_days_away = ?
            WHERE id = ?
            """,
            updates,
        )


def _rest_days_before_game(conn: sqlite3.Connection, team_id: int, start_time: str, game_date: str | None = None) -> int | None:
    current_date = _parse_game_date(game_date) if game_date else None
    if current_date is None:
        current_start = _parse_game_start(start_time)
        if current_start is None:
            return None
        current_date = current_start.astimezone(LOCAL_TZ).date()
    else:
        current_start = _parse_game_start(start_time)
    rows = conn.execute(
        """
        SELECT g.start_time, g.game_date
        FROM games g
        WHERE (g.home_team_id = ? OR g.away_team_id = ?)
          AND lower(g.status) NOT IN ('canceled', 'cancelled')
        ORDER BY g.start_time DESC
        """,
        (team_id, team_id),
    ).fetchall()
    previous_dates = []
    for row in rows:
        previous_start = _parse_game_start(row["start_time"])
        if current_start is not None and (previous_start is None or previous_start >= current_start):
            continue
        previous_date = _parse_game_date(row["game_date"])
        if previous_date is None and previous_start is not None:
            previous_date = previous_start.astimezone(LOCAL_TZ).date()
        if previous_date and previous_date < current_date:
            previous_dates.append(previous_date)
    if not previous_dates:
        return None
    rest_days = max((current_date - max(previous_dates)).days, 0)
    if rest_days > 14:
        return None
    return rest_days


def _parse_game_date(value: str | None):
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _parse_game_start(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc)


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
    column = MARKET_COLUMNS.get(market)
    if not column:
        raise ValueError(f"Unsupported market: {market}")
    return float(row[column])


def _estimated_sigma(conn: sqlite3.Connection, player_id: int, market: str, projection: float, game_id: int | None = None) -> float:
    rows = conn.execute(
        """
        SELECT s.*
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        WHERE s.player_id = ?
        ORDER BY g.game_date DESC
        LIMIT 10
        """,
        (player_id,),
    ).fetchall()
    values = [_market_value(row, market) for row in rows]
    if len(values) < 2:
        return MARKET_SIGMA_FLOORS.get(market, 2.0)
    ewma_center = _ewma_newest_first(values, alpha=0.42)
    ewma_sigma = _ewma_sigma_newest_first(values, ewma_center)
    mean = sum(values) / len(values)
    sample_sigma = math.sqrt(sum((value - mean) ** 2 for value in values) / (len(values) - 1))
    floor = max(MARKET_SIGMA_FLOORS.get(market, 2.0), projection * 0.10)
    base_sigma = max((0.60 * ewma_sigma) + (0.25 * sample_sigma) + (0.15 * floor), floor)
    sample_count, avg_minutes = _player_sample_quality(conn, player_id, game_id)
    minute_volatility = _player_minute_volatility(conn, player_id, game_id)
    context = _game_context(conn, player_id, game_id) if game_id else None
    blowout_probability = _blowout_probability(abs(float(context["spread_home"]))) if context and context.get("spread_home") is not None else 0.0
    uncertainty_multiplier = 1.0
    if sample_count < 4:
        uncertainty_multiplier = 1.75
    elif sample_count < 7:
        uncertainty_multiplier = 1.45
    elif avg_minutes < 16:
        uncertainty_multiplier = 1.25
    if minute_volatility >= 10:
        uncertainty_multiplier *= 1.18
    elif minute_volatility >= 7:
        uncertainty_multiplier *= 1.11
    elif minute_volatility >= 5:
        uncertainty_multiplier *= 1.06
    if blowout_probability >= 0.45:
        uncertainty_multiplier *= 1.10
    elif blowout_probability >= 0.30:
        uncertainty_multiplier *= 1.06
    return base_sigma * uncertainty_multiplier


def _weighted_average(values: list[float]) -> float:
    weights = list(range(len(values), 0, -1))
    return sum(value * weight for value, weight in zip(values, weights)) / sum(weights)


def _ewma_newest_first(values: list[float], alpha: float) -> float:
    estimate = values[-1]
    for value in reversed(values[:-1]):
        estimate = (alpha * value) + ((1 - alpha) * estimate)
    return estimate


def _ewma_sigma_newest_first(values: list[float], center: float) -> float:
    alpha = 0.42
    variance = (values[-1] - center) ** 2
    for value in reversed(values[:-1]):
        variance = (alpha * ((value - center) ** 2)) + ((1 - alpha) * variance)
    return math.sqrt(max(variance, 0.0))


def _game_context(conn: sqlite3.Connection, player_id: int, game_id: int | None) -> dict:
    row = conn.execute(
        """
        SELECT
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
        return {"team_id": 0, "opponent_id": 0, "is_home": False, "rest_days": 2}
    team_id = int(row["resolved_team_id"])
    is_home = team_id == int(row["home_team_id"])
    opponent_id = int(row["away_team_id"] if is_home else row["home_team_id"])
    rest_days = int(row["rest_days_home"] if is_home else row["rest_days_away"] or 2)
    return {
        "team_id": team_id,
        "opponent_id": opponent_id,
        "is_home": is_home,
        "rest_days": rest_days,
        "spread_home": float(row["spread_home"]) if row["spread_home"] is not None else None,
        "game_total": float(row["game_total"]) if row["game_total"] is not None else None,
    }


def _blowout_adjustment(conn: sqlite3.Connection, context: dict, rotation_role: str | None) -> dict:
    spread_home = context.get("spread_home")
    if spread_home is None:
        return {"risk": "unknown", "probability": 0.0, "minutes_delta": 0.0, "factor": 1.0}

    team_spread = spread_home if context["is_home"] else -spread_home
    spread_abs = abs(team_spread)
    probability = _blowout_probability(spread_abs)
    risk = _blowout_risk_label(probability)
    role = rotation_role or "starter"
    role_delta = {
        "star": -4.0,
        "starter": -3.0,
        "rotation": -1.5,
        "bench": 2.0,
    }.get(role, -2.0)
    historical_delta = _historical_blowout_minutes_delta(conn, context["team_id"], role)
    minutes_delta = probability * ((0.65 * role_delta) + (0.35 * historical_delta))
    return {
        "risk": risk,
        "probability": probability,
        "minutes_delta": minutes_delta,
        "factor": 1 + (minutes_delta / 34),
    }


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


def _blowout_risk_label(probability: float) -> str:
    if probability >= 0.45:
        return "very high"
    if probability >= 0.30:
        return "high"
    if probability >= 0.15:
        return "medium"
    return "low"


def _historical_blowout_minutes_delta(conn: sqlite3.Connection, team_id: int, role: str | None) -> float:
    rows = conn.execute(
        """
        SELECT
            s.minutes,
            ABS(r.points - r.opponent_points) AS margin
        FROM player_game_stats s
        JOIN players p ON p.id = s.player_id
        JOIN team_game_results r ON r.game_id = s.game_id AND r.team_id = p.team_id
        WHERE p.team_id = ?
          AND p.rotation_role = ?
        """,
        (team_id, role or "starter"),
    ).fetchall()
    close_minutes = [float(row["minutes"]) for row in rows if float(row["margin"]) <= 10]
    blowout_minutes = [float(row["minutes"]) for row in rows if float(row["margin"]) >= 15]
    if len(close_minutes) < 2 or len(blowout_minutes) < 2:
        return 2.0 if role == "bench" else -3.0
    return (sum(blowout_minutes) / len(blowout_minutes)) - (sum(close_minutes) / len(close_minutes))


def _pace_factor(conn: sqlite3.Connection, team_id: int, opponent_id: int) -> float:
    league_pace = _avg_scalar(conn, "SELECT AVG(possessions) FROM team_game_results") or 78.0
    team_pace = _avg_scalar(conn, "SELECT AVG(possessions) FROM team_game_results WHERE team_id = ?", (team_id,)) or league_pace
    opponent_pace = _avg_scalar(conn, "SELECT AVG(possessions) FROM team_game_results WHERE team_id = ?", (opponent_id,)) or league_pace
    expected_pace = (team_pace + opponent_pace) / 2
    return _clamp(expected_pace / league_pace, 0.94, 1.06)


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
        """,
        (opponent_id,),
    ).fetchall()
    league_rows = conn.execute("SELECT * FROM player_game_stats").fetchall()
    if not opponent_rows or not league_rows:
        return 1.0
    opponent_allowed = sum(_market_value(row, market) for row in opponent_rows) / len(opponent_rows)
    league_allowed = sum(_market_value(row, market) for row in league_rows) / len(league_rows)
    if league_allowed <= 0:
        return 1.0
    return _clamp(opponent_allowed / league_allowed, 0.90, 1.10)


def _common_opponent_factor(conn: sqlite3.Connection, player_id: int, market: str, context: dict) -> float:
    common_opponents = _common_opponent_ids(conn, context["team_id"], context["opponent_id"])
    if not common_opponents:
        return 1.0

    placeholders = ",".join("?" for _ in common_opponents)
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
        ORDER BY g.game_date DESC
        LIMIT 10
        """,
        (player_id, *common_opponents),
    ).fetchall()
    if len(player_rows) < 2:
        return 1.0

    all_rows = conn.execute(
        "SELECT s.* FROM player_game_stats s WHERE s.player_id = ? ORDER BY s.game_id DESC LIMIT 10",
        (player_id,),
    ).fetchall()
    if not all_rows:
        return 1.0

    common_avg = sum(_market_value(row, market) for row in player_rows) / len(player_rows)
    player_avg = sum(_market_value(row, market) for row in all_rows) / len(all_rows)
    if player_avg <= 0:
        return 1.0

    raw_factor = common_avg / player_avg
    sample_weight = min(len(player_rows) / 5, 1.0)
    regressed_factor = 1 + ((raw_factor - 1) * sample_weight * 0.5)
    return _clamp(regressed_factor, 0.94, 1.06)


def _common_opponent_ids(conn: sqlite3.Connection, team_id: int, opponent_id: int) -> list[int]:
    team_opponents = _recent_opponent_ids(conn, team_id)
    opponent_opponents = _recent_opponent_ids(conn, opponent_id)
    return sorted(team_opponents.intersection(opponent_opponents))


def _recent_opponent_ids(conn: sqlite3.Connection, team_id: int) -> set[int]:
    rows = conn.execute(
        """
        SELECT
            CASE
                WHEN g.home_team_id = ? THEN g.away_team_id
                ELSE g.home_team_id
            END AS opponent_id
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        WHERE r.team_id = ?
        ORDER BY g.game_date DESC
        LIMIT 10
        """,
        (team_id, team_id),
    ).fetchall()
    return {int(row["opponent_id"]) for row in rows if row["opponent_id"] is not None}


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


def _normal_cdf(x: float, mean: float, sigma: float) -> float:
    z = (x - mean) / (sigma * math.sqrt(2))
    return 0.5 * (1 + math.erf(z))


def _confidence(
    edge: float,
    stat_margin: float,
    sigma: float,
    player_id: int,
    game_id: int,
    conn: sqlite3.Connection,
    market: str,
    side: str,
) -> str:
    sample_count, avg_minutes = _player_sample_quality(conn, player_id, game_id)
    normalized_margin = stat_margin / max(sigma, 1.0)
    market_key = str(market).lower()
    side_key = str(side).lower()
    base_confidence = "low"
    if sample_count < 7 or avg_minutes < 16:
        if edge >= 0.12 and normalized_margin >= 1.25:
            base_confidence = "medium"
    elif edge >= 0.11 and normalized_margin >= 1.10:
        base_confidence = "high"
    elif edge >= 0.07 and normalized_margin >= 0.85:
        base_confidence = "medium"

    if (
        base_confidence == "medium"
        and side_key == "under"
        and market_key in {"points", "rebounds", "threes"}
        and (edge < 0.12 or normalized_margin < 1.05)
    ):
        return "low"
    if (
        base_confidence != "low"
        and side_key == "over"
        and market_key in {"points", "threes"}
        and (edge < 0.10 or normalized_margin < 1.0)
    ):
        return "low"
    return base_confidence


def _player_sample_quality(conn: sqlite3.Connection, player_id: int, game_id: int | None) -> tuple[int, float]:
    if game_id is None:
        rows = conn.execute(
            """
            SELECT s.minutes
            FROM player_game_stats s
            WHERE s.player_id = ?
            ORDER BY s.game_id DESC
            LIMIT 10
            """,
            (player_id,),
        ).fetchall()
    else:
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


def _player_minute_volatility(conn: sqlite3.Connection, player_id: int, game_id: int | None) -> float:
    if game_id is None:
        rows = conn.execute(
            """
            SELECT s.minutes
            FROM player_game_stats s
            WHERE s.player_id = ?
            ORDER BY s.game_id DESC
            LIMIT 10
            """,
            (player_id,),
        ).fetchall()
    else:
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
    if len(rows) < 2:
        return 0.0
    minutes = [float(row["minutes"]) for row in rows]
    mean = sum(minutes) / len(minutes)
    variance = sum((value - mean) ** 2 for value in minutes) / (len(minutes) - 1)
    return math.sqrt(max(variance, 0.0))


def _calibrated_probability(
    conn: sqlite3.Connection,
    raw_probability: float,
    market: str,
    model_version: str,
    side: str | None = None,
) -> float:
    # Use empirical hit rates by market/probability bin with Bayesian shrinkage.
    calibration = _market_calibration(conn, market, model_version, side=side)
    if not calibration:
        return _clamp(raw_probability, 0.05, 0.95)
    bin_key = _probability_bin(raw_probability)
    global_hit = float(calibration["global_hit"])
    bucket = calibration["bins"].get(bin_key)
    if not bucket:
        return _clamp((0.65 * raw_probability) + (0.35 * global_hit), 0.05, 0.95)
    bucket_hits = float(bucket["hits"])
    bucket_total = float(bucket["total"])
    posterior = (bucket_hits + (CALIBRATION_SHRINKAGE_K * global_hit)) / (bucket_total + CALIBRATION_SHRINKAGE_K)
    calibrated = (0.50 * raw_probability) + (0.50 * posterior)
    return _clamp(calibrated, 0.05, 0.95)


def _over_min_margin(market: str) -> float:
    by_market = {
        "threes": 1.10,
        "points": 0.85,
        "rebounds": 0.40,
        "assists": 0.35,
        "points_rebounds": 0.95,
        "points_assists": 0.95,
        "rebounds_assists": 0.60,
        "points_rebounds_assists": 1.10,
    }
    return by_market.get(market, 0.50)


def _under_min_margin(market: str) -> float:
    by_market = {
        "points": 0.90,
        "rebounds": 0.90,
    }
    return by_market.get(market, 0.0)


def _market_calibration(
    conn: sqlite3.Connection,
    market: str,
    model_version: str,
    side: str | None = None,
) -> dict | None:
    side_filter = "AND r.recommended_side = ?" if side is not None else ""
    params: tuple = (model_version, market, side) if side is not None else (model_version, market)
    rows = conn.execute(
        f"""
        WITH ranked AS (
          SELECT
            pp.prop_line_id,
            pp.model_probability,
            pp.recommended_side,
            pl.market,
            ROW_NUMBER() OVER (
              PARTITION BY pp.prop_line_id
              ORDER BY pp.prediction_time DESC, pp.id DESC
            ) AS rn
          FROM prop_predictions pp
          JOIN prop_lines pl ON pl.id = pp.prop_line_id
          WHERE pp.model_version = ?
        )
        SELECT
          r.model_probability,
          r.recommended_side,
          sp.winning_side
        FROM ranked r
        JOIN settled_props sp ON sp.prop_line_id = r.prop_line_id
        WHERE r.rn = 1
          AND r.market = ?
          {side_filter}
          AND r.model_probability IS NOT NULL
        """,
        params,
    ).fetchall()
    if len(rows) < CALIBRATION_MIN_SAMPLES and side is not None:
        return _market_calibration(conn, market, model_version, side=None)
    if len(rows) < CALIBRATION_MIN_SAMPLES:
        return None
    hits = 0
    bins: dict[str, dict[str, float]] = {}
    for row in rows:
        p = float(row["model_probability"])
        win = 1.0 if str(row["recommended_side"]) == str(row["winning_side"]) else 0.0
        hits += int(win)
        key = _probability_bin(p)
        bucket = bins.setdefault(key, {"hits": 0.0, "total": 0.0})
        bucket["hits"] += win
        bucket["total"] += 1.0
    return {"global_hit": (hits / len(rows)), "bins": bins}


def _probability_bin(probability: float) -> str:
    bounded = _clamp(probability, 0.0, 0.9999)
    lower = math.floor(bounded / CALIBRATION_BIN_WIDTH) * CALIBRATION_BIN_WIDTH
    upper = lower + CALIBRATION_BIN_WIDTH
    return f"{lower:.2f}-{upper:.2f}"
