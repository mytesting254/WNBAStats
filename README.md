# WNBA Prop Value

Pregame WNBA prop-value app with one authoritative runtime database.

The app is built around a provider-backed pregame workflow:

1. Store historical stats, prop lines, model predictions, and settled results in Turso Cloud.
2. Keep raw/current JSON files as a fast local cache and API audit trail.
3. Import completed games and player box scores before projecting new props.
4. Rank props by expected value and edge.
5. Compare model versions with holdout metrics before trusting a projection change.

## Stack

- React + TypeScript + Vite frontend
- Python + FastAPI backend
- SQLite runtime database via explicit `WNBA_DB_PATH` or Turso via `USE_TURSO=true`
- JSON/JSONL cache layer
- pytest tests

## Key Architecture Points

- `frontend` is the only public surface. Browser traffic should go to nginx first, and nginx proxies `/api/*` to the backend.
- `backend` owns all provider calls, projections, settlement flows, and admin enforcement.
- SQLite runtime is supported only when `WNBA_DB_PATH` is explicitly set to the authoritative mounted/runtime DB.
- Repo-local `./data/wnba.sqlite` is intentionally rejected as an app runtime path.
- Raw provider pages and derived payloads are cached under `data/cache/` and are used for recovery, replay, and faster reloads.
- The `Data` tab is an operations surface, not a public mutation surface.

## Key Implementations

### Admin Auth

- The app now uses backend session auth for admin actions.
- Admin credentials come from:
  - `ADMIN_USERNAME`
  - `ADMIN_PASSWORD`
- Admin session cookies now follow the effective request scheme:
  - HTTPS requests get a `Secure` cookie
  - HTTP requests get a non-`Secure` cookie so browser sessions persist across refreshes
  - `SESSION_COOKIE_SECURE=true|false` can override this when a deployment needs explicit control
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
  - segment projection output for `Q1` total and `1H` total
  - market overrides
  - team last-10 summaries
  - Covers records
  - prop and sportsbook rows
- H2H data can legitimately be sparse for expansion teams or first-time matchups.
- The frontend now treats H2H states intentionally:
  - `0` meetings: simple note
  - `1` meeting: compact summary
  - `2+` meetings: full table

### Segment Projections

- Segment actuals are stored in `game_segment_results`.
- ESPN summary imports and possession backfills are responsible for populating:
  - `home_q1_points`
  - `away_q1_points`
  - `home_1h_points`
  - `away_1h_points`
- Segment training rows are curated into a separate SQLite file:
  - default path: `data/wnba-segment-training.sqlite`
  - override: `WNBA_SEGMENT_TRAINING_DB_PATH`
- The current live matchup UI exposes:
  - `projected_q1_total`
  - `projected_first_half_total`
- Saved game prediction tracking now also persists:
  - `game_predictions.projected_q1_total`
  - `game_predictions.projected_first_half_total`
- Settled game prediction tracking now also persists:
  - `settled_game_predictions.actual_q1_total`
  - `settled_game_predictions.actual_first_half_total`
  - `settled_game_predictions.q1_total_correct`
  - `settled_game_predictions.first_half_total_correct`
- The current model selection is target-specific:
  - `Q1` uses an opening-focused feature set plus a market anchor blend
  - `1H` uses the narrower pre-expansion core feature set
- Current mounted benchmark winner as of July 17, 2026:
  - `Q1`: `ridge_q1_anchor`
  - `1H`: `ridge_raw`
- If those fields show `N/A`, check these in order:
  - `game_segment_results` has rows in the live runtime DB
  - the live segment training DB has non-zero `included_rows`
  - matchup caches were republished after the segment DB rebuild

### Segment Runtime Scaling

- Segment runtime features must stay on the same scale as segment training.
- The current segment model is trained on first-half context, not full-game context:
  - team scoring context uses historical `1H` points and `1H` allowed
  - possessions are halved from full-game possessions
  - pace references are computed on first-half scale
- If runtime code accidentally feeds full-game team scoring into the segment model, `Q1` and `1H` totals can inflate to impossible values. Treat that as a feature-scale bug, not a model-quality issue.

### Segment Feature Split

- `Q1` and `1H` no longer share the exact same runtime/training feature subset.
- `Q1` keeps the opening-specific additions:
  - home/away `Q1` splits
  - `Q1` offense-vs-defense interaction deltas
  - `Q1` volatility
  - `Q1` fast-start rate
- `1H` currently excludes those `Q1`-specific additions because they improved `Q1` but degraded `1H` holdout performance.

### DFS First-Half Props

- Current DFS first-half estimates are built from full-game prop lines plus curated first-half history.
- Historical first-half player actuals now persist in:
  - `espn_play_by_play_events`
  - `player_first_half_stats`
- ESPN summary/play-by-play imports populate observed first-half stats including:
  - first-half points, rebounds, assists, threes, steals, blocks, turnovers
  - estimated first-half minutes from substitution events when available
- The curated first-half training DB lives beside the runtime DB:
  - default path: `data/wnba-player-half-training.sqlite`
  - override: `WNBA_PLAYER_HALF_TRAINING_DB_PATH`
- DFS first-half model training now fits only rows with observed halftime player stats.
  - fallback share-derived halftime estimates remain in the curated DB for analysis, but are excluded from model fitting
- DFS first-half estimates currently expose:
  - `GET /api/dfs/first-half`
  - `GET /api/player-first-half-history`
  - `GET /api/player-first-half-lines`
