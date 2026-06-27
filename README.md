# WNBA Prop Value

Pregame WNBA prop-value app with local SQLite runtime by default.

The app is built around a provider-backed pregame workflow:

1. Store historical stats, prop lines, model predictions, and settled results in Turso Cloud.
2. Keep raw/current JSON files as a fast local cache and API audit trail.
3. Import completed games and player box scores before projecting new props.
4. Rank props by expected value and edge.
5. Compare model versions with holdout metrics before trusting a projection change.

## Stack

- React + TypeScript + Vite frontend
- Python + FastAPI backend
- Local SQLite database (Turso optional via `USE_TURSO=true`)
- JSON/JSONL cache layer
- pytest tests

## Key Architecture Points

- `frontend` is the only public surface. Browser traffic should go to nginx first, and nginx proxies `/api/*` to the backend.
- `backend` owns all provider calls, projections, settlement flows, and admin enforcement.
- SQLite is the default runtime store and is expected to live on persistent disk.
- Raw provider pages and derived payloads are cached under `data/cache/` and are used for recovery, replay, and faster reloads.
- The `Data` tab is an operations surface, not a public mutation surface.

## Key Implementations

### Admin Auth

- The app now uses backend session auth for admin actions.
- Admin credentials come from:
  - `ADMIN_USERNAME`
  - `ADMIN_PASSWORD`
- Login endpoints:
  - `GET /api/auth/me`
  - `POST /api/auth/login`
  - `POST /api/auth/logout`
- The browser no longer needs a shared mutation secret for normal admin use.
- `API_KEY` still exists as an optional fallback for server-to-server/manual calls.

### CSRF And Proxy-Aware Admin Posts

- Admin POST routes are protected by session validation plus CSRF/origin checks.
- In reverse-proxy deployments, origin validation must respect forwarded headers.
- This repo now accepts a valid admin session when either of these is true:
  - request origin matches the forwarded public host/proto
  - CSRF token matches the session token
- This avoids false `403` failures behind Coolify/Traefik while still protecting browser-admin mutations.

### SQLite Concurrency

- SQLite now runs in `WAL` mode by default.
- `WAL` helps when one admin write job is running and other users are still browsing read views.
- `WAL` does not make SQLite multi-writer. It still assumes:
  - one backend instance
  - low concurrent admin mutation traffic
- If multiple admins will run imports/recalculate at the same time, queueing or a database upgrade should be considered.

### Matchups And Covers Records

- The matchup payload is assembled in the backend and includes:
  - projection output
  - market overrides
  - team last-10 summaries
  - Covers records
  - prop and sportsbook rows
- H2H data can legitimately be sparse for expansion teams or first-time matchups.
- The frontend now treats H2H states intentionally:
  - `0` meetings: simple note
  - `1` meeting: compact summary
  - `2+` meetings: full table

### Roster Surface

- `Roster` remains a separate read tab.
- Roster data is visible normally.
- `Refresh Roster` is hidden unless the viewer has an active admin session.
- Rotowire and ESPN now share the same backend team alias normalization, so city-only or nickname-only labels such as `Chicago`, `Sky`, `Portland`, or `Fire` resolve to the same internal team codes.
- Roster enrichment also normalizes abbreviated/accented player names before lookup, reducing `N/A` role/position rows for provider spellings like `C. Vandersloot`, `D. Carrington`, `K. Samuelson`, or `Luisa Geiselsoder` / `Luisa Geiselsöder`.

## GitHub Codespaces

For Codespaces setup, see [CODESPACES.md](CODESPACES.md).

## VM Deployment

For Linux VM deployment with the local SQLite runtime at `data/wnba.sqlite`,
see [VM.md](VM.md).

## Coolify Deployment

For a Coolify URL-based deployment using Docker Compose, see [COOLIFY.md](COOLIFY.md).

If this VM also has a local repo checkout, do not assume host `/data/wnba.sqlite`
or repo `data/wnba.sqlite` is the same database the deployed backend is using.
Use `python scripts/live_backend.py host-runtime-info` to see the real mounted
runtime root, or `source scripts/live_env.sh` before any host-side maintenance
command that should target the live deployment.

