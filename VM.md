# Running On A VM

This document covers moving the project onto a Linux VM while keeping the
runtime database local to the repo at `data/wnba.sqlite`.

This is the right fit when:

- you want the VM to be the single source of truth
- you want to keep using SQLite instead of Turso
- you will run exactly one backend instance that writes to the DB

This is not a multi-instance deployment model. SQLite is fine for a single VM,
but do not point multiple app servers at the same database file.

## Deployment Shape

Recommended production layout:

- `nginx` serves the built frontend from `frontend/dist`
- `nginx` proxies `/api/*` to FastAPI on `127.0.0.1:8010`
- `uvicorn` runs the backend as a `systemd` service
- the runtime DB stays in the repo at `data/wnba.sqlite`
- snapshots and backups are copied off the VM regularly

## 1. Prepare Your Current Machine

Before moving, make a clean backup of the current local DB.

From the repo root:

```bash
./snapshot.sh create --label pre-vm-migration
```

Copy these paths to the VM:

- `data/wnba.sqlite`
- `data/snapshots/`
- optionally `data/cache/` if you want to preserve raw provider responses

If you use git to deploy, do not rely on git for the SQLite runtime file unless
that is already an intentional part of your workflow.

## 2. Provision The VM

Use a small Ubuntu or Debian VM first. Install the base packages:

```bash
sudo apt-get update
sudo apt-get install -y python3 python3-venv python3-pip nodejs npm nginx git
```

If you want TLS, also plan to install `certbot` later.

## 3. Clone The Repo

Example target path:

```bash
sudo mkdir -p /opt/wnba-stats
sudo chown "$USER":"$USER" /opt/wnba-stats
git clone <your-repo-url> /opt/wnba-stats
cd /opt/wnba-stats
```

If the repo is already on the VM, update it instead:

```bash
cd /opt/wnba-stats
git pull --ff-only origin main
```

## 4. Restore The Local Data

Restore the copied data into the project directory on the VM so the backend can
keep using the same relative DB path:

- `data/wnba.sqlite`
- `data/snapshots/`
- optionally `data/cache/`

The expected runtime path in this repo is:

```text
/opt/wnba-stats/data/wnba.sqlite
```

## 5. Configure The Environment

Create the runtime env file:

```bash
cp .env.example .env
```

Use SQLite explicitly:

```dotenv
USE_TURSO=false
WNBA_DB_PATH=data/wnba.sqlite
ENV=prod
API_KEY=replace_with_a_real_shared_key
ODDS_API_KEY=replace_if_used
SPORTSDATAIO_API_KEY=replace_if_used
EXPOSE_DEBUG_HEADERS=false
```

Important:

- `ENV=prod` means the backend requires `API_KEY`
- keep `WNBA_DB_PATH` relative to the repo root unless you intentionally move it
- leave `USE_TURSO=false`

## 6. Install App Dependencies

Backend:

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r backend/requirements.txt
```

Frontend:

```bash
cd frontend
npm install
npm run build
cd ..
```

## 7. Initialize The Schema Safely

Run the init script after the DB has been restored:

```bash
. .venv/bin/activate
python scripts/init_db.py
```

This should apply schema setup and required bootstrap records without wiping the
restored database.

## 8. Create The Backend Service

This repo includes a ready-to-copy template at
`deploy/wnba-backend.service`.

Copy it into place:

```bash
sudo cp deploy/wnba-backend.service /etc/systemd/system/wnba-backend.service
```

Edit `User=` and `Group=` before enabling it if you are not deploying as the
default placeholder user.

Then load and start the service:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now wnba-backend
sudo systemctl status wnba-backend
```

Useful logs:

```bash
journalctl -u wnba-backend -n 200 --no-pager
```

## 9. Configure Nginx

This repo includes a ready-to-copy template at
`deploy/nginx-wnba-stats.conf`.

Copy it into place:

```bash
sudo cp deploy/nginx-wnba-stats.conf /etc/nginx/sites-available/wnba-stats
```

Edit `server_name` before enabling it.

Enable it:

```bash
sudo ln -s /etc/nginx/sites-available/wnba-stats /etc/nginx/sites-enabled/wnba-stats
sudo nginx -t
sudo systemctl reload nginx
```

If the default nginx site conflicts, disable it:

```bash
sudo rm -f /etc/nginx/sites-enabled/default
sudo nginx -t
sudo systemctl reload nginx
```

## 10. Open Firewall Ports

Allow inbound web traffic:

```bash
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp
sudo ufw enable
```

Do not expose `8010` publicly. Keep FastAPI bound to `127.0.0.1`.

## 11. Verify The Deployment

Check backend health on the VM:

```bash
curl http://127.0.0.1:8010/api/health
```

Check the public site:

```bash
curl http://YOUR_DOMAIN_OR_VM_IP/
curl http://YOUR_DOMAIN_OR_VM_IP/api/health
```

Then open the site in a browser and verify:

- frontend loads
- `/api/health` returns `{"status":"ok"}`
- `/index.html` and `/runtime-config.js` return `Cache-Control: no-store`
- existing data from `data/wnba.sqlite` is visible
- a protected mutation works only with `X-API-Key`

## 12. Add Backups

If SQLite is your production database, backups are mandatory.

Minimum recommendation:

- create snapshots on a schedule
- copy snapshot files off the VM
- keep at least one recent full copy of `data/wnba.sqlite`

Example nightly cron entry:

```cron
0 4 * * * cd /opt/wnba-stats && ./snapshot.sh create --label nightly >> /var/log/wnba-snapshot.log 2>&1
```

That only creates local snapshots. You still need an off-VM copy target such as
object storage, another machine, or a managed backup service.

## 13. Deploying Updates Later

When updating code on the VM:

```bash
cd /opt/wnba-stats
git pull --ff-only origin main
. .venv/bin/activate
pip install -r backend/requirements.txt
cd frontend
npm install
npm run build
cd ..
python scripts/init_db.py
sudo systemctl restart wnba-backend
sudo systemctl reload nginx
```

Take a snapshot before major schema or model changes:

```bash
cd /opt/wnba-stats
./snapshot.sh create --label pre-deploy
```

## 14. Operational Caveats

- `dev.sh` is for development startup, not VM production service management.
- Run one backend writer process only.
- The backend uses local cache files under `data/cache/`, so the service user
  must be able to write there.
- If you rebuild the frontend, nginx serves the new files immediately after the
  build completes, and `index.html` / `runtime-config.js` should not be cached across deploys.
- If the VM disk fills up, SQLite and cache writes will fail. Monitor disk use.

## 15. Recommended Cutover Checklist

1. Snapshot the current machine.
2. Copy `data/wnba.sqlite` and snapshots to the VM.
3. Set `.env` for local SQLite production mode.
4. Install backend and frontend dependencies.
5. Build the frontend.
6. Run `python scripts/init_db.py`.
7. Start `wnba-backend`.
8. Configure nginx.
9. Verify `/api/health` locally and publicly.
10. Test one real import flow with the API key.
11. Set up recurring backups before treating the VM as primary.
