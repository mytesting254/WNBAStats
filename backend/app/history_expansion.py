from __future__ import annotations

import sqlite3
from datetime import date, datetime, timedelta

from .espn_history import import_espn_player_boxscores, import_espn_scoreboard
from .settlement import settle_completed_props
from .timezone_utils import APP_TIMEZONE


def audit_settled_prop_history_gaps(
    conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit_missing_dates: int = 120,
) -> dict:
    today = datetime.now(APP_TIMEZONE).date()
    end = _parse_iso_date(end_date) if end_date else today
    start = _parse_iso_date(start_date) if start_date else max(end - timedelta(days=180), date(end.year - 1, 1, 1))
    if end < start:
        raise ValueError("end_date must be on or after start_date")

    coverage_rows = conn.execute(
        """
        SELECT
            g.game_date,
            COUNT(DISTINCT pl.id) AS final_prop_lines,
            COUNT(DISTINCT sp.id) AS settled_props,
            COUNT(DISTINCT pl.id) - COUNT(DISTINCT sp.id) AS missing_settlements,
            SUM(CASE WHEN EXISTS (SELECT 1 FROM player_game_stats pgs WHERE pgs.game_id = g.id) THEN 1 ELSE 0 END) AS games_with_stats
        FROM games g
        JOIN prop_lines pl ON pl.game_id = g.id
        LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
        WHERE g.status = 'final'
          AND g.game_date >= ?
          AND g.game_date <= ?
        GROUP BY g.game_date
        ORDER BY g.game_date DESC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    missing_date_rows = conn.execute(
        """
        SELECT
            g.game_date,
            COUNT(DISTINCT pl.id) AS final_prop_lines,
            COUNT(DISTINCT sp.id) AS settled_props,
            COUNT(DISTINCT pl.id) - COUNT(DISTINCT sp.id) AS missing_settlements
        FROM games g
        JOIN prop_lines pl ON pl.game_id = g.id
        LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
        WHERE g.status = 'final'
          AND g.game_date >= ?
          AND g.game_date <= ?
        GROUP BY g.game_date
        HAVING COUNT(DISTINCT pl.id) > COUNT(DISTINCT sp.id)
        ORDER BY g.game_date DESC
        LIMIT ?
        """,
        (start.isoformat(), end.isoformat(), int(limit_missing_dates)),
    ).fetchall()

    latest_final_row = conn.execute(
        """
        SELECT MAX(g.game_date) AS latest_final_prop_date
        FROM games g
        JOIN prop_lines pl ON pl.game_id = g.id
        WHERE g.status = 'final'
          AND g.game_date <= ?
        """,
        (today.isoformat(),),
    ).fetchone()
    latest_settled_row = conn.execute(
        """
        SELECT MAX(g.game_date) AS latest_settled_prop_date
        FROM settled_props sp
        JOIN prop_lines pl ON pl.id = sp.prop_line_id
        JOIN games g ON g.id = pl.game_id
        WHERE g.game_date <= ?
        """,
        (today.isoformat(),),
    ).fetchone()
    latest_final_prop_date = latest_final_row["latest_final_prop_date"] if latest_final_row else None
    latest_settled_prop_date = latest_settled_row["latest_settled_prop_date"] if latest_settled_row else None
    lag_days = None
    if latest_final_prop_date and latest_settled_prop_date:
        lag_days = (
            datetime.strptime(latest_final_prop_date, "%Y-%m-%d").date()
            - datetime.strptime(latest_settled_prop_date, "%Y-%m-%d").date()
        ).days

    missing_dates = [str(row["game_date"]) for row in missing_date_rows]
    strict_ok = len(missing_dates) == 0 and (lag_days is None or lag_days <= 0)
    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "latest_final_prop_date": latest_final_prop_date,
        "latest_settled_prop_date": latest_settled_prop_date,
        "settled_prop_lag_days": lag_days,
        "missing_dates": missing_dates,
        "missing_dates_count": len(missing_dates),
        "coverage_by_date": [dict(row) for row in coverage_rows],
        "missing_date_rows": [dict(row) for row in missing_date_rows],
        "strict_ok": strict_ok,
    }


def expand_settled_prop_history(
    conn: sqlite3.Connection,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    force_refresh: bool = True,
    max_dates: int | None = None,
    dry_run: bool = False,
) -> dict:
    before = audit_settled_prop_history_gaps(conn, start_date=start_date, end_date=end_date)
    missing_dates = list(before["missing_dates"])
    if max_dates is not None:
        missing_dates = missing_dates[: max(0, int(max_dates))]
    if dry_run or not missing_dates:
        return {
            "before": before,
            "attempted_dates": missing_dates,
            "attempted_dates_count": len(missing_dates),
            "scoreboards": [],
            "player_stats": [],
            "settlements": None,
            "after": before,
            "dry_run": dry_run,
        }

    scoreboards = []
    player_stats = []
    for date_text in missing_dates:
        season = _season_for_date(date_text)
        scoreboards.append(
            import_espn_scoreboard(
                conn,
                season,
                force_refresh=force_refresh,
                selected_date=date_text,
            )
        )
        player_stats.append(
            import_espn_player_boxscores(
                conn,
                season,
                force_refresh=force_refresh,
                missing_only=True,
                selected_date=date_text,
            )
        )
    settlements = settle_completed_props(conn, selected_dates=missing_dates)
    after = audit_settled_prop_history_gaps(conn, start_date=start_date, end_date=end_date)
    return {
        "before": before,
        "attempted_dates": missing_dates,
        "attempted_dates_count": len(missing_dates),
        "scoreboards": scoreboards,
        "player_stats": player_stats,
        "settlements": settlements,
        "after": after,
        "dry_run": False,
    }


def _parse_iso_date(value: str | None) -> date:
    if not value:
        raise ValueError("date value is required")
    return datetime.strptime(value, "%Y-%m-%d").date()


def _season_for_date(value: str) -> int:
    return _parse_iso_date(value).year
