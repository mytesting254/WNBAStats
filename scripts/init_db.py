from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.bootstrap import ensure_teams
from backend.app.db import connect, init_db


if __name__ == "__main__":
    init_db()
    with connect() as conn:
        ensure_teams(conn)
    print("Initialized WNBA database schema and teams.")
