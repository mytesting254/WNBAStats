from pathlib import Path
import tempfile
import csv
from datetime import datetime

from backend.app.ball_dont_lie import fetch_team_history
from backend.app.history_import import import_game_history_file


def fetch_and_import_team_history(team: str, seasons: list[int]) -> dict:
    """Fetch team history from BallDontLie and import into database."""
    print(f"Fetching history for {team} seasons {seasons}...")

    # Fetch from BallDontLie
    data = fetch_team_history(team=team, seasons=seasons, force_refresh=True)

    # Convert to CSV format
    games = data["games"]
    if not games:
        print(f"No games found for {team}")
        return {"inserted_games": 0, "inserted_team_game_results": 0}

    # Create temporary CSV
    with tempfile.NamedTemporaryFile(mode='w', suffix='.csv', delete=False, newline='') as f:
        writer = csv.writer(f)
        writer.writerow([
            'game_id', 'game_date', 'start_time', 'home_team', 'away_team',
            'home_points', 'away_points', 'status', 'rest_days_home', 'rest_days_away',
            'spread_home', 'game_total', 'possessions'
        ])

        for game in games:
            home_team = game['home_team']['abbreviation']
            away = game.get('away_team') or game.get('visitor_team')
            away_team = away['abbreviation']
            game_date = game['date'][:10]  # YYYY-MM-DD
            start_time = game['date']  # Full datetime
            status_value = str(game.get('status', '')).lower()
            status = 'final' if status_value in {'final', 'post'} else 'scheduled'
            home_score = game.get('home_team_score', game.get('home_score'))
            away_score = game.get('visitor_team_score', game.get('away_score'))

            writer.writerow([
                game['id'],
                game_date,
                start_time,
                home_team,
                away_team,
                home_score,
                away_score,
                status,
                2,  # default rest days
                2,
                None,  # no spread/total from BallDontLie
                None,
                78.0  # default possessions
            ])

        csv_path = f.name

    try:
        # Import the CSV
        result = import_game_history_file(csv_path, clear_existing=False)
        print(f"Imported {result['inserted_games']} games for {team}")
        return result
    finally:
        Path(csv_path).unlink()  # Clean up temp file


def main():
    teams = ['NY', 'CON', 'LV', 'IND', 'ATL', 'CHI', 'DAL', 'GS', 'LA', 'MIN', 'PHX', 'POR', 'SEA', 'TOR', 'WSH']
    seasons = [2025]  # Current season

    total_games = 0
    total_results = 0

    for team in teams:
        try:
            result = fetch_and_import_team_history(team, seasons)
            total_games += result.get('inserted_games', 0)
            total_results += result.get('inserted_team_game_results', 0)
        except Exception as e:
            print(f"Error importing {team}: {e}")

    print(f"\nTotal imported: {total_games} games, {total_results} team results")


if __name__ == "__main__":
    main()
