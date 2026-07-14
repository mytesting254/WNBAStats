# Deploying With Coolify

This repo can be deployed on Coolify as a Docker Compose application with:

- `frontend`: one multi-stage image that builds the Vite app, then serves the built files from nginx on the public URL
- `backend`: FastAPI on the private Docker network
- `wnba_data`: persistent volume for SQLite, caches, and snapshots
  - default Docker volume name: `wnbastats-data`

For broader failure patterns and recovery notes, see [TROUBLESHOOTING.md](TROUBLESHOOTING.md).

## 1. Use The Compose File

In Coolify, create a new application from this repo and select:

- `Docker Compose`
- compose file: `docker-compose.yml`

Expose the `frontend` service publicly. The browser should only talk to the
frontend URL. nginx proxies `/api/*` to the backend service internally.

## 2. Set Environment Variables

Minimum SQLite setup:

```dotenv
USE_TURSO=false
WNBA_DATA_DIR=/data
WNBA_DB_PATH=/data/wnba.sqlite
WNBA_CACHE_DIR=/data/cache
WNBA_SNAPSHOT_DIR=/data/snapshots
ENV=prod
API_KEY=replace_with_a_shared_key
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace_with_a_strong_password
EXPOSE_DEBUG_HEADERS=false
```

Optional provider keys:

```dotenv
ODDS_API_KEY=
SPORTSDATAIO_API_KEY=
BALLDONTLIE_API_KEY=
```

Important:

- `ADMIN_USERNAME` and `ADMIN_PASSWORD` bootstrap the admin account used by the Data tab login.
- Session-authenticated admin POST routes also require a CSRF token header, which the frontend now manages automatically.
- Admin session cookies follow the effective request scheme by default. If your public endpoint is HTTPS through Coolify/Traefik, leave that behavior alone. If you intentionally expose plain HTTP, the app will avoid setting a `Secure` cookie so login survives page refreshes.
- `SESSION_COOKIE_SECURE=true|false` is available if you need to override cookie behavior explicitly for a specific proxy/deploy shape.
- Leave `VITE_API_KEY` unset for public deployments so the browser does not receive a shared mutation secret.
- `API_KEY` remains available as a fallback for server-to-server or manual admin requests that send `X-API-Key`.
- `runtime-config.js` must never fall back to `API_KEY`; only `VITE_API_KEY` may be emitted to the browser.
- If you intentionally use `VITE_API_KEY`, it is embedded into the frontend bundle and visible to any browser user.
- `WNBA_DOCKER_VOLUME_NAME` optionally overrides the Docker volume name. It defaults to `wnbastats-data` so the host mount path is stable and recognizable.

## 3. Attach Persistent Storage

The backend writes to:

- `/data/wnba.sqlite`
- `/data/cache/`
- `/data/snapshots/`

Do not run this app on ephemeral storage if you use SQLite. In Coolify, keep
the `wnba_data` volume persistent across redeploys.

This deployment shape assumes one backend instance only. SQLite is not the
right choice for multiple concurrent writer replicas.

## 4. First Deploy

On the first deploy, the backend will:

1. create `/data/` if it is missing
2. run `python scripts/init_db.py`
3. start FastAPI on port `8010`

If you already have an existing SQLite database, restore it into the persistent
volume before treating the new instance as primary.

If you are changing from an older auto-generated Docker volume name to
`wnbastats-data`, migrate the contents first or reattach the existing volume
under that explicit name before making the new deployment primary.

## 5. Verify The Deployment

After deployment, verify:

- `/` loads the React app
- `/api/health` returns `{"status":"ok"}`
- `/api/matchups` returns JSON through the frontend domain
- `/index.html` and `/runtime-config.js` return `Cache-Control: no-store`
- existing data persists after a redeploy/restart
- Data tab login works with the configured admin credentials
- protected admin POST actions work after login
- the backend can reach external providers from the server network

Recommended first live checks:

1. sign in on the `Data` tab
2. run `Refresh Roster` so Rotowire injury status is current
3. run `Recalculate` to rebuild player props and saved game predictions for the active slate
4. run `Refresh ESPN`
5. verify `Roster` tab loads and only shows `Refresh Roster` for admins

### Run Live Maintenance From The Repo Shell

When this host also has a local checkout, do not assume host `/data/wnba.sqlite`
is the same file used by the deployed backend container. Use the runtime wrapper
instead so maintenance commands execute inside the active backend container for
the current commit:

```bash
python scripts/live_backend.py runtime-info
python scripts/live_backend.py host-runtime-info
python scripts/live_backend.py recalculate
python scripts/live_backend.py exec -- python scripts/import_espn_history.py --dates 2026-06-25
```

