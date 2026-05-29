from __future__ import annotations

import html
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.request import Request, urlopen

from .bootstrap import normalize_team_abbreviation
from .cache import read_json_cache, write_json_cache


ROTOWIRE_LINEUPS_URL = "https://www.rotowire.com/wnba/lineups.php"
RAW_CACHE_NAME = "rotowire_lineups_raw.json"
ROSTER_SNAPSHOT_CACHE_NAME = "rotowire_roster_snapshot.json"
FETCH_TIMEOUT_SECONDS = 12
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
}
UNAVAILABLE_STATUSES = {"OUT", "GTD", "DOUBTFUL", "QUESTIONABLE"}


def import_rotowire_lineups(conn: sqlite3.Connection, force_refresh: bool = False) -> dict:
    cached_payload = read_json_cache(RAW_CACHE_NAME)
    use_cache = not force_refresh and _cache_is_current(conn, cached_payload)
    used_fallback_cache = False
    fetch_error: str | None = None
    if use_cache:
        rows = _cached_rows(cached_payload)
        captured_at = str(cached_payload["captured_at"])
        source = "cache"
    else:
        try:
            page = _fetch_text(ROTOWIRE_LINEUPS_URL)
            captured_at = datetime.now(timezone.utc).isoformat()
            rows = _parse_lineup_injuries(page)
            write_json_cache(
                RAW_CACHE_NAME,
                {
                    "source": "rotowire",
                    "url": ROTOWIRE_LINEUPS_URL,
                    "captured_at": captured_at,
                    "cache_date": datetime.now(timezone.utc).date().isoformat(),
                    "rows": rows,
                },
            )
            source = "rotowire"
        except Exception as exc:
            fallback_rows = _cached_rows(cached_payload) if isinstance(cached_payload, dict) else []
            fallback_captured_at = str(cached_payload.get("captured_at") or "") if isinstance(cached_payload, dict) else ""
            if fallback_rows and fallback_captured_at:
                rows = fallback_rows
                captured_at = fallback_captured_at
                source = "cache"
                use_cache = True
                used_fallback_cache = True
                fetch_error = str(exc)
            else:
                raise

    _update_roster_snapshot_cache(rows, captured_at, source)

    inserted = 0
    unresolved = 0
    unresolved_names: list[str] = []
    skipped_stale = 0
    for row in rows:
        if row["status"] not in UNAVAILABLE_STATUSES:
            continue
        team_abbr = normalize_team_abbreviation(row["team"])
        if not team_abbr:
            unresolved += 1
            unresolved_names.append(f"{row['team']}:{row['player_name']}")
            continue
        player_id = _resolve_player_id(conn, team_abbr, row["player_name"])
        if not player_id:
            unresolved += 1
            unresolved_names.append(f"{team_abbr}:{row['player_name']}")
            continue
        latest = conn.execute(
            "SELECT captured_at FROM injuries WHERE player_id = ? ORDER BY captured_at DESC LIMIT 1",
            (player_id,),
        ).fetchone()
        if latest and str(latest["captured_at"]) >= captured_at:
            skipped_stale += 1
            continue
        conn.execute(
            """
            INSERT INTO injuries (player_id, status, note, captured_at)
            VALUES (?, ?, ?, ?)
            """,
            (player_id, row["status"].lower(), f"rotowire lineups ({row['team']})", captured_at),
        )
        inserted += 1
    conn.commit()
    return {
        "source": source,
        "captured_at": captured_at,
        "parsed_rows": len(rows),
        "inserted": inserted,
        "unresolved": unresolved,
        "skipped_stale": skipped_stale,
        "from_cache": use_cache,
        "used_fallback_cache": used_fallback_cache,
        "fetch_error": fetch_error,
        "ttl_seconds": _refresh_ttl_seconds(conn),
        "unresolved_examples": unresolved_names[:10],
    }


