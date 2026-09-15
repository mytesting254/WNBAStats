#!/usr/bin/env python3
"""Import cached historical Odds API event player props into the configured DB."""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode
from urllib.error import HTTPError
from urllib.request import urlopen

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.db import connect
from backend.app.odds_import import (
    GAME_MARKETS,
    _backfill_provider_player_ids,
    _event_rows,
    _match_or_create_local_game,
    _update_game_market_from_event,
    sync_prop_lines_from_sportsbook,
)
from backend.app.settlement import settle_completed_props

MARKETS = ",".join(
    (
        *GAME_MARKETS,
        "player_points",
        "player_rebounds",
        "player_assists",
        "player_threes",
        "player_points_rebounds",
        "player_points_assists",
        "player_rebounds_assists",
        "player_points_rebounds_assists",
        "player_steals",
        "player_blocks",
        "player_blocks_steals",
    )
)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", default="/data/cache")
    parser.add_argument("--start-date", required=True)
    parser.add_argument("--end-date", required=True)
    parser.add_argument("--fetch", action="store_true")
    args = parser.parse_args()
    cache_dir = Path(args.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)
    if args.fetch:
        key = os.environ["ODDS_API_KEY"]
        from datetime import date, timedelta
        day = date.fromisoformat(args.start_date)
        end = date.fromisoformat(args.end_date)
        while day <= end:
            stamp = f"{day.isoformat()}T16:00:00Z"
            events = _fetch(f"https://api.the-odds-api.com/v4/historical/sports/basketball_wnba/events?{urlencode({'apiKey': key, 'date': stamp})}")
            for event in events.get("data", []):
                event_id = event["id"]
                path = cache_dir / f"historical_event_{event_id}_all.json"
                if not path.exists():
                    payload = _fetch(f"https://api.the-odds-api.com/v4/historical/sports/basketball_wnba/events/{event_id}/odds?{urlencode({'apiKey': key, 'date': stamp, 'regions': 'us', 'markets': MARKETS, 'oddsFormat': 'american'})}")
                    path.write_text(json.dumps(payload), encoding="utf-8")
            day += timedelta(days=1)
    files = sorted(cache_dir.glob("historical_event_*_*.json"))
    imported = settled = 0
    with connect() as conn:
        for path in files:
            payload = json.loads(path.read_text())
            event = payload.get("data", payload)
            game_date = str(event.get("commence_time") or "")[:10]
            if not args.start_date <= game_date <= args.end_date:
                continue
            game_id = _match_or_create_local_game(conn, event)
            _update_game_market_from_event(conn, event, game_id)
            rows = _event_rows(event, datetime.now(timezone.utc).isoformat(), game_id=game_id)
            if not rows and game_id is None:
                continue
            event_id = str(event["id"])
            market_keys = sorted({str(row[10]) for row in rows})
            for market in market_keys:
                conn.execute(
                    "DELETE FROM sportsbook_prop_lines WHERE provider = 'the_odds_api_historical' AND provider_event_id = ? AND market = ?",
                    (event_id, market),
                )
            conn.executemany(
                "INSERT INTO sportsbook_prop_lines (provider,provider_event_id,game_id,game_date,commence_time,home_team,away_team,bookmaker_key,sportsbook,market_key,market,player_name,side,line,price,captured_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                [("the_odds_api_historical",) + row[1:] for row in rows],
            )
            _backfill_provider_player_ids(conn, provider="the_odds_api_historical")
            imported += int(sync_prop_lines_from_sportsbook(conn, game_ids=[game_id]))
            settled += int(settle_completed_props(conn, selected_date=game_date)["settled"])
    print(json.dumps({"imported_prop_lines": imported, "settled_props": settled}))


def _fetch(url: str) -> dict:
    for attempt in range(4):
        try:
            with urlopen(url, timeout=60) as response:
                return json.loads(response.read())
        except HTTPError as exc:
            if exc.code != 429 or attempt == 3:
                raise
            retry_after = exc.headers.get("Retry-After")
            try:
                delay = max(2.0, min(30.0, float(retry_after))) if retry_after else 5.0 * (attempt + 1)
            except ValueError:
                delay = 5.0 * (attempt + 1)
            time.sleep(delay)
    raise RuntimeError("Odds API fetch retry loop exhausted")


if __name__ == "__main__":
    main()
