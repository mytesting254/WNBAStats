from __future__ import annotations

import json
import os
import sqlite3
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
from urllib.request import urlopen

from dotenv import load_dotenv

from .bootstrap import ensure_team
from .cache import read_json_cache, write_json_cache
from .projections import rebuild_predictions


SPORT_KEY = "basketball_wnba"
PROVIDER = "the_odds_api"
BASE_URL = "https://api.the-odds-api.com/v4"
LOCAL_TZ = timezone(timedelta(hours=-4))
DEFAULT_REGIONS = "us"
DEFAULT_BOOKMAKERS = "draftkings,fanduel,betmgm,caesars,espnbet,fanatics,betrivers"
RAW_CACHE_NAME = "sportsbook_props_raw.json"
COMPLETED_GAME_GRACE_HOURS = 4

MARKETS = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_threes": "threes",
    "player_points_rebounds_assists": "points_rebounds_assists",
    "player_steals": "steals",
    "player_blocks": "blocks",
    "player_blocks_steals": "blocks_steals",
}

TEAM_ALIASES = {
    "atlanta dream": "ATL",
    "chicago sky": "CHI",
    "connecticut sun": "CON",
    "dallas wings": "DAL",
    "golden state valkyries": "GS",
    "indiana fever": "IND",
    "las vegas aces": "LV",
    "los angeles sparks": "LA",
    "minnesota lynx": "MIN",
    "new york liberty": "NY",
    "phoenix mercury": "PHX",
    "portland fire": "POR",
    "seattle storm": "SEA",
    "toronto tempo": "TOR",
    "washington mystics": "WSH",
}


def import_the_odds_api_props(conn: sqlite3.Connection, force_refresh: bool = False) -> dict:
    cached_payload = read_json_cache(RAW_CACHE_NAME)
    if cached_payload and not force_refresh:
        result = _replace_sportsbook_rows(conn, cached_payload, datetime.now(timezone.utc).isoformat())
        synced = sync_prop_lines_from_sportsbook(conn)
        return {
            **result,
            "synced_props": synced,
            "status": "loaded_from_cache",
            "source": "cache",
            "message": "Loaded sportsbook props from saved JSON. Use force_refresh=true to fetch fresh odds.",
        }

    load_dotenv()
    api_key = os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY")
    if not api_key:
        return {
            "status": "missing_api_key",
            "message": "Set ODDS_API_KEY to import live sportsbook player props.",
            "imported": 0,
        }

    captured_at = datetime.now(timezone.utc).isoformat()
    markets = ",".join(MARKETS)
    events = _fetch_json(f"{BASE_URL}/sports/{SPORT_KEY}/events?{urlencode({'apiKey': api_key})}")
    fetched_payload = []

    for event in events:
        event_id = event["id"]
        event_odds = _fetch_json(
            f"{BASE_URL}/sports/{SPORT_KEY}/events/{event_id}/odds?"
            + urlencode(
                {
                    "apiKey": api_key,
                    "regions": os.getenv("ODDS_API_REGIONS", DEFAULT_REGIONS),
                    "bookmakers": os.getenv("ODDS_API_BOOKMAKERS", DEFAULT_BOOKMAKERS),
                    "markets": markets,
                    "oddsFormat": "american",
                }
            )
        )
        fetched_payload.append(event_odds)

    merged_payload = _merge_event_cache(cached_payload, fetched_payload)
    write_json_cache(RAW_CACHE_NAME, merged_payload)
    result = _replace_sportsbook_rows(conn, merged_payload, captured_at)
    synced = sync_prop_lines_from_sportsbook(conn)
    return {
        **result,
        "synced_props": synced,
        "status": "imported",
        "source": "provider",
        "fetched_events": len(fetched_payload),
        "cached_events": len(merged_payload),
        "captured_at": captured_at,
    }