def _update_roster_snapshot_cache(rows: list[dict[str, str]], captured_at: str, source: str) -> None:
    normalized_rows = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        team = str(row.get("team") or "").strip().upper()
        player_name = str(row.get("player_name") or "").strip()
        status = str(row.get("status") or "").strip().upper()
        if not team or not player_name or not status:
            continue
        normalized_rows.append({
            "team": team,
            "player_name": player_name,
            "status": status,
        })
    normalized_rows.sort(key=lambda item: (item["team"], item["player_name"], item["status"]))

    previous = read_json_cache(ROSTER_SNAPSHOT_CACHE_NAME)
    previous_rows = previous.get("rows", []) if isinstance(previous, dict) else []
    previous_changed_at = previous.get("changed_at") if isinstance(previous, dict) else None
    changed = normalized_rows != previous_rows
    changed_at = captured_at if changed else (previous_changed_at or captured_at)

    write_json_cache(
        ROSTER_SNAPSHOT_CACHE_NAME,
        {
            "provider": "rotowire",
            "source": source,
            "captured_at": captured_at,
            "changed_at": changed_at,
            "changed": changed,
            "rows": normalized_rows,
        },
    )


def _parse_lineup_injuries(page: str) -> list[dict[str, str]]:
    lines = _lineup_text_lines(page)
    rows: list[dict[str, str]] = []
    i = 0
    current_teams: tuple[str, str] | None = None
    lineup_section_index = 0
    active_team: str | None = None
    in_may_not_play = False
    while i < len(lines):
        line = lines[i]
        matchup = _matchup_pair(line)
        if not matchup and i + 1 < len(lines):
            maybe_away = line.strip().upper()
            maybe_home = lines[i + 1].strip().upper()
            if _is_team_code(maybe_away) and _is_team_code(maybe_home):
                matchup = (maybe_away, maybe_home)
                i += 1
        if matchup:
            current_teams = matchup
            lineup_section_index = 0
            active_team = None
            in_may_not_play = False
            i += 1
            continue
        if line in {"Confirmed Lineup", "Expected Lineup"} and current_teams is not None:
            active_team = current_teams[min(lineup_section_index, 1)]
            lineup_section_index += 1
            in_may_not_play = False
            i += 1
            continue
        if line == "MAY NOT PLAY":
            in_may_not_play = True
            i += 1
            continue
        if in_may_not_play:
            if line in {"Confirmed Lineup", "Expected Lineup"}:
                in_may_not_play = False
                continue
            if _matchup_pair(line) or _is_time_line(line):
                in_may_not_play = False
                continue
            injury_match = re.match(r"^(?P<player>.+?)\s+(?P<status>OUT|GTD|DOUBTFUL|QUESTIONABLE|PROBABLE)$", line, re.I)
            if injury_match and active_team:
                rows.append(
                    {
                        "team": active_team,
                        "player_name": injury_match.group("player").strip(),
                        "status": injury_match.group("status").upper(),
                    }
                )
                i += 1
                continue
            # Rotowire now often renders MAY NOT PLAY rows as three lines:
            # position (G/F/C), then player name, then status.
            if re.fullmatch(r"[A-Z]{1,3}", line):
                player_line = lines[i + 1].strip() if i + 1 < len(lines) else ""
                status_line = lines[i + 2].strip().upper() if i + 2 < len(lines) else ""
                if player_line and status_line in UNAVAILABLE_STATUSES and active_team:
                    rows.append(
                        {
                            "team": active_team,
                            "player_name": player_line,
                            "status": status_line,
                        }
                    )
                    i += 3
                    continue
        i += 1
    return rows