## Troubleshooting

For common deployment, auth, proxy, SQLite, and provider failure cases, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## First Run

### Docker Compose

From the repo root:

```bash
cp .env.example .env
docker compose up -d
```

Open:

```text
http://127.0.0.1:8080
```

Useful commands:

```bash
docker compose logs -f backend
docker compose logs -f frontend
docker compose down
```

### Manual

```powershell
python -m venv .venv
.\.venv\Scripts\pip.exe install -r backend\requirements.txt
Copy .env.example to .env and set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN.
.\.venv\Scripts\python.exe scripts\init_db.py
.\.venv\Scripts\python.exe -m uvicorn backend.app.main:app --port 8010 --reload
```

In a second terminal:

```powershell
cd frontend
npm install
npm run dev
```

Open:

```text
http://127.0.0.1:5184
```

## API Endpoints

```text
GET  /api/health
GET  /api/value-board
GET  /api/sportsbook-props
GET  /api/odds/cache
GET  /api/line-discrepancies
GET  /api/model-performance
GET  /api/model-diagnostics
GET  /api/model-loss-breakdown
GET  /api/models/runs
GET  /api/matchups
GET  /api/watchlist
GET  /api/watchlist-performance
GET  /api/watchlist/snapshots
GET  /api/gem-performance
GET  /api/gems/snapshots
POST /api/odds/import
POST /api/covers/import
POST /api/injuries/import/rotowire
POST /api/history/import/espn
POST /api/history/import/espn-missing
POST /api/history/recompute-ats
POST /api/history/backfill-covers-lines
POST /api/gems/snapshot
POST /api/gems/sync-settlements
POST /api/watchlist/snapshot
POST /api/watchlist/sync-settlements
POST /api/models/train
POST /api/props/repair-current-slate
POST /api/settle-props
```

## API Security

Mutating endpoints are protected by admin session auth or a shared API key fallback when `API_KEY` is configured.

Preferred browser/admin path:

- sign in through the `Data` tab
- use the admin session cookie
- let the frontend manage CSRF automatically

- Send `X-API-Key: <API_KEY>` (or `Authorization: Bearer <API_KEY>`) for:
  - `POST /api/props/repair-current-slate`
  - `POST /api/settle-props`
  - `POST /api/models/train`
  - `POST /api/odds/import`
  - `POST /api/covers/import`
  - `POST /api/injuries/import/rotowire`
  - `POST /api/history/import/espn`
  - `POST /api/history/import/espn-missing`
  - `POST /api/history/recompute-ats`
  - `POST /api/history/backfill-covers-lines`

`force_refresh=true` on these read endpoints also requires the API key:

- `GET /api/value-board?force_refresh=true`
- `GET /api/line-discrepancies?force_refresh=true`
- `GET /api/matchups?force_refresh=true`

Environment behavior:

- `ENV=dev|local|test` with no `API_KEY`: requests are allowed (dev convenience).
- non-dev `ENV` with no `API_KEY`: API startup fails.

Important:

- Do not expose `VITE_API_KEY` in public deployments unless you explicitly want every browser to hold a shared admin secret.
- If admin login works but protected POST routes return `403`, check proxy origin forwarding before assuming route/auth code is broken.

Set `EXPOSE_DEBUG_HEADERS=true` only when you want cache/timing headers exposed in API responses.

## App Tabs

- `Pregame Props`: ranked prop predictions with projection, line, model probability, edge, EV, and confidence.
- `Gems`: ranked high-value props combining model edge, EV, and discrepancy signals with conservative/balanced/aggressive presets, optional matchup grouping, and per-matchup caps.
- `Watchlist`: low-confidence props that still clear minimum EV/edge thresholds for optional tracking.
- `Matchups`: active upcoming games only, with projected score, spread edge, total edge, and confidence.
- `Parlays`: game-scoped candidate legs and sportsbook line discrepancies. Player rows include an `L5` strip (last 5 market outcomes) with hit/miss color coding against the current side+line. Completed games are removed from this view after the stale-game grace window.
- `Discrepancies`: cross-book line gaps and price gaps.
- `Roster`: Rotowire lineup statuses grouped by team, with a manual `Refresh Roster` pull.
- `Model Lab`: latest training metrics, market metrics, model comparison, and run history. This tab is only shown to authenticated admins.
- `Data`: operational controls for saved/fresh odds import, completed-game import, projection rebuilds, and reloads.
- `Data`: includes a `Live Pipeline` card plus a background prop-sync queue card so long-running ingest jobs show current phase and backend progress instead of only button spinners.
- `Data`: includes `Track Gems Daily` and `Track Watchlist Daily` snapshot controls.