def sync_prop_lines_from_sportsbook(conn: sqlite3.Connection) -> int:
    rows = conn.execute(
        """
        SELECT
            spl.game_id,
            spl.player_name,
            p.id AS player_id,
            CASE
                WHEN COUNT(DISTINCT spl.sportsbook) = 1 THEN MAX(spl.sportsbook)
                ELSE 'Best Available'
            END AS sportsbook,
            spl.market,
            spl.line,
            MAX(CASE WHEN spl.side = 'over' THEN spl.price END) AS over_odds,
            MAX(CASE WHEN spl.side = 'under' THEN spl.price END) AS under_odds,
            MAX(spl.captured_at) AS captured_at
        FROM sportsbook_prop_lines spl
        JOIN players p ON (
            (spl.provider_player_id IS NOT NULL AND p.id = spl.provider_player_id)
            OR lower(p.full_name) = lower(spl.player_name)
        )
        WHERE spl.game_id IS NOT NULL
          AND (
              spl.provider = 'covers'
              OR NOT EXISTS (
                  SELECT 1
                  FROM sportsbook_prop_lines covers
                  WHERE covers.provider = 'covers'
                    AND covers.game_id = spl.game_id
              )
          )
          AND EXISTS (
              SELECT 1
              FROM player_game_stats stats
              WHERE stats.player_id = p.id
          )
        GROUP BY spl.game_id, spl.player_name, p.id, spl.market, spl.line
        HAVING over_odds IS NOT NULL AND under_odds IS NOT NULL
        ORDER BY spl.game_id, spl.player_name, spl.market, spl.line
        """
    ).fetchall()
    tracked_keys = {
        (
            int(row["game_id"]),
            int(row["player_id"]),
            row["market"],
            float(row["line"]),
        )
        for row in conn.execute(
            """
            SELECT pl.game_id, pl.player_id, pl.market, pl.line
            FROM prop_lines pl
            JOIN games g ON g.id = pl.game_id
            LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
            WHERE sp.id IS NOT NULL
               OR g.status <> 'scheduled'
            """
        ).fetchall()
    }
    insert_rows = [
        (
            row["game_id"],
            row["player_id"],
            row["sportsbook"],
            row["market"],
            row["line"],
            int(row["over_odds"]),
            int(row["under_odds"]),
            row["captured_at"],
        )
        for row in rows
        if (
            int(row["game_id"]),
            int(row["player_id"]),
            row["market"],
            float(row["line"]),
        )
        not in tracked_keys
    ]

    conn.execute(
        """
        DELETE FROM prop_predictions
        WHERE prop_line_id IN (
            SELECT pl.id
            FROM prop_lines pl
            JOIN games g ON g.id = pl.game_id
            LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
            WHERE sp.id IS NULL
              AND g.status = 'scheduled'
        )
        """
    )
    conn.execute(
        """
        DELETE FROM prop_lines
        WHERE id IN (
            SELECT pl.id
            FROM prop_lines pl
            JOIN games g ON g.id = pl.game_id
            LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
            WHERE sp.id IS NULL
              AND g.status = 'scheduled'
        )
        """
    )
    conn.executemany(
        """
        INSERT INTO prop_lines (
            game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        insert_rows,
    )
    rebuild_predictions(conn)
    return len(rows)


def _merge_event_cache(cached_payload: object, fetched_payload: list[dict]) -> list[dict]:
    merged: dict[str, dict] = {}
    if isinstance(cached_payload, list):
        for event in cached_payload:
            if isinstance(event, dict) and event.get("id"):
                merged[str(event["id"])] = event
    for event in fetched_payload:
        if event.get("id"):
            merged[str(event["id"])] = event
    return sorted(merged.values(), key=lambda event: str(event.get("commence_time", "")))


def _replace_sportsbook_rows(conn: sqlite3.Connection, raw_payload: list[dict], captured_at: str) -> dict:
    imported_rows = []
    for event_odds in raw_payload:
        imported_rows.extend(_event_rows(conn, event_odds, captured_at))
    conn.execute("DELETE FROM sportsbook_prop_lines WHERE provider = ?", (PROVIDER,))
    conn.executemany(
        """
        INSERT INTO sportsbook_prop_lines (
            provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
            bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        imported_rows,
    )
    conn.commit()
    return {
        "events": len(raw_payload),
        "imported": len(imported_rows),
        "captured_at": captured_at,
    }


def _event_rows(conn: sqlite3.Connection, event_odds: dict, captured_at: str) -> list[tuple]:
    imported_rows = []
    event_id = event_odds["id"]
    game_id = _match_or_create_local_game(conn, event_odds)
    game_date = _game_date(event_odds["commence_time"])
    for bookmaker in event_odds.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            app_market = MARKETS.get(market.get("key"))
            if not app_market:
                continue
            over_by_player: dict[tuple[str, float], dict] = {}
            under_by_player: dict[tuple[str, float], dict] = {}
            for outcome in market.get("outcomes", []):
                player_name = outcome.get("description") or outcome.get("name")
                side = str(outcome.get("name", "")).lower()
                line = outcome.get("point")
                price = outcome.get("price")
                if not player_name or side not in {"over", "under"} or line is None or price is None:
                    continue
                key = (str(player_name), float(line))
                target = over_by_player if side == "over" else under_by_player
                target[key] = {
                    "player_name": str(player_name),
                    "side": side,
                    "line": float(line),
                    "price": int(price),
                }
            for target in (over_by_player, under_by_player):
                for item in target.values():
                    imported_rows.append(
                        (
                            PROVIDER,
                            event_id,
                            game_id,
                            game_date,
                            event_odds["commence_time"],
                            event_odds["home_team"],
                            event_odds["away_team"],
                            bookmaker["key"],
                            bookmaker["title"],
                            market["key"],
                            app_market,
                            item["player_name"],
                            item["side"],
                            item["line"],
                            item["price"],
                            captured_at,
                        )
                    )
    return imported_rows


def list_sportsbook_props(conn: sqlite3.Connection, game_id: int | None = None) -> list[dict]:
    where = "WHERE game_id = ?" if game_id is not None else ""
    params = (game_id,) if game_id is not None else ()
    rows = conn.execute(
        f"""
        SELECT *
        FROM sportsbook_prop_lines
        {where}
        ORDER BY commence_time, player_name, market, side, sportsbook
        """,
        params,
    ).fetchall()
    payload = [dict(row) for row in rows]
    if game_id is not None:
        return payload
    cutoff = datetime.now(timezone.utc) - timedelta(hours=COMPLETED_GAME_GRACE_HOURS)
    return [
        row for row in payload
        if row.get("commence_time") and _parse_utc(str(row["commence_time"])) >= cutoff
    ]


def odds_cache_summary() -> dict:
    payload = read_json_cache(RAW_CACHE_NAME)
    if not isinstance(payload, list):
        return {"exists": False, "events": 0, "future_events": 0, "latest_commence_time": None}
    now = datetime.now(timezone.utc)
    future_events = [
        event for event in payload
        if isinstance(event, dict)
        and event.get("commence_time")
        and _parse_utc(str(event["commence_time"])) >= now
    ]
    latest = max((str(event.get("commence_time")) for event in payload if isinstance(event, dict) and event.get("commence_time")), default=None)
    return {
        "exists": True,
        "events": len(payload),
        "future_events": len(future_events),
        "latest_commence_time": latest,
    }


def line_discrepancies(conn: sqlite3.Connection, game_id: int | None = None) -> list[dict]:
    props = list_sportsbook_props(conn, game_id)
    groups: dict[tuple, list[dict]] = {}
    for prop in props:
        key = (
            prop["game_id"],
            prop["away_team"],
            prop["home_team"],
            prop["commence_time"],
            prop["player_name"],
            prop["market"],
            prop["side"],
        )
        groups.setdefault(key, []).append(prop)

    discrepancies = []
    for key, rows in groups.items():
        if len(rows) < 2:
            continue
        lines = [float(row["line"]) for row in rows]
        prices = [int(row["price"]) for row in rows]
        line_gap = max(lines) - min(lines)
        price_gap = max(prices) - min(prices)
        if line_gap <= 0 and price_gap < 15:
            continue
        best_price = max(rows, key=lambda row: int(row["price"]))
        low_line = min(rows, key=lambda row: float(row["line"]))
        high_line = max(rows, key=lambda row: float(row["line"]))
        discrepancies.append(
            {
                "game_id": key[0],
                "matchup": f"{key[1]} at {key[2]}",
                "commence_time": key[3],
                "player_name": key[4],
                "market": key[5],
                "side": key[6],
                "books": len({row["sportsbook"] for row in rows}),
                "line_gap": round(line_gap, 2),
                "price_gap": price_gap,
                "best_price": {
                    "sportsbook": best_price["sportsbook"],
                    "line": best_price["line"],
                    "price": best_price["price"],
                },
                "low_line": {
                    "sportsbook": low_line["sportsbook"],
                    "line": low_line["line"],
                    "price": low_line["price"],
                },
                "high_line": {
                    "sportsbook": high_line["sportsbook"],
                    "line": high_line["line"],
                    "price": high_line["price"],
                },
                "book_lines": sorted(
                    [
                        {
                            "sportsbook": row["sportsbook"],
                            "line": row["line"],
                            "price": row["price"],
                        }
                        for row in rows
                    ],
                    key=lambda row: (float(row["line"]), str(row["sportsbook"])),
                ),
            }
        )
    return sorted(discrepancies, key=lambda item: (item["line_gap"], item["price_gap"]), reverse=True)


def _fetch_json(url: str) -> object:
    with urlopen(url, timeout=30) as response:
        return json.loads(response.read().decode("utf-8"))


def _match_or_create_local_game(conn: sqlite3.Connection, event: dict) -> int | None:
    home = TEAM_ALIASES.get(str(event.get("home_team", "")).lower())
    away = TEAM_ALIASES.get(str(event.get("away_team", "")).lower())
    if not home or not away:
        return None
    commence_time = _parse_utc(str(event["commence_time"]))
    rows = conn.execute(
        """
        SELECT g.id, g.start_time
        FROM games g
        JOIN teams home ON home.id = g.home_team_id
        JOIN teams away ON away.id = g.away_team_id
        WHERE home.abbreviation = ?
          AND away.abbreviation = ?
        ORDER BY g.start_time
        """,
        (home, away),
    ).fetchall()
    for row in rows:
        existing_start = _parse_game_start(str(row["start_time"]))
        if existing_start and abs((existing_start - commence_time).total_seconds()) < 60:
            return int(row["id"])

    home_team_id = ensure_team(conn, str(event.get("home_team", "")))
    away_team_id = ensure_team(conn, str(event.get("away_team", "")))
    if not home_team_id or not away_team_id:
        return None
    cursor = conn.execute(
        """
        INSERT INTO games (
            game_date, start_time, home_team_id, away_team_id, status,
            rest_days_home, rest_days_away, spread_home, game_total
        ) VALUES (?, ?, ?, ?, 'scheduled', 2, 2, NULL, NULL)
        """,
        (game_date, event["commence_time"], home_team_id, away_team_id),
    )
    return int(cursor.lastrowid)


def _game_date(commence_time: str) -> str:
    value = commence_time.replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(LOCAL_TZ).date().isoformat()


def _parse_utc(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)


def _parse_game_start(value: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc)
