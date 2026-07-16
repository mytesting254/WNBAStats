# Troubleshooting

This guide covers the most likely production and local failure cases for the WNBA app.

## Quick Triage

Check these first:

1. `GET /api/health`
2. `GET /api/matchups`
3. browser devtools Network tab
4. backend logs
5. frontend domain/proxy bindings

If `health` works but the app is blank, the problem is usually routing, payload shape, or frontend runtime behavior rather than backend startup.

For stale-frontend suspicion, also check:

1. `curl -I https://<domain>/index.html`
2. `curl -I https://<domain>/runtime-config.js`
3. confirm both return `Cache-Control: no-store`
4. confirm `/assets/*` responses come from the current frontend image/build

## Public Routing Problems

### Symptom

- homepage loads but API calls fail
- `/api/*` returns the wrong payload
- app is blank even though containers are healthy

### What To Check

- the public domain is attached to the frontend service only
- frontend nginx is serving `/`
- `/api/*` is being proxied by nginx to the backend
- no stale Coolify/Traefik override is hijacking `/api`

### Strong Signal

If `https://<domain>/api/matchups` returns a response directly from `uvicorn` instead of `nginx`, public routing is wrong.

## Admin Login Works But Data POSTs Fail

### Symptom

- `POST /api/auth/login` succeeds
- `Refresh Covers`, `Recalculate`, `Refresh ESPN`, or similar actions return `403`

### Likely Cause

- proxy-origin mismatch
- CSRF/session validation mismatch through Coolify/Traefik

### What To Check

- browser is using the intended public hostname
- backend sees forwarded host/proto headers
- `GET /api/auth/me` shows an authenticated admin session
- the failing POST includes `X-CSRF-Token`

### Notes

This app now accepts a valid admin session when either:

- forwarded origin matches the public host/proto
- CSRF token matches the session token

If login still works but POSTs fail, inspect the backend log line and response body before changing unrelated app logic.

## Unauthorized On Mutation Routes

### Symptom

- mutation routes return `401 Unauthorized`

### Likely Cause

- no valid admin session
- bad or missing `API_KEY` on manual requests

### What To Check

- sign in again from the `Data` tab
- confirm the request is a browser session-authenticated call
- for curl/manual calls, send:
  - `X-API-Key: <API_KEY>`
  - or `Authorization: Bearer <API_KEY>`

## Admin Login Is Lost On Refresh Or Redeploy

### Symptom

- `POST /api/auth/login` succeeds
- a page refresh shows the admin as signed out
- a new container deploy appears to log the admin out immediately

### Likely Cause

- the browser never persisted the session cookie
- the app is being served over plain `http`, but cookie security was forced for `https`
- proxy headers make the backend believe the public scheme is different from the browser-visible scheme

### What To Check

- inspect the login response cookie attributes in the browser devtools
- if the public app is HTTPS, ensure the proxy sends `X-Forwarded-Proto: https`
- if the public app is intentionally HTTP, do not force `SESSION_COOKIE_SECURE=true`
- `GET /api/auth/me` immediately after login should stay authenticated across reloads

### Notes

- The app now sets the admin session cookie `Secure` flag from the effective request scheme by default.
- Use `SESSION_COOKIE_SECURE=true|false` only when you need to override that behavior for a specific deployment.

## Model Lab Missing Or Train Fails

### Symptom

- the `Models` / `Model Lab` tab is not visible
- clicking `Train` returns `Unauthorized. Sign in on the Data tab and try Train again.`
- clicking `Train` returns a session verification / same public domain error

### Expected Behavior

- the `Model Lab` tab is only visible to authenticated admins
- if auth is lost while that tab is open, the frontend automatically returns to `Pregame Props`

### What To Check

- `GET /api/auth/me` shows `authenticated: true` and `user.is_admin: true`
- the browser is still on the same public hostname used for login
- the `Train` request includes the session cookie and `X-CSRF-Token`

### How To Read Failures

- `401`: no valid admin session; sign in again on the `Data` tab
- `403`: admin session exists, but CSRF/origin/session verification failed; sign out, sign back in, then retry from the same public domain

## Live Props Use `component` Instead Of `adaptive-context-v1`

### Symptom

- recent rows in `prop_predictions` show `model_version = 'component'`
- live props still build, but they are not using the learned player model

### Likely Cause

- uploaded learned-model cache files no longer match the active live DB fingerprint
- the runtime was rebuilt against newer history/settlement data than the uploaded cache files were produced from
- the backend container has not yet been restarted onto code that prewarms learned models during live rebuilds

### What To Check

- latest backend image/container includes the projection rebuild change
- backend startup completed normally after deploy/restart
- a fresh live prop rebuild has been triggered after the container came up
- recent `prop_predictions.model_version` rows switch back to `adaptive-context-v1`

### Expected Behavior