- Live DFS first-half tracking now snapshots the current estimate per `prop_line_id` and settles it later against `player_first_half_stats`.
- Snapshot rows live in `dfs_first_half_projection_snapshots` and settled outcomes live in `dfs_first_half_projection_settlements`.
- The DFS UI depends on current `prop_lines`. Without current prop ingestion, the DFS first-half tab has no live slate to estimate against.
- DFS first-half estimates are generated from current full-game `prop_lines` through `prop_predictions`; Covers does not supply a separate DFS slate.
- Because DFS snapshots key off `prop_line_id`, any scheduled prop-line replacement must invalidate the old DFS snapshot chain before deleting the old line. Odds-only updates can reuse the same `prop_line_id`.
- For current slates, the fastest recovery path is:
  1. load the saved odds cache into the live runtime
  2. sync sportsbook rows into `prop_lines`
  3. rebuild/publish live prediction payloads
- If the live VM already has saved raw odds under the active runtime cache, use the mounted runtime context instead of repo-local `data/cache`.

### Live Cached Prop Imports

- Saved raw prop caches are runtime-scoped, not repo-scoped.
- On deployed Docker/Coolify setups, do not assume repo-local `data/cache` is the live cache used by the running backend.
- Resolve the active runtime first:
  - `python scripts/live_backend.py runtime-info`
  - `python scripts/live_backend.py host-runtime-info`
- The live odds import path can load saved raw cache without a provider refresh:
  - `POST /api/odds/import` with `force_refresh=false`
- That path:
  - loads saved Odds API JSON from the active runtime cache
  - refreshes `sportsbook_prop_lines`
  - syncs changed rows into `prop_lines`
  - rebuilds current predictions
  - republishes current read payloads
- If live data is present in the DB but new routes return `404`, treat that as a deploy/runtime mismatch. The mounted volume may be current while the served backend image is still old.

### Segment Tracking And Backfill

- Segment projections are tracked only when `game_predictions` rows are written.
- For live slates, that means:
  - deploy the code
  - rebuild current scheduled game predictions
  - let normal final-score settlement populate `settled_game_predictions`
- Backfilling completed games has two modes:
  - approximate retrospective backfill: easy, but can leak present-day context into old games
  - chronological replay backfill: preferred for honest historical tracking
- A trustworthy historical replay should:
  - iterate games in date order
  - use only pre-tipoff data available before each game
  - avoid present-day injury leakage
  - save prediction rows first, then settle against actual `Q1` / `1H` results
- Do not treat a naive retrospective projection pass over completed games as clean model-evaluation history.

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

Three DB locations can exist on the same VM:

- repo-local `data/wnba.sqlite`: local dev or copied snapshot data for this checkout
- host `/data/wnba.sqlite`: an arbitrary host path that may belong to some older setup and may not be mounted into the active backend container
- live runtime DB: whatever `python scripts/live_backend.py runtime-info` reports as `/data/wnba.sqlite` inside the active backend container, backed by the host path from `host-runtime-info`

When checking production freshness, use one of these only:

```bash
python scripts/live_backend.py runtime-info
python scripts/live_backend.py host-runtime-info
python scripts/live_backend.py doctor
python scripts/live_backend.py exec -- python -c "import sqlite3; conn=sqlite3.connect('/data/wnba.sqlite'); print(conn.execute(\"SELECT MAX(game_date) FROM games WHERE status='final'\").fetchone()[0])"
```

To confirm today’s live prop sync against the active runtime DB:

```bash
python scripts/live_backend.py exec -- python -c "import sqlite3; conn=sqlite3.connect('/data/wnba.sqlite'); print(conn.execute(\"SELECT COUNT(*) FROM prop_lines pl JOIN games g ON g.id = pl.game_id WHERE g.game_date = '2026-08-07'\").fetchone()[0])"
python scripts/live_backend.py exec -- python -c "import sqlite3; conn=sqlite3.connect('/data/wnba.sqlite'); print(conn.execute(\"SELECT COUNT(*) FROM prop_predictions pp JOIN prop_lines pl ON pl.id = pp.prop_line_id JOIN games g ON g.id = pl.game_id WHERE g.game_date = '2026-08-07'\").fetchone()[0])"
```

`python scripts/live_backend.py doctor` is the fastest sanity check when a VM
contains multiple WNBA SQLite files. It resolves the active backend container,
prints the live mounted-volume DB summary, and compares it against repo-local
`data/wnba.sqlite` and host `/data/wnba.sqlite`.

When a live prop rewrite may flip current sides, capture an explicit before/after
snapshot instead of relying on ad hoc SQL after rows are deleted:

```bash
python scripts/live_backend.py exec -- python scripts/audit_current_prop_predictions.py snapshot --output /tmp/prop-before.json --market points_rebounds_assists --market rebounds --market threes
python scripts/live_backend.py exec -- python scripts/audit_current_prop_predictions.py snapshot --output /tmp/prop-after.json --market points_rebounds_assists --market rebounds --market threes
python scripts/live_backend.py exec -- python scripts/audit_current_prop_predictions.py diff /tmp/prop-before.json /tmp/prop-after.json
```

That preserves exact `prop_line_id`, side, edge, and timestamp changes for the
target markets even when the rewrite path deletes old `prop_predictions` rows.

Docker Compose deployments now pin the persistent volume name to
`wnbastats-data` by default. The repo checkout is code-only; runtime SQLite,
cache, and snapshot state should live on the attached app volume, not under a
repo-local `data/` path or symlink.

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
POST /api/history/backfill-odds-lines
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
  - `POST /api/history/backfill-odds-lines`

`force_refresh=true` on these read endpoints also requires the API key:

- `GET /api/value-board?force_refresh=true`
- `GET /api/line-discrepancies?force_refresh=true`
- `GET /api/matchups?force_refresh=true`

Environment behavior:

- `ENV=dev|local|test` with no `API_KEY`: requests are allowed (dev convenience).
- non-dev `ENV` with no `API_KEY`: API startup fails.

