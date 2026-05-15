# Running In GitHub Codespaces

This project runs in Codespaces with two processes:

- FastAPI backend on port `8000`
- Vite React frontend on port `5174`

Codespaces needs both servers to bind to `0.0.0.0` so GitHub can forward the ports.

## 1. Create A Codespace

Open the repository on GitHub, then choose:

```text
Code > Codespaces > Create codespace on main
```

Wait for the Codespace terminal to finish starting.

## 2. Install Backend Dependencies

From the repository root:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r backend/requirements.txt
cp .env.example .env
# Edit .env and set TURSO_DATABASE_URL and TURSO_AUTH_TOKEN.
python scripts/init_db.py
```

## 3. Start The Backend

In the first terminal:

```bash
source .venv/bin/activate
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

## 4. Start The Frontend

Open a second terminal in Codespaces, then run:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5174
```

## 5. Open The App

Open the Codespaces `Ports` tab and click the forwarded URL for port `5174`.

The backend API docs are available from the forwarded port `8000` URL:

```text
/docs
```

## Update An Existing Codespace

You do not need to recreate a Codespace after pushing new commits. In the
Codespace terminal, check for local changes first:

```bash
git status
```

If the Codespace working tree is clean, pull the latest `main`:

```bash
git pull --ff-only origin main
```

Then restart the backend and frontend terminals so both servers use the new
code.

Backend:

```bash
source .venv/bin/activate
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8000 --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5174
```

If `git status` shows uncommitted changes in Codespaces, commit, stash, or
discard them before pulling.

## Shared Turso Database

The app uses Turso Cloud for normal runtime data. Set the same
`TURSO_DATABASE_URL` and `TURSO_AUTH_TOKEN` in Codespaces that you use locally,
then both environments read and write the same games, props, predictions, and
settled results. Do not copy `data/wnba.sqlite` between machines.

After a clean reset, only canonical WNBA teams are present. Players are created
when ESPN player box scores are imported, usually through `Load Missing ESPN`
after completed games.

To reset Turso to a clean slate with only canonical teams:

```powershell
.\.venv\Scripts\python.exe scripts\clear_runtime_db.py
```

## Optional API Keys

Live sportsbook odds use The Odds API. Set the key before starting the backend:

```bash
export ODDS_API_KEY="your_key_here"
```

For persistent storage in Codespaces, add it as a Codespaces secret in GitHub instead of typing it into the terminal.

## Useful Commands

Run backend tests:

```bash
source .venv/bin/activate
python -m pytest
```

Build the frontend:

```bash
cd frontend
npm run build
```

Reset the Turso database and rebuild scheduled games from saved odds cache:

```bash
source .venv/bin/activate
python scripts/reset_live_db.py
```

## Troubleshooting

- If the frontend opens but API calls fail, confirm the backend is running on port `8000`.
- If a port is not visible, open the `Ports` tab and manually forward `5174` or `8000`.
- If the frontend server prints a localhost URL, still open the Codespaces forwarded `5174` URL.
- If dependencies are missing after rebuilding the Codespace, rerun the install commands above.
