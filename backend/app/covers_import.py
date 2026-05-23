from __future__ import annotations

import html
import re
import sqlite3
import subprocess
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from urllib.parse import urljoin
from urllib.error import URLError
from urllib.request import Request, urlopen

from .bootstrap import ensure_team, normalize_team_abbreviation
from .cache import read_json_cache, write_json_cache
from .odds_import import sync_prop_lines_from_sportsbook


PROVIDER = "covers"
RAW_CACHE_NAME = "covers_props_raw.json"
COVERS_BASE_URL = "https://www.covers.com"
COVERS_MATCHUPS_URL = f"{COVERS_BASE_URL}/sports/wnba/matchups"
LOCAL_TZ = timezone(timedelta(hours=-4))
FETCH_TIMEOUT_SECONDS = 12
REQUEST_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "close",
}

MARKET_SLUGS = {
    "points_scored": "points",
    "total_rebounds": "rebounds",
    "total_assists": "assists",
    "3-pointers_made": "threes",
    "total_points_rebounds_and_assists": "points_rebounds_assists",
}

BOOK_NAMES = {
    "bet365": "bet365",
    "betmgm": "BetMGM",
    "caesars": "Caesars",
    "draftkings": "DraftKings",
    "fanduel": "FanDuel",
    "fanatics_sportsbook": "Fanatics Sportsbook",
}

COVERS_TEAM_ABBREVIATIONS = {
    "PHX": "PHO",
    "POR": "PDX",
    "WSH": "WAS",
}

ROW_COLUMNS = [
    "provider",
    "provider_event_id",
    "game_id",
    "game_date",
    "commence_time",
    "home_team",
    "away_team",
    "bookmaker_key",
    "sportsbook",
    "market_key",
    "market",
    "player_name",
    "side",
    "line",
    "price",
    "captured_at",
]


@dataclass(frozen=True)
class CoversGame:
    event_id: str
    odds_url: str
    matchup_url: str | None = None


@dataclass(frozen=True)
class CoversMetadata:
    event_id: str
    game_id: int | None
    game_date: str
    commence_time: str
    home_team: str
    away_team: str
    spread_home: float | None = None
    game_total: float | None = None
    records: dict | None = None


def import_covers_props(
    conn: sqlite3.Connection,
    selected_date: str | None = None,
    force_refresh: bool = False,
) -> dict:
    captured_at = datetime.now(timezone.utc).isoformat()
    cached_payload = read_json_cache(RAW_CACHE_NAME)
    if selected_date is None and not force_refresh and _covers_cache_is_current(cached_payload):
        result = _replace_covers_rows(conn, cached_payload["rows"], cached_payload.get("games", []))
        synced = sync_prop_lines_from_sportsbook(conn)
        return {
            **result,
            "synced_props": synced,
            "status": "loaded_from_cache",
            "source": "cache",
            "message": "Loaded Covers props from saved JSON. Use Refresh Covers for a fresh scrape.",
        }

    try:
        games = covers_matchup_links(selected_date)
    except Exception as exc:
        if not force_refresh and isinstance(cached_payload, dict) and cached_payload.get("rows"):
            result = _replace_covers_rows(conn, cached_payload["rows"], cached_payload.get("games", []))
            synced = sync_prop_lines_from_sportsbook(conn)
            return {
                **result,
                "synced_props": synced,
                "status": "loaded_from_cache",
                "source": "cache",
                "message": f"Fresh Covers scrape failed while loading matchups; kept saved Covers data. {exc}",
            }
        return {
            "events": 0,
            "imported": 0,
            "captured_at": None,
            "synced_props": 0,
            "status": "failed",
            "source": PROVIDER,
            "message": f"Fresh Covers scrape failed while loading matchups. {exc}",
        }
    imported_rows = []
    metadata_rows: list[CoversMetadata] = []
    errors = []
    for game in games:
        try:
            matchup_page = _fetch_text(game.matchup_url or game.odds_url.removesuffix("/odds"))
            odds_page = _fetch_text(game.odds_url)
            metadata = _metadata_from_page(conn, game, matchup_page, fallback_page=odds_page)
            metadata_rows.append(metadata)
            market_html = odds_page + "".join(_fetch_market_fragments(odds_page))
            imported_rows.extend(_event_rows(metadata, market_html, captured_at))
        except Exception as exc:
            errors.append({"event_id": game.event_id, "error": str(exc)})
            continue

    row_payload = [_tuple_to_row(row) for row in imported_rows]
    game_payload = [_metadata_to_row(row) for row in metadata_rows]
    if not row_payload:
        if not force_refresh and isinstance(cached_payload, dict) and cached_payload.get("rows"):
            result = _replace_covers_rows(conn, cached_payload["rows"], cached_payload.get("games", []))
            synced = sync_prop_lines_from_sportsbook(conn)
            return {
                **result,
                "synced_props": synced,
                "status": "loaded_from_cache",
                "source": "cache",
                "message": "Fresh Covers scrape returned no prop rows; kept saved Covers data.",
                "errors": errors,
            }
        return {
            "events": len(games),
            "imported": 0,
            "captured_at": None,
            "synced_props": 0,
            "status": "failed",
            "source": PROVIDER,
            "message": "Fresh Covers scrape returned no prop rows.",
            "errors": errors,
        }
    write_json_cache(
        RAW_CACHE_NAME,
        {
            "provider": PROVIDER,
            "selected_date": selected_date,
            "cache_date": _today_local(),
            "captured_at": captured_at,
            "events": len(games),
            "rows": row_payload,
            "games": game_payload,
        },
    )
    result = _replace_covers_rows(conn, row_payload, game_payload)
    synced = sync_prop_lines_from_sportsbook(conn)
    return {
        **result,
        "synced_props": synced,
        "status": "imported",
        "source": PROVIDER,
        "captured_at": captured_at,
        "errors": errors,
    }


