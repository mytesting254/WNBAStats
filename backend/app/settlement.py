from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .projections import _market_value


def settle_completed_props(
    conn: sqlite3.Connection,
    *,
    selected_date: str | None = None,
    selected_dates: list[str] | None = None,
) -> dict:
    target_dates = _normalized_dates(selected_date, selected_dates)
    hydrated_dates = _hydrate_team_stats_for_settlement(conn, target_dates)
    date_filter = ""
    params: tuple[str, ...] = ()
    if target_dates:
        placeholders = ",".join("?" for _ in target_dates)
        date_filter = f" AND pc.game_date IN ({placeholders})"
        params = tuple(target_dates)
    rows = conn.execute(
        """
        WITH prop_context AS (
            SELECT
                pl.id AS prop_line_id,
                pl.game_id,
                pl.player_id,
                pl.market,
                pl.line,
                pgs.*,
                g.game_date,
                g.home_team_id,
                g.away_team_id,
                g.spread_home,
                COALESCE(
                    (
                        SELECT h.team_id
                        FROM player_team_history h
                        WHERE h.player_id = p.id
                          AND h.game_id = pl.game_id
                        ORDER BY h.id DESC
                        LIMIT 1
                    ),
                    p.team_id
                ) AS player_team_id
            FROM prop_lines pl
            JOIN games g ON g.id = pl.game_id
            JOIN players p ON p.id = pl.player_id
            JOIN player_game_stats pgs ON pgs.player_id = pl.player_id AND pgs.game_id = pl.game_id
            WHERE g.status = 'final'
        )
        SELECT
            pc.*,
            tgr.points AS team_points,
            tgr.opponent_points AS opponent_points,
            tgr.possessions AS team_possessions,
            tgr.possessions_source AS team_possessions_source,
            opp.possessions AS opponent_possessions,
            opp.possessions_source AS opponent_possessions_source,
            sp.id AS settled_prop_id,
            sp.game_margin AS existing_game_margin,
            sp.team_margin AS existing_team_margin,
            sp.team_spread AS existing_team_spread,
            sp.blowout_result AS existing_blowout_result,
            sp.blowout_threshold AS existing_blowout_threshold,
            sp.team_points AS existing_team_points,
            sp.opponent_points AS existing_opponent_points,
            sp.team_possessions AS existing_team_possessions,
            sp.opponent_possessions AS existing_opponent_possessions,
            sp.pace AS existing_pace,
            sp.team_off_rating AS existing_team_off_rating,
            sp.opponent_off_rating AS existing_opponent_off_rating,
            sp.net_rating AS existing_net_rating,
            sp.team_possessions_source AS existing_team_possessions_source,
            sp.opponent_possessions_source AS existing_opponent_possessions_source
        FROM prop_context pc
        LEFT JOIN team_game_results tgr ON tgr.game_id = pc.game_id AND tgr.team_id = pc.player_team_id
        LEFT JOIN team_game_results opp ON opp.game_id = pc.game_id AND opp.team_id != pc.player_team_id
        LEFT JOIN settled_props sp ON sp.prop_line_id = pc.prop_line_id
        WHERE 1 = 1
        """
        + date_filter
        + """
        ORDER BY pc.game_date, pc.prop_line_id
        """,
        params,
    ).fetchall()

    settled_at = datetime.now(timezone.utc).isoformat()
    settlements = []
    repairs = []
    skipped = 0
    for row in rows:
        try:
            settlement = _build_settlement(row, settled_at)
        except (KeyError, IndexError, ValueError):
            skipped += 1
            continue
        if row["settled_prop_id"] is None:
            settlements.append(settlement)
        elif _needs_repair(row):
            repairs.append(settlement)

    conn.executemany(
        """
        INSERT INTO settled_props (
            prop_line_id, actual_result, winning_side, margin,
            player_minutes, game_margin, team_margin, team_spread,
            blowout_result, blowout_threshold, team_points, opponent_points,
            team_possessions, opponent_possessions, pace, team_off_rating,
            opponent_off_rating, net_rating, team_possessions_source,
            opponent_possessions_source, settled_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        settlements,
    )
    conn.executemany(
        """
        UPDATE settled_props
        SET
            actual_result = ?,
            winning_side = ?,
            margin = ?,
            player_minutes = ?,
            game_margin = ?,
            team_margin = ?,
            team_spread = ?,
            blowout_result = ?,
            blowout_threshold = ?,
            team_points = ?,
            opponent_points = ?,
            team_possessions = ?,
            opponent_possessions = ?,
            pace = ?,
            team_off_rating = ?,
            opponent_off_rating = ?,
            net_rating = ?,
            team_possessions_source = ?,
            opponent_possessions_source = ?,
            settled_at = ?
        WHERE prop_line_id = ?
        """,
        [
            (
                settlement[1],
                settlement[2],
                settlement[3],
                settlement[4],
                settlement[5],
                settlement[6],
                settlement[7],
                settlement[8],
                settlement[9],
                settlement[10],
                settlement[11],
                settlement[12],
                settlement[13],
                settlement[14],
                settlement[15],
                settlement[16],
                settlement[17],
                settlement[18],
                settlement[19],
                settlement[20],
                settlement[0],
            )
            for settlement in repairs
        ],
    )
    conn.commit()
    return {
        "eligible": len(rows),
        "settled": len(settlements),
        "repaired": len(repairs),
        "skipped": skipped,
        "settled_at": settled_at,
        "hydrated_dates": hydrated_dates,
        "selected_date": target_dates[0] if len(target_dates) == 1 else None,
        "selected_dates": target_dates,
    }


def _normalized_dates(selected_date: str | None, selected_dates: list[str] | None) -> list[str]:
    values = [selected_date] if selected_date else []
    values.extend(selected_dates or [])
    cleaned = sorted({str(value).strip() for value in values if str(value).strip()})
    return cleaned


def _hydrate_team_stats_for_settlement(conn: sqlite3.Connection, target_dates: list[str]) -> list[str]:
    candidate_dates = target_dates or _unsettled_final_prop_dates(conn)
    hydrated_dates: list[str] = []
    if not candidate_dates:
        return hydrated_dates
    from .espn_history import import_espn_player_boxscores

    for item in candidate_dates:
        try:
            import_espn_player_boxscores(
                conn,
                _season_for_date(item),
                force_refresh=False,
                missing_only=False,
                selected_date=item,
            )
            hydrated_dates.append(item)
        except Exception:
            continue
    return hydrated_dates


def _unsettled_final_prop_dates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        SELECT DISTINCT g.game_date
        FROM prop_lines pl
        JOIN games g ON g.id = pl.game_id
        LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
        WHERE g.status = 'final'
          AND sp.id IS NULL
        ORDER BY g.game_date
        """
    ).fetchall()
    return [str(row["game_date"]).strip() for row in rows if row["game_date"]]


