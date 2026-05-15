from pathlib import Path
import argparse
import sys
from datetime import date, datetime, timedelta


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
    parser.add_argument("--start-date", help="First game date to import in YYYY-MM-DD format.")
    parser.add_argument("--end-date", help="Last game date to import in YYYY-MM-DD format.")
    parser.add_argument("--skip-scoreboard", action="store_true", help="Import player box scores from existing games without refreshing scoreboard rows.")
    parser.add_argument("--skip-prop-sync", action="store_true", help="Skip syncing sportsbook props and rebuilding predictions after player stat import.")
    parser.add_argument("--max-workers", type=int, default=8, help="Concurrent ESPN summary fetches for player box scores.")
    args = parser.parse_args()

    init_db()
    selected_dates = _date_range(args.start_date, args.end_date)
    totals = {
        "inserted_games": 0,
        "inserted_team_game_results": 0,
        "inserted_player_game_stats": 0,
        "inserted_players": 0,
        "games_checked": 0,
        "skipped_games": 0,
    }
    with connect() as conn:
        for season in args.seasons:
            if selected_dates:
                dates_for_season = [item for item in selected_dates if item.year == season]
                if not dates_for_season:
                    continue
                for index, selected_date in enumerate(dates_for_season, start=1):
                    date_text = selected_date.isoformat()
                    print(f"{season} {date_text} ({index}/{len(dates_for_season)}): starting", flush=True)
                    if not args.skip_scoreboard:
                        result = import_espn_scoreboard(conn, season, force_refresh=args.force_refresh, selected_date=date_text)
                        totals["inserted_games"] += result["inserted_games"]
                        totals["inserted_team_game_results"] += result["inserted_team_game_results"]
                    else:
                        result = {"inserted_games": 0, "inserted_team_game_results": 0}
                    print(
                        f"{season} {date_text} ({index}/{len(dates_for_season)}): "
                        f"{result['inserted_games']} games, {result['inserted_team_game_results']} team result rows",
                        flush=True,
                    )
                    if args.player_stats:
                        stats_result = import_espn_player_boxscores(
                            conn,
                            season,
                            force_refresh=args.force_refresh,
                            missing_only=args.missing_only,
                            selected_date=date_text,
                            max_workers=args.max_workers,
                        )
                        _add_stats_totals(totals, stats_result)
                        print(
                            f"{season} {date_text}: checked {stats_result['games_checked']} games, "
                            f"imported {stats_result['inserted_player_game_stats']} player game rows, "
                            f"{stats_result['inserted_players']} new players, skipped {stats_result['skipped_games']}",
                            flush=True,
                        )
                continue

            if not args.skip_scoreboard:
                result = import_espn_scoreboard(conn, season, force_refresh=args.force_refresh)
                totals["inserted_games"] += result["inserted_games"]
                totals["inserted_team_game_results"] += result["inserted_team_game_results"]
                print(
                    f"{season}: imported {result['inserted_games']} games, "
                    f"{result['inserted_team_game_results']} team result rows",
                    flush=True,
                )
            if args.player_stats:
                stats_result = import_espn_player_boxscores(
                    conn,
                    season,
                    force_refresh=args.force_refresh,
                    missing_only=args.missing_only,
                    max_workers=args.max_workers,
                )
                _add_stats_totals(totals, stats_result)
                print(
                    f"{season}: checked {stats_result['games_checked']} games, "
                    f"imported {stats_result['inserted_player_game_stats']} player game rows, "
                    f"{stats_result['inserted_players']} new players, skipped {stats_result['skipped_games']}",
                    flush=True,
                )
        if args.player_stats and not args.skip_prop_sync:
            synced = sync_prop_lines_from_sportsbook(conn)
            print(f"Synced {synced} sportsbook prop lines into model prop lines")
        elif args.player_stats:
            print("Skipped sportsbook prop sync and prediction rebuild.")
    print(
        f"Total: imported {totals['inserted_games']} games, "
        f"{totals['inserted_team_game_results']} team result rows, "
        f"{totals['inserted_player_game_stats']} player game rows, "
        f"{totals['inserted_players']} new players, checked {totals['games_checked']} games, "
        f"skipped {totals['skipped_games']} games"
    )


def _date_range(start_date: str | None, end_date: str | None) -> list[date]:
    if not start_date and not end_date:
        return []
    if not start_date or not end_date:
        raise SystemExit("--start-date and --end-date must be provided together.")
    start = datetime.strptime(start_date, "%Y-%m-%d").date()
    end = datetime.strptime(end_date, "%Y-%m-%d").date()
    if end < start:
        raise SystemExit("--end-date must be on or after --start-date.")
    days = (end - start).days
    return [start + timedelta(days=offset) for offset in range(days + 1)]


def _add_stats_totals(totals: dict, stats_result: dict) -> None:
    totals["inserted_player_game_stats"] += stats_result["inserted_player_game_stats"]
    totals["inserted_players"] += stats_result["inserted_players"]
    totals["games_checked"] += stats_result["games_checked"]
    totals["skipped_games"] += stats_result["skipped_games"]


if __name__ == "__main__":
    main()
