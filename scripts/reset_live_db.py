from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.bootstrap import ensure_teams
from backend.app.db import connect, init_db
from backend.app.odds_import import import_the_odds_api_props


def reset_live_database() -> dict:
    init_db()
    with connect() as conn:
        conn.execute("DELETE FROM settled_props")
        conn.execute("DELETE FROM prop_predictions")
        conn.execute("DELETE FROM prop_lines")
        conn.execute("DELETE FROM injuries")
        conn.execute("DELETE FROM manual_adjustments")
        conn.execute("DELETE FROM player_game_stats")
        conn.execute("DELETE FROM players")
        conn.execute("DELETE FROM sportsbook_prop_lines")
        conn.execute("DELETE FROM team_game_results")
        conn.execute("DELETE FROM games")
        conn.execute("DELETE FROM model_runs")
        ensure_teams(conn)
        odds_result = import_the_odds_api_props(conn, force_refresh=False)
        counts = {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in [
                "teams",
                "games",
                "team_game_results",
                "players",
                "player_game_stats",
                "prop_lines",
                "prop_predictions",
                "sportsbook_prop_lines",
            ]
        }
    return {"counts": counts, "odds": odds_result}


if __name__ == "__main__":
    result = reset_live_database()
    print("Reset local DB to live/cache-backed data.")
    for table, count in result["counts"].items():
        print(f"{table}: {count}")
    print(f"odds_status: {result['odds'].get('status')}")
    print(f"odds_events: {result['odds'].get('events', 0)}")
    print(f"sportsbook_rows: {result['odds'].get('imported', 0)}")
