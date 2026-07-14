from __future__ import annotations

import os
from datetime import date, datetime
from zoneinfo import ZoneInfo


APP_TIMEZONE_NAME = os.getenv("WNBA_APP_TIMEZONE", "America/New_York")
APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)


def local_now() -> datetime:
    return datetime.now(APP_TIMEZONE)


def local_today_iso() -> str:
    return local_now().date().isoformat()


def local_game_date(game_date: str | date | None, start_time: str | datetime | None) -> str:
    if isinstance(start_time, datetime):
        parsed = start_time
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
        return parsed.astimezone(APP_TIMEZONE).date().isoformat()

    start_text = str(start_time or "").strip()
    if start_text:
        try:
            parsed = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
        except ValueError:
            parsed = None
        if parsed is not None:
            if parsed.tzinfo is None:
                parsed = parsed.replace(tzinfo=ZoneInfo("UTC"))
            return parsed.astimezone(APP_TIMEZONE).date().isoformat()

    if isinstance(game_date, date):
        return game_date.isoformat()
    return str(game_date or "").strip()[:10]