Important:

- Do not expose `VITE_API_KEY` in public deployments unless you explicitly want every browser to hold a shared admin secret.
- `runtime-config.js` should only ever emit `VITE_API_KEY`, never fall back to backend `API_KEY`.
- If admin login works but protected POST routes return `403`, check proxy origin forwarding before assuming route/auth code is broken.

Set `EXPOSE_DEBUG_HEADERS=true` only when you want cache/timing headers exposed in API responses.

## App Tabs

- `Pregame Props`: ranked prop predictions with projection, line, model probability, edge, EV, confidence, and recent opponent history.
- `Gems`: ranked high-value props combining model edge, EV, and discrepancy signals with conservative/balanced/aggressive presets, optional matchup grouping, per-matchup caps, and recent opponent history.
- `Watchlist`: low-confidence props that still clear minimum EV/edge thresholds for optional tracking. Player rows include recent opponent history, and if one matchup drops out after settlement, the view automatically falls forward to the next remaining game tab instead of staying pinned to the removed game.
- `Matchups`: active upcoming games only, with projected score, spread edge, total edge, and confidence.
- `Parlays`: game-scoped candidate legs and sportsbook line discrepancies. Player rows include an `L5` strip (last 5 market outcomes) and a `Line H2H` strip (up to 5 prior market outcomes against the current opponent). Both use hit/miss color coding against the current side+line; the H2H strip also shows minutes from those same games. Completed games are removed from this view after the stale-game grace window.
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

Modeled prop payloads expose `h2h_opponent`, `h2h_values`, and `h2h_minutes`. The UI displays them in a `Line H2H` column between Player and Market/Pick across Pregame Props, Gems, Watchlist, and matchup/parlay tables. Head-to-head history follows the player across team changes while excluding games in which the player represented the current opponent; rows without prior meetings display `No H2H`.

The `Special Props` stocks table also exposes a dedicated `H2H STK` column. It
shows up to five prior `blocks_steals` results against the current opponent
plus the minutes from those same games, using the same player/opponent logic as
the main prop H2H payloads.

`/api/value-board` and `/api/watchlist` use their dedicated short-lived read-through caches and bypass the generic app-response cache. This prevents an older whole-response payload from masking newly published prop fields such as H2H history after a deployment.

Any table with a dedicated `Best` column uses the same fixed-width logo slot so sportsbook marks stay aligned across `Pregame Props`, `Gems`, `Watchlist`, `Parlays`, and discrepancy tables.

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

Recent minutes-model work also added:

- lineup-role context (`recent_team_minute_share`, `recent_minute_rank`, `recent_position_minute_share`)
- same-position opportunity and competition context
- opportunity persistence / trend and returner-pressure features
- curated minutes training data cleanup for injury-exit lows, overtime-like spikes, and extreme one-off role collapses
- stage-level minutes diagnostics with trend / volatility / context slices plus top-loss row exports
- narrow late-stage caps for unsupported upside, including `soft_vacancy` `starter_volatile` rise cases

Recent measured live diagnostics for the minutes path (`adaptive-context-v10-market-gated-context`, evaluation start `2026-07-01`, last checked `2026-07-16`):

- overall MAE: `4.330`
- `recent_blend` MAE: `4.305`
- heuristic MAE: `4.614`
- recent-transfer MAE: `3.156` vs `recent_blend` `3.490`
- `core_starter` MAE: `3.141` vs `recent_blend` `3.063`
- `starter_volatile` MAE: `4.622` vs `recent_blend` `4.640`
- `rotation` MAE: `4.965` vs `recent_blend` `4.945`
- `soft_vacancy` MAE: `4.371` vs `recent_blend` `4.278`
- low-volatility slice: `4.201` vs `recent_blend` `4.058`
- stable-trend slice: `4.242` vs `recent_blend` `4.134`

The minutes layer is materially better than the old heuristic and useful as a production context layer, and the latest refinement pass closed much of the original `starter_volatile` and `rotation` gap while reducing the `soft_vacancy` shortfall. It still trails the simple `recent_blend` baseline overall, with the main remaining regressions concentrated in low-volatility / stable-context slices and a small residual `rotation` gap. Treat it as an actively refined context system, not a finished standalone edge source.

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

When a game is settled and removed from the active slate, the Watchlist tab now reselects the next available matchup automatically so later games on the same slate stay visible without a manual reload.

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

`Refresh Odds`, `Refresh Covers`, `Recalculate`, and roster-triggered current-slate repairs all publish their work into the same background prop-sync status card. Those jobs now report monotonic stage progress: provider/cache load, prop sync, projection rebuild, game rebuild or settlement, optional Covers context refresh, then payload publish. Within one stage the `current/total` values may advance in chunks, but they should not jump backward because one substep reused another substep's totals.

The `Live Pipeline` card now also prefers the persisted `prop_sync_jobs` record over stale in-memory job state during background-thread handoff or container restart windows. That avoids a false "stuck at 17%" view where the UI kept showing `requesting_provider` even after the durable job row had already advanced into prediction rebuild or payload publish.

The frontend now also registers `Recalculate` in that same card immediately and keeps following backend progress for `Refresh Covers` / `Refresh Odds` even if those imports finish while another prop-sync job is already running. Before that fix, the backend could keep working while the card appeared silent because the action did not own the active queue slot.

Fresh calls merge by provider event id, so future events already saved in `sportsbook_props_raw.json` remain cached instead of being discarded.

The raw Odds API cache is a single rolling file, not a dated archive. `Load Saved Odds` only replays cached events whose game date matches the app's current local date, so yesterday's payload can still exist in the JSON file but will be ignored on today's replay path.

