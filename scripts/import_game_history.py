from pathlib import Path
import argparse

from backend.app.history_import import import_game_history
from backend.app.db import connect


def main() -> None:
    parser = argparse.ArgumentParser(description="Import historical WNBA game results into the local app database.")
    parser.add_argument("csv_path", type=Path, help="Path to the game history CSV file.")
    parser.add_argument("--clear", action="store_true", help="Clear existing games and team history before importing.")
    args = parser.parse_args()

    with connect() as conn:
        result = import_game_history(conn, args.csv_path, clear_existing=args.clear)

    print(f"Imported {result['inserted_games']} games from {result['source_file']}")
    print(f"Inserted {result['inserted_team_game_results']} team game result rows")
    if result['clear_existing']:
        print("Existing games and team history were cleared before import.")


if __name__ == "__main__":
    main()
