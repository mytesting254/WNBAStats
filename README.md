# WNBA Prop Value

Local pregame WNBA prop-value app.

The app is built around a provider-backed pregame workflow:

1. Store historical stats, prop lines, model predictions, and settled results in SQLite.
2. Keep raw/current JSON files as a fast local cache and API audit trail.
3. Import completed games and player box scores before projecting new props.
4. Rank props by expected value and edge.
5. Compare model versions with holdout metrics before trusting a projection change.

## Stack

- React + TypeScript + Vite frontend
- Python + FastAPI backend
- SQLite app database
- JSON/JSONL cache layer
- pytest and Python Playwright tests

## GitHub Codespaces

For Codespaces setup, see [CODESPACES.md](CODESPACES.md).

## First Run

From the repo root:

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r backend\requirements.txt
.\.venv\Scripts\python.exe scripts\init_db.py
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --reload
```

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open:

```text
http://127.0.0.1:5174
```

## API Endpoints

```text
GET  /api/health
GET  /api/value-board
GET  /api/sportsbook-props
GET  /api/odds/cache
GET  /api/line-discrepancies
GET  /api/model-performance
GET  /api/models/runs
GET  /api/matchups
POST /api/odds/import
POST /api/covers/import
POST /api/history/import/espn
POST /api/models/train
POST /api/recalculate
POST /api/settle-props
```

## App Tabs

- `Pregame Props`: ranked prop predictions with projection, line, model probability, edge, EV, and confidence.
- `Matchups`: active upcoming games only, with projected score, spread edge, total edge, and confidence.
- `Parlays`: game-scoped candidate legs and sportsbook line discrepancies. Completed games are removed from this view after the stale-game grace window.
- `Discrepancies`: cross-book line gaps and price gaps.
- `Model Lab`: latest training metrics, market metrics, model comparison, and run history.
- `Data`: operational controls for saved/fresh odds import, completed-game import, projection rebuilds, and reloads.

## Pregame Odds Import

Live sportsbook prop import uses The Odds API from the backend only. Set an API key before starting the API:

```powershell
$env:ODDS_API_KEY="your_key_here"
```

Click `Load Saved Odds` in the app to reload the most recent JSON file from `data/cache/sportsbook_props_raw.json` without calling the provider.

Click `Refresh Odds` only when you want a fresh provider call. Fresh calls merge by provider event id, so future events already saved in `sportsbook_props_raw.json` remain cached instead of being discarded.

To inspect the cache without calling the provider:

```text
GET /api/odds/cache
```

To fetch fresh odds:

```text
POST /api/odds/import?force_refresh=true
```

The default endpoint behavior reuses saved JSON when it exists:

```text
POST /api/odds/import
```

Imported rows are stored in `sportsbook_prop_lines` and exposed through:

```text
GET /api/sportsbook-props
GET /api/line-discrepancies
```

Supported WNBA prop markets:

```text
player_points
player_rebounds
player_assists
player_threes
player_points_rebounds_assists
player_steals
player_blocks
player_blocks_steals
```

## Covers Matchup Import

Covers is used for matchup pages that publish WNBA pregame lines, totals, team records, ATS/O-U records, and player prop tables. The importer reads the Covers matchup pages, stores the raw payload in `data/cache/covers_props_raw.json`, updates matching `games.spread_home` and `games.game_total`, and writes player prop offers into `sportsbook_prop_lines` with provider `covers`.

Refresh from the app or call:

```text
POST /api/covers/import?force_refresh=true
```

For a specific date:

```text
POST /api/covers/import?selected_date=2026-05-10&force_refresh=true
```

Covers supplies pregame market context. ESPN remains the completed-game source for final scores and player box scores.

## Team Logos

Logo thumbnails are served locally from:

```text
frontend/public/team-logos/
```

Refresh them with:

```powershell
.\scripts\download_team_logos.ps1
```

## Historical Game Import

Matchups should be built from real imported games. `scripts\init_db.py` only creates the schema and canonical WNBA teams; it does not seed sample games, player stats, or prop lines.

Seed data has been removed from the app path. `backend.app.seed.seed_sample_data()` now raises intentionally so fake games cannot slip into local projections. Tests use isolated temporary fixture databases instead of the runtime SQLite database.

To clear local runtime data and rebuild scheduled games from the saved sportsbook odds JSON:

```powershell
.\.venv\Scripts\python.exe scripts\reset_live_db.py
```

Real matchup history can be imported from a provider-generated CSV:

Example import command:

```powershell
.\.venv\Scripts\python.exe scripts\import_game_history.py path\to\real_game_history.csv --clear
```

The CSV supports these columns:

- `game_id` (optional)
- `game_date`
- `start_time`
- `home_team`
- `away_team`
- `home_points`
- `away_points`
- `status` (`final` or `scheduled`)
- `rest_days_home`
- `rest_days_away`
- `spread_home`
- `game_total`
- `possessions`

The importer writes rows into `games` and completed results into `team_game_results`.

You can also import completed WNBA games from ESPN scoreboard data:

```powershell
.\.venv\Scripts\python.exe scripts\import_espn_history.py --seasons 2025 --force-refresh
```

Omit `--force-refresh` to reuse `data\cache\espn_wnba_scoreboard_<season>.json`.

The same sync is available from the app through `Refresh Results`, or directly:

```text
POST /api/history/import/espn?season=2026&force_refresh=true&include_player_stats=true&include_previous_season=true
```

Completed games are matched to existing sportsbook-derived scheduled games by date/home/away, then marked `final` so they drop out of the upcoming Matchups tab while still contributing to last-10 history.
When `include_player_stats=true`, ESPN player box scores are imported for the requested season and, by default, the previous season. The app then syncs matching sportsbook prop lines into model prop lines so Parlay Candidates use provider-backed player game logs.

After the ESPN sync finishes, the app settles saved player prop predictions and saved game predictions against the imported final scores and box scores.

## Local Data And Generated Files

The repo ignores local runtime data and generated artifacts:

```text
.env
.venv/
frontend/node_modules/
frontend/dist/
data/*.sqlite
data/cache/
__pycache__/
.pytest_cache/
*.log
```

Keep provider JSON caches locally if you want to avoid repeated API calls. They are intentionally not committed.

## BallDontLie historical matchup API

The app can now fetch historical WNBA game results from BallDontLie.

Example request:

```text
GET /api/ball_dont_lie/history?team=NY&seasons=2025
```

Query parameters:

- `team` (required): team abbreviation or full team name
- `seasons` (optional): comma-separated season years
- `start_date` / `end_date` (optional): date range in `YYYY-MM-DD`
- `force_refresh` (optional): set to `true` to bypass cache and re-fetch from BallDontLie

This endpoint returns BallDontLie game payloads for the requested team.

BallDontLie responses are now cached locally in `data/cache/` for repeated requests, so the same team/date query will return cached results unless `force_refresh=true`.

## Data Model

Important tables:

```text
teams
players
games
player_game_stats
injuries
manual_adjustments
prop_lines
prop_predictions
settled_props
game_predictions
settled_game_predictions
```

Every prop prediction is tied to:

```text
prop_line_id
model_version
prediction_time
captured_at
game_start_time
```

That lets us evaluate only predictions made before tipoff.

Every game prediction is tied to:

```text
game_id
model_version
prediction_time
spread_home
game_total
projected_home_points
projected_away_points
winner_pick
ats_pick
total_pick
```

When ESPN later marks the game final, `settled_game_predictions` records the actual score, actual winner, ATS result, total result, and correctness flags for winner, ATS, and over/under.

## Recommended API Plan

Use separate data providers:

- Stats/history/final scores/player box scores: ESPN
- Pregame matchup lines/totals/records/player props: Covers
- Supplemental pregame player props: The Odds API
- Historical matchup lookup: BALLDONTLIE WNBA

The app should write raw API responses into `data/cache/` or `data/raw/`, then normalize into SQLite. The React UI should read from our FastAPI backend, not directly from external APIs.

## Model Notes

Player prop projections use the `adaptive-context-v1` model. It starts with a transparent component projection, then uses a local ridge regression model trained from actual player game logs. The final pregame projection can also blend in sportsbook line context and no-vig price lean when a line is available.

Core features include:

- Exponentially weighted recent form
- Last 5 average
- Last 10 average
- Per-minute production multiplied by projected minutes
- EWMA minutes
- Minutes trend
- Player consistency and volatility
- Game pace adjustment from team possessions
- Opponent allowance adjustment by market
- Common-opponent adjustment, regressed and capped so small samples cannot dominate
- Home/away adjustment
- Rest-days adjustment
- Blowout risk minutes adjustment using game spread and player rotation role
- Manual usage adjustment
- Sportsbook line
- No-vig market probability from over/under prices

Projection and value are intentionally separate. The model first estimates the stat outcome, then converts sportsbook odds into implied probability, edge, and expected value. More advanced ML models should be compared against this component model before replacing it.

## Model Training

The Model Lab tab trains and records two local benchmarks:

```text
component-pregame-v2
  walk-forward component benchmark using only prior games

adaptive-context-v1
  chronological 80/20 holdout for the learned history/context model
```

Each training action saves both runs to `model_runs` with rows, markets, MAE, RMSE, bias, and directional accuracy. The comparison table shows the latest run for each model version side by side. Once real settled prop lines are imported, the same model-run workflow can be extended to ROI, CLV, and edge calibration.

## Accuracy Analysis

`backend/app/accuracy_analysis.py` compares saved prop predictions against completed player box scores in `player_game_stats`. It only analyzes prop lines whose game has an actual player stat row, so pregame and future games are ignored.

Run the CLI from the repo root:

```powershell
.\.venv\Scripts\python.exe scripts\analyze_accuracy.py report --model adaptive-context-v1
.\.venv\Scripts\python.exe scripts\analyze_accuracy.py best --limit 20 --min-edge 0.04
.\.venv\Scripts\python.exe scripts\analyze_accuracy.py worst --limit 20
```

The report includes MAE, RMSE, bias, directional accuracy, confidence calibration, market breakdowns, minutes buckets, and edge-threshold hit rates.

## Matchup Predictions

The Matchups tab now projects:

```text
winner
projected score
projected margin
projected total
ATS pick and edge
over/under pick and edge
confidence
```

The game model combines recent scoring, longer team scoring, opponent points allowed, pace, home/away, and rest. It compares projected margin to `spread_home` and projected total to `game_total`.

Matchup predictions are saved when `/api/matchups` is built. Recalculation and ESPN history imports call the game settlement flow, so final ESPN scores can be compared against the model's saved winner, ATS, and over/under predictions.
