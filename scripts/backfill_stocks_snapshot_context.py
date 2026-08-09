#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.db import connect
from backend.app.player_prop_model import _shared_projection_context
from backend.app.stocks_tracking import (
    _build_player_stocks_feature_bundle,
    _player_stocks_history_rows,
    _stocks_forced_turnover_rate_factor,
    _stocks_opponent_allowed_factor,
    _stocks_pace_factor,
    _stocks_team_turnover_rate_factor,
    _stocks_turnover_pressure_factor,
    ensure_tracking_schema,
    get_tracking_db_path,
)


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Backfill snapshot-time context fields on existing stocks_tracking projection snapshots."
    )
    parser.add_argument("--start-date", help="Only process snapshots on or after YYYY-MM-DD.")
    parser.add_argument("--end-date", help="Only process snapshots on or before YYYY-MM-DD.")
    parser.add_argument("--game-id", type=int, action="append", help="Limit to one or more game ids.")
    parser.add_argument("--limit", type=int, help="Process at most this many snapshots.")
    parser.add_argument(
        "--include-populated",
        action="store_true",
        help="Recompute rows even if snapshot_has_prep_context / snapshot_has_matchup_context are already set.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Compute updates but do not write them.",
    )
    return parser.parse_args()


def _player_snapshot_team_row(conn: sqlite3.Connection, *, player_id: int, game_id: int) -> sqlite3.Row | None:
    return conn.execute(
        """
        SELECT
            COALESCE(
                (
                    SELECT h.team_id
                    FROM player_team_history h
                    WHERE h.player_id = p.id
                      AND h.game_id = ?
                    ORDER BY h.id DESC
                    LIMIT 1
                ),
                p.team_id
            ) AS team_id,
            t.abbreviation AS team_abbr,
            p.rotation_role
        FROM players p
        LEFT JOIN teams t
          ON t.id = COALESCE(
                (
                    SELECT h.team_id
                    FROM player_team_history h
                    WHERE h.player_id = p.id
                      AND h.game_id = ?
                    ORDER BY h.id DESC
                    LIMIT 1
                ),
                p.team_id
            )
        WHERE p.id = ?
        LIMIT 1
        """,
        (int(game_id), int(game_id), int(player_id)),
    ).fetchone()


def _opponent_id_for_snapshot(conn: sqlite3.Connection, *, game_id: int, team_id: int | None) -> int | None:
    if team_id is None:
        return None
    row = conn.execute(
        """
        SELECT
            CASE
                WHEN home_team_id = ? THEN away_team_id
                WHEN away_team_id = ? THEN home_team_id
                ELSE NULL
            END AS opponent_id
        FROM games
        WHERE id = ?
        LIMIT 1
        """,
        (int(team_id), int(team_id), int(game_id)),
    ).fetchone()
    if row is None or row["opponent_id"] is None:
        return None
    return int(row["opponent_id"])


def _historical_team_unavailable_count(conn: sqlite3.Connection, *, game_id: int, team_id: int | None) -> int:
    if team_id is None:
        return 0
    row = conn.execute(
        """
        SELECT COUNT(*)
        FROM player_game_availability
        WHERE game_id = ?
          AND team_id = ?
          AND did_not_play = 1
        """,
        (int(game_id), int(team_id)),
    ).fetchone()
    return 0 if row is None else int(row[0] or 0)


def _candidate_reason(
    *,
    rotation_role: str,
    recent_minutes_avg: float,
    recent_stocks_avg: float,
    recent_games: int,
    last3_minutes_avg: float,
    stability_minutes_avg: float,
    team_unavailable_count: int,
) -> str:
    normalized_role = str(rotation_role or "").strip().lower()
    is_defensive_specialist = (
        recent_stocks_avg >= 1.1
        and recent_minutes_avg >= 8.0
        and normalized_role in {"star", "starter", "rotation", "bench"}
    )
    if last3_minutes_avg >= 16.0 and last3_minutes_avg >= stability_minutes_avg + 4.0:
        return "recent lineup promotion"
    if is_defensive_specialist:
        return "defensive specialist profile"
    if team_unavailable_count >= 2 and recent_minutes_avg >= 8.0:
        return "injury opportunity replacement"
    if recent_stocks_avg >= 1.8:
        return "recent stocks specialist"
    if recent_minutes_avg >= 22.0:
        return "stable rotation minutes"
    if recent_games > 0:
        return "rotation candidate"
    return "historical backfill"


