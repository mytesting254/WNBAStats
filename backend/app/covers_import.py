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
from .game_resolver import resolve_or_create_game
from .odds_import import _backfill_provider_player_ids, sync_prop_lines_from_sportsbook
from .timezone_utils import APP_TIMEZONE, local_today_iso


PROVIDER = "covers"
RAW_CACHE_NAME = "covers_props_raw.json"
RAW_PAGE_CACHE_NAME = "covers_pages_raw.json"
COVERS_BASE_URL = "https://www.covers.com"
COVERS_MATCHUPS_URL = f"{COVERS_BASE_URL}/sports/wnba/matchups"
COVERS_ODDS_URL = f"{COVERS_BASE_URL}/sport/basketball/wnba/odds"
LOCAL_TZ = APP_TIMEZONE
FETCH_TIMEOUT_SECONDS = 12
COVERS_CACHE_TTL_SECONDS = 15 * 60
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
    "total_points_and_rebounds": "points_rebounds",
    "points_and_assists": "points_assists",
    "rebounds_and_assists": "rebounds_assists",
    "total_points_rebounds_and_assists": "points_rebounds_assists",
    "total_steals": "steals",
    "total_blocks": "blocks",
    "total_steals_and_blocks": "blocks_steals",
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
    home_moneyline: float | None = None
    away_moneyline: float | None = None
    away_spread: float | None = None
    away_spread_price: float | None = None
    home_spread_price: float | None = None
    over_total: float | None = None
    over_price: float | None = None
    under_total: float | None = None
    under_price: float | None = None
    records: dict | None = None