If you must run a host-side script directly against the live SQLite files instead
of executing inside the container, load the active runtime volume paths first:

```bash
source scripts/live_env.sh
python scripts/init_db.py
python scripts/import_espn_history.py --dates 2026-06-25
```

This resolves the host-side `WNBA_DB_PATH`, `WNBA_CACHE_DIR`, and
`WNBA_SNAPSHOT_DIR` from the active backend container mount. Do not point host
commands at `/data/wnba.sqlite` or a repo-local `data/wnba.sqlite` by
assumption. Keep runtime state on the attached app volume, not in the repo
checkout.

The same rule applies to raw provider caches. The live Odds API payload is
stored in the resolved runtime cache directory as `sportsbook_props_raw.json`;
do not inspect repo-local `data/cache/` unless that checkout is explicitly the
active runtime root returned by `host-runtime-info` / `live_env.sh`.

### Daily Prop Settlement And Odds Refresh

Use host cron for the daily prop workflow. The 2am Eastern job now refreshes
recent ESPN finals and box scores, prunes unsettled Specials snapshots when the
final-game box score shows the player missing or marked `didNotPlay`, settles
completed props, and then waits for model training to finish. The Odds API
refresh still runs at 3am Eastern only when `/api/matchups` has scheduled
games, so the newly rebuilt props use the updated trained models.

Install with `crontab -e` on the Docker/Coolify host:

```cron
0 * * * * cd /root/WNBAStats && [ "$(TZ=America/New_York date +\%H)" = 02 ] && WNBA_USE_LIVE_CONTAINER=true scripts/live_daily_props.sh settle-and-train >> /var/log/wnba-daily-props.log 2>&1
0 * * * * cd /root/WNBAStats && [ "$(TZ=America/New_York date +\%H)" = 03 ] && WNBA_USE_LIVE_CONTAINER=true scripts/live_daily_props.sh odds-if-matchups >> /var/log/wnba-daily-props.log 2>&1
```

`WNBA_USE_LIVE_CONTAINER=true` runs the HTTP calls inside the detected backend
container, so `API_KEY` and `ODDS_API_KEY` come from the Coolify container
environment. If cron runs from a different checkout path, adjust `cd`.
Debian/Ubuntu cron does not support `CRON_TZ` for scheduling, so the hourly
entries use `date` to run only when the current `America/New_York` hour is 2am
or 3am. Keep the percent signs escaped when editing the crontab.

`settle-and-train` now runs this sequence inside the live backend container:

1. `refresh-results`
2. `prune-specials`
3. `settle`
4. `train-model`

The `prune-specials` step runs `scripts/prune_specials_from_espn_boxscore.py`
against the live runtime and deletes only latest unsettled Specials rows for
`final` games when ESPN's summary box score either omits the player entirely or
marks that player `didNotPlay`.

`settle-and-train` then polls `/api/ops/health` until the background
`/api/models/train` job finishes or times out. Optional tuning:

- `WNBA_TRAIN_POLL_TIMEOUT_SECONDS` defaults to `1200`
- `WNBA_TRAIN_POLL_INTERVAL_SECONDS` defaults to `5`

A copy/paste template is also available at `deploy/wnba-daily-props.cron`.

### Specials Stocks Prep

Use host cron for the night-before `Special` stocks prep workflow. The initial
production schedule prepares tomorrow's slate at `11pm America/New_York`, so
Monday data is built on Sunday night in the live runtime before the slate day.

Install with `crontab -e` on the Docker/Coolify host:

```cron
0 * * * * cd /root/WNBAStats && [ "$(TZ=America/New_York date +\%H)" = 23 ] && WNBA_USE_LIVE_CONTAINER=true scripts/live_stocks_prep.sh tomorrow >> /var/log/wnba-stocks-prep.log 2>&1
```

`WNBA_USE_LIVE_CONTAINER=true` runs the prep command inside the detected
backend container, so the active mounted runtime DB and cache paths are used
instead of repo-local files. Debian/Ubuntu cron does not support `CRON_TZ` for
scheduling, so this follows the same hourly `date` guard pattern as the daily
prop jobs. Keep the percent signs escaped when editing the crontab.

Current prep behavior:

- canonical schedule source: persisted `games` in `wnba.sqlite`
- derived slate copy: `stocks_tracking.sqlite.prepared_games`
- candidate window: last `10` games
- estimator: fast component path with `10`-game recent form, `20`-game stabilizer, home/away context, and `rest_days`
- live validation: mounted-volume ET-today run completed in about `1m 1s` for `3` prepared games / `79` candidates / `79` fresh snapshots

Useful manual variants:

- `scripts/live_stocks_prep.sh today`
- `scripts/live_stocks_prep.sh today-and-tomorrow`
- `scripts/live_stocks_prep.sh dates 2026-07-14,2026-07-15`

A copy/paste template is also available at `deploy/wnba-stocks-prep.cron`.

Live learned-model note:

- uploaded learned-model cache files are a startup optimization, not the source of truth
- live prop rebuilds now prewarm learned models against the active runtime DB before writing predictions
- if the cache filenames no longer match the active runtime DB fingerprint after new ingest/history, the backend retrains from live data instead of silently falling back to `component`
- after deploying a backend change that affects learned-model fingerprints, restart the backend container and trigger a prop rebuild once so fresh `adaptive-context-v1` predictions are written into `prop_predictions`

### Live Snapshot Recovery

The backend already supports filesystem DB snapshots on the attached app volume.
Keep them under `/data/snapshots` and treat them as runtime state, not repo
artifacts.

Create a live snapshot:

```bash
python scripts/live_backend.py exec -- python scripts/snapshot_create.py --name wnba-runtime
```

Recommended scheduled wrapper:

```bash
scripts/live_snapshot_backup.sh
```

Restore from a live snapshot:

```bash
python scripts/live_backend.py exec -- python scripts/snapshot_restore.py /data/snapshots/wnba-runtime.sqlite --force
```

If you need host-side maintenance instead of container-exec, load
`source scripts/live_env.sh` first so `WNBA_DB_PATH` and `WNBA_SNAPSHOT_DIR`
resolve to the same attached volume.

For production scheduling, use a host timer/cron job. Example systemd units:

- `deploy/wnba-live-snapshot.service`
- `deploy/wnba-live-snapshot.timer`

Odds API game markets such as `h2h`, `spreads`, and `totals` are also only
authoritative in that active runtime. They are saved in the raw Odds payload
and used to update live `games` market fields there. Covers refreshes can still
populate missing game markets, but same-day Covers cache should be treated as a
fallback for matchup game lines instead of overwriting already-populated Odds
API game fields.

If auto-detection is ambiguous, pass the container explicitly:

```bash
python scripts/live_backend.py --container backend-RESOURCE-ID-DEPLOY-ID recalculate
```

Recommended deploy-freshness checks:

```bash
curl -I https://YOUR_DOMAIN/
curl -I https://YOUR_DOMAIN/index.html
curl -I https://YOUR_DOMAIN/runtime-config.js
```

Expected result:

- `index.html` and `runtime-config.js` should include `Cache-Control: no-store`
- hashed files under `/assets/` can be cached aggressively because a new build gets new filenames

## 6. Key Coolify Considerations

### Public Routing

- Attach the public domain to the frontend service only.
- Do not expose the backend directly on the public domain for normal app traffic.
- `/api/*` should flow:
  - browser -> frontend nginx -> backend
- Do not mount a persistent volume over `/usr/share/nginx/html`; the built frontend files should come from the frontend image itself on each deploy.

If `/api` responses come from `uvicorn` directly instead of `nginx`, check for:

- stale Coolify/Traefik dynamic route files
- leftover backend domain bindings
- an old backend resource still attached to the same hostname

### Reverse Proxy Headers

- Admin POST routes depend on proxy-forwarded host/proto headers for origin validation.
- If login succeeds but every admin POST returns `403`, verify that the request is reaching the app through the intended public hostname and proxy chain.

### Persistent Storage

- Keep the SQLite file, cache directory, and snapshots on persistent storage.
- If you recreate the app or services, confirm the data volume is still mounted to `/data`.
- Back up the database before destructive redeploy/rebuild work.

### SQLite Expectations

- Run one backend instance only.
- `WAL` reduces read/write blocking, but does not support multi-writer scaling.
- If you scale backend replicas or run many admin writes concurrently, expect lock contention or inconsistent behavior.

### Provider Reality

- `ODDS_API_KEY` must exist for live Odds API imports.
- Covers imports depend on public HTML scraping and can fail because of:
  - parser drift
  - provider markup changes
  - host/IP blocking

## 7. Recommended Recovery Practices

- Keep at least one recent copy of:
  - `/data/wnba.sqlite`
  - `/data/cache/`
  - `/data/snapshots/`
- Before changing auth/proxy/deploy shape, save a DB backup and note the current working commit.
- If the frontend goes blank, check:
  - `/api/health`
  - `/api/matchups`
  - `/index.html` response headers
  - backend logs
  - Coolify domain bindings
  - proxy overrides

## 8. Backup Requirement

If production uses SQLite, back up the volume contents regularly. At minimum,
retain recent copies of:

- `wnba.sqlite`
- `data/snapshots/`

Do not rely on a single Contabo disk as the only copy of production data.