When same-day Covers cache is also present, matchup payloads now treat Covers game markets as fallback-only. Existing `games` spread/total/moneyline values from Odds API stay primary unless those fields are missing.

## Deployment Checklist

Before treating a deployment as production-ready, verify all of the following:

1. `https://<domain>/api/health` returns `200`.
2. `https://<domain>/api/matchups` returns JSON through nginx, not directly from uvicorn.
3. SQLite data survives container restarts and redeploys.
4. Admin login works from the `Data` tab.
   - If login appears to work but refresh immediately signs the admin out, verify whether the public app is really HTTPS and whether `SESSION_COOKIE_SECURE` is forcing the wrong cookie mode.
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
- `2am America/New_York` cron logs can show `Internal Server Error` even when cron itself ran:
  - on Thursday, July 16, 2026, the actual failure was a bad `settled_props` insert shape during `settle_completed_props`, not a missing scheduled slate and not a skipped cron trigger
- `3am America/New_York` Odds API refresh now prechecks The Odds API `events` endpoint instead of local `/api/matchups` rows:
  - the precheck is quota-free and counts only WNBA events whose `commence_time` lands on the current date in `America/New_York`
  - if the provider reports zero same-day WNBA events, the cron wrapper skips the live props refresh
  - this avoids the old circular dependency where local scheduled games might not exist yet because the Odds API import itself is one of the flows that creates them
- if the matchup card colors a strong defensive rank as bad, verify the frontend build includes the defensive-rank tone fix:
  - defensive ranks use the same ordinal direction as other ranks, so low numbers are good; for example `#3` season defense should render as positive, not negative
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
- A successful refresh can still return `status=imported_game_markets_only` when Covers publishes matchup/game markets but the player-prop parser extracts zero rows. In that state `covers_pages_raw.json` should still be updated for the current local day, while `covers_props_raw.json` will remain absent until at least one player-prop row is parsed.
- Since Tuesday, July 28, 2026, the parser supports both the older Covers `data-linkcont` player-prop anchors and the newer over/under table plus compare-odds modal markup that uses sportsbook logo alt text instead of exposing the old link metadata.
- Saved-cache no-op is date-aware. A cache load skips rewrite only when DB already has Covers rows for `game_date >= cache_date`; historical leftover rows no longer block loading today's cached slate.
- When Covers write/sync hits SQLite lock contention, the API returns a `db_locked` status instead of crashing, so retries are safe.
- Final `prop_lines` sync is Covers-first by player/game/market. If Covers and Odds API both exist for the same player market, Covers rows win and overlapping non-Covers rows are ignored.
- Reingest sync is diff-based and identity-aware. Scheduled `prop_lines` are keyed by exact `game_id + player_id + market + line`.
- Unchanged scheduled rows stay in place.
- Odds-only changes update the existing row so downstream records keep the same `prop_line_id`.
- Line changes are treated as replacements. The old scheduled `prop_line` is invalidated everywhere it is referenced, then deleted, and the new line is inserted as a new `prop_line_id`.
- Replacement cleanup removes dependent `prop_predictions`, value-board/watchlist/gem snapshot items, and DFS first-half projection snapshots/settlements before deleting the obsolete scheduled line, which prevents foreign-key failures during Covers sync.
- Only inserted rows and rows with changed odds rebuild projections.
- Projection rebuilds now reuse a shared player/game feature context within the run, which reduces repeated minutes/context recomputation when one player has multiple live markets.

Refresh from the app or call:

```text
POST /api/covers/import?force_refresh=true
```

Loading saved Covers cache (`POST /api/covers/import` without `force_refresh`) also performs immediate prop-line sync so model props load without waiting for a background precompute.

The intended Covers refresh pipeline is:

1. Refresh or load cached Covers matchup/player-prop payloads into `sportsbook_prop_lines`.
2. Diff scheduled `prop_lines` against the normalized Covers-preferred slate.
3. Keep unchanged rows, update odds-only changes in place, and replace exact-line changes only after invalidating every dependent record tied to the obsolete `prop_line_id`.
4. Insert new rows and rebuild projections only for inserted or changed lines.
5. Republish current-slate payloads so matchup, value-board, watchlist, gem, and DFS views all point at the new active `prop_line_id` set.

If a live refresh returns matchup context only, inspect the runtime `covers_pages_raw.json` first. That file preserves the fetched matchup page, odds page, and market fragments even when no player-prop rows are written, which makes parser drift easier to diagnose than relying on the API response alone.

Backfill historical Covers lines over a date range (then recompute ATS/total results from those lines):

```text
POST /api/history/backfill-covers-lines?start_date=2025-05-01&end_date=2025-05-31&force_refresh=true
```

Backfill historical game markets from The Odds API over a date range. This path writes daily raw payload snapshots under `data/cache/` and updates `games.spread_home`, `games.game_total`, moneylines, and market prices before recomputing ATS/total results:

```text
POST /api/history/backfill-odds-lines?start_date=2024-05-01&end_date=2025-10-31&force_refresh=true
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

Historical game-market backfill now has two paths:

- Covers backfill is useful when archived matchup pages still expose visible pregame spread/total context.
- The Odds API backfill is the preferred bulk repair path for completed game markets because it can backfill spreads, totals, moneylines, and market prices in one pass and keep a dated raw cache for replay/audit.

For spread/total-only repairs that should not fetch player-prop pages or consume Odds API credits, use the focused Covers matchup-page backfill:

```bash
PYTHONPATH=. .venv/bin/python scripts/backfill_covers_game_lines.py \
  --db-path /data/wnba.sqlite \
  --start-date 2024-05-01 \
  --end-date 2024-10-31 \
  --workers 6 \
  --all-final-dates