def _lineup_text_lines(page: str) -> list[str]:
    lineups_section = _lineups_section_html(page)
    text = html.unescape(lineups_section)
    text = re.sub(r"(?i)</(li|div|p|section|article|tr|td|h1|h2|h3|h4|h5|h6|caption)>", "\n", text)
    text = re.sub(r"(?i)<br\s*/?>", "\n", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\r", "\n", text)
    text = re.sub(r"[ \t]+", " ", text)
    return [line.strip() for line in text.split("\n") if line.strip()]


def _lineups_section_html(page: str) -> str:
    start = re.search(r'<div class="lineups"[^>]*>', page, re.I)
    if not start:
        return page
    end = re.search(r"</main>", page[start.start():], re.I)
    if not end:
        return page[start.start():]
    return page[start.start(): start.start() + end.end()]


def _matchup_pair(line: str) -> tuple[str, str] | None:
    match = re.match(r"^([A-Z]{2,3})\s+([A-Z]{2,3})$", line)
    if not match:
        return None
    return match.group(1), match.group(2)


def _is_team_code(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Z]{2,3}", value))


def _is_time_line(line: str) -> bool:
    return bool(re.match(r"^\d{1,2}:\d{2}\s+[AP]M\s+ET$", line))


def _resolve_player_id(conn: sqlite3.Connection, team_abbreviation: str, player_name: str) -> int | None:
    row = conn.execute(
        """
        SELECT p.id
        FROM players p
        JOIN teams t ON t.id = p.team_id
        WHERE lower(p.full_name) = lower(?)
          AND upper(t.abbreviation) = ?
        LIMIT 1
        """,
        (player_name, team_abbreviation.upper()),
    ).fetchone()
    if row:
        return int(row["id"])

    abbreviated = re.match(r"^(?P<initial>[A-Za-z])[.\s]+\s*(?P<last>[A-Za-z][A-Za-z' -]+)$", player_name)
    if abbreviated:
        like_pattern = f"{abbreviated.group('initial').upper()}% {abbreviated.group('last').strip()}"
        row = conn.execute(
            """
            SELECT p.id
            FROM players p
            JOIN teams t ON t.id = p.team_id
            WHERE p.full_name LIKE ?
              AND upper(t.abbreviation) = ?
            LIMIT 1
            """,
            (like_pattern, team_abbreviation.upper()),
        ).fetchone()
        if row:
            return int(row["id"])
    return None


def _fetch_text(url: str) -> str:
    request = Request(url, headers=REQUEST_HEADERS)
    with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
        return response.read().decode("utf-8", errors="replace")


def _cache_is_current(conn: sqlite3.Connection, payload: object) -> bool:
    if not isinstance(payload, dict):
        return False
    if not isinstance(payload.get("rows"), list) or not payload.get("captured_at"):
        return False
    try:
        captured_at = datetime.fromisoformat(str(payload["captured_at"]).replace("Z", "+00:00"))
    except ValueError:
        return False
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    ttl_seconds = _refresh_ttl_seconds(conn)
    return datetime.now(timezone.utc) - captured_at.astimezone(timezone.utc) <= timedelta(seconds=ttl_seconds)


def _refresh_ttl_seconds(conn: sqlite3.Connection) -> int:
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date().isoformat()
    starts = conn.execute(
        """
        SELECT start_time
        FROM games
        WHERE status = 'scheduled'
          AND game_date = ?
        ORDER BY start_time
        """,
        (today_utc,),
    ).fetchall()
    parsed_starts = []
    for row in starts:
        try:
            parsed = datetime.fromisoformat(str(row["start_time"]).replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=timezone.utc)
            parsed_starts.append(parsed.astimezone(timezone.utc))
        except ValueError:
            continue
    upcoming = [item for item in parsed_starts if item >= now_utc]
    if not upcoming:
        return 3600
    first_tipoff = min(upcoming)
    if first_tipoff - now_utc <= timedelta(hours=3):
        return 900
    return 3600


def _cached_rows(payload: dict) -> list[dict[str, str]]:
    rows = payload.get("rows", [])
    if not isinstance(rows, list):
        return []
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        team = str(row.get("team") or "").strip()
        player_name = str(row.get("player_name") or "").strip()
        status = str(row.get("status") or "").upper().strip()
        if not team or not player_name or not status:
            continue
        normalized.append({"team": team, "player_name": player_name, "status": status})
    return normalized
