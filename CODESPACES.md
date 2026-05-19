# Running In GitHub Codespaces

This project runs in Codespaces with two processes:

- FastAPI backend on port `8010`
- Vite React frontend on port `5184`

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
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8010 --reload
```

## 4. Start The Frontend

Open a second terminal in Codespaces, then run:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5184
```

## 5. Open The App

Open the Codespaces `Ports` tab and click the forwarded URL for port `5184`.

The backend API docs are available from the forwarded port `8010` URL:

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
python -m uvicorn backend.app.main:app --host 0.0.0.0 --port 8010 --reload
```

Frontend:

```bash
cd frontend
npm install
npm run dev -- --host 0.0.0.0 --port 5184
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

Covers is the preferred player-prop source. If Covers rows exist for a game,
the sync from `sportsbook_prop_lines` to model-ready `prop_lines` uses Covers
for that game and treats The Odds API rows as fallback-only.
The model sync dedupes exact lines by `game_id + player_id + market + line`;
when multiple books have the same line, one model row is kept with the best
available over and under prices. Different lines remain separate.

Missing ESPN box score fills write to Turso with batched HTTP pipeline calls.
The `player_game_stats` table is unique by `(player_id, game_id)`, and imports
use replace/upsert semantics so repeated missing-only fills repair gaps without
duplicating player stat rows.
Use the app's `Date Results` control, or repeated `selected_dates` query
parameters on `/api/history/import/espn`, for quick single-date or small-batch
completed-game repairs without a full season refresh.

Model Lab training also uses Turso in normal app runs. The training route opens
the same runtime connection as the rest of the backend, writes metrics to
`model_runs`, and only uses local SQLite when a test or explicit one-off command
sets `WNBA_DB_PATH`.

Check final-game box score coverage from the repo root:

```bash
.venv/bin/python -c "from backend.app.db import connect; \
with connect() as conn: \
    rows=conn.execute(\"select substr(game_date,1,4) season, count(*) final_games, sum(case when exists (select 1 from player_game_stats s where s.game_id=g.id) then 1 else 0 end) with_stats, sum(case when not exists (select 1 from player_game_stats s where s.game_id=g.id) then 1 else 0 end) missing from games g where status='final' group by substr(game_date,1,4) order by season\").fetchall(); \
print([dict(row) for row in rows])"
```

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

- If the frontend opens but API calls fail, confirm the backend is running on port `8010`.
- If a port is not visible, open the `Ports` tab and manually forward `5184` or `8010`.
- If the frontend server prints a localhost URL, still open the Codespaces forwarded `5184` URL.
- If dependencies are missing after rebuilding the Codespace, rerun the install commands above.