- uploaded cache files warm startup when their DB fingerprint matches
- if they do not match, live prop rebuilds train from the active runtime DB instead of silently downgrading to `component`

## Covers Refresh Fails

### Symptom

- `Refresh Covers` fails
- cached Covers data loads but live refresh does not

### Likely Cause

- Covers parser drift
- Covers page markup changed
- source host/IP is blocked or challenged
- scrape fallback/runtime dependencies differ by environment

### What To Check

- backend error detail for the Covers route
- whether the response is an auth failure, parser failure, or network failure
- whether the live container can reach Covers successfully

### Important

This is an HTML scrape path, not a stable API integration. Production failures can happen even when local works.

## Odds Import Fails

### Symptom

- `Refresh Odds` fails or reports missing API key
- `Refresh Odds` stays queued or previously ended in a `504 Gateway Time-out`
- `Refresh Odds`, `Refresh Covers`, `Recalculate`, or roster-change repair appears to jump backward in the progress card
- the `Live Pipeline` card stays pinned to an old stage like `Requesting provider data` / `17%` even though the import job actually finished
- `Refresh Covers` or `Recalculate` completes work, but the `Live Pipeline` card never shows progress for that run

### Likely Cause

- `ODDS_API_KEY` is not set in production
- old deployment bundle still used the pre-queue synchronous import path
- reverse proxy timed out before the old synchronous import finished
- the raw payload was saved, but it belonged to a previous local date so `Load Saved Odds` does not replay it today
- the deployed backend is older than the stage-splitting fix and is still collapsing prop sync plus projection rebuild into the same progress stage
- the API is returning stale in-memory prop-sync state instead of the newer persisted `prop_sync_jobs` record
- the frontend is preserving an older running progress snapshot instead of replacing it with a newer finished payload
- the frontend build is older than the pipeline-attachment fix, so `Recalculate` never registers with the shared progress card and Covers/Odds imports can go visually silent when another prop-sync job is already running

### What To Check

- backend env var exists
- provider key is attached to the active backend resource
- the deployed backend includes the queued `/api/odds/import` implementation and the `Data` tab shows the `Live Pipeline` card
- the deployed backend includes the progress-stage split for odds sync, covers sync, recalculate, and roster-triggered repair
- the deployed backend includes the `prop_sync_jobs` source-of-truth fix from Thursday, July 16, 2026, so `/api/ops/health` does not fall back to an older in-memory job snapshot
- the deployed frontend includes the progress-merge fix from Thursday, July 16, 2026, so a newer terminal sync state can replace an older running one
- the deployed frontend includes the later July 16, 2026 pipeline-attachment fix so `Recalculate` sets `activePipeline` immediately and Covers/Odds continue following live backend progress when a prop-sync job is already active
- the active runtime cache path contains `sportsbook_props_raw.json`
  the correct path is the backend runtime cache, not necessarily repo-local `data/cache/`
- `/api/odds/cache` reports the expected runtime `path`
- the cached event dates inside `sportsbook_props_raw.json` match the app's current local date if you expect `Load Saved Odds` to replay them
- if player props load but matchup spread/total/moneyline do not, confirm the saved payload includes `h2h`, `spreads`, and `totals` markets and that those values were written onto `games`
- if same-day Covers cache exists, verify matchup payloads are using Covers game markets only as fallback, not overwriting already-populated Odds API game fields
- during an active sync, stage-local counts should only move forward inside the current stage; a pattern like `40/176 -> 20/146 -> 40/176` indicates the old backend code is still running
- if the DB row in `prop_sync_jobs` has already advanced into `rebuilding_predictions` or `publishing_payloads` but the UI still shows `requesting_provider`, the deployed frontend/backend bundle is stale

## Matchup Defense Rank Color Is Backwards

### Symptom

- the Matchups tab shows a strong defense like `#3` in red
- a poor defense rank like `#12` or `#15` appears green

### Likely Cause

- the frontend build is older than the matchup-rank tone fix and still treats defensive rank as if a higher number were better

### What To Check

- confirm the deployed frontend includes the Thursday, July 16, 2026 rank-tone fix
- `Season Def` should treat low rank numbers as positive, the same way `Season Off` and `Season Net` already do
- for the Washington Mystics example on Thursday, July 16, 2026:
  - `def_rank = 3` is correct in the payload
  - the UI should render that badge as positive

## `2am` Cron Shows `Internal Server Error`

### Symptom

- host cron entry clearly fired at `2:00am America/New_York`
- `/var/log/wnba-daily-props.log` shows `Internal Server Error`
- current slate still exists in `games`, so it does not look like a schedule gap

### Likely Cause

- the cron wrapper ran correctly, but the `settle-and-train` path failed inside app code
- on Thursday, July 16, 2026, the confirmed failure was a malformed `settled_props` insert during `settle_completed_props`

### What To Check

