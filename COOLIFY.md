# Deploying With Coolify

This repo can be deployed on Coolify as a Docker Compose application with:

- `frontend-build`: Node building the Vite app into a shared volume
- `frontend`: nginx serving the built Vite app on a public URL
- `backend`: FastAPI on the private Docker network
- `wnba_data`: persistent volume for SQLite, caches, and snapshots

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
EXPOSE_DEBUG_HEADERS=false
```

Optional provider keys:

```dotenv
ODDS_API_KEY=
SPORTSDATAIO_API_KEY=
ODDSPAPI_KEY=
BALLDONTLIE_API_KEY=
```

If you want to use the browser UI for protected admin actions such as imports,
recalculation, or training, also set:

```dotenv
VITE_API_KEY=the_same_value_as_API_KEY
```

Important:

- `VITE_API_KEY` is embedded into the built frontend bundle by the `frontend-build` service.
- Any user with access to the deployed app can inspect it in the browser.
- This is acceptable only for a private/admin deployment.
- If the site is public, leave `VITE_API_KEY` unset and treat the protected
  POST endpoints as server/admin-only until real authentication is added.

Because `VITE_API_KEY` is a build-time value, changing it requires a frontend
rebuild/redeploy.

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

## 5. Verify The Deployment

After deployment, verify:

- `/` loads the React app
- `/api/health` returns `{"status":"ok"}`
- existing data persists after a redeploy/restart
- imports work from the UI only if `VITE_API_KEY` is intentionally configured
- the backend can reach external providers from the server network

## 6. Backup Requirement

If production uses SQLite, back up the volume contents regularly. At minimum,
retain recent copies of:

- `wnba.sqlite`
- `data/snapshots/`

Do not rely on a single Contabo disk as the only copy of production data.
