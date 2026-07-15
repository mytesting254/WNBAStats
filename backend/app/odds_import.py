from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import urlopen

from dotenv import load_dotenv

from .bootstrap import ensure_team
from .cache import read_json_cache, write_json_cache
from .db import sqlite_write_lock
from .game_resolver import resolve_or_create_game
from .paths import get_cache_dir
from .player_identity import normalize_player_lookup_name
from .projections import rebuild_predictions, rebuild_predictions_live
from .timezone_utils import APP_TIMEZONE, local_today_iso


SPORT_KEY = "basketball_wnba"
PROVIDER = "the_odds_api"
BASE_URL = "https://api.the-odds-api.com/v4"
LOCAL_TZ = APP_TIMEZONE
DEFAULT_REGIONS = "us"
DEFAULT_BOOKMAKERS = "draftkings,fanduel,betmgm,caesars,espnbet,fanatics,betrivers"
RAW_CACHE_NAME = "sportsbook_props_raw.json"
HISTORICAL_GAME_MARKETS_CACHE_PREFIX = "historical_game_markets_"
COMPLETED_GAME_GRACE_HOURS = 4
GAME_MARKETS = ("h2h", "spreads", "totals")
PLAYER_MARKET_BATCH_SIZE = 3

PLAYER_MARKETS = {
    "player_points": "points",
    "player_rebounds": "rebounds",
    "player_assists": "assists",
    "player_threes": "threes",
    "player_points_rebounds": "points_rebounds",
    "player_points_assists": "points_assists",
    "player_rebounds_assists": "rebounds_assists",
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


def _normalized_name_expr(column: str) -> str:
    return (
        "lower("
        "replace("
        "replace("
        "replace("
        "replace("
        f"replace({column}, ' ', ''),"
        "'''',"
        "''"
        "),"
        "'.',"
        "''"
        "),"
        "'-',"
        "''"
        "),"
        "'’',"
        "''"
        ")"
        ")"
    )


def _player_name_match_clause(player_column: str, provider_column: str) -> str:
    return (
        f"lower({player_column}) = lower({provider_column}) "
        f"OR {_normalized_name_expr(player_column)} = {_normalized_name_expr(provider_column)}"
    )


def _fuzzy_player_name_match(player_name: str, provider_name: str) -> bool:
    player_tokens = normalize_player_lookup_name(player_name).split()
    provider_tokens = normalize_player_lookup_name(provider_name).split()
    if not player_tokens or not provider_tokens:
        return False
    if player_tokens == provider_tokens:
        return True
    smaller, larger = (player_tokens, provider_tokens)
    if len(smaller) > len(larger):
        smaller, larger = larger, smaller
    if len(smaller) < 2 or smaller[0] != larger[0]:
        return False
    return set(smaller).issubset(set(larger))


@dataclass(frozen=True)
class SyncPropLinesResult:
    synced_props: int
    changed_props: int
    changed_prop_line_ids: list[int]
    touched_game_ids: list[int]


def import_the_odds_api_props(
    conn: sqlite3.Connection,
    force_refresh: bool = False,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
) -> dict:
    result = _import_the_odds_api_provider_rows(conn, force_refresh=force_refresh, progress_callback=progress_callback)
    if not bool(result.get("prop_sync_eligible")):
        result.setdefault("synced_props", 0)
        return result
    synced = 0
    sync_error = None
    try:
        synced = _sync_props_after_import(conn, progress_callback)
    except sqlite3.OperationalError as exc:
        sync_error = str(exc)
    return {
        **result,
        "synced_props": synced,
        "sync_error": sync_error,
    }


def import_historical_odds_api_game_markets(
    conn: sqlite3.Connection,
    *,
    selected_date: str,
    snapshot_time_utc: str = "16:00:00Z",
    force_refresh: bool = False,
) -> dict:
    load_dotenv()
    api_key = os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY")
    if not api_key:
        return {
            "status": "missing_api_key",
            "message": "Set ODDS_API_KEY to import historical Odds API game markets.",
            "selected_date": selected_date,
            "imported": 0,
        }

    snapshot_at = _historical_snapshot_at(selected_date, snapshot_time_utc)
    cache_name = f"{HISTORICAL_GAME_MARKETS_CACHE_PREFIX}{selected_date}_{snapshot_time_utc.replace(':', '').replace('Z', 'z')}.json"
    payload: object
    if not force_refresh:
        payload = read_json_cache(cache_name)
        if payload is None:
            payload = _fetch_historical_game_markets(api_key=api_key, snapshot_at=snapshot_at)
            write_json_cache(cache_name, payload)
    else:
        payload = _fetch_historical_game_markets(api_key=api_key, snapshot_at=snapshot_at)
        write_json_cache(cache_name, payload)

    events = _historical_payload_events(payload)
    fetched_events = 0
    matched_games = 0
    updated_games = 0
    skipped_events = 0
    event_errors: list[dict[str, str]] = []

    for event in events:
        if not isinstance(event, dict):
            skipped_events += 1
            continue
        commence_time = str(event.get("commence_time") or "")
        if not commence_time or _game_date(commence_time) != selected_date:
            continue
        fetched_events += 1
        game_id = _match_or_create_local_game(conn, event)
        if game_id is None:
            skipped_events += 1
            event_errors.append(
                {
                    "event_id": str(event.get("id") or ""),
                    "error": "Could not match event to a local game.",
                }
            )
            continue
        matched_games += 1
        before = conn.execute(
            """
            SELECT spread_home, game_total, home_moneyline, away_moneyline,
                   home_spread_price, away_spread_price, over_price, under_price
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()
        _update_game_market_from_event(conn, event, game_id)
        after = conn.execute(
            """
            SELECT spread_home, game_total, home_moneyline, away_moneyline,
                   home_spread_price, away_spread_price, over_price, under_price
            FROM games
            WHERE id = ?
            """,
            (game_id,),
        ).fetchone()
        if before is None or after is None:
            continue
        tracked_columns = (
            "spread_home",
            "game_total",
            "home_moneyline",
            "away_moneyline",
            "home_spread_price",
            "away_spread_price",
            "over_price",
            "under_price",
        )
        if any(before[column] != after[column] for column in tracked_columns):
            updated_games += 1

    conn.commit()
    return {
        "status": "imported",
        "source": "historical_odds_api",
        "selected_date": selected_date,
        "snapshot_at": snapshot_at,
        "fetched_events": fetched_events,
        "matched_games": matched_games,
        "updated_games": updated_games,
        "skipped_events": skipped_events,
        "imported": updated_games,
        "errors": event_errors,
        "cache_name": cache_name,
    }


def _import_the_odds_api_provider_rows(
    conn: sqlite3.Connection,
    *,
    force_refresh: bool = False,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
) -> dict:
    cached_payload = read_json_cache(RAW_CACHE_NAME)
    if not force_refresh:
        if cached_payload is None:
            return {
                "status": "missing_cache",
                "source": "cache",
                "message": "No saved Odds API cache found. Use Refresh Odds to fetch a new payload first.",
                "imported": 0,
                "prop_sync_eligible": False,
            }
        active_cached_events = _active_cached_events(cached_payload)
        if not active_cached_events:
            return {
                "status": "stale_cache",
                "source": "cache",
                "message": _stale_cache_message(cached_payload),
                "imported": 0,
                "cached_events": len(cached_payload) if isinstance(cached_payload, list) else 0,
                "prop_sync_eligible": False,
            }
        if progress_callback is not None:
            progress_callback("loading_saved_cache", 0, 1, "Loading saved Odds API cache.")
        result = _replace_sportsbook_rows(conn, cached_payload, datetime.now(timezone.utc).isoformat())
        if progress_callback is not None:
            progress_callback("loading_saved_cache", 1, 1, "Saved Odds API cache loaded.")
        return {
            **result,
            "status": "loaded_from_cache",
            "source": "cache",
            "message": "Loaded sportsbook props from saved JSON.",
            "prop_sync_eligible": True,
        }

    load_dotenv()
    api_key = os.getenv("ODDS_API_KEY") or os.getenv("THE_ODDS_API_KEY")
    if not api_key:
        return {
            "status": "missing_api_key",
            "message": "Set ODDS_API_KEY to import live sportsbook player props.",
            "imported": 0,
            "prop_sync_eligible": False,
        }

    captured_at = datetime.now(timezone.utc).isoformat()
    today = local_today_iso()
    events = _fetch_json(f"{BASE_URL}/sports/{SPORT_KEY}/events?{urlencode({'apiKey': api_key})}")
    fetched_payload = []
    errors: list[dict[str, str]] = []
    todays_events = [event for event in events if _is_today_event(event, today=today)]
    total_events = len(todays_events)
    if progress_callback is not None:
        progress_callback(
            "requesting_provider",
            0,
            max(total_events, 1),
            "Requesting today's Odds API event payloads.",
        )

    for index, event in enumerate(todays_events, start=1):
        event_id = event["id"]
        event_odds, event_errors = _fetch_event_odds(event_id, api_key=api_key)
        errors.extend({"event_id": str(event_id), "error": error} for error in event_errors)
        if event_odds is not None:
            fetched_payload.append(event_odds)
        if progress_callback is not None:
            progress_callback(
                "requesting_provider",
                index,
                max(total_events, 1),
                f"Fetched {index} of {total_events} Odds API event payloads.",
            )

    merged_payload = _merge_event_cache(cached_payload, fetched_payload)
    should_persist_payload = bool(fetched_payload) or not errors
    if should_persist_payload:
        write_json_cache(RAW_CACHE_NAME, merged_payload)
        persisted_payload = read_json_cache(RAW_CACHE_NAME)
    else:
        persisted_payload = cached_payload
    if progress_callback is not None:
        progress_callback(
            "requesting_provider",
            max(total_events, 1),
            max(total_events, 1),
            (
                "Saved raw Odds API payload to cache. Syncing sportsbook rows."
                if should_persist_payload
                else "Provider requests failed before any event payloads were saved. Keeping the previous raw cache."
            ),
        )
    result = _replace_sportsbook_rows(
        conn,
        persisted_payload if isinstance(persisted_payload, list) else merged_payload,
        captured_at,
    )
    status = "imported"
    message = None
    prop_sync_eligible = True
    if errors and fetched_payload:
        status = "partial_import"
        message = (
            f"Imported {len(fetched_payload)} event(s) with {len(errors)} provider error(s): "
            f"{_summarize_provider_errors(errors)}"
        )
    elif errors:
        status = "provider_error"
        message = f"Odds API import failed for {len(errors)} request(s): {_summarize_provider_errors(errors)}"
        prop_sync_eligible = False
    return {
        **result,
        "status": status,
        "source": "provider",
        "fetched_events": len(fetched_payload),
        "cached_events": len(merged_payload),
        "captured_at": captured_at,
        "errors": errors,
        "message": message,
        "prop_sync_eligible": prop_sync_eligible,
    }


def _sync_props_after_import(
    conn: sqlite3.Connection,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
) -> int:
    if progress_callback is None:
        return int(sync_prop_lines_from_sportsbook(conn))
    return int(
        sync_prop_lines_from_sportsbook(
            conn,
            progress_callback=lambda current, total, message: progress_callback(
                "syncing_props",
                current,
                total,
                message,
            ),
            rebuild_progress_callback=lambda current, total, message: progress_callback(
                "rebuilding_predictions",
                current,
                total,
                message,
            ),
        )
    )


def sync_prop_lines_from_sportsbook(
    conn: sqlite3.Connection,
    *,
    fast_fail: bool = False,
    game_ids: list[int] | tuple[int, ...] | None = None,
    rebuild_predictions_after: bool = True,
    include_change_details: bool = False,
    progress_callback: Callable[[int, int, str | None], None] | None = None,
    rebuild_progress_callback: Callable[[int, int, str | None], None] | None = None,
) -> int | SyncPropLinesResult:
    if progress_callback is not None:
        progress_callback(0, 1, "Collecting sportsbook props.")
    target_game_ids = sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0})
    game_filter = ""
    game_filter_params: tuple[int, ...] = ()
    if target_game_ids:
        placeholders = ",".join("?" for _ in target_game_ids)
        game_filter = f" AND spl.game_id IN ({placeholders})"
        game_filter_params = tuple(target_game_ids)
    rows = conn.execute(
        """
        SELECT
            spl.game_id,
            spl.player_name,
            p.id AS player_id,
            MAX(CASE WHEN spl.provider = 'covers' THEN 1 ELSE 0 END) AS from_covers,
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
            OR """
        + _player_name_match_clause("p.full_name", "spl.player_name")
        + """
        )
        WHERE spl.game_id IS NOT NULL
          AND EXISTS (
              SELECT 1
              FROM player_game_stats stats
              WHERE stats.player_id = p.id
          )
        """
        + game_filter
        + """
        GROUP BY spl.game_id, spl.player_name, p.id, spl.market, spl.line
        HAVING over_odds IS NOT NULL AND under_odds IS NOT NULL
        ORDER BY spl.game_id, spl.player_name, spl.market, spl.line
        """,
        game_filter_params,
    ).fetchall()
    preferred_covers_keys = {
        (
            int(row["game_id"]),
            int(row["player_id"]),
            str(row["market"]),
        )
        for row in rows
        if int(row["from_covers"] or 0) == 1
    }
    candidate_rows = [
        row
        for row in rows
        if not (
            (
                int(row["game_id"]),
                int(row["player_id"]),
                str(row["market"]),
            )
            in preferred_covers_keys
            and int(row["from_covers"] or 0) != 1
        )
    ]
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
            + (
                f" AND pl.game_id IN ({','.join('?' for _ in target_game_ids)})"
                if target_game_ids
                else ""
            ),
            tuple(target_game_ids) if target_game_ids else (),
        ).fetchall()
    }
    desired_rows = {
        (
            int(row["game_id"]),
            int(row["player_id"]),
            str(row["market"]),
            float(row["line"]),
        ): {
            "game_id": int(row["game_id"]),
            "player_id": int(row["player_id"]),
            "sportsbook": str(row["sportsbook"]),
            "market": str(row["market"]),
            "line": float(row["line"]),
            "over_odds": int(row["over_odds"]),
            "under_odds": int(row["under_odds"]),
            "captured_at": row["captured_at"],
        }
        for row in candidate_rows
        if (
            int(row["game_id"]),
            int(row["player_id"]),
            str(row["market"]),
            float(row["line"]),
        )
        not in tracked_keys
    }
    touched_game_ids = target_game_ids or sorted({int(row["game_id"]) for row in rows if row["game_id"] is not None})
    if progress_callback is not None:
        progress_callback(len(rows), max(len(rows), 1), f"Matched {len(rows)} sportsbook props across {len(touched_game_ids)} games.")
    existing_rows = _open_scheduled_prop_line_rows(conn, touched_game_ids)
    existing_by_key = {
        (
            int(row["game_id"]),
            int(row["player_id"]),
            str(row["market"]),
            float(row["line"]),
        ): row
        for row in existing_rows
        if (
            int(row["game_id"]),
            int(row["player_id"]),
            str(row["market"]),
            float(row["line"]),
        )
        not in tracked_keys
    }

    delete_prop_line_ids = [
        int(row["id"])
        for key, row in existing_by_key.items()
        if key not in desired_rows
    ]
    insert_rows = [
        (
            payload["game_id"],
            payload["player_id"],
            payload["sportsbook"],
            payload["market"],
            payload["line"],
            payload["over_odds"],
            payload["under_odds"],
            payload["captured_at"],
        )
        for key, payload in desired_rows.items()
        if key not in existing_by_key
    ]
    update_rows: list[tuple[str, int, int, str, int]] = []
    rebuild_prop_line_ids: list[int] = []
    for key, desired in desired_rows.items():
        existing = existing_by_key.get(key)
        if existing is None:
            continue
        sportsbook_changed = str(existing["sportsbook"]) != desired["sportsbook"]
        over_changed = int(existing["over_odds"]) != desired["over_odds"]
        under_changed = int(existing["under_odds"]) != desired["under_odds"]
        captured_at_changed = str(existing["captured_at"] or "") != str(desired["captured_at"] or "")
        if not (sportsbook_changed or over_changed or under_changed or captured_at_changed):
            continue
        update_rows.append(
            (
                desired["sportsbook"],
                desired["over_odds"],
                desired["under_odds"],
                str(desired["captured_at"]),
                int(existing["id"]),
            )
        )
        if over_changed or under_changed:
            rebuild_prop_line_ids.append(int(existing["id"]))

    try:
        with sqlite_write_lock():
            _begin_immediate_with_retry(
                conn,
                attempts=4 if fast_fail else 24,
                base_sleep=0.05 if fast_fail else 0.20,
            )
            if delete_prop_line_ids:
                delete_attempts = 4 if fast_fail else 20
                delete_sleep = 0.05 if fast_fail else 0.15
                _delete_by_id_batches(
                    conn,
                    "watchlist_snapshot_items",
                    "prop_line_id",
                    delete_prop_line_ids,
                    attempts=delete_attempts,
                    base_sleep=delete_sleep,
                )
                _delete_by_id_batches(
                    conn,
                    "gem_snapshot_items",
                    "prop_line_id",
                    delete_prop_line_ids,
                    attempts=delete_attempts,
                    base_sleep=delete_sleep,
                )
                _delete_by_id_batches(
                    conn,
                    "prop_predictions",
                    "prop_line_id",
                    delete_prop_line_ids,
                    attempts=delete_attempts,
                    base_sleep=delete_sleep,
                )
                _delete_by_id_batches(
                    conn,
                    "prop_lines",
                    "id",
                    delete_prop_line_ids,
                    attempts=delete_attempts,
                    base_sleep=delete_sleep,
                )
            changed_prop_line_ids: list[int] = []
            if update_rows:
                _executemany_with_lock_retry(
                    conn,
                    """
                    UPDATE prop_lines
                    SET sportsbook = ?, over_odds = ?, under_odds = ?, captured_at = ?
                    WHERE id = ?
                    """,
                    update_rows,
                    attempts=4 if fast_fail else 20,
                    base_sleep=0.05 if fast_fail else 0.15,
                )
                changed_prop_line_ids.extend(rebuild_prop_line_ids)
            if insert_rows:
                if include_change_details or rebuild_predictions_after:
                    for insert_row in insert_rows:
                        cursor = conn.execute(
                            """
                            INSERT INTO prop_lines (
                                game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
                            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                            """,
                            insert_row,
                        )
                        changed_prop_line_ids.append(int(cursor.lastrowid))
                else:
                    conn.executemany(
                        """
                        INSERT INTO prop_lines (
                            game_id, player_id, sportsbook, market, line, over_odds, under_odds, captured_at
                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        insert_rows,
                    )
            changed_prop_line_ids = sorted({int(prop_line_id) for prop_line_id in changed_prop_line_ids})
            active_rebuild_progress_callback = rebuild_progress_callback or progress_callback
            if rebuild_predictions_after and changed_prop_line_ids:
                rebuild_predictions_live(
                    conn,
                    prop_line_ids=changed_prop_line_ids,
                    progress_callback=active_rebuild_progress_callback,
                )
            conn.commit()
            if progress_callback is not None:
                progress_callback(
                    len(rows),
                    max(len(rows), 1),
                    f"Synced {len(insert_rows)} new, {len(update_rows)} updated, {len(delete_prop_line_ids)} removed prop lines.",
                )
            if include_change_details:
                return SyncPropLinesResult(
                    synced_props=len(candidate_rows),
                    changed_props=len(insert_rows) + len(update_rows) + len(delete_prop_line_ids),
                    changed_prop_line_ids=changed_prop_line_ids,
                    touched_game_ids=touched_game_ids,
                )
            return len(candidate_rows)
    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            try:
                conn.rollback()
            except sqlite3.Error:
                pass
            return 0
        raise
    except Exception:
        conn.rollback()
        raise


def _execute_with_lock_retry(
    conn: sqlite3.Connection,
    sql: str,
    params: tuple | list = (),
    *,
    attempts: int = 20,
    base_sleep: float = 0.15,
) -> None:
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(attempts):
        try:
            conn.execute(sql, params)
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "database is locked" not in message:
                raise
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(base_sleep * (attempt + 1))
    if last_error is not None:
        raise last_error


def _executemany_with_lock_retry(
    conn: sqlite3.Connection,
    sql: str,
    seq_of_params,
    *,
    attempts: int = 20,
    base_sleep: float = 0.15,
) -> None:
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(attempts):
        try:
            conn.executemany(sql, seq_of_params)
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "database is locked" not in message:
                raise
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(base_sleep * (attempt + 1))
    if last_error is not None:
        raise last_error


def _begin_immediate_with_retry(
    conn: sqlite3.Connection,
    *,
    attempts: int = 24,
    base_sleep: float = 0.20,
) -> None:
    if conn.in_transaction:
        return
    last_error: sqlite3.OperationalError | None = None
    for attempt in range(attempts):
        try:
            conn.execute("BEGIN IMMEDIATE")
            return
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "database is locked" not in message:
                raise
            last_error = exc
            if attempt == attempts - 1:
                break
            time.sleep(base_sleep * (attempt + 1))
    if last_error is not None:
        raise last_error


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


def _summarize_provider_errors(errors: list[dict[str, str]], *, limit: int = 2) -> str:
    snippets: list[str] = []
    for item in errors[:limit]:
        event_id = str(item.get("event_id") or "").strip()
        error = str(item.get("error") or "").strip()
        if event_id:
            snippets.append(f"{event_id}: {error}")
        else:
            snippets.append(error)
    if not snippets:
        return "unknown provider error"
    remaining = len(errors) - len(snippets)
    if remaining > 0:
        snippets.append(f"+{remaining} more")
    return "; ".join(snippets)


def _stale_cache_message(cached_payload: object) -> str:
    latest_commence_time = None
    if isinstance(cached_payload, list):
        latest_commence_time = max(
            (
                str(event.get("commence_time"))
                for event in cached_payload
                if isinstance(event, dict) and event.get("commence_time")
            ),
            default=None,
        )
    if latest_commence_time:
        return (
            "Saved Odds API cache exists, but it does not contain any events for today's slate. "
            f"Latest cached commence_time: {latest_commence_time}. Use Refresh Odds to fetch current events."
        )
    return "Saved Odds API cache exists, but it does not contain any events for today's slate. Use Refresh Odds to fetch current events."


def _replace_sportsbook_rows(conn: sqlite3.Connection, raw_payload: list[dict], captured_at: str) -> dict:
    active_payload = _active_cached_events(raw_payload)
    imported_rows = []
    for event_odds in active_payload:
        game_id = _match_or_create_local_game(conn, event_odds)
        _update_game_market_from_event(conn, event_odds, game_id)
        imported_rows.extend(_event_rows(event_odds, captured_at, game_id=game_id))
    with sqlite_write_lock():
        _begin_immediate_with_retry(conn)
        _execute_with_lock_retry(
            conn,
            "DELETE FROM sportsbook_prop_lines WHERE provider = ?",
            (PROVIDER,),
        )
        if imported_rows:
            _executemany_with_lock_retry(
                conn,
                """
                INSERT INTO sportsbook_prop_lines (
                    provider, provider_event_id, game_id, game_date, commence_time, home_team, away_team,
                    bookmaker_key, sportsbook, market_key, market, player_name, side, line, price, captured_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                imported_rows,
            )
        provider_player_ids_filled = _backfill_provider_player_ids(conn, provider=PROVIDER)
        conn.commit()
    return {
        "events": len(active_payload),
        "imported": len(imported_rows),
        "captured_at": captured_at,
        "provider_player_ids_filled": provider_player_ids_filled,
    }


def _backfill_provider_player_ids(conn: sqlite3.Connection, *, provider: str | None = None) -> int:
    where_clause = "WHERE spl.provider_player_id IS NULL AND spl.game_id IS NOT NULL"
    params: tuple[object, ...] = ()
    if provider is not None:
        where_clause += " AND spl.provider = ?"
        params = (provider,)
    conn.execute(
        f"""
        WITH candidate_matches AS (
            SELECT
                spl.id AS sportsbook_prop_line_id,
                p.id AS player_id,
                ROW_NUMBER() OVER (
                    PARTITION BY spl.id
                    ORDER BY
                        CASE WHEN lower(p.full_name) = lower(spl.player_name) THEN 0 ELSE 1 END,
                        p.id
                ) AS rn,
                COUNT(*) OVER (PARTITION BY spl.id) AS match_count
            FROM sportsbook_prop_lines spl
            JOIN games g ON g.id = spl.game_id
            JOIN players p ON {_player_name_match_clause("p.full_name", "spl.player_name")}
            LEFT JOIN player_team_history h
                ON h.player_id = p.id
               AND h.game_id = spl.game_id
            {where_clause}
              AND COALESCE(h.team_id, p.team_id) IN (g.home_team_id, g.away_team_id)
              AND EXISTS (
                  SELECT 1
                  FROM player_game_stats stats
                  WHERE stats.player_id = p.id
              )
        )
        UPDATE sportsbook_prop_lines
        SET provider_player_id = (
            SELECT candidate.player_id
            FROM candidate_matches candidate
            WHERE candidate.sportsbook_prop_line_id = sportsbook_prop_lines.id
              AND candidate.rn = 1
        )
        WHERE id IN (
            SELECT candidate.sportsbook_prop_line_id
            FROM candidate_matches candidate
            WHERE candidate.rn = 1
              AND candidate.match_count = 1
        )
        """,
        params,
    )
    filled = int(conn.execute("SELECT changes()").fetchone()[0] or 0)
    return filled + _backfill_provider_player_ids_fuzzy(conn, provider=provider)


def _backfill_provider_player_ids_fuzzy(conn: sqlite3.Connection, *, provider: str | None = None) -> int:
    where_clause = "WHERE spl.provider_player_id IS NULL AND spl.game_id IS NOT NULL"
    params: tuple[object, ...] = ()
    if provider is not None:
        where_clause += " AND spl.provider = ?"
        params = (provider,)
    rows = conn.execute(
        f"""
        SELECT
            spl.id,
            spl.player_name,
            g.home_team_id,
            g.away_team_id
        FROM sportsbook_prop_lines spl
        JOIN games g ON g.id = spl.game_id
        {where_clause}
        ORDER BY spl.id
        """,
        params,
    ).fetchall()
    updates: list[tuple[int, int]] = []
    candidate_cache: dict[tuple[int, int], list[sqlite3.Row]] = {}
    for row in rows:
        team_key = (int(row["home_team_id"]), int(row["away_team_id"]))
        candidates = candidate_cache.get(team_key)
        if candidates is None:
            candidates = conn.execute(
                """
                SELECT DISTINCT p.id, p.full_name
                FROM players p
                WHERE p.team_id IN (?, ?)
                  AND EXISTS (
                      SELECT 1
                      FROM player_game_stats stats
                      WHERE stats.player_id = p.id
                  )
                ORDER BY p.id
                """,
                team_key,
            ).fetchall()
            candidate_cache[team_key] = candidates
        matches = [
            int(candidate["id"])
            for candidate in candidates
            if _fuzzy_player_name_match(str(candidate["full_name"] or ""), str(row["player_name"] or ""))
        ]
        if len(matches) == 1:
            updates.append((matches[0], int(row["id"])))
    if updates:
        conn.executemany(
            "UPDATE sportsbook_prop_lines SET provider_player_id = ? WHERE id = ?",
            updates,
        )
    return len(updates)


def _open_scheduled_prop_line_ids(conn: sqlite3.Connection, game_ids: list[int]) -> list[int]:
    if not game_ids:
        return []
    placeholders = ",".join("?" for _ in game_ids)
    rows = conn.execute(
        f"""
        SELECT pl.id
        FROM prop_lines pl
        JOIN games g ON g.id = pl.game_id
        LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
        WHERE sp.id IS NULL
          AND g.status = 'scheduled'
          AND pl.game_id IN ({placeholders})
        """,
        tuple(game_ids),
    ).fetchall()
    return [int(row[0]) for row in rows]


def _open_scheduled_prop_line_rows(conn: sqlite3.Connection, game_ids: list[int]) -> list[sqlite3.Row]:
    if not game_ids:
        return []
    placeholders = ",".join("?" for _ in game_ids)
    return conn.execute(
        f"""
        SELECT
            pl.id,
            pl.game_id,
            pl.player_id,
            pl.sportsbook,
            pl.market,
            pl.line,
            pl.over_odds,
            pl.under_odds,
            pl.captured_at
        FROM prop_lines pl
        JOIN games g ON g.id = pl.game_id
        LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
        WHERE sp.id IS NULL
          AND g.status = 'scheduled'
          AND pl.game_id IN ({placeholders})
        """,
        tuple(game_ids),
    ).fetchall()


def _delete_by_id_batches(
    conn: sqlite3.Connection,
    table: str,
    id_column: str,
    ids: list[int],
    *,
    batch_size: int = 250,
    attempts: int = 20,
    base_sleep: float = 0.15,
) -> None:
    if not ids:
        return
    for start in range(0, len(ids), batch_size):
        batch = ids[start : start + batch_size]
        placeholders = ",".join("?" for _ in batch)
        _execute_with_lock_retry(
            conn,
            f"DELETE FROM {table} WHERE {id_column} IN ({placeholders})",
            tuple(batch),
            attempts=attempts,
            base_sleep=base_sleep,
        )


def _active_cached_events(raw_payload: object) -> list[dict]:
    if not isinstance(raw_payload, list):
        return []
    today = local_today_iso()
    active_events: list[dict] = []
    for event in raw_payload:
        if _is_today_event(event, today=today):
            active_events.append(event)
    return active_events


def _is_today_event(event: object, *, today: str) -> bool:
    if not isinstance(event, dict):
        return False
    commence_time = event.get("commence_time")
    if not commence_time:
        return False
    return _game_date(str(commence_time)) == today


def _event_rows(event_odds: dict, captured_at: str, *, game_id: int | None) -> list[tuple]:
    imported_rows = []
    event_id = event_odds["id"]
    game_date = _game_date(event_odds["commence_time"])
    for bookmaker in event_odds.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            app_market = PLAYER_MARKETS.get(market.get("key"))
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


def _update_game_market_from_event(conn: sqlite3.Connection, event_odds: dict, game_id: int | None) -> None:
    if game_id is None:
        return
    market = _event_game_market(event_odds)
    updates = []
    params: list[float | int] = []
    for column in (
        "spread_home",
        "game_total",
        "home_moneyline",
        "away_moneyline",
        "home_spread_price",
        "away_spread_price",
        "over_price",
        "under_price",
    ):
        value = market.get(column)
        if value is None:
            continue
        updates.append(f"{column} = ?")
        params.append(float(value))
    if not updates:
        return
    params.append(int(game_id))
    conn.execute(f"UPDATE games SET {', '.join(updates)} WHERE id = ?", tuple(params))


def _event_game_market(event_odds: dict) -> dict[str, float | None]:
    home_team = str(event_odds.get("home_team") or "")
    away_team = str(event_odds.get("away_team") or "")
    result: dict[str, float | None] = {
        "spread_home": None,
        "game_total": None,
        "home_moneyline": None,
        "away_moneyline": None,
        "home_spread_price": None,
        "away_spread_price": None,
        "over_price": None,
        "under_price": None,
    }
    for bookmaker in event_odds.get("bookmakers", []):
        for market in bookmaker.get("markets", []):
            key = str(market.get("key") or "")
            outcomes = market.get("outcomes") or []
            if key == "h2h":
                for outcome in outcomes:
                    name = str(outcome.get("name") or "")
                    price = _coerce_float(outcome.get("price"))
                    if price is None:
                        continue
                    if name == home_team and result["home_moneyline"] is None:
                        result["home_moneyline"] = price
                    elif name == away_team and result["away_moneyline"] is None:
                        result["away_moneyline"] = price
            elif key == "spreads":
                for outcome in outcomes:
                    name = str(outcome.get("name") or "")
                    point = _coerce_float(outcome.get("point"))
                    price = _coerce_float(outcome.get("price"))
                    if point is None:
                        continue
                    if name == home_team:
                        if result["spread_home"] is None:
                            result["spread_home"] = point
                        if result["home_spread_price"] is None:
                            result["home_spread_price"] = price
                    elif name == away_team:
                        if result["spread_home"] is None:
                            result["spread_home"] = -point
                        if result["away_spread_price"] is None:
                            result["away_spread_price"] = price
            elif key == "totals":
                for outcome in outcomes:
                    side = str(outcome.get("name") or "").lower()
                    point = _coerce_float(outcome.get("point"))
                    price = _coerce_float(outcome.get("price"))
                    if point is not None and result["game_total"] is None:
                        result["game_total"] = point
                    if side == "over" and result["over_price"] is None:
                        result["over_price"] = price
                    elif side == "under" and result["under_price"] is None:
                        result["under_price"] = price
        if (
            result["spread_home"] is not None
            and result["game_total"] is not None
            and result["home_moneyline"] is not None
            and result["away_moneyline"] is not None
        ):
            break
    return result


def list_sportsbook_props(conn: sqlite3.Connection, game_id: int | None = None) -> list[dict]:
    where = "AND spl.game_id = ?" if game_id is not None else ""
    params = (game_id,) if game_id is not None else ()
    rows = conn.execute(
        f"""
        SELECT spl.*
        FROM sportsbook_prop_lines spl
        LEFT JOIN games g ON g.id = spl.game_id
        WHERE NOT EXISTS (
            SELECT 1
            FROM prop_lines pl
            JOIN players p ON p.id = pl.player_id
            LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
            WHERE pl.game_id = spl.game_id
              AND lower(p.full_name) = lower(spl.player_name)
              AND lower(pl.market) = lower(spl.market)
              AND CAST(pl.line AS REAL) = CAST(spl.line AS REAL)
              AND (sp.id IS NOT NULL OR lower(COALESCE(g.status, '')) <> 'scheduled')
        )
        {where}
          AND lower(COALESCE(g.status, 'scheduled')) = 'scheduled'
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
    cache_path = get_cache_dir() / RAW_CACHE_NAME
    payload = read_json_cache(RAW_CACHE_NAME)
    if not isinstance(payload, list):
        return {
            "exists": False,
            "path": str(cache_path),
            "events": 0,
            "future_events": 0,
            "latest_commence_time": None,
        }
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
        "path": str(cache_path),
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
    try:
        with urlopen(url, timeout=30) as response:
            return json.loads(response.read().decode("utf-8"))
    except HTTPError as exc:
        detail = _http_error_detail(exc)
        raise RuntimeError(detail) from exc
    except URLError as exc:
        raise RuntimeError(f"Unable to reach Odds API: {exc.reason}") from exc


def _fetch_event_odds(event_id: str, *, api_key: str) -> tuple[dict | None, list[str]]:
    params = {
        "apiKey": api_key,
        "regions": os.getenv("ODDS_API_REGIONS", DEFAULT_REGIONS),
        "bookmakers": os.getenv("ODDS_API_BOOKMAKERS", DEFAULT_BOOKMAKERS),
        "oddsFormat": "american",
    }
    payloads: list[dict] = []
    errors: list[str] = []
    market_groups: list[tuple[str, ...]] = [GAME_MARKETS]
    player_market_keys = list(PLAYER_MARKETS)
    for start in range(0, len(player_market_keys), PLAYER_MARKET_BATCH_SIZE):
        market_groups.append(tuple(player_market_keys[start : start + PLAYER_MARKET_BATCH_SIZE]))
    for market_group in market_groups:
        try:
            payload = _fetch_json(
                f"{BASE_URL}/sports/{SPORT_KEY}/events/{event_id}/odds?"
                + urlencode({**params, "markets": ",".join(market_group)})
            )
        except RuntimeError as exc:
            errors.append(f"markets={','.join(market_group)}: {exc}")
            continue
        if isinstance(payload, dict):
            payloads.append(payload)
    if not payloads:
        return None, errors
    return _merge_event_market_payloads(payloads), errors


def _fetch_historical_game_markets(*, api_key: str, snapshot_at: str) -> object:
    params = {
        "apiKey": api_key,
        "regions": os.getenv("ODDS_API_REGIONS", DEFAULT_REGIONS),
        "bookmakers": os.getenv("ODDS_API_BOOKMAKERS", DEFAULT_BOOKMAKERS),
        "markets": ",".join(GAME_MARKETS),
        "oddsFormat": "american",
        "date": snapshot_at,
    }
    return _fetch_json(f"{BASE_URL}/historical/sports/{SPORT_KEY}/odds?{urlencode(params)}")


def _historical_payload_events(payload: object) -> list[dict]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [item for item in data if isinstance(item, dict)]
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    return []


def _historical_snapshot_at(selected_date: str, snapshot_time_utc: str) -> str:
    try:
        datetime.fromisoformat(f"{selected_date}T{snapshot_time_utc.replace('Z', '+00:00')}")
    except ValueError as exc:
        raise RuntimeError(
            "snapshot_time_utc must look like HH:MM:SSZ, for example 16:00:00Z."
        ) from exc
    return f"{selected_date}T{snapshot_time_utc}"


def _merge_event_market_payloads(payloads: list[dict]) -> dict:
    merged = dict(payloads[0])
    bookmakers_by_key: dict[str, dict] = {}
    for payload in payloads:
        for bookmaker in payload.get("bookmakers", []):
            book_key = str(bookmaker.get("key") or "")
            if not book_key:
                continue
            existing = bookmakers_by_key.get(book_key)
            if existing is None:
                existing = {**bookmaker, "markets": []}
                bookmakers_by_key[book_key] = existing
            existing_markets = {
                str(market.get("key") or ""): market
                for market in existing.get("markets", [])
                if isinstance(market, dict)
            }
            for market in bookmaker.get("markets", []):
                market_key = str(market.get("key") or "")
                if not market_key or market_key in existing_markets:
                    continue
                existing_markets[market_key] = market
            existing["markets"] = list(existing_markets.values())
    merged["bookmakers"] = list(bookmakers_by_key.values())
    return merged


def _http_error_detail(exc: HTTPError) -> str:
    status = getattr(exc, "status", exc.code)
    detail = ""
    try:
        body = exc.read().decode("utf-8").strip()
    except Exception:
        body = ""
    if body:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError:
            detail = body
        else:
            if isinstance(payload, dict):
                detail = str(
                    payload.get("message")
                    or payload.get("error")
                    or payload.get("detail")
                    or body
                )
            else:
                detail = body
    if not detail:
        detail = exc.reason if getattr(exc, "reason", None) else "request failed"
    return f"Odds API HTTP {status}: {detail}"


def _match_or_create_local_game(conn: sqlite3.Connection, event: dict) -> int | None:
    home = TEAM_ALIASES.get(str(event.get("home_team", "")).lower())
    away = TEAM_ALIASES.get(str(event.get("away_team", "")).lower())
    if not home or not away:
        return None
    commence_time_text = str(event["commence_time"])
    game_date = _game_date(commence_time_text)
    commence_time = _parse_utc(commence_time_text)
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
    return resolve_or_create_game(
        conn,
        home_team=str(event.get("home_team", "")),
        away_team=str(event.get("away_team", "")),
        start_time=commence_time_text,
        game_date=game_date,
    )


def _game_date(commence_time: str) -> str:
    value = commence_time.replace("Z", "+00:00")
    return datetime.fromisoformat(value).astimezone(LOCAL_TZ).date().isoformat()


def _coerce_float(value: object) -> float | None:
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


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