Operational expectations:

- Public viewers should be able to browse read tabs without admin credentials.
- `Data` actions should require an admin session.
- `Roster` display is public/readable, but its refresh action is admin-only.
- `Model Lab` is admin-only. If a non-admin loses auth while on that view, the frontend returns to `Pregame Props`.

`Pregame Props` and matchup `props` now suppress low-confidence picks by default unless `edge >= 0.08`.

Minutes projections are computed from a hybrid path:

- recency-weighted heuristic (EWMA, trend, context, venue adjustment)
- learned minutes model blend

The learned minutes path is role-aware and trains separate models for:

- `core_starter`
- `starter_volatile`
- `rotation`
- `bench`
- `fringe`

Prediction uses the matching role model first and falls back to the global minutes model if a role model is unavailable.

Recent holdout comparison (global-only minutes model vs role-aware minutes model):

- MAE: `5.107 -> 5.033` (`-0.074`)
- RMSE: `6.499 -> 6.393` (`-0.106`)
- Bias: `0.329 -> 0.295` (`-0.034`)

Recent accuracy hardening also includes:

- date-aware teammate contribution cutoffs in injury-adjustment logic (prevents future-game leakage)
- market-specific stabilization bands to curb implausible learned-output drift in noisier markets

## Gems Daily Tracking

Gems are generated from current model picks (`/api/value-board`) plus sportsbook discrepancy signals (`/api/line-discrepancies`) and ranked by a composite gem score.

Preset thresholds:

- `conservative`: `EV >= 0.03`, `|edge| >= 0.08`, low confidence excluded, higher min gem score, capped list.
- `balanced`: `EV >= 0.02`, `|edge| >= 0.05`, low confidence allowed, medium min gem score, capped list.
- `aggressive`: `EV >= 0.01`, `|edge| >= 0.035`, low confidence allowed, lower min gem score, capped list.

Daily snapshot endpoint:

```text
POST /api/gems/snapshot?preset=balanced
POST /api/gems/snapshot?snapshot_date=2026-05-29&preset=conservative
```

Snapshot history:

```text
GET /api/gems/snapshots
GET /api/gems/snapshots?preset=balanced&limit=30
```

Gem settlements are now synced from the same `settled_props` source used by regular prop tracking. This runs automatically when settling props and during ESPN history imports.

Manual settlement sync (if needed):

```text
POST /api/gems/sync-settlements
```

## Watchlist Daily Tracking

Watchlist picks are low-confidence model calls that still meet minimum quality gates and are tracked separately from the main board.

Current watchlist gates:

- `confidence = low`
- `EV >= 0.02`
- `0.05 <= |edge| < LOW_CONFIDENCE_EDGE_MIN` (`LOW_CONFIDENCE_EDGE_MIN` defaults to `0.08`)

Daily snapshot endpoint:

```text
POST /api/watchlist/snapshot
POST /api/watchlist/snapshot?snapshot_date=2026-05-29
```

Snapshot history:

```text
GET /api/watchlist/snapshots
GET /api/watchlist/snapshots?limit=30
```

Watchlist settlements are synced from `settled_props` and run automatically on prop settle and ESPN history imports.

Manual settlement sync (if needed):

```text
POST /api/watchlist/sync-settlements
```

## Pregame Odds Import

Live sportsbook prop import uses The Odds API from the backend only. Set an API key before starting the API:

```powershell
$env:ODDS_API_KEY="your_key_here"
```