```

Without `--all-final-dates`, the script repairs only missing final games that already have model prop lines. With the flag, it also repairs games that contribute player/minutes training rows but have no historical prop offer. It fetches only Covers matchup pages, updates canonical `games` market fields, and does not create sportsbook prop rows. Recompute team ATS results and rerun prop settlement after a live repair so `team_game_results` and `settled_props.team_spread` remain aligned.

The live market-context repair completed on `2026-08-02` without paid API calls:

- all regular-season 2024 and 2025 final games now have spread and total context
- all games with historical prop lines have spreads
- all `4,748` settled 2024 props and all `6,811` settled 2025 props have `team_spread`
- the only deliberately unresolved rows are 10 2024 preseason games, two 2025 preseason games, and the 2025 All-Star game
- pre-repair and intermediate safety snapshots were written before mutating the mounted runtime database

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

Important ESPN failure mode: scoreboard and summary imports depend on the request headers ESPN currently accepts. On Sunday, August 9, 2026, the production `User-Agent: WNBAStats/1.0` fingerprint started returning `403 Forbidden` for both scoreboard and summary endpoints even though generic clients such as `python-requests/2.31.0` still succeeded. When that happens, completed games can remain stuck as `games.status = 'scheduled'`, box scores never import because the box-score pass only scans `final` games, prop settlement finds `0` eligible rows, and the DFS first-half tab can still show those stale scheduled games if unresolved prop lines remain attached.

Saved prop settlements are also repairable now. `settled_props` no longer behaves as insert-once-only state: rerunning settlement can backfill incomplete historical rows such as missing `game_margin`, `team_margin`, `team_spread`, and `blowout_result`, and it can resolve the player's team from `player_team_history` for that specific game when current roster state differs from historical roster state.

Injury context is refreshed from RotoWire lineups via:

```text
POST /api/injuries/import/rotowire
POST /api/injuries/import/rotowire?force_refresh=true
```

`/api/matchups` also performs a cache-aware RotoWire refresh to keep injury-adjusted projections current without forcing repeated fetches.
The `Refresh Roster` UI action uses the same RotoWire injury import path, so it updates the `injuries` table before reloading roster status.
Keep that action lightweight: it refreshes injury context, republishes the roster payload immediately, and invalidates matchup cache for lazy rebuild on next read. The heavier projection rebuild still belongs to `Recalculate`.
`POST /api/props/repair-current-slate` now rebuilds both scheduled player prop predictions and saved game predictions for the active slate, so the normal operator flow is `Refresh Roster` followed by `Recalculate`.

The roster API is Rotowire-driven (not ESPN-player-table driven):

```text
GET /api/roster
```

`/api/roster` reads the Rotowire lineup pull/cache rows and returns `team`, `player_name`, `status`, and `captured_at` so team tabs can render current lineup status even when ESPN player IDs are not yet present.

Current player/team assignments are synchronized separately from ESPN by the
overnight cron flow:

```text
POST /api/roster/import/espn
```

That protected mutation queries each WNBA team once, updates
`players.team_id`, records changed assignments as `espn_roster_current`
observations in `player_team_history`, and writes
`data/cache/espn_wnba_rosters_current.json`. Normal `GET /api/roster` requests
remain cache/database reads and never call ESPN. The 2am ET
`settle-and-train` cron flow invokes this roster sync immediately after
importing completed results, ensuring the current roster assignment wins over
the player's last completed box score and is available before settlement and
training.

If a manual ESPN roster sync changes assignments after the current slate has
already been projected, rebuild the slate once after the sync:

```text
POST /api/props/repair-current-slate
```

Do not delete or settle those predictions manually. The repair replaces the
scheduled projections while preserving their sportsbook lines. Model retraining
is not required for a roster-only change. The normal 3am ET odds refresh also
rebuilds the current slate when same-day WNBA events are available.

The roster sync is resilient to provider outages: a complete ESPN failure
leaves the last successful database assignments and snapshot in place, and the
overnight settlement/training flow continues. A partial response updates the
successful teams and retains cached snapshot rows for failed teams.

Roster enrichment still attempts to match those provider names back to local player profiles for `position`, `rotation_role`, and impact metrics. Team normalization is shared with ESPN imports, and player matching now tolerates abbreviated first names plus accent/punctuation differences so provider naming drift is less likely to produce `N/A` roster metadata.

Important failure mode: if the live Rotowire fetch fails, the importer falls back to the last saved raw lineup cache when one exists. That keeps the roster endpoint non-fatal, but it also means the roster tab can show stale provider data even when the API read cache itself is fresh. The roster UI now warns when the latest Rotowire timestamp predates today or when today's matchup teams are missing from the roster feed.

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
GET /api/model-diagnostics?model_version=adaptive-context-v3-team-transition&windows=7,14,30
```

Loss concentration breakdowns from settled props:

```text
GET /api/model-loss-breakdown
GET /api/model-loss-breakdown?model_version=adaptive-context-v3-team-transition&top_n_players=20
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

## Sportsbook Logos

Sportsbook logo assets are served locally from:

```text
frontend/public/sportsbook-logos/
```

Current UI mappings use normalized filenames and extensions:

- `bet365.jpg`
- `betmgm.jpg`
- `betrivers.jpg`
- `caesars.png`
- `draftkings.png`
- `fanatics.jpg`
- `fanduel.jpg`
- `thescore-bet.jpg`

Guidelines:

- Prefer transparent logo assets when possible. Full promo tiles with baked-in colored backgrounds will render as boxes in the UI.
- Keep filenames aligned with the frontend sportsbook map in `frontend/src/App.tsx`.
- `Best` columns use a fixed-width centered logo slot, so replacements should preserve a reasonably tight crop around the mark.

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
Raw ESPN team box-score facts also persist into `team_game_boxscores`, while
`team_game_results` remains the derived team/game context layer used by ratings,
matchups, settlement, and model features.

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

After the ESPN sync finishes, the app settles saved player prop predictions and saved game predictions against the imported final scores and box scores, then rebuilds current predictions from the updated player history. Player-prop settlement now hydrates ESPN team stats for the affected final dates before writing `settled_props`, so pace, offensive-rating, defensive-rating, and net-rating context stays aligned with the same canonical team tables used by training. The settlement writer now derives its placeholder count from a shared `settled_props` column tuple and validates each settlement row width before writing. That safeguard was added after the Thursday, July 16, 2026 overnight cron run failed with SQLite `22 values for 21 columns` during `settle_completed_props`.

If an ESPN refresh reports scoreboard `403` errors for a completed date, do not expect rerunning settlement alone to fix that date. First restore a working ESPN fetch path and rerun the history import so the affected games are marked `final` and receive `team_game_results`, `player_game_stats`, and `player_first_half_stats`; only after that will prop and DFS settlements become eligible.

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

## Live App Volume Snapshot Workflow

For deployed app recovery, keep snapshot files on the attached app volume under `/data/snapshots`, not in Git and not in a repo-local `data/` tree.

Inspect the live runtime first:

```bash
python scripts/live_backend.py runtime-info
python scripts/live_backend.py host-runtime-info
python scripts/live_backend.py doctor
```

Create a live snapshot on the app volume:

```bash
python scripts/live_backend.py exec -- python scripts/snapshot_create.py --name wnba-runtime
```

Recommended scheduled backup wrapper:

```bash
scripts/live_snapshot_backup.sh
```

This creates:

- one rolling snapshot named `wnba-runtime.sqlite`
- one timestamped daily snapshot like `wnba-20260704T120000Z.sqlite`

Restore the live DB from a snapshot already stored on the app volume:

```bash
python scripts/live_backend.py exec -- python scripts/snapshot_restore.py /data/snapshots/wnba-runtime.sqlite --force
```

If you need host-side access to the same live files instead of running inside the container, load:

```bash
source scripts/live_env.sh
```

That resolves `WNBA_DB_PATH`, `WNBA_CACHE_DIR`, and `WNBA_SNAPSHOT_DIR` to the active app-attached volume before you run maintenance commands.
Curated training rebuilds follow the same pathing, so `wnba-training.sqlite` is written beside the active `WNBA_DB_PATH` on the mounted volume unless you override `WNBA_TRAINING_DB_PATH` explicitly.
The admin Data Operations screen now also exposes these resolved runtime paths so the mounted-volume source of truth is visible without shell access.

Do not substitute host `/data/wnba.sqlite` or repo `data/wnba.sqlite` into these commands unless `host-runtime-info` explicitly points there. The resolved mounted-volume path is the only production source of truth.

## Game Totals Note

The `component-game-v2` total-pick path had a train/infer mismatch in the
`total_market` decision layer until Thursday, July 30, 2026. Training used the
pre-decision `baseline_total - game_total` edge, while live inference had been
feeding the post-adjustment `adjusted_total - game_total` edge. That mismatch
materially degraded settled O/U accuracy, especially on `Under` calls.

The runtime fix keeps the decision layer aligned with training by sending the
same pre-decision edge definition through both paths. If game-total accuracy
regresses again, check this before retuning residual weights or pace features.

For automated production backups, prefer a host scheduler instead of an app-process watcher. Example systemd units are provided at:

- `deploy/wnba-live-snapshot.service`
- `deploy/wnba-live-snapshot.timer`

For `Special` stocks prep, use the host-cron wrapper instead of app-local
timers so prep runs against the same live mounted runtime as the backend:

```cron
0 * * * * cd /root/WNBAStats && [ "$(TZ=America/New_York date +\%H)" = 23 ] && WNBA_USE_LIVE_CONTAINER=true scripts/live_stocks_prep.sh tomorrow >> /var/log/wnba-stocks-prep.log 2>&1
```

That prepares tomorrow's slate at `11pm America/New_York`. A copy/paste
template is available at `deploy/wnba-stocks-prep.cron`, and the wrapper also
supports `today`, `today-and-tomorrow`, and explicit `dates` modes for manual
or follow-up refreshes.

The current Specials prep path reads scheduled games from canonical
`wnba.sqlite`, copies the selected slate into `stocks_tracking.sqlite`, and
then runs a fast component-based stocks estimator over the copied slate. The
current live prep estimator uses a last-`10`-game recent window, up to `20`
games for stabilization, and blends same-home/away plus scheduled-game
`rest_days` context. Scheduled prep now also persists richer player-side
Specials inputs in `player_prep_features`, including projected minutes,
minute volatility, injury status/usage deltas, and opportunity context, so
future calibration work can use them without re-querying the live runtime.
Those fields are intentionally prep-only for now: the live Specials scoring
path still uses the simpler validated projection blend after the August 6,
2026 backtest showed that a direct multiplier on those fields improved recent
candidate hit rate but regressed full-history probability calibration.
Snapshot writes now also persist the key prep and matchup inputs directly onto
`projection_snapshots` so settled audits do not depend on mutable cache tables
such as `candidate_players`, `player_prep_features`, or `team_prep_context`.
Historical rows can be backfilled with `scripts/backfill_stocks_snapshot_context.py`
when older snapshots predate the durable fields.
Mounted-runtime validation on `2026-07-13` completed an ET-today run in about
`1m 1s` for `3` prepared games, `79` candidates, and `79` fresh snapshots.

The manual `Generate` action on the `Special Props` tab now follows the same
near-term prep window as the scheduled prep helpers: by default it prepares
today plus tomorrow only. It no longer snapshots every future scheduled game
when run without explicit date or game filters.

The `Special Props` tab now exposes calibration tables for both `2+ Stocks Prob`
and `3+ Stocks Prob`. Each table shows settled count, hits, average predicted
probability, and realized hit rate by probability bucket so the UI can compare
how the 2+ and 3+ models are tracking separately.
When support is too thin for a threshold fit, the board now falls back to more
conservative `High`/`Watch` defaults of `55%` and `45%` instead of the original
`50%` / `40%`.

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

Read payloads keep both identifiers:

- `id`: the `prop_predictions.id` row id for the current model output
- `prop_line_id`: the underlying `prop_lines.id` sportsbook line key

Matchup, value-board, watchlist, and gem payloads should preserve `prop_line_id`
exactly so snapshots, settlement joins, and follow-up rebuilds point back to the
correct source line.

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

The app should write raw API responses into `data/cache/` or `data/raw/`, then normalize into Turso. The React UI should read from our FastAPI backend, not directly from external APIs.

## Model Notes

Player prop projections still default live to the transparent `component-pregame-v2` baseline. The current learned candidate is `adaptive-context-v11-ratings-context`, which starts from the same component projection and then applies an in-process ridge regression model trained from actual player game logs plus ratings-aware team context in the active runtime database.

Model invalidation now keys off aggregate runtime-table signatures instead of only row counts and `MAX(id)`. In-place ESPN possessions repairs, availability repairs, and team-boxscore updates therefore force clean derived-training rebuilds instead of silently reusing stale `wnba-training.sqlite` content.

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

Current live status checked on `2026-07-12`:

- the active runtime volume retained `574` cached historical Odds API event payloads under `/var/lib/docker/volumes/pqsez6mkr14y0bnlhmcdmikg_wnba-data/_data/cache`
- a safety copy of those payloads now also exists at `/root/WNBA_HISTORICAL_PLAYER_PROPS_CACHE`
- the recovered historical payloads cover the regular `8` player markets: `points`, `rebounds`, `assists`, `threes`, `points_rebounds`, `points_assists`, `rebounds_assists`, and `points_rebounds_assists`
- those payloads do not include sportsbook historical `steals`, `blocks`, or `blocks_steals`; the `Special` tab remains in-house projection only

## Model Training

The standard setup for any new curated training dataset is documented in [TRAINING_SETUP.md](TRAINING_SETUP.md). Use that pattern for future trainable models so raw runtime data, curated examples, and derived training assets all stay on the mounted volume.

Model/training doc map:

- [FIXING_NEEDED.md](FIXING_NEEDED.md)
  - consolidated pending model work
  - active-now versus later backlog
- [TRAINING_SETUP.md](TRAINING_SETUP.md)
  - required pattern for new trainable model families
  - mounted-volume persistence, curated DBs, and signatures
- [PLAYER_PROP_CURATION.md](PLAYER_PROP_CURATION.md)
  - player-prop curated training design and remaining cleanup
- [STOCKS_PLAN.md](STOCKS_PLAN.md)
  - Specials/stocks prep, calibration, and performance work
- [TROUBLESHOOTING.md](TROUBLESHOOTING.md)
  - operational/runtime failure cases

The admin-only Model Lab tab trains against the active runtime database and records two benchmarks:

```text
component-pregame-v2
  walk-forward component benchmark using only prior games