def _historical_matchup_context(
    conn: sqlite3.Connection,
    *,
    game_id: int,
    team_id: int | None,
    opponent_id: int | None,
) -> dict[str, float] | None:
    if team_id is None or opponent_id is None:
        return None
    return {
        "pace_factor": float(_stocks_pace_factor(conn, team_id, opponent_id)),
        "steals_allowed_factor": float(_stocks_opponent_allowed_factor(conn, opponent_id, "steals")),
        "blocks_allowed_factor": float(_stocks_opponent_allowed_factor(conn, opponent_id, "blocks")),
        "stocks_allowed_factor": float(_stocks_opponent_allowed_factor(conn, opponent_id, "blocks_steals")),
        "team_turnover_rate_factor": float(_stocks_team_turnover_rate_factor(conn, team_id)),
        "forced_turnover_rate_factor": float(_stocks_forced_turnover_rate_factor(conn, opponent_id)),
        "turnover_pressure_factor": float(_stocks_turnover_pressure_factor(conn, opponent_id)),
    }


def _snapshot_query(args: argparse.Namespace) -> tuple[str, list[object]]:
    filters: list[str] = []
    params: list[object] = []
    if not args.include_populated:
        filters.append(
            "(COALESCE(snapshot_has_prep_context, 0) = 0 OR COALESCE(snapshot_has_matchup_context, 0) = 0 OR snapshot_team_id IS NULL)"
        )
    if args.start_date:
        filters.append("game_date >= ?")
        params.append(str(args.start_date))
    if args.end_date:
        filters.append("game_date <= ?")
        params.append(str(args.end_date))
    game_ids = sorted({int(game_id) for game_id in (args.game_id or []) if int(game_id) > 0})
    if game_ids:
        placeholders = ",".join("?" for _ in game_ids)
        filters.append(f"game_id IN ({placeholders})")
        params.extend(game_ids)
    where_sql = ""
    if filters:
        where_sql = "WHERE " + " AND ".join(filters)
    limit_sql = f" LIMIT {int(args.limit)}" if args.limit and int(args.limit) > 0 else ""
    sql = f"""
        SELECT
            id,
            game_id,
            player_id,
            player_name,
            game_date,
            projected_stocks
        FROM projection_snapshots
        {where_sql}
        ORDER BY game_date, game_id, player_id, id
        {limit_sql}
    """
    return sql, params