Click `Load Saved Odds` in the app to reload the most recent `sportsbook_props_raw.json` file from the active backend runtime cache without calling the provider. In local repo runs that is usually `data/cache/sportsbook_props_raw.json`; in deployed container runs it is usually `/data/cache/sportsbook_props_raw.json`. The import still syncs `sportsbook_prop_lines -> prop_lines` and refreshes model predictions so value-board/watchlist/gem views update immediately.

Click `Refresh Odds` only when you want a fresh provider call. The request now queues immediately in the backend, writes successful raw provider responses into `sportsbook_props_raw.json`, then continues import/sync/publish work in the background. Odds API game markets (`h2h`, `spreads`, `totals`) are saved in that raw payload and update `games` market fields such as spread, total, and moneyline during import. Watch the `Live Pipeline` card in the `Data` tab for request, sync, and publish progress.

Fresh calls merge by provider event id, so future events already saved in `sportsbook_props_raw.json` remain cached instead of being discarded.

The raw Odds API cache is a single rolling file, not a dated archive. `Load Saved Odds` only replays cached events whose game date matches the app's current local date, so yesterday's payload can still exist in the JSON file but will be ignored on today's replay path.

When same-day Covers cache is also present, matchup payloads now treat Covers game markets as fallback-only. Existing `games` spread/total/moneyline values from Odds API stay primary unless those fields are missing.

## Deployment Checklist

Before treating a deployment as production-ready, verify all of the following:

1. `https://<domain>/api/health` returns `200`.
2. `https://<domain>/api/matchups` returns JSON through nginx, not directly from uvicorn.
3. SQLite data survives container restarts and redeploys.
4. Admin login works from the `Data` tab.
5. `https://<domain>/index.html` and `https://<domain>/runtime-config.js` return `Cache-Control: no-store`.
6. The frontend container/image, not a manual file sync, is the source of `/usr/share/nginx/html`.
7. A hard refresh loads the latest UI and the `Recalculate` action calls `POST /api/props/repair-current-slate`.
8. A protected admin POST succeeds after login:
   - `Recalculate`
   - `Refresh ESPN`
   - `Refresh Covers`
9. `ODDS_API_KEY` is set if you expect The Odds API imports to work.
10. Existing snapshots/backups are stored somewhere outside the live volume.

## Troubleshooting Notes

- `401 Unauthorized` on mutation routes usually means:
  - bad/missing `API_KEY` on manual calls
  - or no valid admin session
- `403 Forbidden` after successful admin login usually means:
  - CSRF/origin verification mismatch through the proxy
- blank frontend with working backend often means:
  - bad public `/api` routing
  - stale proxy config