adaptive-context-v11-ratings-context
  walk-forward benchmark for the learned ratings-aware history/context model
```

Each training action saves both runs to `model_runs` with rows, markets, MAE, RMSE, bias, and directional accuracy. Learned-run payloads now also include game residual evaluation rows (`game_ats`, `game_total`, and `game_overall`) with both baseline and blended metrics so saved game predictions can be compared before and after the residual layer. The Model Lab now shows those game residual deltas directly alongside the existing player-market training tables. The comparison table shows the latest run for each model version side by side.

Learned player, minutes, residual, and game-model training defaults to a rolling previous-season window: January 1 of the prior Eastern calendar year through the latest ingested history. For example, 2026 runs train on rows dated `2025-01-01` or later. Set `WNBA_TRAINING_START_DATE=YYYY-MM-DD` for a one-off override. The resolved training start date is part of the model cache key and `model_runs` data signature, so changing the window cannot reuse stale cached models or stale Model Lab results.

In-place game-market repairs also participate in player-model invalidation. The model fingerprint includes populated-row counts and aggregate values for both `games.spread_home` and `games.game_total`, so a spread/total repair rebuilds derived training examples instead of silently reusing examples generated while that context was missing.

Historical-window audit completed on `2026-08-02` after repairing regular-season 2024-2025 market context. Two isolated, freshly derived runs used the same repaired production snapshot and compared their common 2026 holdout:

| Layer | 2025-2026 window | 2024-2026 window | Expanded-window change |
| --- | ---: | ---: | ---: |
| Raw MAE | `3.234` | `3.238` | `+0.004` worse |
| Raw directional accuracy | `56.3%` | `56.0%` | `-0.3 pp` |
| Line-aware final MAE | `3.950` | `3.970` | `+0.020` worse |
| Line-aware final directional accuracy | `55.8%` | `55.0%` | `-0.8 pp` |
| Market-residual MAE | `3.917` | `3.872` | `-0.045` better |
| Market-residual directional accuracy | `52.7%` | `54.8%` | `+2.1 pp` |

The repaired default run evaluated `81,059` rows; the expanded run evaluated `126,665`. The wider window clearly helped the residual layer, but it slightly reduced the quality of the actual final line-aware output. Production therefore remains on the rolling 2025-2026 window. Do not buy/import 2023 prop history solely to widen the global window until a residual-only or recency-weighted experiment demonstrates an improvement in final 2026 holdout metrics.

PowerShell local/server workflow:

```powershell
.\scripts\run_sync_retrain_upload.ps1
```

That command resolves the live Coolify volume path, downloads the active SQLite DB to `data\wnba.sqlite`, runs local training validation with two workers, rewrites the learned model cache filenames for the live DB path marker, and uploads the processed cache JSON files into the live cache directory. This warms production with matching player/minutes/residual caches when the live DB fingerprint still matches the synced copy.

WNBA still stores Model Lab run history in the database, not in artifact files. Uploading processed cache files warms production projections, but it does not add local validation rows to live `model_runs`. Pass `-RunServerTraining` when you also want the live backend to record a server-side training run.

Runtime behavior:

- normal live prop rebuilds now prewarm learned player/minutes/residual models before writing predictions
- if uploaded cache files do not match the active live DB fingerprint, the backend retrains from the active runtime instead of silently downgrading prop predictions to `component`
- full live training runs also prewarm current minutes, market, and residual artifacts into `/data/cache/model_artifacts`
- full live training still runs the entire Model Lab path; recent game-model feature expansion made that path heavier, so game evaluation now refits direct game models once per `YYYY-MM` segment instead of once per historical row
- `adaptive-context-v11-ratings-context` remains the current learned candidate version after deploy/restart, with uploaded cache files acting as a warm start rather than a hard dependency
- weak settled player-prop markets currently stay on the component baseline even when the learned walk-forward gate passes:
  - `points_rebounds`
  - `points_assists`
  - `rebounds_assists`
  - `threes`
- thin-margin player-prop under recommendations are gated more aggressively in weak combo/scoring markets before the side is written live

Latest live mounted-volume run checked on `2026-07-16`:

- `status`: `completed`
- `model_version`: `adaptive-context-v11-ratings-context`
- `run_type`: `walk_forward_segments`
- `training_rows`: `26,730`
- `started_at`: `2026-07-16T02:46:39.383767+00:00`
- `finished_at`: `2026-07-16T02:48:16.307853+00:00`
- `prewarm`: `minutes=6`, `markets=11`, `residuals=1`
- mounted-volume derived counts:
  - `game_training_examples`: `794`
  - `minutes_training_examples`: `15,324`
  - `player_prop_training_examples`: `41,748`
- latest mounted-volume artifacts:
  - `component-pregame-v2__walk_forward_backtest__20260716T0244579998130000`
  - `adaptive-context-v11-ratings-context__walk_forward_segments__20260716T0246393837670000`

Useful options:

- `-SkipLocalTraining` syncs the DB and only uploads any existing processed cache files.
- `-SkipCacheUpload` runs local validation only.
- `-TrainingStartDate YYYY-MM-DD` overrides the dynamic rolling window for that run.
- `-RunServerTraining` queues `/api/models/train` inside the live backend after uploading cache files.
- `-PollServer` prints the live `/api/ops/health` payload after queueing server training.

The current saved game evaluation signature is `v7`. Recent game-model runs use:

- historical game-market backfill from The Odds API for 2024-2025 spreads/totals/moneylines
- direct historical game models for raw margin and raw total
- expanded game-total inputs from existing runtime data: market prices, vig-free O/U probabilities, and matchup pregame roster aggregates derived from the player projection pipeline
- a small market spread anchor for ATS trustworthiness
- settled-game residual layers for ATS and totals
- month-segmented refits for walk-forward game evaluation so training time scales with calendar segments instead of refitting direct game models on nearly every row
- a conservative O/U decision rule that falls back to the raw total edge when the learned market-relative edge is under `1.0`

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
.\.venv\Scripts\python.exe scripts\analyze_accuracy.py report --model adaptive-context-v3-team-transition
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

The game model is no longer just a heuristic score formula. Live matchup output now layers:

- team scoring context: recent scoring, season scoring, opponent points allowed, pace, home/away, rest, and injury availability
- direct historical game training: raw margin and raw total models trained from prior final games only
- market anchoring: a small spread anchor for margin and a small total anchor for totals when a real line exists
- settled-history correction: ATS and total residual models trained on saved game predictions versus final results
- conservative O/U pick logic: if the learned market-relative total edge is too small, the app keeps the raw total edge instead of forcing a noisy flip

Latest saved walk-forward game metrics (`game_eval_signature=v6` at the time of that snapshot) are:

- `game_ats`: `10.431` MAE, `13.024` RMSE, `0.522` directional accuracy
- `game_total`: `12.284` MAE, `15.717` RMSE, `0.462` directional accuracy
- `game_overall`: `11.110` MAE, `13.992` RMSE, `0.535` directional accuracy

Relative to the baseline game formulas, the saved blended model currently improves ATS and overall direction while keeping materially better raw total error. Total market-direction still lags the baseline slightly, so future work should target total-pick calibration rather than more raw total regression weight.

Operational note:

- `/api/models/train` is still the full Model Lab job, not a game-only retrain
- if you only change game-model features, the cron-facing training wrapper can still run longer because player-market walk-forward and cache prewarm remain part of the same job
- the default `scripts/live_daily_props.sh` poll timeout is `1200` seconds; raise `WNBA_TRAIN_POLL_TIMEOUT_SECONDS` if live full-training runtime grows beyond that

Matchup predictions are saved when `/api/matchups` is built, and current-slate recalculation also rewrites saved game predictions for the active/scheduled slate before caches are republished. ESPN history imports and settlement flows then compare final scores against those saved winner, ATS, and over/under predictions.