def import_covers_props(
    conn: sqlite3.Connection,
    selected_date: str | None = None,
    force_refresh: bool = False,
    sync_props: bool = True,
    update_game_markets: bool = True,
) -> dict:
    captured_at = datetime.now(timezone.utc).isoformat()
    cached_payload = read_json_cache(RAW_CACHE_NAME)
    if selected_date is None and not force_refresh and _covers_cache_is_current(cached_payload):
        result = _replace_covers_rows(
            conn,
            cached_payload["rows"],
            cached_payload.get("games", []),
            update_game_markets=update_game_markets,
        )
        synced = 0
        sync_error = None
        if sync_props:
            try:
                synced = sync_prop_lines_from_sportsbook(conn)
            except sqlite3.OperationalError as exc:
                sync_error = str(exc)
        return {
            **result,
            "synced_props": synced,
            "status": "loaded_from_cache",
            "source": "cache",
            "message": "Loaded Covers props from saved JSON.",
            "sync_error": sync_error,
        }

    try:
        games = covers_matchup_links(selected_date)
    except Exception as exc:
        if isinstance(cached_payload, dict) and cached_payload.get("rows"):
            result = _replace_covers_rows(
                conn,
                cached_payload["rows"],
                cached_payload.get("games", []),
                update_game_markets=update_game_markets,
            )
            return {
                **result,
                "synced_props": 0,
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
    odds_board = _fetch_odds_board(selected_date)
    imported_rows = []
    metadata_rows: list[CoversMetadata] = []
    errors = []
    raw_games: list[dict] = []
    for game in games:
        try:
            matchup_page = _fetch_text(game.matchup_url or game.odds_url.removesuffix("/odds"))
            odds_page = _fetch_text(game.odds_url)
            fragments = _fetch_market_fragments(odds_page)
            raw_games.append(
                {
                    "event_id": game.event_id,
                    "odds_url": game.odds_url,
                    "matchup_url": game.matchup_url,
                    "matchup_page": matchup_page,
                    "odds_page": odds_page,
                    "market_fragments": fragments,
                }
            )
            metadata = _metadata_from_page(conn, game, matchup_page, fallback_page=odds_page)
            if odds_board:
                metadata = _merge_moneylines_from_board(metadata, odds_board)
            metadata_rows.append(metadata)
            market_html = odds_page + "".join(fragments)
            imported_rows.extend(_event_rows(metadata, market_html, captured_at))
        except Exception as exc:
            errors.append({"event_id": game.event_id, "error": str(exc)})
            continue

    if raw_games:
        write_json_cache(
            RAW_PAGE_CACHE_NAME,
            {
                "provider": PROVIDER,
                "selected_date": selected_date,
                "cache_date": _today_local(),
                "captured_at": captured_at,
                "events": len(raw_games),
                "games": raw_games,
            },
        )

    row_payload = [_tuple_to_row(row) for row in imported_rows]
    game_payload = [_metadata_to_row(row) for row in metadata_rows]
    if not row_payload:
        if game_payload:
            if update_game_markets:
                _update_covers_game_markets(conn, game_payload)
            conn.commit()
            return {
                "events": len(game_payload),
                "imported": 0,
                "captured_at": captured_at,
                "synced_props": 0,
                "status": "imported_game_markets_only",
                "source": PROVIDER,
                "message": "Imported Covers matchup lines without player prop rows.",
                "errors": errors,
            }
        if isinstance(cached_payload, dict) and cached_payload.get("rows"):
            result = _replace_covers_rows(
                conn,
                cached_payload["rows"],
                cached_payload.get("games", []),
                update_game_markets=update_game_markets,
            )
            return {
                **result,
                "synced_props": 0,
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
    result = _replace_covers_rows(
        conn,
        row_payload,
        game_payload,
        update_game_markets=update_game_markets,
    )
    synced = 0
    sync_error = None
    if sync_props:
        try:
            synced = sync_prop_lines_from_sportsbook(conn)
        except sqlite3.OperationalError as exc:
            sync_error = str(exc)
    return {
        **result,
        "synced_props": synced,
        "status": "imported",
        "source": PROVIDER,
        "captured_at": captured_at,
        "errors": errors,
        "sync_error": sync_error,
    }


def _replace_covers_rows(
    conn: sqlite3.Connection,
    row_payload: list[dict],
    game_payload: list[dict] | None = None,
    *,
    update_game_markets: bool = True,
) -> dict:
    rows = [_row_to_tuple(row) for row in row_payload if isinstance(row, dict)]
    if update_game_markets:
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
    provider_player_ids_filled = _backfill_provider_player_ids(conn, provider=PROVIDER)
    conn.commit()
    return {
        "events": len({row[1] for row in rows}),
        "imported": len(rows),
        "captured_at": max((row[15] for row in rows), default=None),
        "provider_player_ids_filled": provider_player_ids_filled,
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


def _fetch_odds_board(selected_date: str | None) -> dict[tuple[str, str], dict[str, float]] | None:
    if selected_date and selected_date != _today_local():
        return None
    try:
        page = _fetch_text(COVERS_ODDS_URL)
    except Exception:
        return None
    return _moneylines_from_odds_board(page)


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
    if teams_match:
        away_team = _clean_text(teams_match.group("away"))
        home_team = _clean_text(teams_match.group("home"))
    else:
        fallback_teams = _historical_teams_from_page(page) or (_historical_teams_from_page(fallback_page) if fallback_page else None)
        if not fallback_teams:
            raise ValueError(f"Unable to read Covers teams for matchup {game.event_id}")
        away_team, home_team = fallback_teams
    start_match = re.search(r'"startDate":\s*"(?P<start>[^"]+)"', page)
    if not start_match and fallback_page:
        start_match = re.search(r'"startDate":\s*"(?P<start>[^"]+)"', fallback_page)
    if start_match:
        commence_time = _parse_covers_start(start_match.group("start"))
    else:
        commence_time = _historical_start_from_page(page) or (_historical_start_from_page(fallback_page) if fallback_page else None)
    if not commence_time:
        raise ValueError(f"Unable to read Covers start time for matchup {game.event_id}")
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
        market.get("home_moneyline"),
        market.get("away_moneyline"),
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
        home_moneyline=market.get("home_moneyline"),
        away_moneyline=market.get("away_moneyline"),
        away_spread=market.get("away_spread"),
        away_spread_price=market.get("away_spread_price"),
        home_spread_price=market.get("home_spread_price"),
        over_total=market.get("over_total"),
        over_price=market.get("over_price"),
        under_total=market.get("under_total"),
        under_price=market.get("under_price"),
        records=_records_from_page(page),
    )


def _merge_moneylines_from_board(
    metadata: CoversMetadata,
    odds_board: dict[tuple[str, str], dict[str, float]],
) -> CoversMetadata:
    away_abbr = _covers_abbreviation(metadata.away_team)
    home_abbr = _covers_abbreviation(metadata.home_team)
    if not away_abbr or not home_abbr:
        return metadata
    moneylines = odds_board.get((away_abbr, home_abbr))
    if not moneylines:
        return metadata
    return CoversMetadata(
        event_id=metadata.event_id,
        game_id=metadata.game_id,
        game_date=metadata.game_date,
        commence_time=metadata.commence_time,
        home_team=metadata.home_team,
        away_team=metadata.away_team,
        spread_home=metadata.spread_home,
        game_total=metadata.game_total,
        home_moneyline=moneylines.get("home_moneyline"),
        away_moneyline=moneylines.get("away_moneyline"),
        away_spread=metadata.away_spread,
        away_spread_price=metadata.away_spread_price,
        home_spread_price=metadata.home_spread_price,
        over_total=metadata.over_total,
        over_price=metadata.over_price,
        under_total=metadata.under_total,
        under_price=metadata.under_price,
        records=metadata.records,
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
        "points and assists": "points_and_assists",
        "points + assists": "points_and_assists",
        "rebounds and assists": "rebounds_and_assists",
        "rebounds + assists": "rebounds_and_assists",
        "total steals": "total_steals",
        "total blocks": "total_blocks",
        "total steals and blocks": "total_steals_and_blocks",
        "total steals + blocks": "total_steals_and_blocks",
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
        + r'-(?P<side>over|under)-(?P<book>[^"]+)"[^>]*>[\s\S]*?'
        r'<span class="[^"]*fs-12[^"]*">(?P<line>[ou][0-9]+(?:\.[0-9]+)?)</span>[\s\S]*?'
        r'<span class="[^"]*americanOdds[^"]*">(?P<price>[^<]+)</span>'
        ,
        re.I,
    )
    for match in pattern.finditer(player_html):
        line = float(match.group("line")[1:])
        price = int(_clean_text(match.group("price")).replace("+", ""))
        book_key = match.group("book")
        rows.append((match.group("side"), line, price, book_key, BOOK_NAMES.get(book_key, _book_title(book_key))))
    return rows


def _game_market_from_page(page: str, home_team: str, away_team: str) -> dict[str, float | None]:
    text = _visible_text(page)
    historical_market = _historical_market_from_text(text, home_team, away_team)
    if historical_market["spread_home"] is not None or historical_market["game_total"] is not None:
        return historical_market

    structured_market = _game_market_from_odds_html(page, home_team, away_team)
    if any(value is not None for value in structured_market.values()):
        return structured_market

    away_abbr = _covers_abbreviation(away_team)
    home_abbr = _covers_abbreviation(home_team)
    if not away_abbr or not home_abbr:
        return {"spread_home": None, "game_total": None, "home_moneyline": None, "away_moneyline": None}

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
        return {"spread_home": None, "game_total": None, "home_moneyline": None, "away_moneyline": None}
    match = matches[-1]

    away_total = _parse_float(match.group("away_total"))
    home_total = _parse_float(match.group("home_total"))
    game_total = away_total if away_total == home_total else (away_total or home_total)
    return {
        "spread_home": _parse_float(match.group("home_spread")),
        "game_total": game_total,
        "home_moneyline": None,
        "away_moneyline": None,
    }


def _historical_teams_from_page(page: str | None) -> tuple[str, str] | None:
    if not page:
        return None
    text = _visible_text(page)
    block = _historical_betting_block(text)
    if block:
        matchup = re.search(
            r"(?P<away_abbr>[A-Z]{2,3})\s+(?P<away>[A-Za-z .'-]+?)\s+[+-]\d+(?:\.\d+)?[\s\S]*?"
            r"(?P<home_abbr>[A-Z]{2,3})\s+(?P<home>[A-Za-z .'-]+?)\s+[+-]\d+(?:\.\d+)?",
            block,
        )
        if matchup:
            return matchup.group("away_abbr").upper(), matchup.group("home_abbr").upper()
    heading = re.search(
        r"(?P<away>[A-Za-z .'-]+?)\s+vs\s+(?P<home>[A-Za-z .'-]+?)\s+Results,\s+Match Player Stats\s*&\s*Records",
        text,
        re.I,
    )
    if not heading:
        return None
    away_team = _clean_text(heading.group("away"))
    home_team = _clean_text(heading.group("home"))
    return away_team, home_team


def _historical_market_from_text(text: str, home_team: str, away_team: str) -> dict[str, float | None]:
    away_abbr = _covers_abbreviation(away_team)
    home_abbr = _covers_abbreviation(home_team)
    if not away_abbr or not home_abbr:
        return {"spread_home": None, "game_total": None, "home_moneyline": None, "away_moneyline": None}
    block = _historical_betting_block(text)
    if not block:
        return {"spread_home": None, "game_total": None, "home_moneyline": None, "away_moneyline": None}

    away_body = _team_market_body(block, away_abbr, home_abbr)
    home_body = _team_market_body(block, home_abbr, None)
    away_spread = _first_spread_value(away_body)
    home_spread = _first_spread_value(home_body)
    away_total = _first_total_value(away_body)
    home_total = _first_total_value(home_body)
    game_total = away_total if away_total == home_total else (away_total or home_total)
    return {
        "spread_home": home_spread,
        "game_total": game_total,
        "home_moneyline": None,
        "away_moneyline": None,
        "away_spread": away_spread,
        "away_spread_price": None,
        "home_spread_price": None,
        "over_total": away_total,
        "over_price": None,
        "under_total": home_total,
        "under_price": None,
    }


def _historical_betting_block(text: str) -> str | None:
    match = re.search(
        r"Betting Information Team ATS \(Margin\) O/U \(Margin\)\s+(?P<body>.*?)\s+Line Movement",
        text,
        re.I,
    )
    if not match:
        return None
    return _clean_text(match.group("body"))


def _historical_start_from_page(page: str | None) -> str | None:
    if not page:
        return None
    text = _visible_text(page)
    match = re.search(
        r"(?P<time>\d{1,2}:\d{2}\s*[AP]M)\s+ET\s+·\s+(?P<date>[A-Za-z]{3,9}\s+\d{1,2},\s+\d{4})",
        text,
        re.I,
    )
    if not match:
        return None
    cleaned = f"{match.group('date')} {match.group('time').upper()}"
    parsed = datetime.strptime(cleaned, "%B %d, %Y %I:%M%p").replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc).isoformat()


def _team_market_body(block: str, team_abbr: str, next_team_abbr: str | None) -> str:
    pattern = rf"\b{re.escape(team_abbr)}\b\s+[A-Za-z .'-]+?(?P<body>.*)"
    match = re.search(pattern, block)
    if not match:
        return ""
    body = match.group("body")
    if next_team_abbr:
        next_match = re.search(rf"\b{re.escape(next_team_abbr)}\b\s+[A-Za-z .'-]+", body)
        if next_match:
            body = body[: next_match.start()]
    return _clean_text(body)


def _first_spread_value(value: str) -> float | None:
    match = re.search(r"([+-]\d+(?:\.\d+)?)", value)
    return _parse_float(match.group(1)) if match else None


def _first_total_value(value: str) -> float | None:
    match = re.search(r"(\d+(?:\.\d+)?)[ou]\b", value, re.I)
    return _parse_float(match.group(1)) if match else None


def _game_market_from_odds_html(page: str, home_team: str, away_team: str) -> dict[str, float | None]:
    away_abbr = _covers_abbreviation(away_team)
    home_abbr = _covers_abbreviation(home_team)
    if not away_abbr or not home_abbr:
        return {"spread_home": None, "game_total": None, "home_moneyline": None, "away_moneyline": None}

    away_row_match = re.search(
        rf'<div class="team-row away-row">[\s\S]*?<span class="team-ShortName">\s*{re.escape(away_abbr)}\s*</span>[\s\S]*?'
        r'<span class="team-odds spread">(?P<away_spread>[+-]?\d+(?:\.\d+)?)</span>[\s\S]*?'
        r'<span class="team-odds totals">[ou](?P<away_total>\d+(?:\.\d+)?)</span>',
        page,
        re.I,
    )
    home_row_match = re.search(
        rf'<div class="team-row home-row">[\s\S]*?<span class="team-ShortName">\s*{re.escape(home_abbr)}\s*</span>[\s\S]*?'
        r'<span class="team-odds spread">(?P<home_spread>[+-]?\d+(?:\.\d+)?)</span>[\s\S]*?'
        r'<span class="team-odds totals">[ou](?P<home_total>\d+(?:\.\d+)?)</span>',
        page,
        re.I,
    )

    spread_section = _market_table_section(page, "Spread")
    total_section = _market_table_section(page, "Total")
    moneyline_section = _market_table_section(page, "Moneyline")

    spread_match = re.search(
        r'<thead>[\s\S]*?'
        r'<th[^>]*>\s*(?P<away>[A-Z]{2,5})\s*</th>[\s\S]*?'
        r'<th[^>]*>\s*(?P<home>[A-Z]{2,5})\s*</th>[\s\S]*?</thead>[\s\S]*?<tbody>[\s\S]*?<tr[^>]*>[\s\S]*?'
        r'<td[^>]*>[\s\S]*?<span class="fw-bold fs-12">(?P<away_line>[^<]+)</span>[\s\S]*?'
        r'<span class="fw-bold americanOdds fs-13">(?P<away_price>[^<]+)</span>[\s\S]*?</td>[\s\S]*?'
        r'<td[^>]*>[\s\S]*?<span class="fw-bold fs-12">(?P<home_line>[^<]+)</span>[\s\S]*?'
        r'<span class="fw-bold americanOdds fs-13">(?P<home_price>[^<]+)</span>[\s\S]*?</td>',
        spread_section or "",
        re.I,
    )
    total_match = re.search(
        r'<thead>[\s\S]*?'
        r'<th[^>]*>\s*OVER\s*</th>[\s\S]*?'
        r'<th[^>]*>\s*UNDER\s*</th>[\s\S]*?</thead>[\s\S]*?<tbody>[\s\S]*?<tr[^>]*>[\s\S]*?'
        r'<td[^>]*>[\s\S]*?<span class="fw-bold fs-12">(?P<over_line>[^<]+)</span>[\s\S]*?'
        r'<span class="fw-bold americanOdds fs-13">(?P<over_price>[^<]+)</span>[\s\S]*?</td>[\s\S]*?'
        r'<td[^>]*>[\s\S]*?<span class="fw-bold fs-12">(?P<under_line>[^<]+)</span>[\s\S]*?'
        r'<span class="fw-bold americanOdds fs-13">(?P<under_price>[^<]+)</span>[\s\S]*?</td>',
        total_section or "",
        re.I,
    )
    moneyline_match = re.search(
        r'<thead>[\s\S]*?'
        r'<th[^>]*>\s*(?P<away>[A-Z]{2,5})\s*</th>[\s\S]*?'
        r'<th[^>]*>\s*(?P<home>[A-Z]{2,5})\s*</th>[\s\S]*?</thead>[\s\S]*?<tbody>[\s\S]*?<tr[^>]*>[\s\S]*?'
        r'<td[^>]*>[\s\S]*?<span class="fw-bold fs-12">(?P<away_ml>[^<]+)</span>[\s\S]*?</td>[\s\S]*?'
        r'<td[^>]*>[\s\S]*?<span class="fw-bold fs-12">(?P<home_ml>[^<]+)</span>[\s\S]*?</td>',
        moneyline_section or "",
        re.I,
    )

    away_moneyline = None
    home_moneyline = None
    if moneyline_match:
        if moneyline_match.group("away").upper() == away_abbr and moneyline_match.group("home").upper() == home_abbr:
            away_moneyline = _parse_moneyline(moneyline_match.group("away_ml"))
            home_moneyline = _parse_moneyline(moneyline_match.group("home_ml"))

    away_spread = _parse_market_line(spread_match.group("away_line")) if spread_match and spread_match.group("away").upper() == away_abbr else None
    spread_home = _parse_market_line(spread_match.group("home_line")) if spread_match and spread_match.group("home").upper() == home_abbr else None
    away_spread_price = _parse_moneyline(spread_match.group("away_price")) if spread_match and spread_match.group("away").upper() == away_abbr else None
    home_spread_price = _parse_moneyline(spread_match.group("home_price")) if spread_match and spread_match.group("home").upper() == home_abbr else None
    over_total = _parse_market_line(total_match.group("over_line")) if total_match else None
    under_total = _parse_market_line(total_match.group("under_line")) if total_match else None
    over_price = _parse_moneyline(total_match.group("over_price")) if total_match else None
    under_price = _parse_moneyline(total_match.group("under_price")) if total_match else None
    if over_total is not None and under_total is not None and abs(over_total - under_total) > 5:
        normalized_total = max(over_total, under_total)
        over_total = normalized_total
        under_total = normalized_total
    game_total = over_total if over_total == under_total else (over_total or under_total)

    return {
        "spread_home": spread_home if spread_home is not None else (_parse_float(home_row_match.group("home_spread")) if home_row_match else None),
        "game_total": game_total,
        "home_moneyline": home_moneyline,
        "away_moneyline": away_moneyline,
        "away_spread": away_spread,
        "away_spread_price": away_spread_price,
        "home_spread_price": home_spread_price,
        "over_total": over_total,
        "over_price": over_price,
        "under_total": under_total,
        "under_price": under_price,
    }


def _moneylines_from_odds_board(page: str) -> dict[tuple[str, str], dict[str, float]]:
    text = _visible_text(page)
    marker = "Moneyline Odds Table"
    start = text.find(marker)
    if start < 0:
        return {}
    table_text = text[start:]
    pattern = re.compile(
        r"(?:Today,?|[A-Z][a-z]{2}\s+\d{1,2},?)\s+\d{1,2}:\d{2}\s+"
        r"(?P<away>[A-Z]{2,5})\s+(?P<home>[A-Z]{2,5})\s+"
        r"(?P<away_ml>[+-]\d+)\s+\S+\s+\S+\s+"
        r"(?P<home_ml>[+-]\d+)\s+\S+\s+\S+\s+"
        r"Odds\s*&\s*Props",
        re.S,
    )
    results: dict[tuple[str, str], dict[str, float]] = {}
    for match in pattern.finditer(table_text):
        results[(match.group("away").upper(), match.group("home").upper())] = {
            "away_moneyline": float(match.group("away_ml")),
            "home_moneyline": float(match.group("home_ml")),
        }
    return results


def _records_from_page(page: str) -> dict:
    return {
        "team_table": _parse_team_table(page),
        "head_to_head": _parse_h2h_rows(_table_by_caption(page, "Head-To-Head")),
        "away_last_10": _parse_team_rows(_team_last_10_table(page, "away")),
        "home_last_10": _parse_team_rows(_team_last_10_table(page, "home")),
    }


def _parse_team_table(page: str) -> list[dict]:
    text = _visible_text(page)
    pattern = re.compile(
        r"Team\s+Table\s+Team\s+Record\s+ATS\s+O/U\s+Away\s+Home\s+"
        r"(?P<t1>[A-Z]{2,5})\s+(?P<r1>\d+-\d+)\s+(?P<ats1>\d+-\d+-\d+)\s+(?P<ou1>\d+-\d+-\d+)\s+(?P<a1>\d+-\d+)\s+(?P<h1>\d+-\d+)\s+"
        r"(?P<t2>[A-Z]{2,5})\s+(?P<r2>\d+-\d+)\s+(?P<ats2>\d+-\d+-\d+)\s+(?P<ou2>\d+-\d+-\d+)\s+(?P<a2>\d+-\d+)\s+(?P<h2>\d+-\d+)",
        re.I,
    )
    match = pattern.search(text)
    if not match:
        return []
    return [
        {
            "team": match.group("t1").upper(),
            "record": match.group("r1"),
            "ats": match.group("ats1"),
            "ou": match.group("ou1"),
            "away": match.group("a1"),
            "home": match.group("h1"),
        },
        {
            "team": match.group("t2").upper(),
            "record": match.group("r2"),
            "ats": match.group("ats2"),
            "ou": match.group("ou2"),
            "away": match.group("a2"),
            "home": match.group("h2"),
        },
    ]


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


def _parse_moneyline(value: str | None) -> float | None:
    if value is None:
        return None
    try:
        return float(html.unescape(value).replace("+", "").strip())
    except ValueError:
        return None


def _parse_market_line(value: str | None) -> float | None:
    if value is None:
        return None
    cleaned = html.unescape(value).strip()
    if cleaned and cleaned[0].lower() in {"o", "u"}:
        cleaned = cleaned[1:]
    return _parse_float(cleaned)


def _market_table_section(page: str, title: str) -> str | None:
    match = re.search(
        rf'<h2 class="fs-9">{re.escape(title)}</h2>[\s\S]*?<table class="w-100 m-0 bg-white">[\s\S]*?</table>',
        page,
        re.I,
    )
    if not match:
        return None
    return match.group(0)


def _match_or_create_local_game(
    conn: sqlite3.Connection,
    home_team: str,
    away_team: str,
    game_date: str,
    commence_time: str,
    spread_home: float | None = None,
    game_total: float | None = None,
    home_moneyline: float | None = None,
    away_moneyline: float | None = None,
) -> int | None:
    return resolve_or_create_game(
        conn,
        home_team=home_team,
        away_team=away_team,
        start_time=commence_time,
        game_date=game_date,
        spread_home=spread_home,
        game_total=game_total,
        home_moneyline=home_moneyline,
        away_moneyline=away_moneyline,
    )


def _update_covers_game_markets(conn: sqlite3.Connection, game_payload: list[dict]) -> None:
    for row in game_payload:
        if not isinstance(row, dict) or row.get("game_id") is None:
            continue
        _update_game_market(
            conn,
            int(row["game_id"]),
            _coerce_float(row.get("spread_home")),
            _coerce_float(row.get("game_total")),
            _coerce_float(row.get("home_moneyline")),
            _coerce_float(row.get("away_moneyline")),
        )


def _update_game_market(
    conn: sqlite3.Connection,
    game_id: int,
    spread_home: float | None,
    game_total: float | None,
    home_moneyline: float | None,
    away_moneyline: float | None,
) -> None:
    updates = []
    params = []
    if spread_home is not None:
        updates.append("spread_home = ?")
        params.append(spread_home)
    if game_total is not None and game_total > 0:
        updates.append("game_total = ?")
        params.append(game_total)
    if home_moneyline is not None:
        updates.append("home_moneyline = ?")
        params.append(home_moneyline)
    if away_moneyline is not None:
        updates.append("away_moneyline = ?")
        params.append(away_moneyline)
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
    captured_at_raw = payload.get("captured_at")
    if not isinstance(captured_at_raw, str):
        return False
    try:
        captured_at = datetime.fromisoformat(captured_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return False
    if captured_at.tzinfo is None:
        captured_at = captured_at.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - captured_at.astimezone(timezone.utc) <= timedelta(seconds=COVERS_CACHE_TTL_SECONDS)


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
        "home_moneyline": row.home_moneyline,
        "away_moneyline": row.away_moneyline,
        "away_spread": row.away_spread,
        "away_spread_price": row.away_spread_price,
        "home_spread_price": row.home_spread_price,
        "over_total": row.over_total,
        "over_price": row.over_price,
        "under_total": row.under_total,
        "under_price": row.under_price,
        "records": row.records,
    }


def _today_local() -> str:
    return local_today_iso()


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
