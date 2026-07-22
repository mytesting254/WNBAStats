from __future__ import annotations

import re
import sqlite3


WNBA_TEAMS = [
    (1, "ATL", "Atlanta Dream", "/team-logos/atl.png"),
    (2, "CHI", "Chicago Sky", "/team-logos/chi.png"),
    (3, "CON", "Connecticut Sun", "/team-logos/conn.png"),
    (4, "DAL", "Dallas Wings", "/team-logos/dal.png"),
    (5, "GS", "Golden State Valkyries", "/team-logos/gs.png"),
    (6, "IND", "Indiana Fever", "/team-logos/ind.png"),
    (7, "LV", "Las Vegas Aces", "/team-logos/lv.png"),
    (8, "LA", "Los Angeles Sparks", "/team-logos/la.png"),
    (9, "MIN", "Minnesota Lynx", "/team-logos/min.png"),
    (10, "NY", "New York Liberty", "/team-logos/ny.png"),
    (11, "PHX", "Phoenix Mercury", "/team-logos/phx.png"),
    (12, "SEA", "Seattle Storm", "/team-logos/sea.png"),
    (13, "TOR", "Toronto Tempo", "/team-logos/tor.png"),
    (14, "WSH", "Washington Mystics", "/team-logos/wsh.png"),
    (15, "POR", "Portland Fire", "/team-logos/por.png"),
]

TEAM_BY_ABBREVIATION = {team[1]: team for team in WNBA_TEAMS}
TEAM_ALIASES = {
    team[1].lower(): team[1]
    for team in WNBA_TEAMS
} | {
    team[2].lower(): team[1]
    for team in WNBA_TEAMS
} | {
    "atlanta": "ATL",
    "dream": "ATL",
    "chicago": "CHI",
    "sky": "CHI",
    "nyl": "NY",
    "conn": "CON",
    "connecticut": "CON",
    "sun": "CON",
    "dallas": "DAL",
    "wings": "DAL",
    "golden state": "GS",
    "lva": "LV",
    "las": "LA",
    "valkyries": "GS",
    "indiana": "IND",
    "fever": "IND",
    "vegas": "LV",
    "las vegas": "LV",
    "aces": "LV",
    "los angeles": "LA",
    "sparks": "LA",
    "minnesota": "MIN",
    "lynx": "MIN",
    "new york": "NY",
    "liberty": "NY",
    "pho": "PHX",
    "phoenix": "PHX",
    "mercury": "PHX",
    "was": "WSH",
    "washington": "WSH",
    "mystics": "WSH",
    "gsv": "GS",
    "val": "GS",
    "portland": "POR",
    "pdx": "POR",
    "por": "POR",
    "fire": "POR",
    "seattle": "SEA",
    "storm": "SEA",
    "toronto": "TOR",
    "tempo": "TOR",
}
TEAM_NAME_BY_NORMALIZED_KEY = {
    re.sub(r"[^a-z0-9]+", " ", team[2].lower()).strip(): team[1]
    for team in WNBA_TEAMS
}


def ensure_teams(conn: sqlite3.Connection) -> None:
    for team_id, abbreviation, name, logo_url in WNBA_TEAMS:
        existing = conn.execute(
            "SELECT id FROM teams WHERE upper(abbreviation) = ?",
            (abbreviation,),
        ).fetchone()
        if existing:
            conn.execute(
                "UPDATE teams SET name = ?, logo_url = ? WHERE id = ?",
                (name, logo_url, int(existing["id"])),
            )
            continue

        id_taken = conn.execute("SELECT id FROM teams WHERE id = ?", (team_id,)).fetchone()
        if id_taken:
            conn.execute(
                "INSERT INTO teams (abbreviation, name, logo_url) VALUES (?, ?, ?)",
                (abbreviation, name, logo_url),
            )
        else:
            conn.execute(
                "INSERT INTO teams (id, abbreviation, name, logo_url) VALUES (?, ?, ?, ?)",
                (team_id, abbreviation, name, logo_url),
            )


def normalize_team_abbreviation(team_name: str | None) -> str | None:
    if not team_name:
        return None
    raw = team_name.strip()
    key = raw.lower()
    if not key:
        return None
    if key in TEAM_ALIASES:
        return TEAM_ALIASES[key]

    # Covers/feeds sometimes include ranking, record, or parenthetical suffixes.
    key = re.sub(r"\([^)]*\)", " ", key)
    key = re.sub(r"^no\.?\s*\d+\s+", "", key)
    key = re.sub(r"[^a-z0-9]+", " ", key).strip()
    if not key:
        return None

    if key in TEAM_ALIASES:
        return TEAM_ALIASES[key]
    if key in TEAM_NAME_BY_NORMALIZED_KEY:
        return TEAM_NAME_BY_NORMALIZED_KEY[key]

    compact = key.replace(" ", "")
    if compact in TEAM_ALIASES:
        return TEAM_ALIASES[compact]
    return raw.upper() if len(raw) <= 3 else None


def ensure_team(conn: sqlite3.Connection, team_name: str) -> int | None:
    abbreviation = normalize_team_abbreviation(team_name)
    if not abbreviation:
        return None
    ensure_teams(conn)
    row = conn.execute(
        "SELECT id FROM teams WHERE upper(abbreviation) = ?",
        (abbreviation.upper(),),
    ).fetchone()
    if row:
        return int(row["id"])

    next_id = conn.execute("SELECT COALESCE(MAX(id), 0) + 1 FROM teams").fetchone()[0]
    conn.execute(
        "INSERT INTO teams (id, abbreviation, name, logo_url) VALUES (?, ?, ?, ?)",
        (next_id, abbreviation.upper(), team_name.strip(), None),
    )
    return int(next_id)
