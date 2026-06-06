## Backend Setup

### Dev

1. Copy `.env.dev.example` to `.env.dev` if you want custom local settings.
2. Create the virtualenv and install dependencies:

```bash
cd /root/app-src/backend
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

3. Start the API:

```bash
cd /root/app-src/backend
./run-dev.sh
```

The API will listen on `http://localhost:8000`.
If port `8000` is already in use, run `PORT=8001 ./run-dev.sh`.

### Prod

1. Copy `.env.prod.example` to `.env.prod` and fill in real values.
2. Install dependencies into `.venv`.
3. Start the API:

```bash
cd /root/app-src/backend
./run-prod.sh
```

On this host, port `8000` is already occupied. Use `PORT=8002` for the prod service unless you free `8000`.

### Prod checks

Run a local production health check:

```bash
cd /root/app-src/backend
./check-prod.sh
```

Deploy updated backend code and restart the service:

```bash
cd /root/app-src/backend
./deploy-prod.sh
```

### Dev debugging

Run the dev backend:

```bash
cd /root/app-src/backend
PORT=8001 ./run-dev.sh
```

Then use the debug scripts from another shell:

```bash
cd /root/app-src/backend
./debug-read.sh
./debug-import-props.sh covers false
./debug-refresh-espn.sh
./debug-settle.sh
./debug-smoke.sh
```

If your dev API requires a key, export it first:

```bash
export DEV_API_KEY=your-dev-key
```

For a visual dev-only page:

```bash
cd /root/app-src/backend
PORT=5173 ./run-debug-ui.sh
```

Then open:

```text
http://127.0.0.1:5173
```

### Notes

- Dev mode allows open mutation routes when `API_KEY` is not set.
- Prod mode requires `API_KEY`.
- Keep dev and prod on separate databases.