- confirm cron actually fired:
  - `crontab -l`
  - `tail -n 40 /var/log/wnba-daily-props.log`
- reproduce the failing path inside the live backend:
  - `python3 scripts/live_backend.py exec -- python - <<'PY'`
  - call `_import_espn_history_impl(...)` or `settle_completed_props(...)`
- if SQLite reports `22 values for 21 columns`, inspect [backend/app/settlement.py](/root/WNBAStats/backend/app/settlement.py:1)

### Notes

- this failure mode is not evidence that cron skipped the run
- the repo now uses a shared `SETTLED_PROP_COLUMNS` tuple plus settlement-row width validation before `executemany`
- the live container still needs a redeploy before it benefits from that safeguard

## SQLite And Persistence Problems

### Symptom

- data disappears after redeploy
- restored DB does not appear in the app
- app starts but important records are missing

### Likely Cause

- wrong volume mounted
- old DB restored into the wrong container/resource
- ephemeral storage used by mistake

### What To Check

- `/data/wnba.sqlite` exists in the active backend container
- Coolify volume is attached to `/data`
- cache and snapshot directories also point to `/data`
- the canonical Docker volume name should be `wnbastats-data` unless you
  intentionally overrode `WNBA_DOCKER_VOLUME_NAME`
- restored file is the actual runtime DB, not a stale or partial backup
- from the repo shell, `python scripts/live_backend.py host-runtime-info` points
  to the same host-side runtime root you expect
- if you run host-side maintenance directly, load `source scripts/live_env.sh`
  first so `WNBA_DB_PATH` resolves to the active backend volume instead of a
  shadow `/data` or repo-local SQLite file
- do not symlink repo `data/` into the live volume; keep the repo checkout
  code-only and let the app-attached volume remain the runtime source of truth

## SQLite Locking Or Concurrency Issues

### Symptom

- admin actions are slow
- imports/recalculate collide
- `database is locked`

### What To Know

- the app uses SQLite
- it now uses `WAL`
- `WAL` helps reads during writes but does not support multi-writer scale

### What To Check

- only one backend instance is running
- multiple admins are not launching heavy write flows simultaneously

### Recommended Direction

- keep one backend instance
- serialize heavy admin jobs if contention becomes common

## Blank Or Incomplete Matchup Panels

### Symptom

- matchup page loads but some sub-panels look empty or odd

### Common Causes

- sparse real source data
- expansion-team matchups with no prior history
- Covers records missing for a specific game

### H2H Specific Note

The H2H display is now intentionally sparse-aware:

- `0` meetings: note only
- `1` meeting: compact summary
- `2+` meetings: full table

So an absent or very small H2H view is not always a bug.

## Roster Tab Behavior

### Current Intended Behavior

- roster data is readable normally
- `Refresh Roster` is admin-only
- Rotowire and ESPN team labels should normalize to the same internal team code before roster enrichment runs.

### If Some Roster Rows Show `N/A`

- first check whether the issue is a player-name or team-alias mismatch rather than missing roster data
- city-only or nickname-only team labels such as `Chicago`, `Sky`, `Portland`, or `Fire` should now normalize correctly
- abbreviated or accented player spellings such as `C. Vandersloot`, `D. Carrington`, `K. Samuelson`, or `Luisa Geiselsöder` should resolve to the local player profile if the player exists in the DB
- if only `position` / `role` / impact fields are `N/A`, but `player_name`, `team`, and `status` are present, inspect roster-player resolution before changing Rotowire parsing

### If Today's Teams Are Missing From The Roster Tab

- first compare the roster tab against today's matchup slate; if the matchup view shows a team but the roster tabs do not, suspect stale Rotowire source data before changing name resolution
- `Refresh Roster` can fall back to the last saved `rotowire_lineups_raw.json` when the live fetch fails; a fresh API cache does not guarantee a fresh provider snapshot
- check the roster panel warning banner and the latest `captured_at` value; if it predates today, inspect the latest Rotowire fetch error before changing parser or player-matching code
- a DNS / network failure such as `<urlopen error [Errno -3] Temporary failure in name resolution>` will produce exactly this pattern: today's games exist in `games`, but roster rows still reflect an older Rotowire pull
- if you need to separate parser issues from fetch issues, verify whether the live Rotowire HTML contains the missing matchup before touching `_parse_lineup_injuries`

If the roster tab is missing entirely or the `Data` tab breaks after roster changes, compare the current frontend against the last known-good tab layout before changing backend behavior.

## Deployment Recovery Workflow

When production behavior becomes unclear:

1. save a DB backup
2. save current logs
3. note the live commit hash
4. verify `/api/health`
5. verify `/api/matchups`
6. verify admin login
7. test one protected POST route
8. change one layer at a time

Avoid changing auth, proxy, DB mode, and frontend behavior all at once. That makes root cause hard to isolate.