- Covers refresh failures are often scraper/parser issues, not route issues.
- H2H can be legitimately sparse for new matchups; that is not always a data bug.

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
player_points_rebounds
player_points_assists
player_rebounds_assists
player_points_rebounds_assists
player_steals
player_blocks
player_blocks_steals
```

## Covers Matchup Import

Covers is used for matchup pages that publish WNBA pregame lines, totals, team records, ATS/O-U records, and player prop tables. The importer reads the Covers matchup pages, stores the raw payload in `data/cache/covers_props_raw.json`, updates matching `games.spread_home` and `games.game_total`, and writes player prop offers into `sportsbook_prop_lines` with provider `covers`.

Cache behavior safeguards:

- Covers cache is day-scoped by local date. When a new local day starts, stale Covers cache files are purged before import (`covers_props_raw.json` and `covers_pages_raw.json`).
- Saved-cache no-op is date-aware. A cache load skips rewrite only when DB already has Covers rows for `game_date >= cache_date`; historical leftover rows no longer block loading today's cached slate.
- When Covers write/sync hits SQLite lock contention, the API returns a `db_locked` status instead of crashing, so retries are safe.
- Final `prop_lines` sync is Covers-first by player/game/market. If Covers and Odds API both exist for the same player market, Covers rows win and overlapping non-Covers rows are ignored.
- Reingest sync is diff-based. Unchanged scheduled `prop_lines` stay in place, deleted lines clean up their dependent snapshots/predictions, and only inserted or odds-changed rows rebuild projections.
- Projection rebuilds now reuse a shared player/game feature context within the run, which reduces repeated minutes/context recomputation when one player has multiple live markets.

Refresh from the app or call:

```text
POST /api/covers/import?force_refresh=true
```

Loading saved Covers cache (`POST /api/covers/import` without `force_refresh`) also performs immediate prop-line sync so model props load without waiting for a background precompute.

Backfill historical Covers lines over a date range (then recompute ATS/total results from those lines):

```text
POST /api/history/backfill-covers-lines?start_date=2025-05-01&end_date=2025-05-31&force_refresh=true
```

Recompute ATS/total outcomes from currently stored `games.spread_home` and `games.game_total`:

```text
POST /api/history/recompute-ats
```

For a specific date:

```text
POST /api/covers/import?selected_date=2026-05-10&force_refresh=true
```

Covers supplies pregame market context and is the preferred source for player prop lines. When a game has Covers prop rows in `sportsbook_prop_lines`, the model prop-line sync builds `prop_lines` from Covers rows for that game and ignores overlapping The Odds API rows. Other providers are only used as a fallback for games without Covers props. ESPN remains the completed-game source for final scores and player box scores.

Historical Covers matchup pages can differ from current pregame pages. The importer now supports older matchup layouts that expose only visible-text market blocks such as `Betting Information Team ATS (Margin) O/U (Margin)` and can backfill `games.spread_home` / `games.game_total` even when no player prop rows are present for that page. That historical backfill path is useful when early-season or repaired dates have completed box scores and settled props but are still missing matchup market context.

On app load, Covers records shown in matchups are read from `data/cache/covers_props_raw.json` only when `cache_date` matches the current local date. If the cache date is stale, that cache file is deleted and Covers records are not displayed until a fresh Covers import runs.

Covers team abbreviations can differ from the app's canonical team codes. The importer normalizes those provider-only codes before reading game lines, including Phoenix `PHO`, Portland `PDX`, and Washington `WAS`.

Game identity resolution is provider-agnostic. ESPN event ids, Covers matchup ids, and The Odds API events now resolve into a canonical local game key (`home team`, `away team`, and start time window), so provider id mismatches do not create duplicate scheduled games.
Model-ready `prop_lines` are one row per exact `game_id + player_id + market + line`. If multiple sportsbooks publish the same line, the sync keeps one row with the best available over price and best available under price across those books. Distinct lines, such as 12.5 and 13.5, remain separate model rows.

## Daily Matchup Workflow

The Matchups tab reads saved scheduled games from Turso. It does not call ESPN on page load. `/api/matchups` selects rows from `games` where `status = 'scheduled'`, then shows only games whose `start_time` falls on the current local date. If tomorrow's games are already saved, they appear automatically tomorrow when the dashboard reloads.

Scheduled game rows are usually created before tip by `Load Saved Odds`, `Refresh Odds`, or `Refresh Covers`. Those importers match existing games by teams and start time, create missing scheduled games, and attach sportsbook props, spread, and total context.

Use `Load Missing ESPN` as the normal in-season completed-game operation. It updates today's ESPN scoreboard only, imports player box scores for today's final games that are missing stats, settles saved predictions, and syncs matching sportsbook rows into model prop lines.

Use the `Date Results` operation when only one completed date, or a small batch of completed dates, needs to be repaired quickly. It fetches ESPN scoreboard and box score data only for the selected dates, settles saved player and game predictions, syncs model prop lines, and rebuilds current predictions.

Use `Refresh ESPN` only for a larger hard refresh or backfill. That path fetches fresh ESPN data for the current season and previous season. Box score imports are idempotent in Turso: `player_game_stats` is unique by `(player_id, game_id)`, player upserts are batched, and stat inserts use `INSERT OR REPLACE`, so repeated missing-only fills can safely repair gaps without duplicating rows.

Saved prop settlements are also repairable now. `settled_props` no longer behaves as insert-once-only state: rerunning settlement can backfill incomplete historical rows such as missing `game_margin`, `team_margin`, `team_spread`, and `blowout_result`, and it can resolve the player's team from `player_team_history` for that specific game when current roster state differs from historical roster state.

Injury context is refreshed from RotoWire lineups via:

```text
POST /api/injuries/import/rotowire
POST /api/injuries/import/rotowire?force_refresh=true
```

`/api/matchups` also performs a cache-aware RotoWire refresh to keep injury-adjusted projections current without forcing repeated fetches.

The roster API is Rotowire-driven (not ESPN-player-table driven):

```text
GET /api/roster
```

`/api/roster` reads the Rotowire lineup pull/cache rows and returns `team`, `player_name`, `status`, and `captured_at` so team tabs can render current lineup status even when ESPN player IDs are not yet present.

Roster enrichment still attempts to match those provider names back to local player profiles for `position`, `rotation_role`, and impact metrics. Team normalization is shared with ESPN imports, and player matching now tolerates abbreviated first names plus accent/punctuation differences so provider naming drift is less likely to produce `N/A` roster metadata.

Each Rotowire pull also writes a normalized roster snapshot cache:

```text
data/cache/rotowire_roster_snapshot.json
```

The snapshot includes both `captured_at` and `changed_at`, and only advances `changed_at` when the normalized lineup/status rows actually change.

Prop projections now use injury status context:

- Player marked `OUT`/`inactive`/`suspended`/`unavailable`: projection hard-capped to zero.
- `GTD`/`questionable`/`doubtful`/`probable`: availability dampening applied.
- Teammate absences: role-weighted usage and minute bump for active players.

## Model Diagnostics Endpoints

Rolling diagnostics from settled props:

```text
GET /api/model-diagnostics
GET /api/model-diagnostics?model_version=adaptive-context-v1&windows=7,14,30
```

Loss concentration breakdowns from settled props:

```text
GET /api/model-loss-breakdown
GET /api/model-loss-breakdown?model_version=adaptive-context-v1&top_n_players=20
```

## Turso Clean-Slate Tracking

The runtime database starts with only canonical WNBA teams. The `players` table is intentionally empty until ESPN player box scores are imported. Sportsbook odds and Covers imports create games and raw prop offers, but they do not create model-ready players by themselves because player prop predictions require ESPN player IDs and historical player game stats.

Normal clean-slate flow:

```text
Refresh Odds / Refresh Covers before games
Load Missing ESPN after games finish
Current-slate repair
```

Raw provider responses and dashboard snapshots still use local JSON cache files under `data/cache/` so repeated loads are faster and avoid unnecessary provider calls. Turso stores the normalized records that must survive across devices: games, players, player stats, prop lines, predictions, settled results, and model runs.

To check whether all final games have player box scores in Turso:

```bash
.venv/bin/python -c "from backend.app.db import connect; \
with connect() as conn: \
    rows=conn.execute(\"select substr(game_date,1,4) season, count(*) final_games, sum(case when exists (select 1 from player_game_stats s where s.game_id=g.id) then 1 else 0 end) with_stats, sum(case when not exists (select 1 from player_game_stats s where s.game_id=g.id) then 1 else 0 end) missing from games g where status='final' group by substr(game_date,1,4) order by season\").fetchall(); \
print([dict(row) for row in rows])"
```

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

Seed data has been removed from the app path. `backend.app.seed.seed_sample_data()` now raises intentionally so fake games cannot slip into runtime projections. Tests use isolated temporary fixture databases instead of the runtime Turso database.

To clear runtime data and rebuild scheduled games from the saved sportsbook odds JSON:

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

The normal app action, `Load Missing ESPN`, is date-scoped. It defaults to today's local date:

```text
POST /api/history/import/espn
```

For a specific date:

```text
POST /api/history/import/espn?selected_date=2026-05-12&include_player_stats=true&missing_only=true
```

For a small batch of dates, pass repeated `selected_dates` values or a comma-separated value:

```text
POST /api/history/import/espn?selected_dates=2026-05-15&selected_dates=2026-05-16&include_player_stats=true&force_refresh=true
POST /api/history/import/espn?selected_dates=2026-05-15,2026-05-16&include_player_stats=true&force_refresh=true
```

The larger season backfill is available through `Refresh ESPN`, or directly:

```text
POST /api/history/import/espn?season=2026&force_refresh=true&include_player_stats=true&include_previous_season=true
```

Completed games are matched to existing sportsbook-derived scheduled games by date/home/away, then marked `final` so they drop out of the upcoming Matchups tab while still contributing to last-10 history.
When `include_player_stats=true`, ESPN player box scores are imported for the selected date, selected date batch, or requested season. The app then syncs matching sportsbook prop lines into deduped model prop lines so Parlay Candidates use provider-backed player game logs without counting identical sportsbook lines multiple times.

After the ESPN sync finishes, the app settles saved player prop predictions and saved game predictions against the imported final scores and box scores, then rebuilds current predictions from the updated player history.

## Local Data And Generated Files

The runtime database lives in Turso Cloud. The backend requires `TURSO_DATABASE_URL`
and `TURSO_AUTH_TOKEN` for normal app runs, so every device that uses the same
credentials reads and writes the same stats and tracking history. Local SQLite is
not used by default; it is only enabled by tests or one-off commands that set
`USE_LOCAL_DB=true` and `WNBA_DB_PATH`.

To reset Turso to a clean slate with only canonical teams:

```powershell
.\.venv\Scripts\python.exe scripts\clear_runtime_db.py
```

The repo still ignores local generated artifacts:

```text
.env
.venv/
frontend/node_modules/
frontend/dist/
data/cache/
__pycache__/
.pytest_cache/
*.log
```

Keep provider JSON caches locally if you want to avoid repeated API calls. They are intentionally not committed.

## Local SQLite Snapshot Workflow

If you run local-first (`USE_LOCAL_DB=true`) and want cross-device continuity, use snapshots as explicit checkpoints.

One-command startup (restore latest snapshot, then run dev servers):

```bash
./snapshot.sh
```

PowerShell equivalent:

```powershell
.\snapshot.ps1
```

`./snapshot.sh` now does both automatically:

- startup: restores the latest snapshot if available
- shutdown: updates the rolling snapshot after `dev.sh` exits
- retention: the default rolling snapshot is a single file, `wnba-runtime.sqlite`

Create a snapshot:

```bash
./snapshot.sh create
```

Restore a snapshot:

```bash
./snapshot.sh restore wnba-runtime.sqlite
```

List snapshots:

```bash
./snapshot.sh list
```

Latest snapshot path:

```bash
./snapshot.sh latest
```

What the scripts do:

- `snapshot.sh`: single command wrapper around create/restore/list/latest operations.
- `snapshot.sh` with no args (or `auto`/`start`): if `data/wnba.sqlite` is missing, restore the latest snapshot, then run `dev.sh`. If the runtime DB already exists, startup skips restore so newer local data is not rolled back.
- `snapshot.ps1` with no args (or `auto`/`start`): if `data/wnba.sqlite` is missing, restore the latest snapshot, then run `dev.ps1`. If the runtime DB already exists, startup skips restore so newer local data is not rolled back.
- `snapshot_create.py`: checkpoints WAL, copies the SQLite file into a rolling snapshot file (`wnba-runtime.sqlite` by default), writes a JSON manifest with `created_at`, `schema_version`, `app_commit_sha`, `row_counts`, `source_device`, and `sha256`, then prunes older snapshots if you also keep named legacy files around.
- `snapshot_restore.py`: validates checksum + schema version, creates a timestamped backup of the current DB, removes stale `-wal/-shm`, then atomically replaces the DB file.
- `dev.sh` and `dev.ps1` default snapshot/dev startup to local SQLite by exporting `USE_TURSO=0` and `WNBA_DB_PATH=data/wnba.sqlite` unless you override them explicitly.

Network access notes:

- `dev.sh` defaults to `127.0.0.1` outside Codespaces and `0.0.0.0` inside Codespaces. Override with `BACKEND_HOST` or `FRONTEND_HOST` if needed.
- `dev.ps1` binds backend/frontend to `0.0.0.0` for same-network or Codespaces access.
- Default ports are backend `8010` and frontend `5184`.
- `dev.sh` and `dev.ps1` auto-select the next open ports when defaults are busy and print the resolved URLs.
- `snapshot.sh` and `snapshot.ps1` (`auto`/`start`) inherit the same dynamic port behavior because they delegate app startup to the matching dev script.
- Access from the host machine still works via `127.0.0.1:<resolved-port>`.

Safety:

- Restore fails on schema mismatch unless `--force` is set.
- Restore fails on checksum mismatch.
- No auto-merge is performed between snapshots.

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

The app should write raw API responses into `data/cache/` or `data/raw/`, then normalize into Turso. The React UI should read from our FastAPI backend, not directly from external APIs.

## Model Notes

Player prop projections use the `adaptive-context-v1` model. It starts with a transparent component projection, then uses an in-process ridge regression model trained from actual player game logs in the active runtime database. In normal app runs that database is Turso; local SQLite training is only used by tests or explicit commands that set `WNBA_DB_PATH`.

The live player-prop path now uses three historical layers when a sportsbook line is available:

- a raw stat projection model trained on prior player game logs
- a settled-history market-relative residual model trained on `actual_result - line`
- a settled-history market calibration pass for recommendation probabilities

The final pregame projection can blend raw projection, sportsbook line context, residual model output, and no-vig price lean when a line is available.

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

Probability conversion and uncertainty controls:

- Market-specific sigma floors to avoid overconfident tails on low-variance histories
- Dynamic sigma multipliers based on sample depth, average minutes, minutes volatility, and spread-based blowout risk
- Post-projection probability calibration by market (`points`, `rebounds`, `threes`, etc.) using settled prop outcomes
- Bin-based empirical calibration with Bayesian shrinkage to market-level hit rate so thin bins do not overfit
- Clamped calibrated probabilities before EV/edge ranking to reduce systematic overestimation

Projection and value are intentionally separate. The model first estimates the stat outcome, then converts sportsbook odds into implied probability, edge, and expected value. More advanced ML models should be compared against this component model before replacing it.

## Model Training

The admin-only Model Lab tab trains against the active Turso database and records two benchmarks:

```text
component-pregame-v2
  walk-forward component benchmark using only prior games

