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

Reset the local SQLite database and rebuild scheduled games from saved odds cache:

```bash
source .venv/bin/activate
python scripts/reset_live_db.py
```

## Troubleshooting

- If the frontend opens but API calls fail, confirm the backend is running on port `8000`.
- If a port is not visible, open the `Ports` tab and manually forward `5174` or `8000`.
- If the frontend server prints a localhost URL, still open the Codespaces forwarded `5174` URL.
- If dependencies are missing after rebuilding the Codespace, rerun the install commands above.
