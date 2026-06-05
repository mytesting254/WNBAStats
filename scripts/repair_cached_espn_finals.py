from __future__ import annotations

from collections import defaultdict
from datetime import datetime
from pathlib import Path
import json
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect, init_db
from backend.app.espn_history import import_espn_player_boxscores


def main() -> None:
    init_db()
    with connect() as conn:
        stale_games = conn.execute(
            """
            SELECT id, game_date
            FROM games
            WHERE status = 'scheduled'
              AND game_date <= ?
            ORDER BY game_date, id
            """,
            (datetime.now().date().isoformat(),),
        ).fetchall()

        promoted = 0
        promoted_dates: set[str] = set()
        for game in stale_games:
            game_id = int(game["id"])
            cache_path = ROOT_DIR / "data" / "cache" / f"espn_wnba_summary_{game_id}.json"
            if not cache_path.exists():
                continue
            try:
                payload = json.loads(cache_path.read_text(encoding="utf-8"))
            except json.JSONDecodeError:
                continue
            competition = ((payload.get("header") or {}).get("competitions") or [{}])[0]
            status_type = (competition.get("status") or {}).get("type") or {}
            is_final = bool(status_type.get("completed")) or str(status_type.get("name") or "").upper() in {"STATUS_FINAL", "FINAL"}
            if not is_final:
                continue
            competitors = competition.get("competitors") or []
            home = next((item for item in competitors if item.get("homeAway") == "home"), None)
            away = next((item for item in competitors if item.get("homeAway") == "away"), None)
            home_score = _parse_score(home)
            away_score = _parse_score(away)
            total = float(home_score + away_score) if home_score is not None and away_score is not None else None
            conn.execute(
                """
                UPDATE games
                SET status = 'final',
                    game_total = COALESCE(?, game_total)
                WHERE id = ?
                """,
                (total, game_id),
            )
            promoted += 1
            promoted_dates.add(str(game["game_date"]))

        imported_stats = 0
        imported_players = 0
        games_checked = 0
        skipped_games = 0
        for date_text in sorted(promoted_dates):
            result = import_espn_player_boxscores(
                conn,
                season=int(date_text[:4]),
                force_refresh=False,
                missing_only=True,
                selected_date=date_text,
            )
            imported_stats += int(result["inserted_player_game_stats"])
            imported_players += int(result["inserted_players"])
            games_checked += int(result["games_checked"])
            skipped_games += int(result["skipped_games"])

        updated_players = _repair_player_teams_from_recent_games(conn)
        conn.commit()

    print(f"promoted_games={promoted}")
    print(f"promoted_dates={sorted(promoted_dates)}")
    print(f"games_checked={games_checked}")
    print(f"imported_player_game_stats={imported_stats}")
    print(f"imported_players={imported_players}")
    print(f"skipped_games={skipped_games}")
    print(f"updated_players={updated_players}")


def _parse_score(competitor: dict | None) -> int | None:
    if not competitor:
        return None
    try:
        return int(competitor.get("score"))
    except (TypeError, ValueError):
        return None


def _repair_player_teams_from_recent_games(conn) -> int:
    rows = conn.execute(
        """
        WITH recent_games AS (
            SELECT
                s.player_id,
                g.id AS game_id,
                g.game_date,
                g.home_team_id,
                g.away_team_id,
                ROW_NUMBER() OVER (PARTITION BY s.player_id ORDER BY g.game_date DESC, g.id DESC) AS rn
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
        """
    ).fetchall()

    updates = 0
    for row in rows:
        cursor = conn.execute(
            "UPDATE players SET team_id = ? WHERE id = ? AND COALESCE(team_id, -1) <> ?",
            (int(row["team_id"]), int(row["player_id"]), int(row["team_id"])),
        )
        updates += int(cursor.rowcount or 0)
    return updates


if __name__ == "__main__":
    main()
