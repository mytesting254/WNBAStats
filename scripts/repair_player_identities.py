from __future__ import annotations

from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect, init_db


def main() -> None:
    init_db()
    with connect() as conn:
        before = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM players p
            JOIN (
                WITH recent_games AS (
                    SELECT
                        s.player_id,
                        g.home_team_id,
                        g.away_team_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY s.player_id
                            ORDER BY g.game_date DESC, g.id DESC
                        ) AS rn
                    FROM player_game_stats s
                    JOIN games g ON g.id = s.game_id
                ),
                recent_window AS (
                    SELECT *
                    FROM recent_games
                    WHERE rn <= 8
                ),
                side_counts AS (
                    SELECT player_id, home_team_id AS team_id, COUNT(*) AS appearances
                    FROM recent_window
                    GROUP BY player_id, home_team_id
                    UNION ALL
                    SELECT player_id, away_team_id AS team_id, COUNT(*) AS appearances
                    FROM recent_window
                    GROUP BY player_id, away_team_id
                ),
                collapsed AS (
                    SELECT player_id, team_id, SUM(appearances) AS appearances
                    FROM side_counts
                    GROUP BY player_id, team_id
                ),
                ranked AS (
                    SELECT
                        player_id,
                        team_id,
                        appearances,
                        ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY appearances DESC, team_id DESC) AS rn,
                        LEAD(appearances) OVER (PARTITION BY player_id ORDER BY appearances DESC, team_id DESC) AS next_appearances
                    FROM collapsed
                )
                SELECT player_id, team_id
                FROM ranked
                WHERE rn = 1
                  AND appearances >= 3
                  AND appearances > COALESCE(next_appearances, 0)
            ) resolved ON resolved.player_id = p.id
            WHERE COALESCE(p.team_id, -1) <> COALESCE(resolved.team_id, -1)
            """
        ).fetchone()["c"]

        conn.execute(
            """
            WITH recent_games AS (
                SELECT
                    s.player_id,
                    g.home_team_id,
                    g.away_team_id,
                    ROW_NUMBER() OVER (
                        PARTITION BY s.player_id
                        ORDER BY g.game_date DESC, g.id DESC
                    ) AS rn
                FROM player_game_stats s
                JOIN games g ON g.id = s.game_id
            ),
            recent_window AS (
                SELECT *
                FROM recent_games
                WHERE rn <= 8
            ),
            side_counts AS (
                SELECT player_id, home_team_id AS team_id, COUNT(*) AS appearances
                FROM recent_window
                GROUP BY player_id, home_team_id
                UNION ALL
                SELECT player_id, away_team_id AS team_id, COUNT(*) AS appearances
                FROM recent_window
                GROUP BY player_id, away_team_id
            ),
            collapsed AS (
                SELECT player_id, team_id, SUM(appearances) AS appearances
                FROM side_counts
                GROUP BY player_id, team_id
            ),
            ranked AS (
                SELECT
                    player_id,
                    team_id,
                    appearances,
                    ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY appearances DESC, team_id DESC) AS rn,
                    LEAD(appearances) OVER (PARTITION BY player_id ORDER BY appearances DESC, team_id DESC) AS next_appearances
                FROM collapsed
            ),
            resolved AS (
                SELECT player_id, team_id
                FROM ranked
                WHERE rn = 1
                  AND appearances >= 3
                  AND appearances > COALESCE(next_appearances, 0)
            )
            UPDATE players
            SET team_id = (
                SELECT resolved.team_id
                FROM resolved
                WHERE resolved.player_id = players.id
            )
            WHERE id IN (SELECT player_id FROM resolved)
            """
        )

        after = conn.execute(
            """
            SELECT COUNT(*) AS c
            FROM players p
            JOIN (
                WITH recent_games AS (
                    SELECT
                        s.player_id,
                        g.home_team_id,
                        g.away_team_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY s.player_id
                            ORDER BY g.game_date DESC, g.id DESC
                        ) AS rn
                    FROM player_game_stats s
                    JOIN games g ON g.id = s.game_id
                ),
                recent_window AS (
                    SELECT *
                    FROM recent_games
                    WHERE rn <= 8
                ),
                side_counts AS (
                    SELECT player_id, home_team_id AS team_id, COUNT(*) AS appearances
                    FROM recent_window
                    GROUP BY player_id, home_team_id
                    UNION ALL
                    SELECT player_id, away_team_id AS team_id, COUNT(*) AS appearances
                    FROM recent_window
                    GROUP BY player_id, away_team_id
                ),
                collapsed AS (
                    SELECT player_id, team_id, SUM(appearances) AS appearances
                    FROM side_counts
                    GROUP BY player_id, team_id
                ),
                ranked AS (
                    SELECT
                        player_id,
                        team_id,
                        appearances,
                        ROW_NUMBER() OVER (PARTITION BY player_id ORDER BY appearances DESC, team_id DESC) AS rn,
                        LEAD(appearances) OVER (PARTITION BY player_id ORDER BY appearances DESC, team_id DESC) AS next_appearances
                    FROM collapsed
                )
                SELECT player_id, team_id
                FROM ranked
                WHERE rn = 1
                  AND appearances >= 3
                  AND appearances > COALESCE(next_appearances, 0)
            ) resolved ON resolved.player_id = p.id
            WHERE COALESCE(p.team_id, -1) <> COALESCE(resolved.team_id, -1)
            """
        ).fetchone()["c"]
        conn.commit()

    print(f"mismatch_before={int(before or 0)}")
    print(f"mismatch_after={int(after or 0)}")
    print(f"updated_players={int((before or 0) - (after or 0))}")


if __name__ == "__main__":
    main()