def _replace_covers_rows(conn: sqlite3.Connection, row_payload: list[dict], game_payload: list[dict] | None = None) -> dict:
    rows = [_row_to_tuple(row) for row in row_payload if isinstance(row, dict)]
    _update_covers_game_markets(conn, game_payload or [])
    conn.execute("DELETE FROM sportsbook_prop_lines WHERE provider = ?", (PROVIDER,))
    conn.executemany(
        """
        INSERT INTO sportsbook_prop_lines (
            provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
            bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows,
    )
    conn.commit()
    return {
        "events": len({row[1] for row in rows}),
        "imported": len(rows),
        "captured_at": max((row[15] for row in rows), default=None),
    }


def covers_matchup_links(selected_date: str | None = None) -> list[CoversGame]:
    url = COVERS_MATCHUPS_URL
    if selected_date:
        url = f"{url}?selectedDate={selected_date}"
    page = _fetch_text(url)
    seen = set()
    games = []
    for href, event_id in _covers_matchup_hrefs(page):
        if event_id in seen:
            continue
        seen.add(event_id)
        odds_url = urljoin(COVERS_BASE_URL, href)
        if not odds_url.endswith("/odds"):
            odds_url = odds_url.rstrip("/") + "/odds"
        games.append(CoversGame(event_id=event_id, odds_url=odds_url, matchup_url=odds_url.removesuffix("/odds")))
    return games


def _covers_matchup_hrefs(page: str) -> list[tuple[str, str]]:
    matches: list[tuple[str, str]] = []
    for match in re.finditer(
        r'href=(?:"|\')(?P<href>/sports?/basketball/wnba/matchup/(?P<id>\d+)(?:/odds)?)(?:"|\')',
        page,
        re.I,
    ):
        matches.append((match.group("href"), match.group("id")))
    return matches


def _metadata_from_page(conn: sqlite3.Connection, game: CoversGame, page: str, fallback_page: str | None = None) -> CoversMetadata:
    teams_match = re.search(r"Game Odds\s+(?P<away>.+?)\s+vs\.\s+(?P<home>.+?)\s*</caption>", page, re.S)
    if not teams_match:
        teams_match = re.search(r'"name":\s*"(?P<away>.+?)\s+vs\s+(?P<home>.+?)"', page)
    if not teams_match and fallback_page:
        teams_match = re.search(r"Game Odds\s+(?P<away>.+?)\s+vs\.\s+(?P<home>.+?)\s*</caption>", fallback_page, re.S)
        if not teams_match:
            teams_match = re.search(r'"name":\s*"(?P<away>.+?)\s+vs\s+(?P<home>.+?)"', fallback_page)
    if not teams_match:
        raise ValueError(f"Unable to read Covers teams for matchup {game.event_id}")

    away_team = _clean_text(teams_match.group("away"))
    home_team = _clean_text(teams_match.group("home"))
    start_match = re.search(r'"startDate":\s*"(?P<start>[^"]+)"', page)
    if not start_match and fallback_page:
        start_match = re.search(r'"startDate":\s*"(?P<start>[^"]+)"', fallback_page)
    if not start_match:
        raise ValueError(f"Unable to read Covers start time for matchup {game.event_id}")
    commence_time = _parse_covers_start(start_match.group("start"))
    game_date = commence_time[:10]
    market = _game_market_from_page(page, home_team, away_team)
    if fallback_page:
        fallback_market = _game_market_from_page(fallback_page, home_team, away_team)
        if fallback_market["spread_home"] is not None or fallback_market["game_total"] is not None:
            market = fallback_market
    game_id = _match_or_create_local_game(
        conn,
        home_team,
        away_team,
        game_date,
        commence_time,
        market.get("spread_home"),
        market.get("game_total"),
    )
    return CoversMetadata(
        event_id=game.event_id,
        game_id=game_id,
        game_date=game_date,
        commence_time=commence_time,
        home_team=home_team,
        away_team=away_team,
        spread_home=market.get("spread_home"),
        game_total=market.get("game_total"),
        records=_records_from_page(page),
    )


def _fetch_market_fragments(page: str) -> list[str]:
    fragments = []
    for match in re.finditer(r'<article id="\d+" data-url="(?P<url>[^"]+)"[\s\S]*?<h2[^>]*>(?P<title>[^<]+)</h2>', page):
        market = _market_from_title(match.group("title"))
        if not market or market not in MARKET_SLUGS:
            continue
        try:
            fragments.append(_fetch_text(urljoin(COVERS_BASE_URL, html.unescape(match.group("url")))))
        except Exception:
            continue
    return fragments


def _event_rows(metadata: CoversMetadata, page: str, captured_at: str) -> list[tuple]:
    rows = []
    seen = set()
    for article in _article_sections(page):
        market_key = _market_key_from_article(article)
        market = MARKET_SLUGS.get(market_key)
        if not market:
            continue
        for player_html in _player_sections(article):
            player_name = _player_name(player_html)
            if not player_name:
                continue
            for side, line, price, book_key, book_name in _odds_from_player(player_html, market_key):
                key = (metadata.event_id, player_name, market, side, line, price, book_key)
                if key in seen:
                    continue
                seen.add(key)
                rows.append(
                    (
                        PROVIDER,
                        metadata.event_id,
                        metadata.game_id,
                        metadata.game_date,
                        metadata.commence_time,
                        metadata.home_team,
                        metadata.away_team,
                        book_key,
                        book_name,
                        market_key,
                        market,
                        player_name,
                        side,
                        line,
                        price,
                        captured_at,
                    )
                )
    return rows


def _article_sections(page: str) -> list[str]:
    matches = list(re.finditer(r'<article id="\d+"', page))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(page)
        sections.append(page[match.start():end])
    return sections


def _player_sections(article: str) -> list[str]:
    matches = list(re.finditer(r'class="playerContainer\b', article))
    sections = []
    for index, match in enumerate(matches):
        end = matches[index + 1].start() if index + 1 < len(matches) else len(article)
        start = max(article.rfind("<a ", 0, match.start()), 0)
        sections.append(article[start:end])
    return sections


def _market_key_from_article(article: str) -> str | None:
    title_match = re.search(r"<h2[^>]*>(?P<title>[^<]+)</h2>", article)
    if title_match:
        return _market_from_title(title_match.group("title"))
    link_match = re.search(r"matchup-odds-(?:best_odds|compare_odds)-click-wnba-(?P<market>.+?)-(?:over|under)-", article)
    return link_match.group("market") if link_match else None


def _market_from_title(title: str) -> str | None:
    normalized = _clean_text(title).lower()
    return {
        "points scored": "points_scored",
        "total rebounds": "total_rebounds",
        "total assists": "total_assists",
        "3-pointers made": "3-pointers_made",
        "3 pointers made": "3-pointers_made",
        "total points and rebounds": "total_points_and_rebounds",
        "total points + rebounds": "total_points_and_rebounds",
        "total points rebounds and assists": "total_points_rebounds_and_assists",
        "total points + rebounds + assists": "total_points_rebounds_and_assists",
    }.get(normalized)


def _player_name(player_html: str) -> str | None:
    match = re.search(r'<span class="category[^"]*">\s*(?P<name>[^<]+?)\s*</span>', player_html)
    return _clean_text(match.group("name")) if match else None


def _odds_from_player(player_html: str, market_key: str) -> list[tuple[str, float, int, str, str]]:
    rows = []
    pattern = re.compile(
        r'data-linkcont="matchup-odds-(?:best_odds|compare_odds)-click-wnba-'
        + re.escape(market_key)
        + r'-(?P<side>over|under)-(?P<book>[^"]+)">[\s\S]*?'
        r'<span class="fw-bold fs-12">(?P<line>[ou][0-9]+(?:\.[0-9]+)?)</span>\s*'
        r'<span class="fw-bold americanOdds fs-13">(?P<price>[^<]+)</span>'
    )
    for match in pattern.finditer(player_html):
        line = float(match.group("line")[1:])
        price = int(_clean_text(match.group("price")).replace("+", ""))
        book_key = match.group("book")
        rows.append((match.group("side"), line, price, book_key, BOOK_NAMES.get(book_key, _book_title(book_key))))
    return rows


def _game_market_from_page(page: str, home_team: str, away_team: str) -> dict[str, float | None]:
    text = _visible_text(page)
    away_abbr = _covers_abbreviation(away_team)
    home_abbr = _covers_abbreviation(home_team)
    if not away_abbr or not home_abbr:
        return {"spread_home": None, "game_total": None}

    spread_pattern = re.compile(
        rf"\b{re.escape(away_abbr)}\b\s+(?:\d+\s+)?(?P<away_spread>[+-]?\d+(?:\.\d+)?)"
        rf"(?:\s+[+-]?\d+)?\s+o(?P<away_total>\d+(?:\.\d+)?)(?:\s+[+-]?\d+)?\b.*?"
        rf"\b{re.escape(home_abbr)}\b\s+(?:\d+\s+)?(?P<home_spread>[+-]?\d+(?:\.\d+)?)"
        rf"(?:\s+[+-]?\d+)?\s+"
        rf"u(?P<home_total>\d+(?:\.\d+)?)\b",
        re.I,
    )
    matches = list(spread_pattern.finditer(text))
    if not matches:
        return {"spread_home": None, "game_total": None}
    match = matches[-1]

    away_total = _parse_float(match.group("away_total"))
    home_total = _parse_float(match.group("home_total"))
    game_total = away_total if away_total == home_total else (away_total or home_total)
    return {
        "spread_home": _parse_float(match.group("home_spread")),
        "game_total": game_total,
    }


def _records_from_page(page: str) -> dict:
    return {
        "head_to_head": _parse_h2h_rows(_table_by_caption(page, "Head-To-Head")),
        "away_last_10": _parse_team_rows(_team_last_10_table(page, "away")),
        "home_last_10": _parse_team_rows(_team_last_10_table(page, "home")),
    }


def _table_by_caption(page: str, caption: str) -> str | None:
    pattern = re.compile(
        rf"<table[^>]*>\s*<caption[^>]*>\s*{re.escape(caption)}\s*</caption>.*?</table>",
        re.I | re.S,
    )
    match = pattern.search(page)
    return match.group(0) if match else None


def _team_last_10_table(page: str, side: str) -> str | None:
    section = re.search(rf'<section class="{side}-team-section"[\s\S]*?</section>', page, re.I)
    return _table_by_caption(section.group(0), "Team - Last 10") if section else None


def _parse_h2h_rows(table: str | None) -> list[dict]:
    rows = []
    for cells in _table_cells(table):
        if len(cells) < 5:
            continue
        score = _score_from_cell(cells[2])
        if not score:
            continue
        rows.append(
            {
                "date": _clean_cell(cells[0]),
                "home": _clean_cell(cells[1]),
                "winner": _winner_from_cell(cells[2]),
                "score": score,
                "ats": _clean_cell(cells[3]),
                "total": _clean_cell(cells[4]),
            }
        )
    return rows[:10]


def _parse_team_rows(table: str | None) -> list[dict]:
    rows = []
    for cells in _table_cells(table):
        if len(cells) < 5:
            continue
        score = _score_from_cell(cells[2])
        if not score:
            continue
        rows.append(
            {
                "date": _clean_cell(cells[0]),
                "opponent": _opponent_from_cell(cells[1]),
                "location": "away" if re.search(r">\s*@\s*<", cells[1]) else "home",
                "result": _result_from_cell(cells[2]),
                "score": score,
                "ats": _clean_cell(cells[3]),
                "total": _clean_cell(cells[4]),
            }
        )
    return rows[:10]


def _table_cells(table: str | None) -> list[list[str]]:
    if not table:
        return []
    rows = []
    for row in re.findall(r"<tr[^>]*>(.*?)</tr>", table, re.I | re.S):
        cells = re.findall(r"<td[^>]*>(.*?)</td>", row, re.I | re.S)
        if cells:
            rows.append(cells)
    return rows


def _winner_from_cell(value: str) -> str | None:
    match = re.search(r"alt=\"(?P<winner>[^\"]+) logo\"", value, re.I)
    return _clean_text(match.group("winner")) if match else None


def _opponent_from_cell(value: str) -> str:
    match = re.search(r"<a[^>]*>(?P<opponent>[^<]+)</a>", value, re.I | re.S)
    return _clean_text(match.group("opponent")) if match else _clean_cell(value).replace("@", "").strip()


def _result_from_cell(value: str) -> str | None:
    match = re.search(r"<b>\s*(?P<result>[WLT])\s*</b>", value, re.I)
    return match.group("result").upper() if match else None


def _score_from_cell(value: str) -> str | None:
    match = re.search(r"(?P<score>\d+\s*-\s*\d+)", _clean_cell(value))
    return re.sub(r"\s+", " ", match.group("score")).strip() if match else None


def _clean_cell(value: str) -> str:
    return _clean_text(re.sub(r"<[^>]+>", " ", html.unescape(value)))


def _covers_abbreviation(team_name: str) -> str | None:
    abbreviation = normalize_team_abbreviation(team_name)
    if not abbreviation:
        return None
    return COVERS_TEAM_ABBREVIATIONS.get(abbreviation, abbreviation)


def _visible_text(page: str) -> str:
    without_tags = re.sub(r"<[^>]+>", " ", html.unescape(page))
    return _clean_text(without_tags)


def _parse_float(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(value.replace("+", ""))
    except ValueError:
        return None


def _match_or_create_local_game(
    conn: sqlite3.Connection,
    home_team: str,
    away_team: str,
    game_date: str,
    commence_time: str,
    spread_home: float | None = None,
    game_total: float | None = None,
) -> int | None:
    home_team_id = ensure_team(conn, home_team)
    away_team_id = ensure_team(conn, away_team)
    if not home_team_id or not away_team_id:
        return None

    rows = conn.execute(
        """
        SELECT id, start_time
        FROM games
        WHERE home_team_id = ?
          AND away_team_id = ?
        ORDER BY start_time
        """,
        (home_team_id, away_team_id),
    ).fetchall()
    parsed_start = _parse_game_start(commence_time)
    for row in rows:
        existing_start = _parse_game_start(str(row["start_time"]))
        if parsed_start and existing_start and abs((existing_start - parsed_start).total_seconds()) < 60:
            game_id = int(row["id"])
            _update_game_market(conn, game_id, spread_home, game_total)
            return game_id

    cursor = conn.execute(
        """
        INSERT INTO games (
            game_date, start_time, home_team_id, away_team_id, status,
            rest_days_home, rest_days_away, spread_home, game_total
        ) VALUES (?, ?, ?, ?, 'scheduled', 2, 2, NULL, NULL)
        """,
        (game_date, commence_time, home_team_id, away_team_id),
    )
    game_id = int(cursor.lastrowid)
    _update_game_market(conn, game_id, spread_home, game_total)
    return game_id


def _update_covers_game_markets(conn: sqlite3.Connection, game_payload: list[dict]) -> None:
    for row in game_payload:
        if not isinstance(row, dict) or row.get("game_id") is None:
            continue
        _update_game_market(
            conn,
            int(row["game_id"]),
            _coerce_float(row.get("spread_home")),
            _coerce_float(row.get("game_total")),
        )


def _update_game_market(conn: sqlite3.Connection, game_id: int, spread_home: float | None, game_total: float | None) -> None:
    updates = []
    params = []
    if spread_home is not None:
        updates.append("spread_home = ?")
        params.append(spread_home)
    if game_total is not None and game_total > 0:
        updates.append("game_total = ?")
        params.append(game_total)
    if not updates:
        return
    params.append(game_id)
    conn.execute(f"UPDATE games SET {', '.join(updates)} WHERE id = ?", params)


def _coerce_float(value) -> float | None:
    if value is None:
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _parse_covers_start(value: str) -> str:
    cleaned = _clean_text(value)
    parsed = datetime.strptime(cleaned, "%m/%d/%Y %H:%M:%S %z")
    return parsed.astimezone(timezone.utc).isoformat()


def _parse_game_start(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc)


def _covers_cache_is_current(payload: object) -> bool:
    if not isinstance(payload, dict) or payload.get("provider") != PROVIDER:
        return False
    rows = payload.get("rows")
    if not isinstance(rows, list) or not rows:
        return False
    return payload.get("cache_date") == _today_local()


def _tuple_to_row(row: tuple) -> dict:
    return dict(zip(ROW_COLUMNS, row))


def _row_to_tuple(row: dict) -> tuple:
    return tuple(row.get(column) for column in ROW_COLUMNS)


def _metadata_to_row(row: CoversMetadata) -> dict:
    return {
        "provider_event_id": row.event_id,
        "game_id": row.game_id,
        "game_date": row.game_date,
        "commence_time": row.commence_time,
        "home_team": row.home_team,
        "away_team": row.away_team,
        "spread_home": row.spread_home,
        "game_total": row.game_total,
        "records": row.records,
    }


def _today_local() -> str:
    return datetime.now(LOCAL_TZ).date().isoformat()


def _fetch_text(url: str) -> str:
    request = Request(url, headers=REQUEST_HEADERS)
    try:
        with urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            return response.read().decode("utf-8", errors="replace")
    except (TimeoutError, URLError, OSError) as exc:
        return _fetch_text_with_curl(url, exc)


def _fetch_text_with_curl(url: str, original_error: Exception) -> str:
    command = [
        "curl",
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--max-time",
        str(FETCH_TIMEOUT_SECONDS),
        "-H",
        f"User-Agent: {REQUEST_HEADERS['User-Agent']}",
        "-H",
        f"Accept: {REQUEST_HEADERS['Accept']}",
        "-H",
        f"Accept-Language: {REQUEST_HEADERS['Accept-Language']}",
        url,
    ]
    try:
        result = subprocess.run(command, check=True, capture_output=True, text=True, timeout=FETCH_TIMEOUT_SECONDS + 2)
        return result.stdout
    except (FileNotFoundError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as curl_error:
        raise RuntimeError(f"Unable to fetch {url}: {original_error}; curl fallback failed: {curl_error}") from curl_error


def _clean_text(value: str) -> str:
    return re.sub(r"\s+", " ", html.unescape(value)).strip()


def _book_title(value: str) -> str:
    return value.replace("_", " ").replace("-", " ").title()
