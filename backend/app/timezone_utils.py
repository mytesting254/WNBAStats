from __future__ import annotations

import os
from datetime import datetime
from zoneinfo import ZoneInfo


APP_TIMEZONE_NAME = os.getenv("WNBA_APP_TIMEZONE", "America/New_York")
APP_TIMEZONE = ZoneInfo(APP_TIMEZONE_NAME)


def local_now() -> datetime:
    return datetime.now(APP_TIMEZONE)


def local_today_iso() -> str:
    return local_now().date().isoformat()