def main() -> None:
    args = _parse_args()
    tracking_path = ensure_tracking_schema()
    with connect() as conn:
        tracking = sqlite3.connect(tracking_path)
        tracking.row_factory = sqlite3.Row
        try:
            sql, params = _snapshot_query(args)
            rows = tracking.execute(sql, tuple(params)).fetchall()
            runtime_cache: dict[str, dict[tuple, object]] = {}
            updates: list[tuple[object, ...]] = []
            missing_team = 0
            missing_history = 0
            for row in rows:
                snapshot_id = int(row["id"])
                game_id = int(row["game_id"])
                player_id = int(row["player_id"])
                game_date = str(row["game_date"])
                projected_stocks = float(row["projected_stocks"] or 0.0)

                team_row = _player_snapshot_team_row(conn, player_id=player_id, game_id=game_id)
                if team_row is None:
                    missing_team += 1
                    continue
                team_id = int(team_row["team_id"]) if team_row["team_id"] is not None else None
                team_abbr = str(team_row["team_abbr"] or "")
                rotation_role = str(team_row["rotation_role"] or "")
                opponent_id = _opponent_id_for_snapshot(conn, game_id=game_id, team_id=team_id)
                matchup_context = _historical_matchup_context(conn, game_id=game_id, team_id=team_id, opponent_id=opponent_id)

                shared = _shared_projection_context(
                    conn,
                    player_id=player_id,
                    game_id=game_id,
                    before_game_date=game_date,
                    allow_training=False,
                    use_injury_context=True,
                    use_live_minutes_context=True,
                    runtime_cache=runtime_cache,
                )
                history_rows = list(shared.get("history_rows") or [])
                recent_games = min(len(history_rows), 12)
                if not history_rows:
                    missing_history += 1
                recent_slice = history_rows[:12]
                last3_slice = history_rows[:3]
                recent_minutes_avg = (
                    sum(float(item["minutes"] or 0.0) for item in recent_slice) / len(recent_slice)
                    if recent_slice
                    else 0.0
                )
                recent_stocks_avg = (
                    sum(float(item["steals"] or 0.0) + float(item["blocks"] or 0.0) for item in recent_slice) / len(recent_slice)
                    if recent_slice
                    else 0.0
                )
                last3_minutes_avg = (
                    sum(float(item["minutes"] or 0.0) for item in last3_slice) / len(last3_slice)
                    if last3_slice
                    else 0.0
                )
                stability_minutes_avg = (
                    sum(float(item["minutes"] or 0.0) for item in history_rows) / len(history_rows)
                    if history_rows
                    else 0.0
                )
                candidate_reason = _candidate_reason(
                    rotation_role=rotation_role,
                    recent_minutes_avg=recent_minutes_avg,
                    recent_stocks_avg=recent_stocks_avg,
                    recent_games=recent_games,
                    last3_minutes_avg=last3_minutes_avg,
                    stability_minutes_avg=stability_minutes_avg,
                    team_unavailable_count=_historical_team_unavailable_count(conn, game_id=game_id, team_id=team_id),
                )

                bundle = _build_player_stocks_feature_bundle(
                    conn,
                    player_id=player_id,
                    game_id=game_id,
                    game_date=game_date,
                    market="blocks_steals",
                    base_projection=projected_stocks,
                    matchup_context=matchup_context,
                )
                injury = dict(shared.get("injury") or {})
                opportunity_context = list(shared.get("opportunity_context") or [0.0, 0.0, 0.0, 0.0])
                updates.append(
                    (
                        team_id,
                        team_abbr,
                        rotation_role,
                        candidate_reason,
                        round(recent_minutes_avg, 6),
                        round(recent_stocks_avg, 6),
                        int(recent_games),
                        float(shared.get("projected_minutes") or 0.0),
                        float(shared.get("minute_volatility") or 0.0),
                        (
                            None
                            if bundle["recent_hit_rate_2_plus"] is None
                            else float(bundle["recent_hit_rate_2_plus"])
                        ),
                        str(injury.get("status") or "available"),
                        float(injury.get("availability_factor") or 1.0),
                        float(injury.get("usage_multiplier") or 1.0),
                        float(injury.get("minutes_delta") or 0.0),
                        float(opportunity_context[0] if len(opportunity_context) >= 1 else 0.0),
                        float(opportunity_context[1] if len(opportunity_context) >= 2 else 0.0),
                        float(opportunity_context[2] if len(opportunity_context) >= 3 else 0.0),
                        float(opportunity_context[3] if len(opportunity_context) >= 4 else 0.0),
                        None if matchup_context is None else float(matchup_context["pace_factor"]),
                        None if matchup_context is None else float(matchup_context["stocks_allowed_factor"]),
                        None if matchup_context is None else float(matchup_context["turnover_pressure_factor"]),
                        1 if history_rows else 0,
                        1 if matchup_context is not None else 0,
                        snapshot_id,
                    )
                )

            if updates and not args.dry_run:
                tracking.executemany(
                    """
                    UPDATE projection_snapshots
                    SET
                        snapshot_team_id = ?,
                        snapshot_team_abbr = ?,
                        snapshot_rotation_role = ?,
                        snapshot_candidate_reason = ?,
                        snapshot_recent_minutes_avg = ?,
                        snapshot_recent_stocks_avg = ?,
                        snapshot_recent_games = ?,
                        snapshot_projected_minutes = ?,
                        snapshot_minute_volatility = ?,
                        snapshot_recent_hit_rate_2_plus = ?,
                        snapshot_injury_status = ?,
                        snapshot_injury_availability_factor = ?,
                        snapshot_injury_usage_multiplier = ?,
                        snapshot_injury_minutes_delta = ?,
                        snapshot_opportunity_unavailable = ?,
                        snapshot_opportunity_key_outs = ?,
                        snapshot_opportunity_persistence = ?,
                        snapshot_opportunity_competition = ?,
                        snapshot_pace_factor = ?,
                        snapshot_stocks_allowed_factor = ?,
                        snapshot_turnover_pressure_factor = ?,
                        snapshot_has_prep_context = ?,
                        snapshot_has_matchup_context = ?
                    WHERE id = ?
                    """,
                    updates,
                )
                tracking.commit()

            payload = {
                "tracking_db_path": str(get_tracking_db_path()),
                "dry_run": bool(args.dry_run),
                "selected_rows": len(rows),
                "updated_rows": len(updates),
                "missing_team_rows": missing_team,
                "missing_history_rows": missing_history,
            }
            print(json.dumps(payload, indent=2, sort_keys=True))
        finally:
            tracking.close()


if __name__ == "__main__":
    main()
