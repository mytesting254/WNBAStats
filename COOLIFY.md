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
ADMIN_USERNAME=admin
ADMIN_PASSWORD=replace_with_a_strong_password
EXPOSE_DEBUG_HEADERS=false
```

Optional provider keys:

```dotenv
ODDS_API_KEY=
SPORTSDATAIO_API_KEY=
ODDSPAPI_KEY=
BALLDONTLIE_API_KEY=
```

Important:

- `ADMIN_USERNAME` and `ADMIN_PASSWORD` bootstrap the admin account used by the Data tab login.
- Session-authenticated admin POST routes also require a CSRF token header, which the frontend now manages automatically.
- Leave `VITE_API_KEY` unset for public deployments so the browser does not receive a shared mutation secret.
- `API_KEY` remains available as a fallback for server-to-server or manual admin requests that send `X-API-Key`.
- If you intentionally use `VITE_API_KEY`, it is embedded into the frontend bundle and visible to any browser user.

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
- Data tab login works with the configured admin credentials
- the backend can reach external providers from the server network

## 6. Backup Requirement

If production uses SQLite, back up the volume contents regularly. At minimum,
retain recent copies of:

- `wnba.sqlite`
- `data/snapshots/`

Do not rely on a single Contabo disk as the only copy of production data.