adaptive-context-v1
  chronological 80/20 holdout for the learned history/context model
```

Each training action saves both runs to Turso in `model_runs` with rows, markets, MAE, RMSE, bias, and directional accuracy. Learned-run payloads now also include game residual evaluation rows (`game_ats`, `game_total`, and `game_overall`) with both baseline and blended metrics so saved game predictions can be compared before and after the residual layer. The Model Lab now shows those game residual deltas directly alongside the existing player-market training tables. The comparison table shows the latest run for each model version side by side. Prediction-time learned models also train from the active connection when the app is using Turso, instead of reopening a local SQLite file.

Training auth expectations:

- If `Train` returns `401`, there is no valid admin session. Sign in again from the `Data` tab.
- If `Train` returns `403`, the admin session exists but session verification failed. Sign out, sign back in, and retry from the same public domain so the session cookie, CSRF token, and forwarded origin stay aligned.

The player model now also supports a separate market-relative residual fit by market. That residual model is used only when a real sportsbook line exists, because its target is line-relative (`actual_result - line`) rather than raw stat outcome. Training/reporting metrics should therefore be read as two related layers:

- raw stat projection quality
- market-relative settled-line quality

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

Game projections now also include a first-pass settled-history residual layer:

- ATS residual model: learns historical cover-margin outcome (`actual_margin + spread_home`) from saved game predictions.
- Total residual model: learns historical total-vs-market outcome (`actual_total - game_total`) from saved game predictions.
- Live output blends those learned residual edges back into the heuristic base projection with a low weight, so the market-relative history can correct recurring margin/total bias without discarding the existing team-form model.

Matchup predictions are saved when `/api/matchups` is built. Recalculation and ESPN history imports call the game settlement flow, so final ESPN scores can be compared against the model's saved winner, ATS, and over/under predictions.