def _season_for_date(value: str) -> int:
    return int(str(value).split("-", 1)[0])


def _build_settlement(row: sqlite3.Row, settled_at: str) -> tuple:
    actual = _market_value(row, row["market"])
    line = float(row["line"])
    winning_side = "push"
    if actual > line:
        winning_side = "over"
    elif actual < line:
        winning_side = "under"
    team_margin = None
    game_margin = None
    if row["team_points"] is not None and row["opponent_points"] is not None:
        team_margin = float(row["team_points"] - row["opponent_points"])
        game_margin = abs(team_margin)
    team_spread = None
    if row["spread_home"] is not None:
        spread_home = float(row["spread_home"])
        team_spread = spread_home if int(row["player_team_id"]) == int(row["home_team_id"]) else -spread_home
    team_points = float(row["team_points"]) if row["team_points"] is not None else None
    opponent_points = float(row["opponent_points"]) if row["opponent_points"] is not None else None
    team_possessions = float(row["team_possessions"]) if row["team_possessions"] is not None else None
    opponent_possessions = float(row["opponent_possessions"]) if row["opponent_possessions"] is not None else None
    pace = _average_nullable(team_possessions, opponent_possessions)
    team_off_rating = _rating_per_100(team_points, team_possessions)
    opponent_off_rating = _rating_per_100(opponent_points, opponent_possessions)
    net_rating = (
        round(team_off_rating - opponent_off_rating, 2)
        if team_off_rating is not None and opponent_off_rating is not None
        else None
    )
    blowout_threshold = 15.0
    blowout_result = "unknown"
    if game_margin is not None:
        blowout_result = "yes" if game_margin >= blowout_threshold else "no"
    return (
        row["prop_line_id"],
        actual,
        winning_side,
        actual - line,
        float(row["minutes"] or 0.0),
        game_margin,
        team_margin,
        team_spread,
        blowout_result,
        blowout_threshold,
        team_points,
        opponent_points,
        team_possessions,
        opponent_possessions,
        pace,
        team_off_rating,
        opponent_off_rating,
        net_rating,
        str(row["team_possessions_source"] or "").strip() or None,
        str(row["opponent_possessions_source"] or "").strip() or None,
        settled_at,
    )


def _needs_repair(row: sqlite3.Row) -> bool:
    if row["existing_game_margin"] is None:
        return True
    if row["existing_team_margin"] is None:
        return True
    if row["existing_team_spread"] is None and row["spread_home"] is not None:
        return True
    existing_blowout = str(row["existing_blowout_result"] or "").strip().lower()
    if existing_blowout in {"", "unknown"}:
        return True
    if row["existing_blowout_threshold"] is None:
        return True
    if row["existing_team_possessions"] is None and row["team_possessions"] is not None:
        return True
    if row["existing_opponent_possessions"] is None and row["opponent_possessions"] is not None:
        return True
    if row["existing_pace"] is None and (row["team_possessions"] is not None or row["opponent_possessions"] is not None):
        return True
    if row["existing_team_off_rating"] is None and row["team_possessions"] is not None:
        return True
    if row["existing_opponent_off_rating"] is None and row["opponent_possessions"] is not None:
        return True
    if row["existing_net_rating"] is None and row["team_possessions"] is not None and row["opponent_possessions"] is not None:
        return True
    if row["existing_team_possessions_source"] is None and row["team_possessions_source"] is not None:
        return True
    return row["existing_opponent_possessions_source"] is None and row["opponent_possessions_source"] is not None


def _rating_per_100(points: float | None, possessions: float | None) -> float | None:
    if points is None or possessions is None or possessions <= 0:
        return None
    return round((points / possessions) * 100.0, 2)


def _average_nullable(left: float | None, right: float | None) -> float | None:
    values = [value for value in (left, right) if value is not None]
    if not values:
        return None
    return round(sum(values) / len(values), 2)
