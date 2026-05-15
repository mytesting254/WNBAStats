from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.bootstrap import ensure_teams
from backend.app.db import connect, init_db


TABLES = [
    "settled_game_predictions",
    "game_predictions",
    "settled_props",
    "prop_predictions",
    "prop_lines",
    "injuries",
    "manual_adjustments",
    "player_game_stats",
    "players",
    "sportsbook_prop_lines",
    "team_game_results",
    "games",
    "model_runs",
]


def clear_runtime_database() -> dict[str, int]:
    init_db()
    with connect() as conn:
        conn.execute("PRAGMA foreign_keys = OFF")
        for table in TABLES:
            conn.execute(f"DELETE FROM {table}")
        conn.execute("PRAGMA foreign_keys = ON")
        ensure_teams(conn)
        return {
            table: conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ["teams", *reversed(TABLES)]
        }


if __name__ == "__main__":
    counts = clear_runtime_database()
    print("Cleared runtime database. Tracking starts from the next import/action.")
    for table, count in counts.items():
        print(f"{table}: {count}")
