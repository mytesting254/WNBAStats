from __future__ import annotations

import sqlite3
from datetime import datetime, timezone

from .projections import _market_value


def settle_completed_props(conn: sqlite3.Connection) -> dict:
    rows = conn.execute(
        """
        SELECT
            pl.id AS prop_line_id,
            pl.market,
            pl.line,
            pgs.*,
            g.home_team_id,
            g.away_team_id,
            g.spread_home,
            p.team_id AS player_team_id,
            tgr.points AS team_points,
            tgr.opponent_points AS opponent_points
        FROM prop_lines pl
        JOIN games g ON g.id = pl.game_id
        JOIN players p ON p.id = pl.player_id
        JOIN player_game_stats pgs ON pgs.player_id = pl.player_id AND pgs.game_id = pl.game_id
        LEFT JOIN team_game_results tgr ON tgr.game_id = pl.game_id AND tgr.team_id = p.team_id
        WHERE g.status = 'final'
          AND NOT EXISTS (
              SELECT 1
              FROM settled_props sp
              WHERE sp.prop_line_id = pl.id
          )
        ORDER BY g.game_date, pl.id
        """
    ).fetchall()

    settled_at = datetime.now(timezone.utc).isoformat()
    settlements = []
    skipped = 0
    for row in rows:
        try:
            actual = _market_value(row, row["market"])
        except (KeyError, IndexError, ValueError):
            skipped += 1
            continue
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
        blowout_threshold = 15.0
        blowout_result = "unknown"
        if game_margin is not None:
            blowout_result = "yes" if game_margin >= blowout_threshold else "no"
        settlements.append(
            (
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
                settled_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO settled_props (
            prop_line_id, actual_result, winning_side, margin,
            player_minutes, game_margin, team_margin, team_spread,
            blowout_result, blowout_threshold, settled_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        settlements,
    )
    conn.commit()
    return {
        "eligible": len(rows),
        "settled": len(settlements),
        "skipped": skipped,
        "settled_at": settled_at,
    }
