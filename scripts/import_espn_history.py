from pathlib import Path
import argparse
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect, init_db
from backend.app.espn_history import import_espn_player_boxscores, import_espn_scoreboard
from backend.app.odds_import import sync_prop_lines_from_sportsbook


def main() -> None:
    parser = argparse.ArgumentParser(description="Import real completed WNBA games from ESPN scoreboard data.")
    parser.add_argument("--seasons", nargs="+", type=int, default=[2025], help="WNBA seasons to import.")
    parser.add_argument("--force-refresh", action="store_true", help="Fetch fresh ESPN data instead of using cache.")
    parser.add_argument("--player-stats", action="store_true", help="Also import ESPN player box scores and rebuild prop predictions.")
    parser.add_argument("--missing-only", action="store_true", help="Only import ESPN player box scores for games missing player stats.")
    args = parser.parse_args()

    init_db()
    totals = {"inserted_games": 0, "inserted_team_game_results": 0}
    with connect() as conn:
        for season in args.seasons:
            result = import_espn_scoreboard(conn, season, force_refresh=args.force_refresh)
            totals["inserted_games"] += result["inserted_games"]
            totals["inserted_team_game_results"] += result["inserted_team_game_results"]
            print(
                f"{season}: imported {result['inserted_games']} games, "
                f"{result['inserted_team_game_results']} team result rows"
            )
            if args.player_stats:
                stats_result = import_espn_player_boxscores(
                    conn,
                    season,
                    force_refresh=args.force_refresh,
                    missing_only=args.missing_only,
                )
                print(
                    f"{season}: imported {stats_result['inserted_player_game_stats']} player game rows, "
                    f"{stats_result['inserted_players']} new players"
                )
        if args.player_stats:
            synced = sync_prop_lines_from_sportsbook(conn)
            print(f"Synced {synced} sportsbook prop lines into model prop lines")
    print(
        f"Total: imported {totals['inserted_games']} games, "
        f"{totals['inserted_team_game_results']} team result rows"
    )


if __name__ == "__main__":
    main()
