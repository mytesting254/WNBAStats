"""Capture the WNBA scouting table exactly as RotoWire displays it."""

from __future__ import annotations

import hashlib
import json
import re
import uuid
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.request import Request, urlopen

from .rotowire_import import REQUEST_HEADERS


BASE_URL = "https://www.rotowire.com/wnba/team/"
TEAM_SLUGS = {
    "ATL": "atlanta-dream-atl", "CHI": "chicago-sky-chi",
    "CON": "connecticut-sun-con", "DAL": "dallas-wings-dal",
    "GS": "golden-state-valkyries-gsv", "IND": "indiana-fever-ind",
    "LA": "los-angeles-sparks-las", "LV": "las-vegas-aces-lva",
    "MIN": "minnesota-lynx-min", "NY": "new-york-liberty-nyl",
    "PHX": "phoenix-mercury-pho", "POR": "portland-fire-por",
    "SEA": "seattle-storm-sea", "TOR": "toronto-tempo-tor",
    "WSH": "washington-mystics-was",
}
FIELDS = (
    "Efficiency", "Pace", "Effective FG%", "Turnover %", "FTA/FGA",
    "3P%", "2P%", "FT%", "3PA/FGA",
)
CELL_RE = re.compile(r"^(-?\d+(?:\.\d+)?)\s*\((\d+)(?:st|nd|rd|th)\)$")


class _ScoutingTableParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.in_table = False
        self.depth = 0
        self.in_row = False
        self.in_cell = False
        self.cell_parts: list[str] = []
        self.row: list[str] = []
        self.rows: list[list[str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        classes = dict(attrs).get("class") or ""
        if tag == "table" and "scoutreport" in classes.split() and not self.in_table:
            self.in_table = True
            self.depth = 1
        elif self.in_table and tag == "table":
            self.depth += 1
        elif self.in_table and tag == "tr":
            self.in_row = True
            self.row = []
        elif self.in_row and tag in {"td", "th"}:
            self.in_cell = True
            self.cell_parts = []

    def handle_endtag(self, tag: str) -> None:
        if not self.in_table:
            return
        if tag in {"td", "th"} and self.in_cell:
            self.row.append(" ".join("".join(self.cell_parts).split()))
            self.in_cell = False
        elif tag == "tr" and self.in_row:
            if self.row:
                self.rows.append(self.row)
            self.in_row = False
        elif tag == "table":
            self.depth -= 1
            if self.depth == 0:
                self.in_table = False

    def handle_data(self, data: str) -> None:
        if self.in_cell:
            self.cell_parts.append(data)


def parse_scouting_table(html: str) -> dict[str, dict[str, dict[str, str | float | int] | None]]:
    parser = _ScoutingTableParser()
    parser.feed(html)
    rows = parser.rows
    if not rows or rows[0] != ["Scouting Report", "Offense", "Defense"]:
        raise ValueError("RotoWire scouting table header missing or changed")
    parsed: dict[str, dict] = {}
    for row in rows[1:]:
        if not row or row[0] not in FIELDS:
            raise ValueError(f"Unexpected RotoWire scouting row: {row!r}")
        if row[0] in parsed:
            raise ValueError(f"Duplicate RotoWire scouting row: {row[0]}")
        expected = 2 if row[0] == "Pace" else 3
        if len(row) != expected:
            raise ValueError(f"Incorrect cell count for {row[0]}: {row!r}")
        def cell(raw: str) -> dict[str, str | float | int]:
            match = CELL_RE.fullmatch(raw)
            if match is None:
                raise ValueError(f"Unexpected RotoWire scouting value: {raw!r}")
            return {"display": raw, "value": float(match.group(1)), "rank": int(match.group(2))}
        parsed[row[0]] = {"offense": cell(row[1]), "defense": cell(row[2]) if len(row) == 3 else None}
    if tuple(parsed) != FIELDS:
        raise ValueError(f"RotoWire scouting fields changed: {tuple(parsed)!r}")
    return parsed


def ensure_schema(conn) -> None:
    conn.execute("""
        CREATE TABLE IF NOT EXISTS rotowire_scouting_snapshots (
            id INTEGER PRIMARY KEY,
            pull_id TEXT NOT NULL,
            team_id INTEGER NOT NULL,
            team_abbreviation TEXT NOT NULL,
            source_url TEXT NOT NULL,
            fetched_at TEXT NOT NULL,
            fetched_date TEXT NOT NULL,
            page_sha256 TEXT NOT NULL,
            report_json TEXT NOT NULL,
            UNIQUE(pull_id, team_id)
        )
    """)
    conn.execute("""
        CREATE INDEX IF NOT EXISTS idx_rotowire_scouting_team_time
        ON rotowire_scouting_snapshots(team_id, fetched_at DESC)
    """)


def _fetch_html(url: str) -> str:
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=20) as response:
        return response.read().decode("utf-8", errors="replace")


def pull_scouting_reports(conn, *, fetch_html=None, now=None) -> dict:
    """Fetch all 15 pages and persist each successful real-page observation."""
    ensure_schema(conn)
    fetch = fetch_html or _fetch_html
    clock = now or (lambda: datetime.now(timezone.utc))
    pull_id = str(uuid.uuid4())
    captured = []
    failures = {}
    for abbreviation, slug in TEAM_SLUGS.items():
        url = BASE_URL + slug
        try:
            team = conn.execute("SELECT id FROM teams WHERE upper(abbreviation) = ?", (abbreviation,)).fetchone()
            if team is None:
                raise ValueError(f"Unknown local team: {abbreviation}")
            html = fetch(url)
            report = parse_scouting_table(html)
            fetched_at = clock().astimezone(timezone.utc).isoformat()
            conn.execute("""
                INSERT INTO rotowire_scouting_snapshots
                    (pull_id, team_id, team_abbreviation, source_url, fetched_at,
                     fetched_date, page_sha256, report_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (pull_id, int(team["id"]), abbreviation, url, fetched_at,
                  fetched_at[:10], hashlib.sha256(html.encode("utf-8")).hexdigest(),
                  json.dumps(report, sort_keys=True)))
            conn.commit()
            captured.append(abbreviation)
        except Exception as exc:
            failures[abbreviation] = str(exc)
    return {"pull_id": pull_id, "captured": captured, "failures": failures,
            "complete": len(captured) == len(TEAM_SLUGS)}
