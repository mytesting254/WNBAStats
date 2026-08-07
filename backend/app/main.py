from __future__ import annotations

import asyncio
from collections import Counter
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import sqlite3
import threading
import time
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

from .accuracy_analysis import build_accuracy_report, build_accuracy_report_for_days
from .auth import (
    SESSION_COOKIE_NAME,
    SessionUser,
    authenticate_user,
    create_session,
    delete_session,
    get_session_user,
    session_cookie_max_age,
)
from .bootstrap import ensure_teams, normalize_team_abbreviation
from .cache import delete_json_cache, read_json_cache, write_json_cache
from .covers_import import CoversGame, RAW_CACHE_NAME as COVERS_RAW_CACHE_NAME, _game_market_from_page, _import_covers_provider_rows, _metadata_from_page, import_covers_props
from .db import connect, init_db, sqlite_write_lock, using_turso
from .dfs_model import build_current_dfs_first_half_estimates
from .espn_history import import_espn_player_boxscores, import_espn_scoreboard
from .espn_roster import sync_espn_rosters
from .game_prediction_tracking import save_game_prediction, save_game_predictions, settle_completed_game_predictions
from .game_predictions import _GamePredictionCache, _team_injury_impact, project_game
from .odds_import import (
    _fuzzy_player_name_match,
    import_historical_odds_api_game_markets,
    RAW_CACHE_NAME as ODDS_RAW_CACHE_NAME,
    SyncPropLinesResult,
    _import_the_odds_api_provider_rows,
    import_the_odds_api_props,
    line_discrepancies,
    list_sportsbook_props,
    odds_cache_summary,
    _player_name_match_clause,
    sync_prop_lines_from_sportsbook,
)
from .player_identity import player_is_skeletal, player_lookup_parts, resolve_player_identity
from .prop_ingestion import (
    PropPostProcessPolicy,
    PropPipelineResult,
    build_covers_provider_ingestion,
    build_no_provider_ingestion,
    build_odds_provider_ingestion,
    run_post_pipeline_steps,
    run_prop_sync_pipeline,
)
from .projections import LiveRebuildResult, rebuild_predictions, rebuild_predictions_live
from .rotowire_import import RAW_CACHE_NAME as ROTOWIRE_RAW_CACHE_NAME, import_rotowire_lineups
from .settlement import settle_completed_props
from .segment_predictions import project_game_segments
from .stocks_tracking import (
    SPECIALS_DEFAULT_HIGH_THRESHOLD,
    SPECIALS_DEFAULT_WATCH_THRESHOLD,
    default_special_threshold_recommendations,
    fit_special_threshold_recommendations,
    get_tracking_db_path,
    list_game_board_summaries,
    list_special_stocks,
    prepare_stocks_data,
    prune_special_snapshots,
    queue_prepare_stocks_games,
    settled_latest_special_rows,
    settle_stocks,
)
from .player_prop_model import (
    MODEL_VERSION,
    _blowout_adjustment,
    _classify_minutes_role,
    _days_between_game_dates,
    _ewma_newest_first,
    _historical_game_context,
    _historical_minutes_opportunity_context,
    _minute_volatility,
    _minutes_lineup_context_from_player_rows,
    _player_training_rows,
    _project_minutes,
    _recent_trend,
    _team_transition_features_from_player_rows,
    _training_start_date,
    prewarm_model_cache,
)
from .training import latest_model_run, list_model_runs, run_parameter_tuning, run_walk_forward_training
from .timezone_utils import APP_TIMEZONE, local_today_iso
from .paths import get_cache_dir, get_db_path, get_snapshot_dir, get_training_db_path


app = FastAPI(title="WNBA Prop Value API")
COMPLETED_GAME_GRACE_HOURS = 4
LOCAL_TZ = APP_TIMEZONE
LOW_CONFIDENCE_EDGE_MIN = float(os.getenv("LOW_CONFIDENCE_EDGE_MIN", "0.08"))
LOW_CONFIDENCE_EDGE_MAX = float(os.getenv("LOW_CONFIDENCE_EDGE_MAX", "0.18"))
PLAYER_DATA_MAX_LAG_DAYS = int(os.getenv("PLAYER_DATA_MAX_LAG_DAYS", "5"))
PLAYER_DATA_MIN_RECENT_GAMES = int(os.getenv("PLAYER_DATA_MIN_RECENT_GAMES", "3"))
PLAYER_DATA_RECENT_WINDOW_DAYS = int(os.getenv("PLAYER_DATA_RECENT_WINDOW_DAYS", "30"))
ENABLE_PLAYER_FRESHNESS_GATE = os.getenv("ENABLE_PLAYER_FRESHNESS_GATE", "0").strip().lower() in {"1", "true", "yes"}
VALUE_BOARD_CACHE_NAME = "current_value_board.json"
MATCHUPS_CACHE_NAME = "current_matchups.json"
WATCHLIST_CACHE_NAME = "current_watchlist.json"
LINE_DISCREPANCIES_CACHE_NAME = "line_discrepancies.json"
MODEL_PERFORMANCE_CACHE_NAME = "model_performance.json"
GEM_PERFORMANCE_CACHE_NAME = "gem_performance.json"
WATCHLIST_PERFORMANCE_CACHE_NAME = "watchlist_performance.json"
MODEL_RUNS_CACHE_NAME = "model_runs.json"
ROSTER_CACHE_NAME = "roster.json"
APP_RESPONSE_CACHE_PREFIX = "app_response_cache_"
MATCHUP_SNAPSHOT_CACHE_PREFIX = "matchup_snapshot_"
GEM_MIN_EV = float(os.getenv("GEM_MIN_EV", "0.02"))
GEM_MIN_EDGE = float(os.getenv("GEM_MIN_EDGE", "0.05"))
READ_CACHE_VERSION = 1
INCREASED_ROLE_USAGE_THRESHOLD = 1.04
INCREASED_ROLE_MINUTES_THRESHOLD = 1.0
APP_RESPONSE_CACHE_VERSION = 3
APP_RESPONSE_CACHE_TTL_SECONDS = int(os.getenv("APP_RESPONSE_CACHE_TTL_SECONDS", "3600"))
VALUE_BOARD_TTL_SECONDS = int(os.getenv("VALUE_BOARD_TTL_SECONDS", "300"))
WATCHLIST_TTL_SECONDS = int(os.getenv("WATCHLIST_TTL_SECONDS", "300"))
LINE_DISCREPANCIES_TTL_SECONDS = int(os.getenv("LINE_DISCREPANCIES_TTL_SECONDS", "300"))
MATCHUPS_TTL_SECONDS = int(os.getenv("MATCHUPS_TTL_SECONDS", "300"))
MODEL_PERFORMANCE_TTL_SECONDS = int(os.getenv("MODEL_PERFORMANCE_TTL_SECONDS", "300"))
GEM_PERFORMANCE_TTL_SECONDS = int(os.getenv("GEM_PERFORMANCE_TTL_SECONDS", "300"))
WATCHLIST_PERFORMANCE_TTL_SECONDS = int(os.getenv("WATCHLIST_PERFORMANCE_TTL_SECONDS", "300"))
MODEL_RUNS_TTL_SECONDS = int(os.getenv("MODEL_RUNS_TTL_SECONDS", "300"))
ROSTER_TTL_SECONDS = int(os.getenv("ROSTER_TTL_SECONDS", "300"))
RATE_LIMIT_MUTATION_CAPACITY = float(os.getenv("RATE_LIMIT_MUTATION_CAPACITY", "10"))
RATE_LIMIT_MUTATION_REFILL_PER_SEC = float(os.getenv("RATE_LIMIT_MUTATION_REFILL_PER_SEC", "0.5"))
RATE_LIMIT_REFRESH_CAPACITY = float(os.getenv("RATE_LIMIT_REFRESH_CAPACITY", "6"))
RATE_LIMIT_REFRESH_REFILL_PER_SEC = float(os.getenv("RATE_LIMIT_REFRESH_REFILL_PER_SEC", "0.33"))
_RATE_BUCKETS: dict[tuple[str, str], tuple[float, float]] = {}
_RATE_LOCK = threading.Lock()
_PROP_SYNC_LOCK = threading.Lock()
_MODEL_TRAIN_LOCK = threading.Lock()
_DB_MAINTENANCE_LOCK = threading.Lock()
_ESPN_HISTORY_IMPORT_LOCK = threading.Lock()
_READ_CACHE_REBUILD_LOCKS: dict[str, threading.Lock] = {}
_READ_CACHE_REBUILD_LOCKS_LOCK = threading.Lock()
PROP_SYNC_STALE_SECONDS = int(os.getenv("PROP_SYNC_STALE_SECONDS", "1800"))
RECENT_FINALS_SETTLEMENT_LOOKBACK_DAYS = int(os.getenv("RECENT_FINALS_SETTLEMENT_LOOKBACK_DAYS", "3"))
_PROP_SYNC_STATE: dict[str, Any] = {
    "job_id": None,
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "last_result": None,
    "status": "idle",
    "scope": None,
    "target_game_ids": [],
    "stage": None,
    "stage_index": 0,
    "stage_total": 1,
    "current": 0,
    "total": 0,
    "percent": 0.0,
    "message": None,
    "updated_at": None,
}
_MODEL_TRAIN_STATE: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "last_result": None,
    "status": "idle",
    "message": None,
    "updated_at": None,
}
_UNSET = object()
DELETE_STALE_PAYLOAD_ACK = "DELETE STALE PAYLOAD"
READ_CACHE_FILES = {
    VALUE_BOARD_CACHE_NAME,
    MATCHUPS_CACHE_NAME,
    WATCHLIST_CACHE_NAME,
    LINE_DISCREPANCIES_CACHE_NAME,
    MODEL_PERFORMANCE_CACHE_NAME,
    GEM_PERFORMANCE_CACHE_NAME,
    WATCHLIST_PERFORMANCE_CACHE_NAME,
    MODEL_RUNS_CACHE_NAME,
    ROSTER_CACHE_NAME,
}


class LoginRequest(BaseModel):
    username: str
    password: str


class DeleteStalePayloadRequest(BaseModel):
    acknowledgement: str


LOGGER = logging.getLogger("wnbastats.audit")
if not logging.getLogger().handlers:
    logging.basicConfig(level=logging.INFO, format="%(message)s")


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _truncate_text(value: Any, limit: int = 400) -> str | None:
    if value is None:
        return None
    text = str(value)
    if len(text) <= limit:
        return text
    return f"{text[:limit]}...<{len(text) - limit} more chars>"


def _normalize_audit_payload(value: Any, *, depth: int = 0) -> Any:
    if depth >= 4:
        return _truncate_text(value, 200)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    if isinstance(value, str):
        return _truncate_text(value, 500)
    if isinstance(value, dict):
        items = list(value.items())
        payload = {
            str(key): _normalize_audit_payload(item, depth=depth + 1)
            for key, item in items[:40]
        }
        if len(items) > 40:
            payload["_truncated_keys"] = len(items) - 40
        return payload
    if isinstance(value, (list, tuple, set)):
        items = list(value)
        payload = [_normalize_audit_payload(item, depth=depth + 1) for item in items[:25]]
        if len(items) > 25:
            payload.append({"_truncated_items": len(items) - 25})
        return payload
    return _truncate_text(repr(value), 300)


def _audit_log(level: int, event_type: str, **fields: Any) -> None:
    payload = {
        "ts": _utc_now_iso(),
        "event": event_type,
        **{key: _normalize_audit_payload(value) for key, value in fields.items() if value is not None},
    }
    LOGGER.log(level, json.dumps(payload, ensure_ascii=True, separators=(",", ":")))


def _request_id(request: Request | None) -> str:
    if request is None:
        return uuid.uuid4().hex
    existing_state = getattr(request.state, "audit_request_id", None)
    if existing_state:
        return str(existing_state)
    existing = (request.headers.get("x-request-id") or "").strip()
    request_id = existing or uuid.uuid4().hex
    request.state.audit_request_id = request_id
    return request_id


def _request_client_ip(request: Request | None) -> str | None:
    if request is None:
        return None
    forwarded_for = (request.headers.get("x-forwarded-for") or "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for
    return request.client.host if request.client is not None else None


def _audit_actor(request: Request | None) -> dict[str, Any]:
    if request is None:
        return {"actor_type": "system", "actor_id": None, "actor_name": "system"}
    user = _current_session_user(request)
    if user is not None:
        return {"actor_type": "session", "actor_id": str(user.user_id), "actor_name": user.username}
    if request.headers.get("x-api-key") or request.headers.get("authorization"):
        return {"actor_type": "api_key", "actor_id": None, "actor_name": "api_key"}
    return {"actor_type": "anonymous", "actor_id": None, "actor_name": "anonymous"}


def _create_mutation_audit_record(
    request: Request,
    action: str,
    *,
    details: Any = None,
    target: Any = None,
) -> int | None:
    actor = _audit_actor(request)
    now = _utc_now_iso()
    try:
        with connect() as conn:
            cursor = conn.execute(
                """
                INSERT INTO mutation_audit_log (
                    action, status, request_id, method, path, actor_type, actor_id, actor_name,
                    client_ip, details_json, target_json, created_at, updated_at
                ) VALUES (?, 'started', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    action,
                    _request_id(request),
                    request.method.upper(),
                    request.url.path,
                    actor["actor_type"],
                    actor["actor_id"],
                    actor["actor_name"],
                    _request_client_ip(request),
                    _json_text(_normalize_audit_payload(details)) if details is not None else None,
                    _json_text(_normalize_audit_payload(target)) if target is not None else None,
                    now,
                    now,
                ),
            )
            conn.commit()
            audit_id = int(cursor.lastrowid) if cursor.lastrowid is not None else None
    except Exception as exc:
        _audit_log(logging.ERROR, "audit.mutation.persist_failed", action=action, error=str(exc))
        return None
    _audit_log(logging.INFO, "audit.mutation.started", action=action, audit_id=audit_id, path=request.url.path, actor=actor)
    return audit_id


def _finish_mutation_audit_record(
    audit_id: int | None,
    *,
    status: str,
    result: Any = None,
    error_text: str | None = None,
) -> None:
    if not audit_id:
        return
    now = _utc_now_iso()
    try:
        with connect() as conn:
            conn.execute(
                """
                UPDATE mutation_audit_log
                SET status = ?, result_json = ?, error_text = ?, updated_at = ?
                WHERE id = ?
                """,
                (
                    status,
                    _json_text(_normalize_audit_payload(result)) if result is not None else None,
                    _truncate_text(error_text, 1200),
                    now,
                    int(audit_id),
                ),
            )
            conn.commit()
    except Exception as exc:
        _audit_log(logging.ERROR, "audit.mutation.finish_failed", audit_id=audit_id, error=str(exc))


def _run_audited_mutation(
    request: Request,
    action: str,
    func: Callable[[], Any],
    *,
    details: Any = None,
    target: Any = None,
) -> Any:
    audit_id = _create_mutation_audit_record(request, action, details=details, target=target)
    try:
        result = func()
    except HTTPException as exc:
        _finish_mutation_audit_record(audit_id, status=f"http_{exc.status_code}", error_text=str(exc.detail))
        _audit_log(logging.WARNING, "audit.mutation.http_error", action=action, audit_id=audit_id, status_code=exc.status_code, error=str(exc.detail))
        raise
    except Exception as exc:
        _finish_mutation_audit_record(audit_id, status="error", error_text=str(exc))
        _audit_log(logging.ERROR, "audit.mutation.error", action=action, audit_id=audit_id, error=str(exc))
        raise
    _finish_mutation_audit_record(audit_id, status="completed", result=result)
    _audit_log(logging.INFO, "audit.mutation.completed", action=action, audit_id=audit_id, result=result)
    return result


def _create_job_run(
    job_type: str,
    *,
    request: Request | None = None,
    trigger_action: str | None = None,
    source_path: str | None = None,
    metadata: Any = None,
    target: Any = None,
) -> int | None:
    actor = _audit_actor(request)
    started_at = _utc_now_iso()
    try:
        with sqlite_write_lock():
            with connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO job_runs (
                        job_type, status, trigger_action, request_id, source_path, actor_type, actor_id,
                        actor_name, target_json, metadata_json, started_at, updated_at
                    ) VALUES (?, 'queued', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        job_type,
                        trigger_action,
                        _request_id(request),
                        source_path,
                        actor["actor_type"],
                        actor["actor_id"],
                        actor["actor_name"],
                        _json_text(_normalize_audit_payload(target)) if target is not None else None,
                        _json_text(_normalize_audit_payload(metadata)) if metadata is not None else None,
                        started_at,
                        started_at,
                    ),
                )
                conn.commit()
                job_run_id = int(cursor.lastrowid) if cursor.lastrowid is not None else None
    except Exception as exc:
        _audit_log(logging.ERROR, "audit.job.persist_failed", job_type=job_type, error=str(exc))
        return None
    _audit_log(logging.INFO, "audit.job.started", job_type=job_type, job_run_id=job_run_id, actor=actor, target=target)
    return job_run_id


def _append_job_run_event(
    job_run_id: int | None,
    event_type: str,
    message: str,
    *,
    level: str = "info",
    details: Any = None,
) -> None:
    if not job_run_id:
        return
    try:
        with sqlite_write_lock():
            with connect() as conn:
                conn.execute(
                    """
                    INSERT INTO job_run_events (job_run_id, created_at, level, event_type, message, details_json)
                    VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        int(job_run_id),
                        _utc_now_iso(),
                        level,
                        event_type,
                        message,
                        _json_text(_normalize_audit_payload(details)) if details is not None else None,
                    ),
                )
                conn.commit()
    except Exception as exc:
        _audit_log(logging.ERROR, "audit.job.event_failed", job_run_id=job_run_id, error=str(exc), event_type=event_type)
        return
    log_level = logging.ERROR if level == "error" else logging.WARNING if level == "warning" else logging.INFO
    _audit_log(log_level, event_type, job_run_id=job_run_id, message=message, details=details)


def _finish_job_run(
    job_run_id: int | None,
    *,
    status: str,
    result: Any = None,
    error_text: str | None = None,
) -> None:
    if not job_run_id:
        return
    finished_at = _utc_now_iso()
    try:
        with sqlite_write_lock():
            with connect() as conn:
                row = conn.execute("SELECT started_at FROM job_runs WHERE id = ?", (int(job_run_id),)).fetchone()
                duration_ms = None
                if row and row["started_at"]:
                    try:
                        started = datetime.fromisoformat(str(row["started_at"]).replace("Z", "+00:00"))
                        finished = datetime.fromisoformat(finished_at.replace("Z", "+00:00"))
                        duration_ms = round((finished - started).total_seconds() * 1000.0, 2)
                    except ValueError:
                        duration_ms = None
                conn.execute(
                    """
                    UPDATE job_runs
                    SET status = ?, result_json = ?, last_error = ?, finished_at = ?, duration_ms = ?, updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        status,
                        _json_text(_normalize_audit_payload(result)) if result is not None else None,
                        _truncate_text(error_text, 1200),
                        finished_at,
                        duration_ms,
                        finished_at,
                        int(job_run_id),
                    ),
                )
                conn.commit()
    except Exception as exc:
        _audit_log(logging.ERROR, "audit.job.finish_failed", job_run_id=job_run_id, error=str(exc))
        return
    _audit_log(
        logging.ERROR if status == "failed" else logging.INFO,
        "audit.job.completed",
        job_run_id=job_run_id,
        status=status,
        error=error_text,
        result=result,
    )


def _is_sqlite_locked_error(exc: sqlite3.OperationalError) -> bool:
    return "locked" in str(exc).lower()


def _should_cache_app_response(request: Request) -> bool:
    if request.method.upper() != "GET":
        return False
    if not request.url.path.startswith("/api/"):
        return False
    if request.url.path.startswith("/api/auth/"):
        return False
    if request.url.path in {
        "/api/health",
        "/api/ops/health",
        "/api/operations/health",
        "/api/cache/status",
        "/api/cache/events",
        "/api/props/sync-status",
        "/api/matchups",
        "/api/value-board",
        "/api/watchlist",
        "/api/special/stocks",
        "/api/special/stats",
        "/api/roster",
    }:
        return False
    return True


def _app_response_cache_name(request: Request) -> str:
    cache_key = f"{request.url.path}?{request.url.query}"
    digest = hashlib.sha256(cache_key.encode("utf-8")).hexdigest()
    return f"{APP_RESPONSE_CACHE_PREFIX}{digest}.json"


def _app_response_cache_envelope(status_code: int, payload: Any) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "cache_key_version": APP_RESPONSE_CACHE_VERSION,
        "cached_at": now.isoformat(),
        "cache_date": _local_today_iso(),
        "ttl_seconds": APP_RESPONSE_CACHE_TTL_SECONDS,
        "source": "app_response_cache",
        "status_code": status_code,
        "payload": payload,
    }


def _read_app_response_cache(cache_name: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
    cached = read_json_cache(cache_name)
    if not isinstance(cached, dict):
        return None
    if cached.get("cache_key_version") != APP_RESPONSE_CACHE_VERSION:
        return None
    cached_at_raw = cached.get("cached_at")
    cache_date = cached.get("cache_date")
    ttl_seconds = cached.get("ttl_seconds")
    status_code = cached.get("status_code")
    if (
        not isinstance(cached_at_raw, str)
        or not isinstance(cache_date, str)
        or not isinstance(ttl_seconds, int)
        or not isinstance(status_code, int)
    ):
        return None
    if cache_date != _local_today_iso():
        return None
    try:
        cached_at = datetime.fromisoformat(cached_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    if not allow_stale and datetime.now(timezone.utc) - cached_at > timedelta(seconds=ttl_seconds):
        return None
    return {
        "status_code": status_code,
        "payload": cached.get("payload"),
    }


def _cached_app_response(entry: dict[str, Any], cache_status: str) -> JSONResponse:
    response = JSONResponse(content=entry.get("payload"), status_code=int(entry.get("status_code") or 200))
    response.headers["X-App-Cache"] = cache_status
    response.headers["X-Cache"] = f"APP_{cache_status}"
    response.headers["X-Compute-Ms"] = "0.00"
    return response


def _with_conditional_etag(request: Request, response: Response, body: bytes) -> Response:
    etag = f'"{hashlib.sha256(body).hexdigest()}"'
    response.headers["ETag"] = etag
    if request.headers.get("if-none-match") != etag:
        return response
    return Response(
        status_code=304,
        headers={
            "ETag": etag,
            "Cache-Control": response.headers.get("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0"),
            "X-App-Cache": response.headers.get("X-App-Cache", "BYPASS"),
            "X-Cache": response.headers.get("X-Cache", "BYPASS"),
            "X-Compute-Ms": response.headers.get("X-Compute-Ms", "0.00"),
        },
    )


def _local_today_iso() -> str:
    return local_today_iso()


def _app_local_game_date(game_date: Any, start_time: Any) -> str:
    start_text = str(start_time or "").strip()
    if start_text:
        try:
            parsed = datetime.fromisoformat(start_text.replace("Z", "+00:00"))
            return parsed.astimezone(APP_TIMEZONE).date().isoformat()
        except ValueError:
            pass
    return str(game_date or "").strip()[:10]


def _json_text(value: Any) -> str:
    return json.dumps(value, ensure_ascii=True, separators=(",", ":"))


def _row_value(row: Any, key: str, default: Any = None) -> Any:
    if row is None:
        return default
    if isinstance(row, dict):
        return row.get(key, default)
    if isinstance(row, sqlite3.Row):
        return row[key] if key in row.keys() else default
    getter = getattr(row, "get", None)
    if callable(getter):
        return getter(key, default)
    try:
        return row[key]
    except Exception:
        return default


def _create_prop_sync_job_record(
    scope: str,
    *,
    started_at: str,
    status: str,
    message: str | None,
    target_game_ids: list[int] | None = None,
) -> int | None:
    try:
        with sqlite_write_lock():
            with connect() as conn:
                cursor = conn.execute(
                    """
                    INSERT INTO prop_sync_jobs (
                        scope, status, started_at, stage, stage_index, stage_total,
                        current_count, total_count, percent, message, target_game_ids_json, updated_at
                    ) VALUES (?, ?, ?, ?, 0, 1, 0, 0, 0.0, ?, ?, ?)
                    """,
                    (
                        scope,
                        status,
                        started_at,
                        "queued",
                        message,
                        _json_text(list(target_game_ids or [])),
                        started_at,
                    ),
                )
                conn.commit()
                return int(cursor.lastrowid) if cursor.lastrowid is not None else None
    except Exception as exc:
        print(f"[prop-sync] unable to create job record: {exc}")
        return None


def _persist_prop_sync_job_snapshot(snapshot: dict[str, Any], *, conn=None) -> None:
    job_id = snapshot.get("job_id")
    if not job_id:
        return
    try:
        owns_connection = conn is None
        with sqlite_write_lock():
            db_conn = conn if conn is not None else connect()
            db_conn.execute(
                """
                UPDATE prop_sync_jobs
                SET
                    scope = ?,
                    status = ?,
                    started_at = ?,
                    finished_at = ?,
                    stage = ?,
                    stage_index = ?,
                    stage_total = ?,
                    current_count = ?,
                    total_count = ?,
                    percent = ?,
                    message = ?,
                    last_error = ?,
                    last_result_json = ?,
                    target_game_ids_json = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (
                    snapshot.get("scope"),
                    snapshot.get("status") or "idle",
                    snapshot.get("started_at"),
                    snapshot.get("finished_at"),
                    snapshot.get("stage"),
                    int(snapshot.get("stage_index") or 0),
                    int(snapshot.get("stage_total") or 1),
                    int(snapshot.get("current") or 0),
                    int(snapshot.get("total") or 0),
                    float(snapshot.get("percent") or 0.0),
                    snapshot.get("message"),
                    snapshot.get("last_error"),
                    _json_text(snapshot.get("last_result")) if snapshot.get("last_result") is not None else None,
                    _json_text(list(snapshot.get("target_game_ids") or [])),
                    snapshot.get("updated_at"),
                    int(job_id),
                ),
            )
            if owns_connection:
                db_conn.commit()
                db_conn.close()
    except Exception as exc:
        print(f"[prop-sync] unable to persist job snapshot: {exc}")


def _mutate_prop_sync_state(
    *,
    conn=None,
    job_id: Any = _UNSET,
    running: Any = _UNSET,
    started_at: Any = _UNSET,
    finished_at: Any = _UNSET,
    last_error: Any = _UNSET,
    last_result: Any = _UNSET,
    status: Any = _UNSET,
    scope: Any = _UNSET,
    target_game_ids: Any = _UNSET,
    stage: Any = _UNSET,
    stage_index: Any = _UNSET,
    stage_total: Any = _UNSET,
    current: Any = _UNSET,
    total: Any = _UNSET,
    message: Any = _UNSET,
) -> dict[str, Any]:
    with _PROP_SYNC_LOCK:
        updates = {
            "job_id": job_id,
            "running": running,
            "started_at": started_at,
            "finished_at": finished_at,
            "last_error": last_error,
            "last_result": last_result,
            "status": status,
            "scope": scope,
            "target_game_ids": target_game_ids,
            "stage": stage,
            "stage_index": stage_index,
            "stage_total": stage_total,
            "current": current,
            "total": total,
            "message": message,
        }
        for key, value in updates.items():
            if value is _UNSET:
                continue
            if key == "target_game_ids" and value is not None:
                _PROP_SYNC_STATE[key] = list(value)
            elif key == "last_result" and value is not None:
                _PROP_SYNC_STATE[key] = dict(value)
            else:
                _PROP_SYNC_STATE[key] = value

        current_value = int(_PROP_SYNC_STATE.get("current") or 0)
        total_value = int(_PROP_SYNC_STATE.get("total") or 0)
        stage_index_value = int(_PROP_SYNC_STATE.get("stage_index") or 0)
        stage_total_value = max(1, int(_PROP_SYNC_STATE.get("stage_total") or 1))
        phase_ratio = 0.0
        if total_value > 0:
            phase_ratio = min(1.0, max(0.0, current_value / total_value))
        if stage_index_value <= 0:
            percent = 0.0
        else:
            percent = min(1.0, max(0.0, ((stage_index_value - 1) + phase_ratio) / stage_total_value))
        _PROP_SYNC_STATE["percent"] = percent
        _PROP_SYNC_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        snapshot = {
            key: (list(value) if isinstance(value, list) else dict(value) if isinstance(value, dict) else value)
            for key, value in _PROP_SYNC_STATE.items()
        }
    _persist_prop_sync_job_snapshot(snapshot, conn=conn)
    return snapshot


def _mutate_model_training_state(
    *,
    running: Any = _UNSET,
    started_at: Any = _UNSET,
    finished_at: Any = _UNSET,
    last_error: Any = _UNSET,
    last_result: Any = _UNSET,
    status: Any = _UNSET,
    message: Any = _UNSET,
) -> dict[str, Any]:
    with _MODEL_TRAIN_LOCK:
        updates = {
            "running": running,
            "started_at": started_at,
            "finished_at": finished_at,
            "last_error": last_error,
            "last_result": last_result,
            "status": status,
            "message": message,
        }
        for key, value in updates.items():
            if value is _UNSET:
                continue
            if key == "last_result" and value is not None:
                _MODEL_TRAIN_STATE[key] = dict(value)
            else:
                _MODEL_TRAIN_STATE[key] = value
        _MODEL_TRAIN_STATE["updated_at"] = datetime.now(timezone.utc).isoformat()
        return {
            key: (dict(value) if isinstance(value, dict) else value)
            for key, value in _MODEL_TRAIN_STATE.items()
        }


def _queue_model_training_job(request: Request | None = None) -> dict[str, Any]:
    with _MODEL_TRAIN_LOCK:
        if _MODEL_TRAIN_STATE.get("running"):
            return {
                "status": "busy",
                "started_at": _MODEL_TRAIN_STATE.get("started_at"),
                "message": str(_MODEL_TRAIN_STATE.get("message") or "Model training is already running."),
            }

    started_at = datetime.now(timezone.utc).isoformat()
    job_run_id = _create_job_run(
        "model_training",
        request=request,
        trigger_action="api.models.train" if request is not None else "internal.model_training",
        source_path=request.url.path if request is not None else "/api/models/train",
        metadata={"started_at": started_at},
    )
    _mutate_model_training_state(
        running=True,
        started_at=started_at,
        finished_at=None,
        last_error=None,
        last_result=None,
        status="queued",
        message="Model training queued.",
    )
    _append_job_run_event(job_run_id, "job.queued", "Model training queued.")

    def _run() -> None:
        _mutate_model_training_state(
            running=True,
            started_at=started_at,
            finished_at=None,
            last_error=None,
            last_result=None,
            status="running",
            message="Model training is running.",
        )
        _append_job_run_event(job_run_id, "job.running", "Model training started.")
        try:
            with sqlite_write_lock():
                with connect() as conn:
                    result = run_walk_forward_training(conn)
            _invalidate_read_caches()
            with sqlite_write_lock():
                with connect() as conn:
                    result["published_payloads"] = _publish_post_mutation_read_payloads(conn)
            _mutate_model_training_state(
                running=False,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_error=None,
                last_result=result,
                status=str(result.get("status") or "completed"),
                message="Model training finished.",
            )
            _append_job_run_event(job_run_id, "job.publish", "Model training finished and payloads were published.", details=result)
            _finish_job_run(job_run_id, status=str(result.get("status") or "completed"), result=result)
        except Exception as exc:
            _mutate_model_training_state(
                running=False,
                started_at=started_at,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_error=str(exc),
                last_result=None,
                status="error",
                message=f"Model training failed: {exc}",
            )
            _append_job_run_event(job_run_id, "job.failed", f"Model training failed: {exc}", level="error")
            _finish_job_run(job_run_id, status="failed", error_text=str(exc))

    threading.Thread(target=_run, name="model-training", daemon=True).start()
    return {
        "status": "queued",
        "started_at": started_at,
        "message": "Model training queued. Results will appear when the background job finishes.",
    }


def _set_prop_sync_progress(
    *,
    conn=None,
    stage: str | None = None,
    stage_index: int | None = None,
    stage_total: int | None = None,
    current: int | None = None,
    total: int | None = None,
    message: str | None = None,
    scope: str | None = None,
    target_game_ids: list[int] | None = None,
) -> None:
    _mutate_prop_sync_state(
        conn=conn,
        status="running",
        stage=stage if stage is not None else _UNSET,
        stage_index=max(0, int(stage_index)) if stage_index is not None else _UNSET,
        stage_total=max(1, int(stage_total)) if stage_total is not None else _UNSET,
        current=max(0, int(current)) if current is not None else _UNSET,
        total=max(0, int(total)) if total is not None else _UNSET,
        message=message if message is not None else _UNSET,
        scope=scope if scope is not None else _UNSET,
        target_game_ids=target_game_ids if target_game_ids is not None else _UNSET,
    )


def _begin_prop_sync_job(
    scope: str,
    *,
    stage: str = "queued",
    message: str = "Queued for background processing.",
    target_game_ids: list[int] | None = None,
) -> str:
    started_at = datetime.now(timezone.utc).isoformat()
    job_id = _create_prop_sync_job_record(
        scope,
        started_at=started_at,
        status="queued",
        message=message,
        target_game_ids=target_game_ids,
    )
    _mutate_prop_sync_state(
        job_id=job_id,
        running=True,
        started_at=started_at,
        finished_at=None,
        last_error=None,
        last_result=None,
        status="queued",
        scope=scope,
        target_game_ids=list(target_game_ids or []),
        stage=stage,
        stage_index=0,
        stage_total=1,
        current=0,
        total=0,
        message=message,
    )
    return started_at


def _row_to_prop_sync_state(row) -> dict[str, Any] | None:
    if row is None:
        return None
    try:
        target_game_ids = json.loads(row["target_game_ids_json"] or "[]")
    except (TypeError, json.JSONDecodeError):
        target_game_ids = []
    try:
        last_result = json.loads(row["last_result_json"]) if row["last_result_json"] else None
    except (TypeError, json.JSONDecodeError):
        last_result = None
    state = {
        "job_id": int(row["id"]),
        "running": str(row["status"] or "") in {"queued", "running"},
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "last_error": row["last_error"],
        "last_result": last_result,
        "status": row["status"],
        "scope": row["scope"],
        "target_game_ids": list(target_game_ids) if isinstance(target_game_ids, list) else [],
        "stage": row["stage"],
        "stage_index": int(row["stage_index"] or 0),
        "stage_total": int(row["stage_total"] or 1),
        "current": int(row["current_count"] or 0),
        "total": int(row["total_count"] or 0),
        "percent": float(row["percent"] or 0.0),
        "message": row["message"],
        "updated_at": row["updated_at"],
    }
    return _mark_stale_prop_sync_state(state)


def _parse_iso_datetime(value: Any) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _mark_stale_prop_sync_state(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if state is None or not state.get("running"):
        return state
    updated_at = _parse_iso_datetime(state.get("updated_at")) or _parse_iso_datetime(state.get("started_at"))
    if updated_at is None:
        return state
    age_seconds = (datetime.now(timezone.utc) - updated_at.astimezone(timezone.utc)).total_seconds()
    if age_seconds < PROP_SYNC_STALE_SECONDS:
        return state
    stale_state = dict(state)
    stale_state["running"] = False
    stale_state["status"] = "stale"
    stale_state["message"] = stale_state.get("message") or "Recovered stale queued/running job."
    stale_state["finished_at"] = stale_state.get("finished_at") or stale_state.get("updated_at") or stale_state.get("started_at")
    stale_state["last_error"] = stale_state.get("last_error") or "Recovered stale queued/running job."
    return stale_state


def _latest_prop_sync_job_state() -> dict[str, Any] | None:
    try:
        with connect() as conn:
            row = conn.execute(
                """
                SELECT *
                FROM prop_sync_jobs
                ORDER BY started_at DESC, id DESC
                LIMIT 1
                """
            ).fetchone()
    except Exception as exc:
        print(f"[prop-sync] unable to read latest job record: {exc}")
        return None
    return _row_to_prop_sync_state(row)


def _resolved_prop_sync_state() -> tuple[dict[str, Any], dict[str, Any] | None]:
    with _PROP_SYNC_LOCK:
        sync_state = dict(_PROP_SYNC_STATE)
    latest_job = _latest_prop_sync_job_state()
    if not latest_job:
        return sync_state, None

    sync_updated_at = _parse_cache_timestamp(sync_state.get("updated_at"))
    latest_updated_at = _parse_cache_timestamp(latest_job.get("updated_at"))
    sync_job_id = sync_state.get("job_id")
    latest_job_id = latest_job.get("job_id")

    if sync_job_id is None:
        return latest_job, latest_job
    if latest_job_id is not None and sync_job_id != latest_job_id:
        # The persisted job table is the durable source of truth across
        # container restarts and background-thread handoffs. When it reports a
        # different job id, prefer that record over older in-memory state.
        if latest_job_id > int(sync_job_id):
            return latest_job, latest_job
        if latest_updated_at and (not sync_updated_at or latest_updated_at >= sync_updated_at):
            return latest_job, latest_job
        return sync_state, latest_job
    if latest_updated_at and (not sync_updated_at or latest_updated_at > sync_updated_at):
        return latest_job, latest_job
    if not sync_state.get("started_at"):
        return latest_job, latest_job
    return sync_state, latest_job


def _sqlite_sidecar_info(path: Path) -> dict[str, Any]:
    return {
        "path": str(path),
        "exists": path.exists(),
        "size_bytes": path.stat().st_size if path.exists() else 0,
    }


def _sqlite_checkpoint_payload(row: Any) -> dict[str, int] | None:
    if row is None:
        return None
    values = tuple(row)
    if len(values) < 3:
        return None
    return {
        "busy": int(values[0]),
        "log_frames": int(values[1]),
        "checkpointed_frames": int(values[2]),
    }


def _audit_sqlite_lock(timeout_seconds: float = 1.0) -> dict[str, Any]:
    if using_turso():
        return {
            "engine": "turso",
            "status": "unsupported",
            "locked": False,
            "message": "Lock audit is only available when the app is using local SQLite.",
        }

    db_path = get_db_path()
    wal_path = db_path.with_suffix(f"{db_path.suffix}-wal")
    shm_path = db_path.with_suffix(f"{db_path.suffix}-shm")
    payload: dict[str, Any] = {
        "engine": "sqlite",
        "status": "ok",
        "locked": False,
        "db_path": str(db_path),
        "db_exists": db_path.exists(),
        "wal": _sqlite_sidecar_info(wal_path),
        "shm": _sqlite_sidecar_info(shm_path),
        "writable": False,
        "checkpoint": None,
        "message": "SQLite write path is available.",
    }
    if not db_path.exists():
        payload["status"] = "missing"
        payload["message"] = "SQLite database file does not exist."
        return payload

    conn = sqlite3.connect(db_path, timeout=timeout_seconds, isolation_level=None)
    try:
        conn.execute(f"PRAGMA busy_timeout = {max(1, int(timeout_seconds * 1000))}")
        try:
            payload["checkpoint"] = _sqlite_checkpoint_payload(conn.execute("PRAGMA wal_checkpoint(PASSIVE)").fetchone())
        except sqlite3.OperationalError as exc:
            if _is_sqlite_locked_error(exc):
                payload["locked"] = True
                payload["status"] = "db_locked"
                payload["message"] = "SQLite is currently locked by an active writer."
                payload["checkpoint_error"] = str(exc)
            else:
                raise
        try:
            conn.execute("BEGIN IMMEDIATE")
            payload["writable"] = True
        except sqlite3.OperationalError as exc:
            if _is_sqlite_locked_error(exc):
                payload["locked"] = True
                payload["status"] = "db_locked"
                payload["message"] = "SQLite is currently locked by an active writer."
                payload["write_probe_error"] = str(exc)
            else:
                raise
        finally:
            if payload["writable"]:
                conn.execute("ROLLBACK")
    finally:
        conn.close()
    return payload


def _recover_sqlite_lock() -> dict[str, Any]:
    audit = _audit_sqlite_lock()
    if audit.get("engine") != "sqlite":
        return audit
    if audit.get("status") == "missing":
        return audit
    if audit.get("locked"):
        return {
            **audit,
            "recovered": False,
            "message": "SQLite is actively locked by another writer. Stop the running write job or extra backend instance before retrying recovery.",
        }
    if not _DB_MAINTENANCE_LOCK.acquire(blocking=False):
        return {
            **audit,
            "status": "busy",
            "recovered": False,
            "message": "Database maintenance is already running.",
        }

    db_path = get_db_path()
    try:
        conn = sqlite3.connect(db_path, timeout=1, isolation_level=None)
        try:
            conn.execute("PRAGMA busy_timeout = 1000")
            try:
                conn.execute("BEGIN IMMEDIATE")
                checkpoint = _sqlite_checkpoint_payload(conn.execute("PRAGMA wal_checkpoint(TRUNCATE)").fetchone())
                conn.execute("ROLLBACK")
            except sqlite3.OperationalError as exc:
                if not _is_sqlite_locked_error(exc):
                    raise
                try:
                    conn.execute("ROLLBACK")
                except sqlite3.Error:
                    pass
                refreshed = _audit_sqlite_lock()
                return {
                    **refreshed,
                    "status": "db_locked",
                    "recovered": False,
                    "checkpoint": None,
                    "message": "SQLite is still locked by another writer. Stop the running write job or extra backend instance before retrying recovery.",
                    "recovery_error": str(exc),
                }
        finally:
            conn.close()
    finally:
        _DB_MAINTENANCE_LOCK.release()

    refreshed = _audit_sqlite_lock()
    return {
        **refreshed,
        "recovered": not refreshed.get("locked", False),
        "checkpoint": checkpoint,
        "message": (
            "SQLite recovery completed. WAL checkpoint/truncate succeeded and the database accepted a write probe."
            if not refreshed.get("locked", False)
            else "SQLite remains locked after recovery attempt."
        ),
    }


def _parse_cache_timestamp(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


def _cache_file_is_stale(path: Path, today_iso: str) -> bool:
    if path.suffix != ".json":
        return False
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return True

    if not isinstance(payload, dict):
        return False

    if path.name.startswith(APP_RESPONSE_CACHE_PREFIX) or path.name in READ_CACHE_FILES:
        cached_at = _parse_cache_timestamp(payload.get("cached_at"))
        cache_date = payload.get("cache_date")
        ttl_seconds = payload.get("ttl_seconds")
        if cached_at is None or not isinstance(ttl_seconds, int):
            return True
        if isinstance(cache_date, str) and cache_date:
            if cache_date != today_iso:
                return True
        return datetime.now(timezone.utc) - cached_at > timedelta(seconds=ttl_seconds)

    captured_at = _parse_cache_timestamp(payload.get("captured_at"))
    if captured_at is not None:
        return captured_at.astimezone(LOCAL_TZ).date().isoformat() != today_iso

    cache_date = payload.get("cache_date")
    if isinstance(cache_date, str) and cache_date:
        return cache_date != today_iso

    return False


def _audit_stale_payloads() -> dict[str, Any]:
    cache_dir = get_cache_dir()
    if not cache_dir.exists():
        return {"stale": 0, "checked": 0, "files": [], "message": "No cache directory found."}
    today_iso = _local_today_iso()
    stale_files: list[str] = []
    checked = 0
    for path in sorted(cache_dir.iterdir()):
        if not path.is_file():
            continue
        checked += 1
        if _cache_file_is_stale(path, today_iso):
            stale_files.append(path.name)
    return {
        "stale": len(stale_files),
        "checked": checked,
        "files": stale_files,
        "message": "Stale cache payloads found." if stale_files else "No stale cache payloads found.",
    }


def _delete_stale_payloads() -> dict[str, Any]:
    audit = _audit_stale_payloads()
    deleted: list[str] = []
    for name in audit["files"]:
        path = get_cache_dir() / name
        try:
            path.unlink()
            deleted.append(name)
        except OSError:
            continue
    return {
        "deleted": len(deleted),
        "checked": int(audit["checked"]),
        "files": deleted,
        "message": "Deleted stale cache payloads." if deleted else "No stale cache payloads found.",
    }


def _delete_app_response_caches() -> None:
    cache_dir = get_cache_dir()
    if not cache_dir.exists():
        return
    for path in cache_dir.iterdir():
        if not path.is_file():
            continue
        if not path.name.startswith(APP_RESPONSE_CACHE_PREFIX):
            continue
        try:
            path.unlink()
        except OSError:
            continue

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "http://localhost:5173",
        "http://127.0.0.1:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5174",
        "http://localhost:5184",
        "http://127.0.0.1:5184",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def on_startup() -> None:
    if not _is_dev_env() and not (_configured_api_key() or _bootstrap_admin_configured()):
        raise RuntimeError("Configure either API_KEY or ADMIN_USERNAME/ADMIN_PASSWORD when ENV is not dev/local/test.")
    if _is_dev_env() and not _configured_api_key():
        print("[security] API_KEY not set; mutating endpoints are open in dev/test mode.")
    stale_cleanup = _delete_stale_payloads()
    if stale_cleanup["deleted"]:
        print(
            f"[cache] deleted {stale_cleanup['deleted']} stale payload(s) on startup: "
            f"{', '.join(stale_cleanup['files'])}"
        )
    init_db()
    with connect() as conn:
        ensure_teams(conn)
    _start_model_prewarm()
    _start_read_payload_prewarm()


def _start_model_prewarm() -> None:
    def _worker() -> None:
        try:
            with connect() as conn:
                prewarm_model_cache(conn)
        except Exception as exc:
            print(f"[startup] model prewarm skipped: {exc}")

    thread = threading.Thread(target=_worker, name="model-prewarm", daemon=True)
    thread.start()


def _start_read_payload_prewarm() -> None:
    def _worker() -> None:
        try:
            with connect() as conn:
                _publish_current_read_payloads(conn)
        except Exception as exc:
            print(f"[startup] read payload prewarm skipped: {exc}")

    thread = threading.Thread(target=_worker, name="read-payload-prewarm", daemon=True)
    thread.start()


@app.middleware("http")
async def cache_api_get_responses(request: Request, call_next):
    cache_name = _app_response_cache_name(request) if _should_cache_app_response(request) else None
    if cache_name is not None:
        cached = _read_app_response_cache(cache_name)
        if cached is not None:
            response = _cached_app_response(cached, "HIT")
            return _with_conditional_etag(request, response, response.body)
    try:
        response = await call_next(request)
    except sqlite3.OperationalError as exc:
        if cache_name and _is_sqlite_locked_error(exc):
            cached = _read_app_response_cache(cache_name, allow_stale=True)
            if cached is not None:
                print(f"[app-cache] {request.url.path} serving stale payload after sqlite lock: {exc}")
                return _cached_app_response(cached, "STALE")
        raise

    if cache_name is None or response.status_code != 200:
        return response

    if response.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
        return response

    if hasattr(response, "body_iterator"):
        body = b""
        async for chunk in response.body_iterator:
            body += chunk
    else:
        body = getattr(response, "body", b"")
    rebuilt = Response(
        content=body,
        status_code=response.status_code,
        headers=dict(response.headers),
        media_type=response.media_type,
    )
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return rebuilt
    write_json_cache(cache_name, _app_response_cache_envelope(response.status_code, payload))
    rebuilt.headers["X-App-Cache"] = "STORE"
    return _with_conditional_etag(request, rebuilt, body)


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/special/stocks")
def special_stocks() -> list[dict[str, Any]]:
    path = get_tracking_db_path()
    if not path.exists():
        return []
    today_iso = _local_today_iso()
    visible_statuses = {"scheduled", "in_progress", "final"}
    visible_rows: list[dict[str, Any]] = []
    with connect() as conn:
        prune_special_snapshots(conn)
        rows = list_special_stocks()
        if not rows:
            return []
        requested_pairs = sorted(
            {
                (int(item["game_id"]), int(item["player_id"]))
                for item in rows
                if item.get("game_id") is not None and item.get("player_id") is not None
            }
        )
        if not requested_pairs:
            return []
        board_summaries = {
            int(row["game_id"]): row
            for row in list_game_board_summaries(
                game_ids=sorted({game_id for game_id, _ in requested_pairs}),
            )
        }
        active_prop_pairs = _active_special_prop_pairs(conn, requested_pairs)
        game_rows = _special_game_rows_by_pair(conn, requested_pairs)
        recent_history = _special_recent_history_by_pair(conn, requested_pairs, limit=5)
        for item in rows:
            game_id = item.get("game_id")
            player_id = item.get("player_id")
            if game_id is None or player_id is None:
                continue
            pair = (int(game_id), int(player_id))
            if pair not in active_prop_pairs:
                continue
            game_row = game_rows.get(pair)
            if game_row is None:
                continue
            game_date = _app_local_game_date(game_row["game_date"], game_row["start_time"])
            if game_date != today_iso:
                continue
            game_status = str(game_row["status"] or "").lower()
            if game_status not in visible_statuses:
                continue
            item["start_time"] = game_row["start_time"]
            item["home_team"] = game_row["home_team"]
            item["away_team"] = game_row["away_team"]
            item["game_status"] = game_status
            item["position"] = game_row["position"]
            item["team"] = game_row["team"]
            item["team_logo_url"] = game_row["team_logo_url"]
            board_summary = board_summaries.get(int(game_id))
            if board_summary is not None:
                item["board_player_count"] = int(board_summary.get("player_count") or 0)
                item["board_candidate_count_50_plus"] = int(board_summary.get("candidate_count_50_plus") or 0)
                item["board_candidate_threshold"] = float(
                    board_summary.get("candidate_threshold") or SPECIALS_DEFAULT_HIGH_THRESHOLD
                )
                item["board_candidate_count_threshold"] = int(board_summary.get("candidate_count_threshold") or 0)
                item["board_avg_prob_2_plus"] = float(board_summary.get("avg_prob_2_plus") or 0.0)
                item["board_avg_prob_3_plus"] = float(board_summary.get("avg_prob_3_plus") or 0.0)
                item["board_top_projected_stocks"] = float(board_summary.get("top_projected_stocks") or 0.0)
                item["board_top_prob_2_plus"] = float(board_summary.get("top_prob_2_plus") or 0.0)
                item["board_top_player_name"] = board_summary.get("top_player_name")
            item["recent_values"] = list(recent_history.get(pair, {}).get("recent_values", []))
            item["recent_minutes"] = list(recent_history.get(pair, {}).get("recent_minutes", []))
            visible_rows.append(item)
    return visible_rows


@app.get("/api/special/stats")
def special_stocks_stats() -> dict[str, Any]:
    empty_buckets = [
        {"label": "0-40%", "min_prob": 0.0, "max_prob": 0.4, "count": 0, "hits": 0, "avg_prob": None, "hit_rate": None},
        {"label": "40-50%", "min_prob": 0.4, "max_prob": 0.5, "count": 0, "hits": 0, "avg_prob": None, "hit_rate": None},
        {"label": "50-60%", "min_prob": 0.5, "max_prob": 0.6, "count": 0, "hits": 0, "avg_prob": None, "hit_rate": None},
        {"label": "60%+", "min_prob": 0.6, "max_prob": None, "count": 0, "hits": 0, "avg_prob": None, "hit_rate": None},
    ]
    path = get_tracking_db_path()
    if not path.exists():
        return {
            "total_latest": 0,
            "settled_count": 0,
            "pending_count": 0,
            "hits_2_plus": 0,
            "hit_rate_2_plus": None,
            "avg_prob_2_plus": None,
            "hits_3_plus": 0,
            "hit_rate_3_plus": None,
            "avg_prob_3_plus": None,
            "candidate_count_50_plus": 0,
            "candidate_hits_2_plus": 0,
            "candidate_hit_rate_2_plus": None,
            "recommended_candidate_threshold": SPECIALS_DEFAULT_HIGH_THRESHOLD,
            "recommended_candidate_count": 0,
            "recommended_candidate_hits_2_plus": 0,
            "recommended_candidate_hit_rate_2_plus": None,
            "threshold_recommendations": default_special_threshold_recommendations(),
            "calibration_buckets": empty_buckets,
            "calibration_buckets_3_plus": empty_buckets,
        }
    with connect() as conn:
        prune_special_snapshots(conn)
        rows = list_special_stocks()
    with sqlite3.connect(path) as tracking:
        tracking.row_factory = sqlite3.Row
        settled_rows = settled_latest_special_rows(tracking)
    pending_count = sum(1 for row in rows if _row_value(row, "actual_stocks") is None)
    hits_2_plus = sum(1 for row in settled_rows if float(_row_value(row, "actual_stocks") or 0.0) >= 2.0)
    avg_prob_2_plus = (
        sum(float(_row_value(row, "stocks_prob_2_plus") or 0.0) for row in settled_rows) / len(settled_rows)
        if settled_rows
        else None
    )
    hits_3_plus = sum(1 for row in settled_rows if float(_row_value(row, "actual_stocks") or 0.0) >= 3.0)
    avg_prob_3_plus = (
        sum(float(_row_value(row, "stocks_prob_3_plus") or 0.0) for row in settled_rows) / len(settled_rows)
        if settled_rows
        else None
    )
    candidate_rows = [row for row in settled_rows if float(_row_value(row, "stocks_prob_2_plus") or 0.0) >= 0.5]
    candidate_hits_2_plus = sum(1 for row in candidate_rows if float(_row_value(row, "actual_stocks") or 0.0) >= 2.0)
    bucket_defs = [
        ("0-40%", 0.0, 0.4),
        ("40-50%", 0.4, 0.5),
        ("50-60%", 0.5, 0.6),
        ("60%+", 0.6, None),
    ]
    def _calibration_buckets(prob_key: str, hit_threshold: float) -> list[dict[str, Any]]:
        buckets: list[dict[str, Any]] = []
        for label, min_prob, max_prob in bucket_defs:
            bucket_rows = [
                row for row in settled_rows
                if float(_row_value(row, prob_key) or 0.0) >= min_prob
                and (max_prob is None or float(_row_value(row, prob_key) or 0.0) < max_prob)
            ]
            bucket_hits = sum(1 for row in bucket_rows if float(_row_value(row, "actual_stocks") or 0.0) >= hit_threshold)
            avg_bucket_prob = (
                sum(float(_row_value(row, prob_key) or 0.0) for row in bucket_rows) / len(bucket_rows)
                if bucket_rows
                else None
            )
            buckets.append({
                "label": label,
                "min_prob": min_prob,
                "max_prob": max_prob,
                "count": len(bucket_rows),
                "hits": bucket_hits,
                "avg_prob": round(avg_bucket_prob, 4) if avg_bucket_prob is not None else None,
                "hit_rate": round(bucket_hits / len(bucket_rows), 4) if bucket_rows else None,
            })
        return buckets

    calibration_buckets = _calibration_buckets("stocks_prob_2_plus", 2.0)
    calibration_buckets_3_plus = _calibration_buckets("stocks_prob_3_plus", 3.0)
    threshold_recommendations = fit_special_threshold_recommendations(settled_rows)
    recommended_candidate_threshold = float(
        threshold_recommendations.get("high_confidence_threshold") or SPECIALS_DEFAULT_HIGH_THRESHOLD
    )
    recommended_candidate_rows = [
        row for row in settled_rows if float(_row_value(row, "stocks_prob_2_plus") or 0.0) >= recommended_candidate_threshold
    ]
    recommended_candidate_hits_2_plus = sum(
        1 for row in recommended_candidate_rows if float(_row_value(row, "actual_stocks") or 0.0) >= 2.0
    )
    return {
        "total_latest": len(rows),
        "settled_count": len(settled_rows),
        "pending_count": pending_count,
        "hits_2_plus": hits_2_plus,
        "hit_rate_2_plus": round(hits_2_plus / len(settled_rows), 4) if settled_rows else None,
        "avg_prob_2_plus": round(avg_prob_2_plus, 4) if avg_prob_2_plus is not None else None,
        "hits_3_plus": hits_3_plus,
        "hit_rate_3_plus": round(hits_3_plus / len(settled_rows), 4) if settled_rows else None,
        "avg_prob_3_plus": round(avg_prob_3_plus, 4) if avg_prob_3_plus is not None else None,
        "candidate_count_50_plus": len(candidate_rows),
        "candidate_hits_2_plus": candidate_hits_2_plus,
        "candidate_hit_rate_2_plus": round(candidate_hits_2_plus / len(candidate_rows), 4) if candidate_rows else None,
        "recommended_candidate_threshold": round(recommended_candidate_threshold, 2),
        "recommended_candidate_count": len(recommended_candidate_rows),
        "recommended_candidate_hits_2_plus": recommended_candidate_hits_2_plus,
        "recommended_candidate_hit_rate_2_plus": (
            round(recommended_candidate_hits_2_plus / len(recommended_candidate_rows), 4)
            if recommended_candidate_rows
            else None
        ),
        "threshold_recommendations": threshold_recommendations,
        "calibration_buckets": calibration_buckets,
        "calibration_buckets_3_plus": calibration_buckets_3_plus,
    }


@app.get("/api/ops/health")
@app.get("/api/operations/health")
def ops_health() -> dict[str, Any]:
    sync_state, latest_job = _resolved_prop_sync_state()
    with _MODEL_TRAIN_LOCK:
        model_training_state = dict(_MODEL_TRAIN_STATE)
    return {
        "status": "ok",
        "prop_sync": sync_state,
        "prop_sync_job": latest_job,
        "model_training": model_training_state,
        "runtime_storage": {
            "db_path": str(get_db_path()),
            "training_db_path": str(get_training_db_path()),
            "cache_dir": str(get_cache_dir()),
            "snapshot_dir": str(get_snapshot_dir()),
            "source_of_truth": "wnba.sqlite",
            "training_store": "wnba-training.sqlite",
            "team_boxscores_table": "team_game_boxscores",
            "team_results_table": "team_game_results",
            "team_boxscores_flow": "Raw ESPN team boxscores persist in team_game_boxscores. Training DB remains derived-only.",
        },
    }


def _is_dev_env() -> bool:
    return os.getenv("ENV", "dev").strip().lower() in {"dev", "local", "test"}


def _bootstrap_admin_configured() -> bool:
    return bool(os.getenv("ADMIN_USERNAME", "").strip() and os.getenv("ADMIN_PASSWORD", "").strip())


def _configured_api_key() -> str | None:
    value = os.getenv("API_KEY")
    if not value:
        return None
    return value.strip() or None


def _presented_api_key(x_api_key: str | None, authorization: str | None) -> str | None:
    if x_api_key and x_api_key.strip():
        return x_api_key.strip()
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
        return token or None
    return None


def _enforce_api_key(x_api_key: str | None, authorization: str | None) -> None:
    configured = _configured_api_key()
    if configured is None:
        if _is_dev_env():
            return
        raise HTTPException(status_code=500, detail="API key authentication is not configured.")
    presented = _presented_api_key(x_api_key, authorization)
    if presented != configured:
        raise HTTPException(status_code=401, detail="Unauthorized")


def _session_cookie_secure(request: Request | None = None) -> bool:
    override = os.getenv("SESSION_COOKIE_SECURE", "").strip().lower()
    if override in {"1", "true", "yes", "on"}:
        return True
    if override in {"0", "false", "no", "off"}:
        return False
    if request is not None:
        forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
        if forwarded_proto:
            return forwarded_proto == "https"
        return request.url.scheme.lower() == "https"
    return not _is_dev_env()


def _auth_payload(user: SessionUser | None) -> dict[str, Any]:
    return {
        "authenticated": user is not None,
        "user": {
            "username": user.username,
            "is_admin": user.is_admin,
        } if user is not None else None,
        "csrf_token": user.csrf_token if user is not None else None,
    }


def _current_session_user(request: Request) -> SessionUser | None:
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    with connect() as conn:
        return get_session_user(conn, session_token)


def _origin_allowed(request: Request) -> bool:
    origin = request.headers.get("origin")
    if not origin:
        return True
    forwarded_proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip()
    forwarded_host = (request.headers.get("x-forwarded-host") or "").split(",")[0].strip()
    host = forwarded_host or (request.headers.get("host") or "").strip()
    scheme = forwarded_proto or request.url.scheme
    if not host or not scheme:
        try:
            expected = str(request.base_url).rstrip("/")
        except Exception:
            return False
        return origin.rstrip("/") == expected
    expected = f"{scheme}://{host}".rstrip("/")
    return origin.rstrip("/") == expected


def _require_admin_session_or_api_key(
    request: Request,
    x_api_key: str | None,
    authorization: str | None,
    x_csrf_token: str | None,
) -> None:
    user = _current_session_user(request)
    if user is not None and user.is_admin:
        origin_ok = _origin_allowed(request)
        csrf_ok = bool(x_csrf_token and x_csrf_token.strip() == user.csrf_token)
        if not origin_ok and not csrf_ok:
            raise HTTPException(status_code=403, detail="Admin session verification failed.")
        return
    _enforce_api_key(x_api_key, authorization)


def _consume_rate_limit(client_key: str, scope: str, capacity: float, refill_per_sec: float) -> None:
    now = time.monotonic()
    bucket_key = (client_key, scope)
    with _RATE_LOCK:
        tokens, last = _RATE_BUCKETS.get(bucket_key, (capacity, now))
        elapsed = max(0.0, now - last)
        tokens = min(capacity, tokens + elapsed * refill_per_sec)
        if tokens < 1.0:
            retry_after = max(1, int((1.0 - tokens) / max(refill_per_sec, 0.001)))
            raise HTTPException(status_code=429, detail="Rate limit exceeded", headers={"Retry-After": str(retry_after)})
        _RATE_BUCKETS[bucket_key] = (tokens - 1.0, now)


def _client_key(request: Request) -> str:
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def _resolve_roster_player(conn: Any, team_abbreviation: str, player_name: str) -> dict[str, Any] | None:
    return resolve_player_identity(conn, team_abbreviation, player_name, prefer_rich=True)


def _best_roster_candidate(candidates: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not candidates:
        return None
    rich = [candidate for candidate in candidates if not player_is_skeletal(candidate)]
    return rich[0] if rich else candidates[0]


def _dedupe_roster_candidates(candidates: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[int] = set()
    deduped: list[dict[str, Any]] = []
    for candidate in candidates:
        player_id = int(candidate["player_id"])
        if player_id in seen:
            continue
        seen.add(player_id)
        deduped.append(candidate)
    return deduped


def _build_roster_player_index(player_rows: list[Any]) -> dict[str, dict[Any, list[dict[str, Any]]]]:
    exact_team_name: dict[tuple[str, str], list[dict[str, Any]]] = {}
    team_normalized: dict[tuple[str, str], list[dict[str, Any]]] = {}
    team_initial_last: dict[tuple[str, str, str], list[dict[str, Any]]] = {}
    team_last: dict[tuple[str, str], list[dict[str, Any]]] = {}
    global_normalized: dict[str, list[dict[str, Any]]] = {}
    global_initial_last: dict[tuple[str, str], list[dict[str, Any]]] = {}

    for row in player_rows:
        candidate = dict(row)
        team = str(candidate.get("team_abbreviation") or "").strip().upper()
        full_name = str(candidate.get("full_name") or "").strip()
        if not team or not full_name:
            continue
        lowered_name = full_name.lower()
        normalized_name, first_initial, last_name = player_lookup_parts(full_name)

        exact_team_name.setdefault((team, lowered_name), []).append(candidate)
        if normalized_name:
            team_normalized.setdefault((team, normalized_name), []).append(candidate)
            global_normalized.setdefault(normalized_name, []).append(candidate)
        if first_initial and last_name:
            team_initial_last.setdefault((team, first_initial, last_name), []).append(candidate)
            global_initial_last.setdefault((first_initial, last_name), []).append(candidate)
        if last_name:
            team_last.setdefault((team, last_name), []).append(candidate)

    return {
        "exact_team_name": exact_team_name,
        "team_normalized": team_normalized,
        "team_initial_last": team_initial_last,
        "team_last": team_last,
        "global_normalized": global_normalized,
        "global_initial_last": global_initial_last,
    }


def _resolve_roster_player_display_fast(
    team_abbreviation: str,
    player_name: str,
    player_index: dict[str, dict[Any, list[dict[str, Any]]]],
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    normalized_team = normalize_team_abbreviation(team_abbreviation) or team_abbreviation.upper()
    normalized_name, first_initial, last_name = player_lookup_parts(player_name)
    lowered_name = str(player_name or "").strip().lower()

    exact_matches = _dedupe_roster_candidates(
        player_index["exact_team_name"].get((normalized_team, lowered_name), [])
    )
    player = _best_roster_candidate(exact_matches)

    if player is None:
        team_candidates: list[dict[str, Any]] = []
        if normalized_name:
            team_candidates.extend(player_index["team_normalized"].get((normalized_team, normalized_name), []))
        if first_initial and last_name:
            team_candidates.extend(player_index["team_initial_last"].get((normalized_team, first_initial, last_name), []))
        if last_name:
            team_candidates.extend(player_index["team_last"].get((normalized_team, last_name), []))
        player = _best_roster_candidate(_dedupe_roster_candidates(team_candidates))
    return player, None


def _player_has_team_history(conn: Any, player_id: int, team_abbreviation: str) -> bool:
    normalized_team = normalize_team_abbreviation(team_abbreviation) or team_abbreviation.upper()
    row = conn.execute(
        """
        SELECT 1
        FROM player_team_history h
        JOIN teams t ON t.id = h.team_id
        WHERE h.player_id = ?
          AND upper(t.abbreviation) = ?
        LIMIT 1
        """,
        (int(player_id), normalized_team),
    ).fetchone()
    return row is not None


def _resolve_roster_player_display(
    conn: Any,
    team_abbreviation: str,
    player_name: str,
    *,
    player_rows: list[Any] | None = None,
    player_index: dict[str, dict[Any, list[dict[str, Any]]]] | None = None,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    normalized_team = normalize_team_abbreviation(team_abbreviation) or team_abbreviation.upper()
    if player_index is not None:
        player, display_fallback = _resolve_roster_player_display_fast(team_abbreviation, player_name, player_index)
        if player is not None:
            return player, display_fallback
        player = resolve_player_identity(
            conn,
            team_abbreviation,
            player_name,
            prefer_rich=True,
            player_rows=player_rows,
        )
        if player is None:
            return None, None
        if str(player.get("team_abbreviation") or "").strip().upper() == normalized_team:
            return player, None
        if _player_has_team_history(conn, int(player["player_id"]), normalized_team):
            return player, None
        return None, None
    player = resolve_player_identity(
        conn,
        team_abbreviation,
        player_name,
        prefer_rich=True,
        player_rows=player_rows,
    )
    return player, None


def _player_recent_profile(conn: Any, player_id: int) -> dict[str, float | None]:
    row = conn.execute(
        """
        SELECT
            AVG(sample.minutes) AS recent_minutes_avg,
            AVG(sample.contrib) AS recent_contribution_avg
        FROM (
            SELECT
                s.minutes AS minutes,
                (s.points + (0.70 * s.rebounds) + (0.70 * s.assists)) AS contrib
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            WHERE s.player_id = ?
            ORDER BY g.game_date DESC
            LIMIT 10
        ) sample
        """,
        (player_id,),
    ).fetchone()
    if not row:
        return {
            "recent_minutes_avg": None,
            "recent_contribution_avg": None,
        }
    return {
        "recent_minutes_avg": round(float(row["recent_minutes_avg"]), 1) if row["recent_minutes_avg"] is not None else None,
        "recent_contribution_avg": round(float(row["recent_contribution_avg"]), 1) if row["recent_contribution_avg"] is not None else None,
    }


def _roster_status_weight(status: str) -> float:
    return {
        "OUT": 1.0,
        "INACTIVE": 1.0,
        "SUSPENDED": 1.0,
        "UNAVAILABLE": 1.0,
        "DOUBTFUL": 0.75,
        "QUESTIONABLE": 0.35,
        "GTD": 0.35,
        "PROBABLE": 0.10,
    }.get(status.strip().upper(), 0.0)


def _roster_role_weight(role: str | None) -> float:
    return {
        "star": 1.25,
        "starter": 1.0,
        "rotation": 0.75,
        "bench": 0.5,
    }.get(str(role or "starter").strip().lower(), 0.9)


def _roster_team_injury_impacts(conn: Any, team_ids: dict[str, int]) -> dict[str, dict[str, float | int]]:
    default_team_impact = {"factor": 1.0, "missing_key_players": 0, "penalty_points": 0.0}
    if not team_ids:
        return {}

    ordered = sorted({int(team_id) for team_id in team_ids.values() if int(team_id) > 0})
    if not ordered:
        return {}

    placeholders = ",".join("?" for _ in ordered)
    rows = conn.execute(
        f"""
        WITH latest_injuries AS (
            SELECT team_id, player_id, status, rotation_role
            FROM (
                SELECT
                    p.team_id,
                    p.id AS player_id,
                    lower(trim(i.status)) AS status,
                    p.rotation_role,
                    ROW_NUMBER() OVER (
                        PARTITION BY i.player_id
                        ORDER BY i.captured_at DESC, i.id DESC
                    ) AS rn
                FROM injuries i
                JOIN players p ON p.id = i.player_id
                WHERE p.team_id IN ({placeholders})
            )
            WHERE rn = 1
        ),
        ranked_contrib AS (
            SELECT
                s.player_id,
                (s.points + (0.70 * s.rebounds) + (0.70 * s.assists)) AS contrib,
                ROW_NUMBER() OVER (
                    PARTITION BY s.player_id
                    ORDER BY g.game_date DESC, s.game_id DESC
                ) AS rn
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            JOIN latest_injuries li ON li.player_id = s.player_id
        ),
        recent_contrib AS (
            SELECT player_id, AVG(contrib) AS contribution
            FROM ranked_contrib
            WHERE rn <= 10
            GROUP BY player_id
        )
        SELECT
            li.team_id,
            li.status,
            li.rotation_role,
            COALESCE(rc.contribution, 0.0) AS contribution
        FROM latest_injuries li
        LEFT JOIN recent_contrib rc ON rc.player_id = li.player_id
        """,
        tuple(ordered),
    ).fetchall()

    status_weight = {
        "out": 1.0,
        "inactive": 1.0,
        "suspended": 1.0,
        "unavailable": 1.0,
        "doubtful": 0.75,
        "questionable": 0.35,
        "probable": 0.10,
    }

    impacts_by_team_id: dict[int, dict[str, float | int]] = {
        team_id: dict(default_team_impact) for team_id in ordered
    }
    for row in rows:
        team_id = int(row["team_id"])
        status = str(row["status"] or "").strip()
        status_factor = status_weight.get(status)
        if status_factor is None:
            continue
        contribution = float(row["contribution"] or 0.0)
        if contribution <= 0:
            continue
        role_factor = _roster_role_weight(row["rotation_role"])
        weighted_impact = contribution * status_factor * role_factor
        current = impacts_by_team_id.setdefault(team_id, dict(default_team_impact))
        current["penalty_points"] = float(current["penalty_points"]) + weighted_impact
        if status_factor >= 0.75 and role_factor >= 1.0:
            current["missing_key_players"] = int(current["missing_key_players"]) + 1

    for team_id, impact in impacts_by_team_id.items():
        penalty_points = float(impact["penalty_points"])
        penalty_ratio = max(0.0, min((penalty_points / 18.0) * 0.08, 0.18))
        impact["factor"] = max(0.82, min(1.0 - penalty_ratio, 1.0))
        impact["penalty_points"] = round(penalty_points, 2)

    team_by_id = {team_id: team for team, team_id in team_ids.items()}
    return {
        team_by_id[team_id]: impacts_by_team_id.get(team_id, dict(default_team_impact))
        for team_id in ordered
        if team_id in team_by_id
    }


def _build_roster_enrichment(conn: Any, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not hasattr(conn, "execute"):
        return rows

    default_team_impact = {"factor": 1.0, "missing_key_players": 0, "penalty_points": 0.0}
    teams = sorted({str(row["team"]) for row in rows})
    team_ids: dict[str, int] = {}
    if teams:
        placeholders = ",".join("?" for _ in teams)
        team_rows = conn.execute(
            f"""
            SELECT id, upper(abbreviation) AS abbreviation
            FROM teams
            WHERE upper(abbreviation) IN ({placeholders})
            """,
            tuple(teams),
        ).fetchall()
        team_ids = {
            str(team_row["abbreviation"]): int(team_row["id"])
            for team_row in team_rows
            if team_row["id"] is not None and team_row["abbreviation"] is not None
        }
    team_impact_cache = _roster_team_injury_impacts(conn, team_ids)

    player_rows = conn.execute(
        """
        SELECT
            p.id AS player_id,
            p.full_name,
            p.team_id,
            t.abbreviation AS team_abbreviation,
            p.rotation_role,
            p.position
        FROM players p
        LEFT JOIN teams t ON t.id = p.team_id
        """
    ).fetchall()
    player_index = _build_roster_player_index(player_rows)

    resolved_rows: list[tuple[dict[str, Any], dict[str, Any] | None, dict[str, Any] | None]] = []
    profile_player_ids: set[int] = set()
    out_since_player_ids: set[int] = set()
    for row in rows:
        player, display_fallback = _resolve_roster_player_display(
            conn,
            str(row["team"]),
            str(row["player_name"]),
            player_rows=player_rows,
            player_index=player_index,
        )
        resolved_rows.append((row, player, display_fallback))
        if player:
            out_since_player_ids.add(int(player["player_id"]))
            profile_player = display_fallback or player
            profile_player_ids.add(int(profile_player["player_id"]))

    profile_by_player = _player_recent_profiles(conn, profile_player_ids)
    out_since_by_player = _roster_out_since_map(conn, out_since_player_ids)

    enriched: list[dict[str, Any]] = []
    for row, player, display_fallback in resolved_rows:
        team = str(row["team"])
        team_impact = team_impact_cache.get(team, default_team_impact)
        if not player:
            enriched.append(
                {
                    **row,
                    "rotation_role": None,
                    "position": None,
                    "out_since": None,
                    "recent_minutes_avg": None,
                    "recent_contribution_avg": None,
                    "player_impact_score": None,
                    "team_injury_factor": team_impact["factor"],
                    "team_missing_key_players": team_impact["missing_key_players"],
                    "team_penalty_points": team_impact["penalty_points"],
                }
            )
            continue

        profile_player = display_fallback or player
        profile = profile_by_player.get(int(profile_player["player_id"]), {"recent_minutes_avg": None, "recent_contribution_avg": None})
        position = player.get("position") or (display_fallback.get("position") if display_fallback else None)
        rotation_role = player.get("rotation_role") or (display_fallback.get("rotation_role") if display_fallback else None)
        contribution = float(profile["recent_contribution_avg"] or 0.0)
        impact_score = contribution * _roster_status_weight(str(row["status"])) * _roster_role_weight(rotation_role)
        enriched.append(
            {
                **row,
                "player_id": int(player["player_id"]),
                "rotation_role": rotation_role,
                "position": position,
                "out_since": out_since_by_player.get(int(player["player_id"])) if str(row["status"] or "").strip().lower() in _UNAVAILABLE_PLAYER_STATUSES else None,
                "recent_minutes_avg": profile["recent_minutes_avg"],
                "recent_contribution_avg": profile["recent_contribution_avg"],
                "player_impact_score": round(impact_score, 1) if impact_score > 0 else None,
                "team_injury_factor": team_impact["factor"],
                "team_missing_key_players": team_impact["missing_key_players"],
                "team_penalty_points": team_impact["penalty_points"],
            }
        )
    return enriched


def _player_recent_profiles(conn: Any, player_ids: set[int]) -> dict[int, dict[str, float | None]]:
    if not player_ids:
        return {}
    ordered_ids = sorted(int(player_id) for player_id in player_ids)
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"""
        WITH ranked AS (
            SELECT
                s.player_id,
                s.minutes AS minutes,
                (s.points + (0.70 * s.rebounds) + (0.70 * s.assists)) AS contrib,
                ROW_NUMBER() OVER (
                    PARTITION BY s.player_id
                    ORDER BY g.game_date DESC, s.game_id DESC
                ) AS rn
            FROM player_game_stats s
            JOIN games g ON g.id = s.game_id
            WHERE s.player_id IN ({placeholders})
        )
        SELECT
            player_id,
            AVG(minutes) AS recent_minutes_avg,
            AVG(contrib) AS recent_contribution_avg
        FROM ranked
        WHERE rn <= 10
        GROUP BY player_id
        """,
        tuple(ordered_ids),
    ).fetchall()
    profiles = {
        int(row["player_id"]): {
            "recent_minutes_avg": round(float(row["recent_minutes_avg"]), 1) if row["recent_minutes_avg"] is not None else None,
            "recent_contribution_avg": round(float(row["recent_contribution_avg"]), 1) if row["recent_contribution_avg"] is not None else None,
        }
        for row in rows
        if row["player_id"] is not None
    }
    for player_id in ordered_ids:
        profiles.setdefault(player_id, {"recent_minutes_avg": None, "recent_contribution_avg": None})
    return profiles


def _roster_out_since_map(conn: Any, player_ids: set[int]) -> dict[int, str | None]:
    if not player_ids:
        return {}
    ordered_ids = sorted(int(player_id) for player_id in player_ids)
    placeholders = ",".join("?" for _ in ordered_ids)
    rows = conn.execute(
        f"""
        SELECT player_id, lower(trim(status)) AS status, captured_at
        FROM injuries
        WHERE player_id IN ({placeholders})
        ORDER BY player_id, captured_at DESC, id DESC
        """,
        tuple(ordered_ids),
    ).fetchall()
    out_since: dict[int, str | None] = {player_id: None for player_id in ordered_ids}
    resolved_players: set[int] = set()
    for row in rows:
        player_id = int(row["player_id"])
        if player_id in resolved_players:
            continue
        status = str(row["status"] or "").strip().lower()
        if status not in _UNAVAILABLE_PLAYER_STATUSES:
            resolved_players.add(player_id)
            continue
        out_since[player_id] = str(row["captured_at"])
    return out_since


def _roster_out_since(conn: Any, player_id: int, status: str) -> str | None:
    if str(status or "").strip().lower() not in _UNAVAILABLE_PLAYER_STATUSES:
        return None
    rows = conn.execute(
        """
        SELECT lower(trim(status)) AS status, captured_at
        FROM injuries
        WHERE player_id = ?
        ORDER BY captured_at DESC, id DESC
        """,
        (int(player_id),),
    ).fetchall()
    if not rows:
        return None
    streak_start: str | None = None
    for row in rows:
        row_status = str(row["status"] or "").strip().lower()
        if row_status not in _UNAVAILABLE_PLAYER_STATUSES:
            break
        streak_start = str(row["captured_at"])
    return streak_start


_UNAVAILABLE_PLAYER_STATUSES = {"out", "inactive", "suspended", "unavailable"}


def _latest_player_injury_status(conn: Any, player_id: int) -> str | None:
    row = conn.execute(
        """
        SELECT lower(trim(status)) AS status
        FROM injuries
        WHERE player_id = ?
        ORDER BY captured_at DESC, id DESC
        LIMIT 1
        """,
        (int(player_id),),
    ).fetchone()
    if not row:
        return None
    status = str(row["status"] or "").strip().lower()
    return status or None


def _player_is_unavailable(conn: Any, player_id: int) -> bool:
    status = _latest_player_injury_status(conn, player_id)
    return status in _UNAVAILABLE_PLAYER_STATUSES


def _protect_mutation(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    _consume_rate_limit(_client_key(request), "mutation", RATE_LIMIT_MUTATION_CAPACITY, RATE_LIMIT_MUTATION_REFILL_PER_SEC)
    _require_admin_session_or_api_key(request, x_api_key, authorization, x_csrf_token)
    if using_turso():
        return
    with sqlite_write_lock():
        yield


def _protect_force_refresh(
    request: Request,
    force_refresh: bool = False,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    x_csrf_token: Annotated[str | None, Header(alias="X-CSRF-Token")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if not force_refresh:
        return
    _consume_rate_limit(_client_key(request), "refresh", RATE_LIMIT_REFRESH_CAPACITY, RATE_LIMIT_REFRESH_REFILL_PER_SEC)
    _require_admin_session_or_api_key(request, x_api_key, authorization, x_csrf_token)


@app.post("/api/special/stocks/generate", dependencies=[Depends(_protect_mutation)])
def generate_special_stocks(request: Request) -> dict[str, int]:
    return _run_audited_mutation(
        request,
        "special.stocks.generate",
        lambda: _generate_special_stocks(),
    )


def _generate_special_stocks() -> dict[str, int]:
    with connect() as conn:
        result = prepare_stocks_data(conn)
    return {"generated": int(result.get("snapshots_written") or 0)}


@app.get("/api/auth/me")
def auth_me(request: Request) -> dict[str, Any]:
    return _auth_payload(_current_session_user(request))


@app.get("/api/cache/stale-payloads", dependencies=[Depends(_protect_mutation)])
def audit_stale_payloads() -> dict[str, Any]:
    return _audit_stale_payloads()


@app.get("/api/cache/status")
def cache_status() -> dict[str, Any]:
    return {
        "status": "ok",
        "views": {
            "props": _cache_status_payload(VALUE_BOARD_CACHE_NAME),
            "watchlist": _cache_status_payload(WATCHLIST_CACHE_NAME),
            "matchups": _cache_status_payload(MATCHUPS_CACHE_NAME),
            "parlays": {
                "matchups": _cache_status_payload(MATCHUPS_CACHE_NAME),
                "props": _cache_status_payload(VALUE_BOARD_CACHE_NAME),
            },
        },
    }


def _cache_events_since(after_id: int) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, created_at, views_json
            FROM cache_events
            WHERE id > ?
            ORDER BY id ASC
            LIMIT 100
            """,
            (max(0, int(after_id)),),
        ).fetchall()
    events: list[dict[str, Any]] = []
    for row in rows:
        try:
            views = json.loads(str(row["views_json"]))
        except (TypeError, ValueError, json.JSONDecodeError):
            views = []
        if not isinstance(views, list):
            views = []
        events.append({
            "id": int(row["id"]),
            "created_at": str(row["created_at"]),
            "views": [str(view) for view in views],
        })
    return events


def _latest_cache_event_id() -> int:
    with connect() as conn:
        row = conn.execute("SELECT COALESCE(MAX(id), 0) AS id FROM cache_events").fetchone()
    return int(row["id"] or 0) if row else 0


@app.get("/api/cache/events")
async def cache_events(
    request: Request,
    after_id: int | None = Query(None, ge=0),
    last_event_id: Annotated[str | None, Header(alias="Last-Event-ID")] = None,
) -> StreamingResponse:
    if after_id is not None:
        initial_event_id = after_id
    else:
        try:
            initial_event_id = max(0, int(last_event_id or ""))
        except ValueError:
            initial_event_id = _latest_cache_event_id()

    async def stream():
        last_id = initial_event_id
        while not await request.is_disconnected():
            for event in _cache_events_since(last_id):
                last_id = int(event["id"])
                yield f"id: {last_id}\nevent: cache-update\ndata: {_json_text(event)}\n\n"
            yield ": keepalive\n\n"
            await asyncio.sleep(2)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-store, no-cache, must-revalidate, max-age=0", "X-Accel-Buffering": "no"},
    )


@app.post("/api/cache/stale-payloads/delete", dependencies=[Depends(_protect_mutation)])
def delete_stale_payloads(request: Request, payload: DeleteStalePayloadRequest) -> dict[str, Any]:
    return _run_audited_mutation(
        request,
        "cache.stale_payloads.delete",
        lambda: _delete_stale_payloads_checked(payload),
        details={"acknowledgement": payload.acknowledgement},
    )


def _delete_stale_payloads_checked(payload: DeleteStalePayloadRequest) -> dict[str, Any]:
    if payload.acknowledgement.strip() != DELETE_STALE_PAYLOAD_ACK:
        raise HTTPException(status_code=400, detail=f"Type {DELETE_STALE_PAYLOAD_ACK!r} to confirm stale payload deletion.")
    return _delete_stale_payloads()


@app.get("/api/db/lock", dependencies=[Depends(_protect_mutation)])
def audit_db_lock() -> dict[str, Any]:
    return _audit_sqlite_lock()


@app.post("/api/db/unlock", dependencies=[Depends(_protect_mutation)])
def recover_db_lock(request: Request) -> dict[str, Any]:
    return _run_audited_mutation(request, "db.unlock", _recover_sqlite_lock)


@app.post("/api/auth/login")
def auth_login(request: Request, payload: LoginRequest, response: Response) -> dict[str, Any]:
    username = payload.username.strip()
    password = payload.password
    if not username or not password:
        raise HTTPException(status_code=400, detail="Username and password are required.")
    with connect() as conn:
        user = authenticate_user(conn, username, password)
        if user is None or not user.is_admin:
            raise HTTPException(status_code=401, detail="Invalid credentials.")
        session_token, csrf_token = create_session(conn, user)
        user = SessionUser(user_id=user.user_id, username=user.username, is_admin=user.is_admin, csrf_token=csrf_token)
    response.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_token,
        max_age=session_cookie_max_age(),
        httponly=True,
        secure=_session_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return _auth_payload(user)


@app.post("/api/auth/logout")
def auth_logout(request: Request, response: Response) -> dict[str, Any]:
    session_token = request.cookies.get(SESSION_COOKIE_NAME)
    with connect() as conn:
        delete_session(conn, session_token)
    response.delete_cookie(
        key=SESSION_COOKIE_NAME,
        httponly=True,
        secure=_session_cookie_secure(request),
        samesite="lax",
        path="/",
    )
    return _auth_payload(None)


@app.get("/api/admin/provider-status", dependencies=[Depends(_protect_mutation)])
def admin_provider_status() -> dict[str, Any]:
    return {"odds_api": {"configured": bool(os.getenv("ODDS_API_KEY", "").strip())}}


@app.get("/api/admin/audit-log", dependencies=[Depends(_protect_mutation)])
def admin_audit_log(limit: int = Query(default=100, ge=1, le=500), action: str | None = None) -> dict[str, Any]:
    query = """
        SELECT id, action, status, request_id, method, path, actor_type, actor_id, actor_name,
               client_ip, details_json, target_json, result_json, error_text, created_at, updated_at
        FROM mutation_audit_log
    """
    params: list[Any] = []
    if action:
        query += " WHERE action = ?"
        params.append(action.strip())
    query += " ORDER BY created_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for key in ("details_json", "target_json", "result_json"):
            try:
                item[key.removesuffix("_json")] = json.loads(str(item.pop(key))) if item.get(key) else None
            except (TypeError, ValueError, json.JSONDecodeError):
                item[key.removesuffix("_json")] = None
        items.append(item)
    return {"count": len(items), "items": items}


@app.get("/api/admin/job-runs", dependencies=[Depends(_protect_mutation)])
def admin_job_runs(
    limit: int = Query(default=100, ge=1, le=500),
    job_type: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    query = """
        SELECT id, job_type, status, trigger_action, request_id, source_path, actor_type, actor_id,
               actor_name, target_json, metadata_json, result_json, last_error, started_at,
               finished_at, duration_ms, updated_at
        FROM job_runs
        WHERE 1 = 1
    """
    params: list[Any] = []
    if job_type:
        query += " AND job_type = ?"
        params.append(job_type.strip())
    if status:
        query += " AND status = ?"
        params.append(status.strip())
    query += " ORDER BY started_at DESC, id DESC LIMIT ?"
    params.append(int(limit))
    with connect() as conn:
        rows = conn.execute(query, tuple(params)).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        for key in ("target_json", "metadata_json", "result_json"):
            try:
                item[key.removesuffix("_json")] = json.loads(str(item.pop(key))) if item.get(key) else None
            except (TypeError, ValueError, json.JSONDecodeError):
                item[key.removesuffix("_json")] = None
        items.append(item)
    return {"count": len(items), "items": items}


@app.get("/api/admin/job-runs/{job_run_id}/events", dependencies=[Depends(_protect_mutation)])
def admin_job_run_events(job_run_id: int) -> dict[str, Any]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT id, job_run_id, created_at, level, event_type, message, details_json
            FROM job_run_events
            WHERE job_run_id = ?
            ORDER BY created_at ASC, id ASC
            """,
            (int(job_run_id),),
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        try:
            item["details"] = json.loads(str(item.pop("details_json"))) if item.get("details_json") else None
        except (TypeError, ValueError, json.JSONDecodeError):
            item["details"] = None
        items.append(item)
    return {"count": len(items), "items": items}


@app.get("/api/admin/team-conflicts", dependencies=[Depends(_protect_mutation)])
def team_conflicts(limit: int = Query(default=100, ge=1, le=500)) -> list[dict]:
    with connect() as conn:
        rows = conn.execute(
            """
            WITH history_counts AS (
                SELECT
                    h.player_id,
                    h.team_id,
                    COUNT(*) AS team_count,
                    MAX(COALESCE(g.game_date, substr(h.observed_at, 1, 10))) AS last_seen_date
                FROM player_team_history h
                LEFT JOIN games g ON g.id = h.game_id
                GROUP BY h.player_id, h.team_id
            ),
            resolved AS (
                SELECT
                    hc.player_id,
                    hc.team_id AS resolved_team_id,
                    hc.team_count AS resolved_team_count,
                    hc.last_seen_date AS resolved_last_seen_date
                FROM history_counts hc
                WHERE NOT EXISTS (
                    SELECT 1
                    FROM history_counts other
                    WHERE other.player_id = hc.player_id
                      AND (
                        other.team_count > hc.team_count
                        OR (
                            other.team_count = hc.team_count
                            AND other.last_seen_date > hc.last_seen_date
                        )
                        OR (
                            other.team_count = hc.team_count
                            AND other.last_seen_date = hc.last_seen_date
                            AND other.team_id > hc.team_id
                        )
                      )
                )
            )
            SELECT
                p.id AS player_id,
                p.full_name,
                p.team_id AS current_team_id,
                current_team.abbreviation AS current_team,
                resolved.resolved_team_id,
                resolved_team.abbreviation AS resolved_team,
                resolved.resolved_team_count,
                resolved.resolved_last_seen_date,
                (
                    SELECT COALESCE(SUM(hc2.team_count), 0)
                    FROM history_counts hc2
                    WHERE hc2.player_id = p.id
                ) AS total_history_rows
            FROM players p
            JOIN resolved ON resolved.player_id = p.id
            JOIN teams current_team ON current_team.id = p.team_id
            JOIN teams resolved_team ON resolved_team.id = resolved.resolved_team_id
            WHERE p.team_id != resolved.resolved_team_id
            ORDER BY
                resolved.resolved_team_count DESC,
                resolved.resolved_last_seen_date DESC,
                p.full_name ASC
            LIMIT ?
            """,
            (int(limit),),
        ).fetchall()
    return [dict(row) for row in rows]


def _cache_envelope(payload: Any, ttl_seconds: int) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "cache_key_version": READ_CACHE_VERSION,
        "cached_at": now.isoformat(),
        "cache_date": _local_today_iso(),
        "ttl_seconds": ttl_seconds,
        "source": "api_read_cache",
        "payload": payload,
    }


def _matchup_snapshot_cache_name(snapshot_key: str) -> str:
    digest = hashlib.sha256(snapshot_key.encode("utf-8")).hexdigest()
    return f"{MATCHUP_SNAPSHOT_CACHE_PREFIX}{digest}.json"


def _matchup_snapshot_payload(
    matchup: dict[str, Any],
    *,
    snapshot_key: str,
    game_ids: list[int],
    value_board_props: list[dict[str, Any]],
) -> dict[str, Any]:
    return {
        "snapshot_key": snapshot_key,
        "game_id": int(matchup["id"]),
        "game_ids": sorted({int(game_id) for game_id in game_ids if int(game_id) > 0}),
        "matchup": matchup,
        "value_board_props": value_board_props,
    }


def _read_matchup_snapshot(snapshot_key: str, *, allow_stale: bool = False) -> dict[str, Any] | None:
    cached = _read_cached_payload(_matchup_snapshot_cache_name(snapshot_key), allow_stale=allow_stale)
    return cached if isinstance(cached, dict) else None


def _read_cached_payload(cache_name: str, *, allow_stale: bool = False) -> Any | None:
    cached = read_json_cache(cache_name)
    if not isinstance(cached, dict):
        return None
    if cached.get("cache_key_version") != READ_CACHE_VERSION:
        return None
    cached_at_raw = cached.get("cached_at")
    cache_date = cached.get("cache_date")
    ttl_seconds = cached.get("ttl_seconds")
    if not isinstance(cached_at_raw, str) or not isinstance(cache_date, str) or not isinstance(ttl_seconds, int):
        return None
    if not allow_stale and isinstance(cached.get("invalidated_at"), str):
        return None
    if cache_date != _local_today_iso():
        return None
    try:
        cached_at = datetime.fromisoformat(cached_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    if not allow_stale and datetime.now(timezone.utc) - cached_at > timedelta(seconds=ttl_seconds):
        return None
    return cached.get("payload")


def _cache_status_payload(cache_name: str) -> dict[str, Any]:
    cached = read_json_cache(cache_name)
    now = datetime.now(timezone.utc)
    status: dict[str, Any] = {
        "cache_name": cache_name,
        "exists": isinstance(cached, dict),
        "cached_at": None,
        "cache_date": None,
        "ttl_seconds": None,
        "source": None,
        "is_fresh": False,
        "age_seconds": None,
    }
    if not isinstance(cached, dict):
        return status
    cached_at = _parse_cache_timestamp(cached.get("cached_at"))
    ttl_seconds = cached.get("ttl_seconds")
    cache_date = cached.get("cache_date")
    status["cache_date"] = cache_date if isinstance(cache_date, str) else None
    status["ttl_seconds"] = ttl_seconds if isinstance(ttl_seconds, int) else None
    status["source"] = cached.get("source") if isinstance(cached.get("source"), str) else None
    if cached_at is None:
        return status
    age_seconds = max(0.0, (now - cached_at).total_seconds())
    is_fresh = (
        isinstance(cache_date, str)
        and cache_date == _local_today_iso()
        and isinstance(ttl_seconds, int)
        and age_seconds <= ttl_seconds
        and not isinstance(cached.get("invalidated_at"), str)
    )
    status["cached_at"] = cached_at.isoformat()
    status["age_seconds"] = round(age_seconds, 2)
    status["is_fresh"] = is_fresh
    return status


def _read_through_cache(cache_name: str, ttl_seconds: int, compute: Callable[[], Any]) -> Any:
    cached_payload = _read_cached_payload(cache_name)
    if cached_payload is not None:
        return cached_payload
    payload = compute()
    write_json_cache(cache_name, _cache_envelope(payload, ttl_seconds))
    return payload


def _read_cache_rebuild_lock(cache_name: str) -> threading.Lock:
    with _READ_CACHE_REBUILD_LOCKS_LOCK:
        lock = _READ_CACHE_REBUILD_LOCKS.get(cache_name)
        if lock is None:
            lock = threading.Lock()
            _READ_CACHE_REBUILD_LOCKS[cache_name] = lock
        return lock


def _start_read_cache_rebuild(cache_name: str, ttl_seconds: int, compute: Callable[[], Any]) -> bool:
    """Start one refresh worker for a stale cache entry, if one is not already running."""
    lock = _read_cache_rebuild_lock(cache_name)
    if not lock.acquire(blocking=False):
        return False

    def _worker() -> None:
        started = datetime.now(timezone.utc)
        try:
            payload = compute()
            write_json_cache(cache_name, _cache_envelope(payload, ttl_seconds))
            elapsed_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
            print(f"[cache] {cache_name} background rebuild completed in {elapsed_ms:.2f}ms")
        except Exception as exc:
            print(f"[cache] {cache_name} background rebuild failed: {exc}")
        finally:
            lock.release()

    threading.Thread(target=_worker, name=f"read-cache-{cache_name}", daemon=True).start()
    return True


def _read_through_cache_with_meta(cache_name: str, ttl_seconds: int, compute: Callable[[], Any]) -> tuple[Any, str, float]:
    cached_payload = _read_cached_payload(cache_name)
    if cached_payload is not None:
        return cached_payload, "HIT", 0.0

    stale_payload = _read_cached_payload(cache_name, allow_stale=True)
    if stale_payload is not None:
        rebuilding = _start_read_cache_rebuild(cache_name, ttl_seconds, compute)
        return stale_payload, "STALE" if rebuilding else "REBUILDING", 0.0

    rebuild_lock = _read_cache_rebuild_lock(cache_name)
    with rebuild_lock:
        cached_payload = _read_cached_payload(cache_name)
        if cached_payload is not None:
            return cached_payload, "HIT", 0.0
        started = datetime.now(timezone.utc)
        try:
            payload = compute()
        except sqlite3.OperationalError as exc:
            if _is_sqlite_locked_error(exc):
                stale_payload = _read_cached_payload(cache_name, allow_stale=True)
                if stale_payload is not None:
                    compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
                    print(f"[cache] {cache_name} serving stale payload after sqlite lock: {exc}")
                    return stale_payload, "STALE", round(compute_ms, 2)
            raise
        compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        write_json_cache(cache_name, _cache_envelope(payload, ttl_seconds))
        return payload, "MISS", round(compute_ms, 2)


def _prediction_side_conflicts_with_projection(item: Mapping[str, Any]) -> bool:
    try:
        projection = float(item.get("projection") or 0.0)
        line = float(item.get("line") or 0.0)
    except (TypeError, ValueError):
        return False
    side = str(item.get("recommended_side") or "").strip().lower()
    return (projection > line and side == "under") or (projection < line and side == "over")


def _repair_prediction_item_if_needed(conn, item: dict[str, Any]) -> dict[str, Any]:
    del conn
    if not _prediction_side_conflicts_with_projection(item):
        return item
    projection = float(item.get("projection") or 0.0)
    line = float(item.get("line") or 0.0)
    corrected_side = "over" if projection > line else "under"
    item["recommended_side"] = corrected_side
    reason = str(item.get("reason") or "").strip()
    if "[display-corrected side]" not in reason:
        item["reason"] = f"{reason} [display-corrected side]".strip()
    return item


def _invalidate_read_caches() -> None:
    invalidated_at = datetime.now(timezone.utc).isoformat()
    cache_names = set(READ_CACHE_FILES)
    cache_dir = get_cache_dir()
    if cache_dir.exists():
        cache_names.update(path.name for path in cache_dir.glob(f"{MATCHUP_SNAPSHOT_CACHE_PREFIX}*.json"))
    for name in cache_names:
        cached = read_json_cache(name)
        if not isinstance(cached, dict) or cached.get("cache_key_version") != READ_CACHE_VERSION:
            continue
        cached["invalidated_at"] = invalidated_at
        write_json_cache(name, cached)
    _delete_app_response_caches()


def _cached_rotowire_refresh_metadata() -> dict[str, Any]:
    payload = read_json_cache(ROTOWIRE_RAW_CACHE_NAME)
    if not isinstance(payload, dict):
        return {
            "source": "unavailable",
            "captured_at": None,
            "from_cache": False,
            "status": "missing",
            "message": "Rotowire raw cache unavailable.",
        }
    source = str(payload.get("source") or "cache")
    return {
        "source": source,
        "captured_at": payload.get("captured_at"),
        "from_cache": source != "rotowire",
        "status": "cached",
        "message": None,
    }


def _roster_payload(conn, *, refresh_lineups: bool = True) -> list[dict]:
    if refresh_lineups:
        try:
            import_rotowire_lineups(conn, force_refresh=False)
        except Exception:
            # Keep roster payload non-fatal so cache publication can proceed even if live fetch fails.
            pass
    payload = read_json_cache(ROTOWIRE_RAW_CACHE_NAME)
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    captured_at = payload.get("captured_at") if isinstance(payload, dict) else None
    normalized = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_team = str(row.get("team") or "").strip()
        team = normalize_team_abbreviation(raw_team) or raw_team.upper()
        player_name = str(row.get("player_name") or "").strip()
        status = str(row.get("status") or "").strip().upper()
        if not team or not player_name or not status:
            continue
        normalized.append(
            {
                "team": team,
                "player_name": player_name,
                "status": status,
                "captured_at": captured_at,
            }
        )
    normalized.sort(key=lambda item: (item["team"], item["player_name"]))
    return _build_roster_enrichment(conn, normalized)


def _roster_payload_lightweight() -> list[dict[str, Any]]:
    payload = read_json_cache(ROTOWIRE_RAW_CACHE_NAME)
    rows = payload.get("rows", []) if isinstance(payload, dict) else []
    captured_at = payload.get("captured_at") if isinstance(payload, dict) else None
    normalized: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_team = str(row.get("team") or "").strip()
        team = normalize_team_abbreviation(raw_team) or raw_team.upper()
        player_name = str(row.get("player_name") or "").strip()
        status = str(row.get("status") or "").strip().upper()
        if not team or not player_name or not status:
            continue
        normalized.append(
            {
                "team": team,
                "player_name": player_name,
                "status": status,
                "captured_at": captured_at,
            }
        )
    normalized.sort(key=lambda item: (item["team"], item["player_name"]))
    return normalized


def _publish_roster_cache_async() -> dict[str, Any]:
    started_at = datetime.now(timezone.utc).isoformat()

    def _run() -> None:
        try:
            with connect() as conn:
                roster_payload = _roster_payload(conn, refresh_lineups=False)
                write_json_cache(ROSTER_CACHE_NAME, _cache_envelope(roster_payload, ROSTER_TTL_SECONDS))
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True).start()
    return {"status": "queued", "started_at": started_at}


def _model_runs_payload(conn) -> dict:
    return {
        "latest": latest_model_run(conn),
        "runs": list_model_runs(conn),
    }


def _model_performance_payload(conn) -> dict:
        total_settled_row = conn.execute(
                "SELECT COUNT(*) AS count FROM settled_props"
        ).fetchone()
        total_settled = int(total_settled_row["count"] or 0)
        rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        pp.*,
                        pl.market,
                        ROW_NUMBER() OVER (
                            PARTITION BY pp.prop_line_id
                            ORDER BY pp.prediction_time DESC, pp.id DESC
                        ) AS rn
                    FROM prop_predictions pp
                    JOIN prop_lines pl ON pl.id = pp.prop_line_id
                )
                SELECT
                    r.recommended_side,
                    r.expected_value,
                    r.edge,
                    r.confidence,
                    r.market,
                    sp.winning_side
                FROM ranked r
                JOIN settled_props sp ON sp.prop_line_id = r.prop_line_id
                WHERE r.rn = 1
                """
        ).fetchall()
        qualified = [row for row in rows if _include_value_board_pick(dict(row))]
        evaluated = len(qualified)
        if total_settled == 0:
                return {
                        "total_settled": 0,
                        "settled": 0,
                        "wins": 0,
                        "win_rate": None,
                        "average_ev": None,
                        "message": "No settled props yet. Settle completed games to evaluate the model.",
                }
        if evaluated == 0:
                return {
                        "total_settled": total_settled,
                        "settled": 0,
                        "wins": 0,
                        "win_rate": None,
                        "average_ev": None,
                        "message": f"{total_settled} settled prop{'s' if total_settled != 1 else ''} exist, but there are no matching model predictions.",
                }
        wins = sum(1 for row in qualified if row["recommended_side"] == row["winning_side"])
        avg_ev = sum(float(row["expected_value"]) for row in qualified) / evaluated
        return {
                "total_settled": total_settled,
                "settled": evaluated,
                "wins": wins,
                "win_rate": round(wins / evaluated, 4),
                "average_ev": round(avg_ev, 4),
                "message": f"Evaluated {evaluated} settled value-board pick{'s' if evaluated != 1 else ''}.",
        }


def _gem_performance_payload(conn) -> dict:
        rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        pp.*,
                        pl.market,
                        ROW_NUMBER() OVER (
                            PARTITION BY pp.prop_line_id
                            ORDER BY pp.prediction_time DESC, pp.id DESC
                        ) AS rn
                    FROM prop_predictions pp
                    JOIN prop_lines pl ON pl.id = pp.prop_line_id
                )
                SELECT
                    r.recommended_side,
                    r.market,
                    r.edge,
                    r.expected_value,
                    r.confidence,
                    sp.winning_side
                FROM ranked r
                JOIN settled_props sp ON sp.prop_line_id = r.prop_line_id
                WHERE r.rn = 1
                """
        ).fetchall()
        open_rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        pp.*,
                        pl.game_id,
                        ROW_NUMBER() OVER (
                            PARTITION BY pp.prop_line_id
                            ORDER BY pp.prediction_time DESC, pp.id DESC
                        ) AS rn
                    FROM prop_predictions pp
                    JOIN prop_lines pl ON pl.id = pp.prop_line_id
                )
                SELECT
                    r.edge,
                    r.expected_value,
                    r.confidence
                FROM ranked r
                JOIN games g ON g.id = r.game_id
                LEFT JOIN settled_props sp ON sp.prop_line_id = r.prop_line_id
                WHERE r.rn = 1
                    AND g.status = 'scheduled'
                    AND sp.id IS NULL
                """
        ).fetchall()
        qualified = []
        for row in rows:
                edge = float(row["edge"] or 0.0)
                ev = float(row["expected_value"] or 0.0)
                confidence = str(row["confidence"] or "").strip().lower()
                if ev >= GEM_MIN_EV and abs(edge) >= GEM_MIN_EDGE and confidence != "low":
                        qualified.append(row)
        current_open_conservative = 0
        current_open_balanced = 0
        current_open_aggressive = 0
        for row in open_rows:
                edge = float(row["edge"] or 0.0)
                ev = float(row["expected_value"] or 0.0)
                confidence = str(row["confidence"] or "").strip().lower()
                if ev >= 0.03 and abs(edge) >= 0.08 and confidence != "low":
                        current_open_conservative += 1
                if ev >= 0.02 and abs(edge) >= 0.05:
                        current_open_balanced += 1
                if ev >= 0.01 and abs(edge) >= 0.035:
                        current_open_aggressive += 1
        if not qualified:
                return {
                        "qualified": 0,
                        "wins": 0,
                        "win_rate": None,
                        "current_open_conservative": current_open_conservative,
                        "current_open_balanced": current_open_balanced,
                        "current_open_aggressive": current_open_aggressive,
                        "message": "No settled picks currently meet gem thresholds.",
                }
        wins = sum(1 for row in qualified if str(row["recommended_side"]) == str(row["winning_side"]))
        return {
                "qualified": len(qualified),
                "wins": wins,
                "win_rate": round(wins / len(qualified), 4),
                "current_open_conservative": current_open_conservative,
                "current_open_balanced": current_open_balanced,
                "current_open_aggressive": current_open_aggressive,
                "message": f"Evaluated {len(qualified)} settled gem-qualified picks.",
        }


def _watchlist_performance_payload(conn) -> dict:
        rows = conn.execute(
                """
                WITH ranked AS (
                    SELECT
                        pp.*,
                        pl.market,
                        ROW_NUMBER() OVER (
                            PARTITION BY pp.prop_line_id
                            ORDER BY pp.prediction_time DESC, pp.id DESC
                        ) AS rn
                    FROM prop_predictions pp
                    JOIN prop_lines pl ON pl.id = pp.prop_line_id
                )
                SELECT
                    r.recommended_side,
                    r.market,
                    r.edge,
                    r.expected_value,
                    r.confidence,
                    sp.winning_side
                FROM ranked r
                JOIN settled_props sp ON sp.prop_line_id = r.prop_line_id
                WHERE r.rn = 1
                """
        ).fetchall()
        qualified = [row for row in rows if _include_watchlist_pick(dict(row))]
        if not qualified:
                return {
                        "qualified": 0,
                        "wins": 0,
                        "win_rate": None,
                        "message": "No settled watchlist-qualified picks yet.",
                }
        wins = sum(1 for row in qualified if str(row["recommended_side"]) == str(row["winning_side"]))
        return {
                "qualified": len(qualified),
                "wins": wins,
                "win_rate": round(wins / len(qualified), 4),
                "message": f"Evaluated {len(qualified)} settled watchlist-qualified picks.",
        }


def _scheduled_matchup_game_groups(conn, game_ids: list[int] | None = None) -> list[tuple[dict, list[int]]]:
    params: tuple[int, ...] = ()
    game_filter = ""
    if game_ids:
        normalized_ids = sorted({int(game_id) for game_id in game_ids if int(game_id) > 0})
        if not normalized_ids:
            return []
        placeholders = ",".join("?" for _ in normalized_ids)
        game_filter = f" AND g.id IN ({placeholders})"
        params = tuple(normalized_ids)
    games = conn.execute(
        """
        SELECT
            g.id,
            g.game_date,
            g.start_time,
            home.id AS home_team_id,
            home.abbreviation AS home_team,
            home.name AS home_team_name,
            home.logo_url AS home_logo_url,
            away.id AS away_team_id,
            away.abbreviation AS away_team,
            away.name AS away_team_name
            ,away.logo_url AS away_logo_url
            ,g.rest_days_home
            ,g.rest_days_away
            ,g.spread_home
            ,g.game_total
            ,g.home_moneyline
            ,g.away_moneyline
            ,g.home_spread_price
            ,g.away_spread_price
            ,g.over_price
            ,g.under_price
        FROM games g
        JOIN teams home ON home.id = g.home_team_id
        JOIN teams away ON away.id = g.away_team_id
        WHERE g.status = 'scheduled'
        """
        + game_filter
        + """
        ORDER BY g.start_time
        """,
        params,
    ).fetchall()
    games = [game for game in games if _is_today_active_game_time(game["start_time"])]
    return _coalesce_matchup_games(games)


def _build_matchup_payload_item(
    conn,
    game,
    game_ids: list[int],
    *,
    injury_refresh: dict[str, Any],
    covers_records: dict[int, dict],
    covers_market_odds: dict[int, dict],
    prediction_state_by_game: dict[int, dict[str, Any]],
    game_prediction_cache: _GamePredictionCache,
    team_ratings_by_team_id: dict[int, dict[str, Any]],
) -> dict[str, Any]:
    home_summary = _team_last_10_summary(conn, int(game["home_team_id"]))
    away_summary = _team_last_10_summary(conn, int(game["away_team_id"]))
    game_id = int(game["id"])
    market_override = covers_market_odds.get(game_id, {})
    group_covers_records = next(
        (covers_records.get(int(candidate_id)) for candidate_id in game_ids if covers_records.get(int(candidate_id))),
        covers_records.get(game_id),
    )
    home_rest_days = _rest_days_before_game(conn, int(game["home_team_id"]), game["start_time"], game["game_date"])
    away_rest_days = _rest_days_before_game(conn, int(game["away_team_id"]), game["start_time"], game["game_date"])
    game_context = dict(game)
    game_context["spread_home"] = _prefer_market_value(
        game_context.get("spread_home"),
        market_override.get("spread_home"),
        _is_real_spread,
    )
    game_context["game_total"] = _prefer_market_value(
        game_context.get("game_total"),
        market_override.get("game_total"),
        _is_real_total,
    )
    game_context["home_moneyline"] = _prefer_market_value(
        game_context.get("home_moneyline"),
        market_override.get("home_moneyline"),
        _is_real_moneyline,
    )
    game_context["away_moneyline"] = _prefer_market_value(
        game_context.get("away_moneyline"),
        market_override.get("away_moneyline"),
        _is_real_moneyline,
    )
    for field in ("home_spread_price", "away_spread_price", "over_price", "under_price"):
        game_context[field] = _prefer_market_value(
            game_context.get(field),
            market_override.get(field),
            _is_real_moneyline,
        )
    game_context["rest_days_home"] = home_rest_days if home_rest_days is not None else 2
    game_context["rest_days_away"] = away_rest_days if away_rest_days is not None else 2
    prediction = project_game(conn, game_context, runtime_cache=game_prediction_cache)
    segment_prediction = project_game_segments(conn, game_context, runtime_cache=game_prediction_cache)
    game_prediction_id = _latest_game_prediction_id(conn, game_id)
    market_payload = _matchup_game_markets(game_context)
    home_team_ratings = team_ratings_by_team_id.get(int(game["home_team_id"]))
    away_team_ratings = team_ratings_by_team_id.get(int(game["away_team_id"]))
    prediction_state = next(
        (prediction_state_by_game.get(int(candidate_id)) for candidate_id in game_ids if prediction_state_by_game.get(int(candidate_id))),
        prediction_state_by_game.get(game_id),
    ) or {
        "model_prop_count": 0,
        "prediction_count": 0,
        "sportsbook_prop_count": 0,
        "model_props_ready": False,
        "model_props_status": "empty",
    }
    return {
        "id": game["id"],
        "game_ids": sorted({int(candidate_id) for candidate_id in game_ids}),
        "game_prediction_id": game_prediction_id,
        "game_date": game["game_date"],
        "start_time": game["start_time"],
        "home_team": game["home_team"],
        "home_team_name": game["home_team_name"],
        "home_logo_url": game["home_logo_url"],
        "away_team": game["away_team"],
        "away_team_name": game["away_team_name"],
        "away_logo_url": game["away_logo_url"],
        "home_rest_days": home_rest_days,
        "away_rest_days": away_rest_days,
        "spread_home": game_context["spread_home"],
        "game_total": game_context["game_total"],
        "home_moneyline": game_context["home_moneyline"],
        "away_moneyline": game_context["away_moneyline"],
        "home_spread_price": game_context.get("home_spread_price"),
        "away_spread_price": game_context.get("away_spread_price"),
        "over_price": game_context.get("over_price"),
        "under_price": game_context.get("under_price"),
        **market_payload,
        **prediction_state,
        "blowout_risk": _blowout_display(game_context["spread_home"], "starter")["blowout_risk"],
        **prediction,
        **segment_prediction,
        "home": home_summary,
        "away": away_summary,
        "covers_records": group_covers_records,
        "props": _value_board_payload_for_games(conn, game_ids, include_filtered_only=False),
        "sportsbook_props": _sportsbook_props_for_games(conn, game_ids),
        "line_discrepancies": _line_discrepancies_for_games(conn, game_ids),
        "home_team_ratings": home_team_ratings,
        "away_team_ratings": away_team_ratings,
        "rating_differentials": _matchup_rating_differentials(home_team_ratings, away_team_ratings),
        "h2h_segment_summary": _h2h_segment_summary(
            conn,
            away_team_id=int(game["away_team_id"]),
            home_team_id=int(game["home_team_id"]),
            scheduled_start_time=str(game["start_time"] or ""),
            scheduled_game_id=game_id,
        ),
        "injury_source": injury_refresh.get("source"),
        "injury_captured_at": injury_refresh.get("captured_at"),
        "injury_from_cache": injury_refresh.get("from_cache"),
    }


def _matchups_payload(conn, game_ids: list[int] | None = None) -> list[dict]:
    try:
        injury_refresh = import_rotowire_lineups(conn, force_refresh=False)
    except Exception as exc:
        injury_refresh = {
            "source": "unavailable",
            "captured_at": None,
            "from_cache": False,
            "status": "failed",
            "message": str(exc),
        }
    game_groups = _scheduled_matchup_game_groups(conn, game_ids=game_ids)
    covers_records = _covers_records_by_game(conn)
    covers_market_odds = _covers_market_odds_by_game()
    prediction_state_by_game = _prediction_state_by_game(conn)
    team_ratings_by_team_id = _team_ratings_by_team(
        conn,
        {
            int(team_id)
            for game, _game_ids in game_groups
            for team_id in (int(game["home_team_id"]), int(game["away_team_id"]))
        },
    )
    game_prediction_cache = _GamePredictionCache(
        conn,
        tuple(team_id for game, _ in game_groups for team_id in (int(game["home_team_id"]), int(game["away_team_id"]))),
    )
    payload = []
    for game, game_ids in game_groups:
        payload.append(
            _build_matchup_payload_item(
                conn,
                game,
                game_ids,
                injury_refresh=injury_refresh,
                covers_records=covers_records,
                covers_market_odds=covers_market_odds,
                prediction_state_by_game=prediction_state_by_game,
                game_prediction_cache=game_prediction_cache,
                team_ratings_by_team_id=team_ratings_by_team_id,
            )
        )
    return payload


def _prediction_state_by_game(conn) -> dict[int, dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT
            g.id AS game_id,
            COUNT(DISTINCT pl.id) AS model_prop_count,
            COUNT(DISTINCT pp.id) AS prediction_count,
            COUNT(DISTINCT sp.id) AS sportsbook_prop_count
        FROM games g
        LEFT JOIN prop_lines pl ON pl.game_id = g.id
        LEFT JOIN prop_predictions pp ON pp.prop_line_id = pl.id
        LEFT JOIN sportsbook_prop_lines sp ON sp.game_id = g.id
        WHERE g.status = 'scheduled'
        GROUP BY g.id
        """
    ).fetchall()
    payload: dict[int, dict[str, Any]] = {}
    for row in rows:
        game_id = int(row["game_id"])
        model_prop_count = int(row["model_prop_count"] or 0)
        prediction_count = int(row["prediction_count"] or 0)
        sportsbook_prop_count = int(row["sportsbook_prop_count"] or 0)
        if prediction_count > 0:
            status = "ready"
        elif model_prop_count > 0:
            status = "pending"
        elif sportsbook_prop_count > 0:
            status = "sportsbook_only"
        else:
            status = "empty"
        payload[game_id] = {
            "model_prop_count": model_prop_count,
            "prediction_count": prediction_count,
            "sportsbook_prop_count": sportsbook_prop_count,
            "model_props_ready": prediction_count > 0,
            "model_props_status": status,
        }
    return payload


def _matchup_game_markets(game: dict[str, Any]) -> dict[str, dict[str, float | None]]:
    spread_home = game.get("spread_home")
    spread_home_value = float(spread_home) if isinstance(spread_home, (int, float)) else None
    game_total = game.get("game_total")
    game_total_value = float(game_total) if isinstance(game_total, (int, float)) else None
    home_spread_price = game.get("home_spread_price")
    away_spread_price = game.get("away_spread_price")
    over_price = game.get("over_price")
    under_price = game.get("under_price")
    home_moneyline = game.get("home_moneyline")
    away_moneyline = game.get("away_moneyline")
    return {
        "spread_market": {
            "away_line": (-spread_home_value if spread_home_value is not None else None),
            "away_price": float(away_spread_price) if isinstance(away_spread_price, (int, float)) else None,
            "home_line": spread_home_value,
            "home_price": float(home_spread_price) if isinstance(home_spread_price, (int, float)) else None,
        },
        "total_market": {
            "over_line": game_total_value,
            "over_price": float(over_price) if isinstance(over_price, (int, float)) else None,
            "under_line": game_total_value,
            "under_price": float(under_price) if isinstance(under_price, (int, float)) else None,
        },
        "moneyline_market": {
            "away_price": float(away_moneyline) if isinstance(away_moneyline, (int, float)) else None,
            "home_price": float(home_moneyline) if isinstance(home_moneyline, (int, float)) else None,
        },
    }


def _prefer_market_value(
    current: Any,
    fallback: Any,
    validator: Callable[[Any], bool],
) -> Any:
    if validator(current):
        return current
    if validator(fallback):
        return fallback
    return current


def _latest_game_prediction_id(conn, game_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT id
        FROM game_predictions
        WHERE game_id = ?
        ORDER BY prediction_time DESC, id DESC
        LIMIT 1
        """,
        (game_id,),
    ).fetchone()
    if row is None:
        return None
    return int(row["id"])


def _publish_matchup_snapshot_payloads(
    conn,
    game_ids: list[int] | None = None,
    *,
    injury_refresh: dict[str, Any] | None = None,
) -> dict[str, int]:
    published: dict[str, int] = {}
    if injury_refresh is None:
        try:
            injury_refresh = import_rotowire_lineups(conn, force_refresh=False)
        except Exception as exc:
            injury_refresh = {
                "source": "unavailable",
                "captured_at": None,
                "from_cache": False,
                "status": "failed",
                "message": str(exc),
            }
    covers_records = _covers_records_by_game(conn)
    covers_market_odds = _covers_market_odds_by_game()
    prediction_state_by_game = _prediction_state_by_game(conn)
    game_groups = _scheduled_matchup_game_groups(conn, game_ids=game_ids)
    team_ratings_by_team_id = _team_ratings_by_team(
        conn,
        {
            int(team_id)
            for game, _grouped_game_ids in game_groups
            for team_id in (int(game["home_team_id"]), int(game["away_team_id"]))
        },
    )
    game_prediction_cache = _GamePredictionCache(
        conn,
        tuple(team_id for game, _ in game_groups for team_id in (int(game["home_team_id"]), int(game["away_team_id"]))),
    )
    for game, grouped_game_ids in game_groups:
        matchup = _build_matchup_payload_item(
            conn,
            game,
            grouped_game_ids,
            injury_refresh=injury_refresh,
            covers_records=covers_records,
            covers_market_odds=covers_market_odds,
            prediction_state_by_game=prediction_state_by_game,
            game_prediction_cache=game_prediction_cache,
            team_ratings_by_team_id=team_ratings_by_team_id,
        )
        snapshot_key = _matchup_snapshot_key(game)
        snapshot_payload = _matchup_snapshot_payload(
            matchup,
            snapshot_key=snapshot_key,
            game_ids=grouped_game_ids,
            value_board_props=_value_board_payload_for_games(conn, grouped_game_ids, include_filtered_only=True),
        )
        write_json_cache(_matchup_snapshot_cache_name(snapshot_key), _cache_envelope(snapshot_payload, MATCHUPS_TTL_SECONDS))
        published[_matchup_snapshot_cache_name(snapshot_key)] = len(grouped_game_ids)
    return published


def _active_matchup_snapshot_keys(conn) -> list[str]:
    return [_matchup_snapshot_key(game) for game, _ in _scheduled_matchup_game_groups(conn)]


def _aggregate_matchup_snapshot_payloads(conn, *, allow_stale: bool = False) -> tuple[list[dict], list[dict]] | None:
    snapshot_items: list[dict[str, Any]] = []
    for snapshot_key in _active_matchup_snapshot_keys(conn):
        snapshot = _read_matchup_snapshot(snapshot_key, allow_stale=allow_stale)
        if snapshot is None:
            return None
        matchup = snapshot.get("matchup")
        value_board_props = snapshot.get("value_board_props")
        if not isinstance(matchup, dict) or not isinstance(value_board_props, list):
            return None
        snapshot_items.append(snapshot)
    matchups_payload = [dict(item["matchup"]) for item in snapshot_items]
    matchups_payload.sort(key=lambda item: (str(item.get("start_time") or ""), int(item.get("id") or 0)))
    value_board_payload: list[dict] = []
    for item in snapshot_items:
        value_board_payload.extend([dict(prop) for prop in item["value_board_props"] if isinstance(prop, dict)])
    value_board_payload.sort(key=_value_prop_rank, reverse=True)
    return value_board_payload, matchups_payload


def _publish_matchup_snapshot_aggregates(
    conn,
    *,
    game_ids: list[int] | None = None,
    full_refresh: bool = True,
    injury_refresh: dict[str, Any] | None = None,
) -> dict[str, int]:
    published: dict[str, int] = {}
    if full_refresh:
        published.update(_publish_matchup_snapshot_payloads(conn, injury_refresh=injury_refresh))
    elif game_ids is not None:
        published.update(_publish_matchup_snapshot_payloads(conn, game_ids=game_ids, injury_refresh=injury_refresh))
    aggregates = _aggregate_matchup_snapshot_payloads(conn)
    if aggregates is None:
        published.update(_publish_matchup_snapshot_payloads(conn, injury_refresh=injury_refresh))
        aggregates = _aggregate_matchup_snapshot_payloads(conn)
    if aggregates is None:
        raise RuntimeError("Unable to assemble matchup snapshot aggregates.")
    value_board_payload, matchups_payload = aggregates
    write_json_cache(VALUE_BOARD_CACHE_NAME, _cache_envelope(value_board_payload, VALUE_BOARD_TTL_SECONDS))
    write_json_cache(MATCHUPS_CACHE_NAME, _cache_envelope(matchups_payload, MATCHUPS_TTL_SECONDS))
    published[VALUE_BOARD_CACHE_NAME] = len(value_board_payload)
    published[MATCHUPS_CACHE_NAME] = len(matchups_payload)
    return published


def _publish_current_read_payloads(
    conn,
    *,
    include_matchups: bool = True,
    include_roster: bool = True,
    include_performance: bool = True,
    matchup_game_ids: list[int] | None = None,
    full_matchup_refresh: bool = True,
) -> dict[str, int]:
    published: dict[str, int] = {}
    cached_injury_refresh = _cached_rotowire_refresh_metadata()

    def publish(name: str, ttl_seconds: int, payload: Any, count: int) -> None:
        write_json_cache(name, _cache_envelope(payload, ttl_seconds))
        published[name] = count

    payload_builders = [
        (WATCHLIST_CACHE_NAME, WATCHLIST_TTL_SECONDS, lambda: _watchlist_payload(conn)),
        (LINE_DISCREPANCIES_CACHE_NAME, LINE_DISCREPANCIES_TTL_SECONDS, lambda: _line_discrepancies_payload(conn, None)),
    ]
    if include_roster:
        payload_builders.append((ROSTER_CACHE_NAME, ROSTER_TTL_SECONDS, lambda: _roster_payload(conn, refresh_lineups=False)))
    if include_performance:
        payload_builders.extend(
            [
                (MODEL_RUNS_CACHE_NAME, MODEL_RUNS_TTL_SECONDS, lambda: _model_runs_payload(conn)),
                (MODEL_PERFORMANCE_CACHE_NAME, MODEL_PERFORMANCE_TTL_SECONDS, lambda: _model_performance_payload(conn)),
                (GEM_PERFORMANCE_CACHE_NAME, GEM_PERFORMANCE_TTL_SECONDS, lambda: _gem_performance_payload(conn)),
                (WATCHLIST_PERFORMANCE_CACHE_NAME, WATCHLIST_PERFORMANCE_TTL_SECONDS, lambda: _watchlist_performance_payload(conn)),
            ]
        )

    for name, ttl_seconds, compute in payload_builders:
        try:
            payload = compute()
            if isinstance(payload, dict):
                count = len(payload.get("runs", [])) if "runs" in payload else 1
            else:
                count = len(payload)
            publish(name, ttl_seconds, payload, count)
        except Exception as exc:
            print(f"[startup] {name} prewarm skipped: {exc}")

    if include_matchups:
        try:
            published.update(
                _publish_matchup_snapshot_aggregates(
                    conn,
                    game_ids=matchup_game_ids,
                    full_refresh=full_matchup_refresh,
                    injury_refresh=cached_injury_refresh,
                )
            )
        except Exception as exc:
            print(f"[startup] matchup snapshot publish skipped: {exc}")

    _record_cache_event(conn, published)
    return published


def _record_cache_event(conn, published: dict[str, int]) -> None:
    views = sorted(name for name in published if name in READ_CACHE_FILES)
    if not views:
        return
    try:
        conn.execute(
            """
            INSERT INTO cache_events (created_at, views_json)
            VALUES (?, ?)
            """,
            (datetime.now(timezone.utc).isoformat(), _json_text(views)),
        )
    except Exception as exc:
        print(f"[cache] cache event publish skipped: {exc}")
        return
    try:
        conn.execute(
            """
            DELETE FROM cache_events
            WHERE id NOT IN (
                SELECT id FROM cache_events ORDER BY id DESC LIMIT 100
            )
            """
        )
    except Exception:
        # Retention cleanup must never make a successfully rebuilt cache unavailable.
        pass


def _publish_post_mutation_read_payloads(
    conn,
    *,
    matchup_game_ids: list[int] | None = None,
    full_matchup_refresh: bool = True,
    include_roster: bool = True,
    include_performance: bool = True,
) -> dict[str, int]:
    return _publish_current_read_payloads(
        conn,
        include_roster=include_roster,
        include_performance=include_performance,
        matchup_game_ids=matchup_game_ids,
        full_matchup_refresh=full_matchup_refresh,
    )


def _refresh_roster_read_payloads(conn) -> dict[str, int]:
    published: dict[str, int] = {}
    payloads = [
        # The roster refresh route has already imported Rotowire data for this request.
        # Reuse that state here instead of triggering another lineup fetch while publishing.
        (ROSTER_CACHE_NAME, ROSTER_TTL_SECONDS, lambda: _roster_payload(conn, refresh_lineups=False)),
    ]
    for name, ttl_seconds, compute in payloads:
        payload = compute()
        write_json_cache(name, _cache_envelope(payload, ttl_seconds))
        published[name] = len(payload) if isinstance(payload, list) else 1
    return published


def _clear_scheduled_prop_state(conn, *, clear_source_rows: bool = False) -> None:
    conn.execute(
        """
        DELETE FROM watchlist_snapshot_items
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
        DELETE FROM gem_snapshot_items
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
    if clear_source_rows:
        conn.execute("DELETE FROM sportsbook_prop_lines")
    conn.commit()


def _clear_prop_scrape_caches() -> None:
    delete_json_cache("sportsbook_props.json")
    delete_json_cache(ODDS_RAW_CACHE_NAME)
    delete_json_cache(COVERS_RAW_CACHE_NAME)


def _set_observability_headers(response: Response, cache_name: str, cache_status: str, compute_ms: float) -> None:
    response.headers["Cache-Control"] = "no-store, no-cache, must-revalidate, max-age=0"
    response.headers["Pragma"] = "no-cache"
    response.headers["Expires"] = "0"
    response.headers["X-Cache"] = cache_status
    response.headers["X-Compute-Ms"] = f"{compute_ms:.2f}"
    if os.getenv("EXPOSE_DEBUG_HEADERS", "").strip().lower() not in {"1", "true", "yes"}:
        return
    response.headers["X-Cache-Key"] = f"{cache_name}:v{READ_CACHE_VERSION}"
    print(f"[cache] {cache_name} status={cache_status} compute_ms={compute_ms:.2f}")


def _active_slate_game_ids(conn) -> list[int]:
    games = conn.execute(
        """
        SELECT g.*
        FROM games g
        WHERE g.status = 'scheduled'
        ORDER BY g.start_time
        """
    ).fetchall()
    return sorted(
        {
            int(game["id"])
            for game in games
            if int(game["id"]) > 0 and _is_today_active_game_time(game["start_time"])
        }
    )


def _scheduled_game_ids(conn) -> list[int]:
    today_iso = _local_today_iso()
    next_date_row = conn.execute(
        """
        SELECT MIN(game_date) AS next_game_date
        FROM games
        WHERE status = 'scheduled'
          AND game_date >= ?
        """,
        (today_iso,),
    ).fetchone()
    target_date = str(next_date_row["next_game_date"] or "").strip() if next_date_row is not None else ""
    if not target_date:
        fallback_row = conn.execute(
            """
            SELECT MIN(game_date) AS next_game_date
            FROM games
            WHERE status = 'scheduled'
            """
        ).fetchone()
        target_date = str(fallback_row["next_game_date"] or "").strip() if fallback_row is not None else ""
    if not target_date:
        return []
    return [
        int(row["id"])
        for row in conn.execute(
            """
            SELECT id
            FROM games
            WHERE status = 'scheduled'
              AND game_date = ?
            ORDER BY start_time, id
            """,
            (target_date,),
        ).fetchall()
        if int(row["id"]) > 0
    ]


def rebuild_game_predictions_live(
    conn,
    *,
    game_ids: list[int] | None = None,
    chunk_size: int = 20,
    progress_callback: Callable[[int, int, str | None], None] | None = None,
) -> dict[str, int]:
    target_game_ids = sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0})
    game_filter = ""
    params: tuple[object, ...] = ()
    if target_game_ids:
        placeholders = ",".join("?" for _ in target_game_ids)
        game_filter = f" AND g.id IN ({placeholders})"
        params = tuple(target_game_ids)
    rows = conn.execute(
        """
        SELECT
            g.id,
            g.game_date,
            g.start_time,
            g.home_team_id,
            g.away_team_id,
            home.abbreviation AS home_team,
            away.abbreviation AS away_team,
            g.rest_days_home,
            g.rest_days_away,
            g.spread_home,
            g.game_total,
            g.home_moneyline,
            g.away_moneyline,
            g.home_spread_price,
            g.away_spread_price,
            g.over_price,
            g.under_price
        FROM games g
        JOIN teams home ON home.id = g.home_team_id
        JOIN teams away ON away.id = g.away_team_id
        WHERE g.status = 'scheduled'
        """
        + game_filter
        + """
        ORDER BY g.start_time, g.id
        """,
        params,
    ).fetchall()
    total_games = len(rows)
    if total_games == 0:
        if progress_callback is not None:
            progress_callback(0, 0, "No scheduled games needed rebuilding.")
        return {"attempted": 0, "written": 0}

    written = 0
    safe_chunk_size = max(1, int(chunk_size))
    for start in range(0, len(rows), safe_chunk_size):
        batch_games = rows[start : start + safe_chunk_size]
        batch_prediction_cache = _GamePredictionCache(
            conn,
            tuple(team_id for game in batch_games for team_id in (int(game["home_team_id"]), int(game["away_team_id"]))),
        )
        batch_predictions = []
        for game in batch_games:
            prediction = project_game(conn, game, runtime_cache=batch_prediction_cache)
            prediction.update(project_game_segments(conn, game, runtime_cache=batch_prediction_cache))
            batch_predictions.append(prediction)
        written += save_game_predictions(conn, batch_games, batch_predictions)
        if progress_callback is not None:
            processed = min(start + len(batch_games), total_games)
            progress_callback(processed, total_games, f"Built {written} of {total_games} game predictions.")
    return {"attempted": total_games, "written": written}


def _repair_current_slate_target(conn) -> tuple[str, list[int]]:
    target_game_ids = _active_slate_game_ids(conn)
    scope = "current_slate"
    if not target_game_ids:
        target_game_ids = _scheduled_game_ids(conn)
        scope = "scheduled"
    return scope, target_game_ids


def _recent_unsettled_final_dates(conn) -> list[str]:
    cutoff = (datetime.now(LOCAL_TZ).date() - timedelta(days=RECENT_FINALS_SETTLEMENT_LOOKBACK_DAYS)).isoformat()
    rows = conn.execute(
        """
        SELECT DISTINCT game_date
        FROM (
            SELECT g.game_date
            FROM games g
            JOIN prop_lines pl ON pl.game_id = g.id
            LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
            WHERE g.status = 'final'
              AND g.game_date >= ?
              AND sp.id IS NULL
            UNION
            SELECT g.game_date
            FROM games g
            JOIN game_predictions gp ON gp.game_id = g.id
            LEFT JOIN settled_game_predictions sgp ON sgp.game_prediction_id = gp.id
            WHERE g.status = 'final'
              AND g.game_date >= ?
              AND sgp.id IS NULL
        )
        ORDER BY game_date
        """,
        (cutoff, cutoff),
    ).fetchall()
    return [str(row["game_date"]) for row in rows if row["game_date"]]


def _settle_recent_completed_games(conn) -> dict[str, Any]:
    target_dates = _recent_unsettled_final_dates(conn)
    if not target_dates:
        return {
            "selected_dates": [],
            "prop_settlements": {"settled": 0, "repaired": 0, "skipped": 0},
            "game_settlements": {"settled": 0},
            "special_settlements": {"settled": 0},
        }
    return {
        "selected_dates": target_dates,
        "prop_settlements": settle_completed_props(conn, selected_dates=target_dates),
        "game_settlements": settle_completed_game_predictions(conn, selected_dates=target_dates),
        "special_settlements": settle_stocks(conn, selected_dates=target_dates),
    }


def _repair_current_slate_props(
    conn,
    *,
    target_game_ids: list[int] | None = None,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
) -> dict[str, Any]:
    if target_game_ids is None:
        scope, target_game_ids = _repair_current_slate_target(conn)
    else:
        target_game_ids = sorted({int(game_id) for game_id in target_game_ids if int(game_id) > 0})
        scope = "injury_update"
    ingestion = build_no_provider_ingestion(
        scope="injury_update" if scope == "injury_update" else "current_slate",
        target_game_ids=target_game_ids,
    )
    if not ingestion.target_game_ids:
        if progress_callback is not None:
            progress_callback("syncing_props", 0, 0, str(ingestion.message or "No scheduled games were available for repair."))
        return {
            "scope": scope,
            "target_game_ids": [],
            "scanned_props": 0,
            "synced_props": 0,
            "rebuilt_predictions": 0,
        }
    pipeline_result: PropPipelineResult = run_prop_sync_pipeline(
        conn,
        request_source=scope,
        target_game_ids=ingestion.target_game_ids,
        sync_props=ingestion.prop_sync_eligible,
        fast_fail=True,
        rebuild_mode=ingestion.rebuild_mode,
        fallback_target_game_rebuild_when_unchanged=ingestion.fallback_target_game_rebuild_when_unchanged,
        initial_sync_message=ingestion.initial_sync_message,
        skip_sync_message=ingestion.skip_sync_message,
        sync_props_fn=sync_prop_lines_from_sportsbook,
        rebuild_predictions_fn=rebuild_predictions_live,
        rebuild_games_fn=rebuild_game_predictions_live,
        snapshot_watchlist_fn=_snapshot_watchlist,
        watchlist_snapshot_date=datetime.now(LOCAL_TZ).date().isoformat(),
        progress_callback=progress_callback,
    )
    return {
        "scope": scope,
        "target_game_ids": list(pipeline_result.target_game_ids),
        "scanned_props": int(pipeline_result.scanned_props),
        "synced_props": int(pipeline_result.synced_props),
        "changed_prop_line_ids": list(pipeline_result.changed_prop_line_ids),
        "attempted_predictions": int(pipeline_result.attempted_predictions),
        "rebuilt_predictions": int(pipeline_result.rebuilt_predictions),
        "skipped_predictions": int(pipeline_result.skipped_predictions),
        "rebuild_errors": list(pipeline_result.rebuild_errors),
        "attempted_game_predictions": int(pipeline_result.attempted_game_predictions),
        "rebuilt_game_predictions": int(pipeline_result.rebuilt_game_predictions),
        "watchlist_snapshot": pipeline_result.watchlist_snapshot,
    }


def _maybe_repair_current_slate_after_settlement(conn) -> dict[str, Any] | None:
    scope, target_game_ids = _repair_current_slate_target(conn)
    if not target_game_ids:
        return None
    placeholders = ",".join("?" for _ in target_game_ids)
    rows = conn.execute(
        f"""
        SELECT DISTINCT pl.game_id
        FROM prop_predictions pp
        JOIN prop_lines pl ON pl.id = pp.prop_line_id
        JOIN games g ON g.id = pl.game_id
        LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
        WHERE g.status = 'scheduled'
          AND sp.id IS NULL
          AND pl.game_id IN ({placeholders})
        """,
        tuple(target_game_ids),
    ).fetchall()
    covered_game_ids = {int(row["game_id"]) for row in rows if row["game_id"] is not None}
    if covered_game_ids.issuperset(target_game_ids):
        return None
    repair_result = _repair_current_slate_props(conn)
    repair_result["repair_reason"] = "scheduled_predictions_missing_after_settlement"
    repair_result["scope"] = scope
    repair_result["missing_game_ids"] = [game_id for game_id in target_game_ids if game_id not in covered_game_ids]
    return repair_result


@app.post("/api/recalculate", dependencies=[Depends(_protect_mutation)])
def recalculate(request: Request, response: Response) -> dict[str, int]:
    # Keep the legacy route for backward compatibility, but make stale clients visible.
    response.headers["Deprecation"] = "true"
    response.headers["Sunset"] = "Wed, 31 Dec 2026 23:59:59 GMT"
    response.headers["Link"] = '</api/props/repair-current-slate>; rel="successor-version"'
    response.headers["X-Legacy-Endpoint"] = "/api/recalculate"
    return _run_audited_mutation(request, "props.recalculate", lambda: _queue_legacy_recalculate_job(request))


def _run_legacy_recalculate_job() -> dict[str, Any]:
    total_stages = 6
    with connect() as conn:
        rebuild_result = _repair_current_slate_props(
            conn,
            progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
                stage=stage,
                stage_index=(
                    1
                    if stage == "syncing_props"
                    else 2
                    if stage == "rebuilding_predictions"
                    else 3
                ),
                stage_total=total_stages,
                current=current,
                total=total,
                message=message,
            ),
        )
    with connect() as conn:
        _set_prop_sync_progress(
            stage="settling_props",
            stage_index=4,
            stage_total=total_stages,
            current=0,
            total=1,
            message="Settling completed player props.",
        )
        settlements = settle_completed_props(conn)
        special_settlements = settle_stocks(conn)
        _set_prop_sync_progress(
            current=1,
            total=1,
            message=f"Settled {int(settlements['settled'])} player props.",
        )
        _set_prop_sync_progress(
            stage="settling_games",
            stage_index=5,
            stage_total=total_stages,
            current=0,
            total=1,
            message="Settling completed game predictions.",
        )
        game_settlements = settle_completed_game_predictions(conn)
        _set_prop_sync_progress(
            current=1,
            total=1,
            message=f"Settled {int(game_settlements['settled'])} game predictions.",
        )
    _invalidate_read_caches()
    run_post_pipeline_steps(
        connect_fn=connect,
        policy=PropPostProcessPolicy(
            publish_mode="current_read",
            publish_start_message="Publishing refreshed read payloads.",
            publish_done_message="Legacy recalculate finished.",
        ),
        progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
            stage=stage,
            stage_index=6,
            stage_total=total_stages,
            current=current,
            total=total,
            message=message,
        ),
        publish_current_read_payloads_fn=lambda conn: _publish_current_read_payloads(conn),
    )
    return {
        "predictions": int(rebuild_result["rebuilt_predictions"]),
        "settled": settlements["settled"],
        "special_settled": special_settlements["settled"],
        "game_settled": game_settlements["settled"],
    }


def _queue_legacy_recalculate_job(request: Request | None = None) -> dict[str, Any]:
    with _PROP_SYNC_LOCK:
        if _PROP_SYNC_STATE["running"]:
            return {
                "status": "busy",
                "started_at": _PROP_SYNC_STATE["started_at"],
                "scope": _PROP_SYNC_STATE.get("scope"),
                "target_game_ids": list(_PROP_SYNC_STATE.get("target_game_ids") or []),
            }
    started_at = _begin_prop_sync_job("legacy_recalculate")
    job_run_id = _create_job_run(
        "legacy_recalculate",
        request=request,
        trigger_action="api.recalculate" if request is not None else "internal.legacy_recalculate",
        source_path=request.url.path if request is not None else "/api/recalculate",
        metadata={"started_at": started_at},
    )
    _append_job_run_event(job_run_id, "job.queued", "Legacy recalculate queued.")

    def _run() -> None:
        try:
            _append_job_run_event(job_run_id, "job.running", "Legacy recalculate started.")
            result = _run_legacy_recalculate_job()
            _mutate_prop_sync_state(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_result={"status": "completed", **result},
                last_error=None,
                status="completed",
                message="Legacy recalculate finished.",
            )
            _finish_job_run(job_run_id, status="completed", result=result)
        except Exception as exc:
            _mutate_prop_sync_state(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_error=str(exc),
                status="failed",
                message=str(exc),
            )
            _append_job_run_event(job_run_id, "job.failed", f"Legacy recalculate failed: {exc}", level="error")
            _finish_job_run(job_run_id, status="failed", error_text=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return {
        "status": "queued",
        "started_at": started_at,
        "scope": "legacy_recalculate",
        "target_game_ids": [],
    }


@app.get("/api/props/sync-status")
def props_sync_status() -> dict:
    sync_state, _latest_job = _resolved_prop_sync_state()
    return sync_state


def _run_current_slate_repair_job(target_game_ids: list[int] | None = None) -> dict[str, Any]:
    total_stages = 5
    with connect() as conn:
        result = _repair_current_slate_props(
            conn,
            target_game_ids=target_game_ids,
            progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
                stage=stage,
                stage_index=1 if stage == "syncing_props" else (2 if stage == "rebuilding_predictions" else 3),
                stage_total=total_stages,
                current=current,
                total=total,
                message=message,
            ),
        )
    _invalidate_read_caches()
    post_result = run_post_pipeline_steps(
        connect_fn=connect,
        policy=PropPostProcessPolicy(
            settle_recent_finals=True,
            publish_mode="targeted",
            matchup_game_ids=list(result.get("target_game_ids") or []),
            full_matchup_refresh=False,
            include_performance=False,
            settle_recent_finals_message="Settling recent completed games.",
            publish_start_message="Publishing repaired payloads.",
            publish_done_message="Current slate repair finished.",
        ),
        progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
            stage=stage,
            stage_index=4 if stage == "settling_recent_finals" else 5,
            stage_total=total_stages,
            current=current,
            total=total,
            message=message,
        ),
        settle_recent_finals_fn=_settle_recent_completed_games,
        publish_post_mutation_payloads_fn=lambda conn, policy: _publish_post_mutation_read_payloads(
            conn,
            matchup_game_ids=list(policy.matchup_game_ids or []),
            full_matchup_refresh=policy.full_matchup_refresh,
            include_performance=policy.include_performance,
        ),
    )
    result["recent_finals_settlement"] = post_result.recent_finals_settlement
    result["published_payloads"] = post_result.published_payloads
    return result


def _run_current_slate_repair_job_with_retry(
    target_game_ids: list[int] | None = None,
    *,
    job_run_id: int | None = None,
    max_attempts: int = 4,
    retry_delay_seconds: float = 2.0,
) -> dict[str, Any]:
    attempt = 1
    while True:
        try:
            return _run_current_slate_repair_job(target_game_ids)
        except sqlite3.OperationalError as exc:
            if not _is_sqlite_locked_error(exc) or attempt >= max_attempts:
                raise
            delay_seconds = retry_delay_seconds * attempt
            message = (
                f"Current slate repair hit a SQLite lock on attempt {attempt}/{max_attempts}; "
                f"retrying in {delay_seconds:.1f}s."
            )
            _append_job_run_event(
                job_run_id,
                "job.retry",
                message,
                level="warning",
                details={"attempt": attempt, "max_attempts": max_attempts, "delay_seconds": delay_seconds},
            )
            time.sleep(delay_seconds)
            attempt += 1


def _queue_current_slate_repair_job(
    target_game_ids: list[int] | None = None,
    *,
    request: Request | None = None,
) -> dict[str, Any]:
    with _PROP_SYNC_LOCK:
        if _PROP_SYNC_STATE["running"]:
            return {
                "status": "busy",
                "started_at": _PROP_SYNC_STATE["started_at"],
                "scope": _PROP_SYNC_STATE.get("scope"),
                "target_game_ids": list(_PROP_SYNC_STATE.get("target_game_ids") or []),
            }
    normalized_target_game_ids = sorted({int(game_id) for game_id in (target_game_ids or []) if int(game_id) > 0})
    started_at = _begin_prop_sync_job("injury_update" if normalized_target_game_ids else "current_slate")
    job_run_id = _create_job_run(
        "current_slate_repair",
        request=request,
        trigger_action="api.props.repair_current_slate" if request is not None else "internal.current_slate_repair",
        source_path=request.url.path if request is not None else "/api/props/repair-current-slate",
        metadata={"started_at": started_at, "scope": "injury_update" if normalized_target_game_ids else "current_slate"},
        target={"game_ids": normalized_target_game_ids},
    )
    _append_job_run_event(job_run_id, "job.queued", "Current slate repair queued.", details={"target_game_ids": normalized_target_game_ids})

    def _run() -> None:
        try:
            _append_job_run_event(job_run_id, "job.running", "Current slate repair started.", details={"target_game_ids": normalized_target_game_ids})
            result = (
                _run_current_slate_repair_job_with_retry(normalized_target_game_ids, job_run_id=job_run_id)
                if normalized_target_game_ids
                else _run_current_slate_repair_job_with_retry(job_run_id=job_run_id)
            )
            _mutate_prop_sync_state(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_result={"status": "completed", **result},
                last_error=None,
                status="completed",
                scope=result.get("scope") or "current_slate",
                target_game_ids=list(result.get("target_game_ids") or []),
                message="Current slate repair finished.",
            )
            _finish_job_run(job_run_id, status="completed", result=result)
        except Exception as exc:
            _mutate_prop_sync_state(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_error=str(exc),
                status="failed",
                message=str(exc),
            )
            _append_job_run_event(job_run_id, "job.failed", f"Current slate repair failed: {exc}", level="error")
            _finish_job_run(job_run_id, status="failed", error_text=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return {
        "status": "queued",
        "started_at": started_at,
        "scope": "injury_update" if normalized_target_game_ids else "current_slate",
        "target_game_ids": normalized_target_game_ids,
    }


def _run_odds_import_job(force_refresh: bool) -> dict[str, Any]:
    total_stages = 6
    with connect() as conn:
        result = _import_the_odds_api_provider_rows(
            conn,
            force_refresh=force_refresh,
            progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
                stage=stage,
                stage_index=(
                    1
                    if stage in {"loading_saved_cache", "requesting_provider"}
                    else 2
                    if stage == "syncing_props"
                    else 3
                ),
                stage_total=total_stages,
                current=current,
                total=total,
                message=message,
            ),
        )
    ingestion = build_odds_provider_ingestion(result)
    if ingestion.status in {"missing_api_key", "missing_cache", "stale_cache", "provider_error"}:
        post_result = run_post_pipeline_steps(
            connect_fn=connect,
            policy=PropPostProcessPolicy(
                refresh_covers_context=True,
                publish_mode="full",
                covers_refresh_start_message="Refreshing Covers matchup context for H2H and team history.",
                covers_refresh_done_message="Covers matchup context refreshed.",
                publish_start_message="Publishing refreshed odds payloads.",
                publish_done_message=str(result.get("message") or "Odds import failed."),
            ),
            progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
                stage=stage,
                stage_index=5 if stage == "refreshing_covers_context" else 6,
                stage_total=total_stages,
                current=current,
                total=total,
                message=message,
            ),
            refresh_covers_context_fn=lambda conn: import_covers_props(
                conn,
                selected_date=_local_today_iso(),
                force_refresh=True,
                sync_props=False,
                update_game_markets=False,
            ),
            publish_post_mutation_payloads_fn=lambda conn, _policy: _publish_post_mutation_read_payloads(conn),
        )
        result["published_payloads"] = post_result.published_payloads
        if post_result.covers_context is not None:
            result["covers_context"] = post_result.covers_context
        if post_result.covers_context_error:
            result["covers_context_error"] = post_result.covers_context_error
        message = str(ingestion.message or "Odds import failed.")
        _mutate_prop_sync_state(
            running=False,
            finished_at=datetime.now(timezone.utc).isoformat(),
            last_error=message,
            last_result=result,
            status="failed",
            scope="odds_import",
            message=message,
        )
        return result
    with connect() as conn:
        pipeline_result = run_prop_sync_pipeline(
            conn,
            request_source=ingestion.source,
            target_game_ids=ingestion.target_game_ids,
            sync_props=ingestion.prop_sync_eligible,
            rebuild_mode=ingestion.rebuild_mode,
            fallback_target_game_rebuild_when_unchanged=ingestion.fallback_target_game_rebuild_when_unchanged,
            skip_rebuild_message="No prop-line changes; skipped prediction rebuild.",
            sync_props_fn=sync_prop_lines_from_sportsbook,
            rebuild_predictions_fn=rebuild_predictions_live,
            progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
                stage=stage,
                stage_index=2 if stage == "syncing_props" else 3,
                stage_total=total_stages,
                current=current,
                total=total,
                message=message,
            ),
        )
        result["synced_props"] = int(pipeline_result.scanned_props)
        result["changed_props"] = int(pipeline_result.synced_props)
        result["target_game_ids"] = list(pipeline_result.target_game_ids)
        result["attempted_predictions"] = int(pipeline_result.attempted_predictions)
        result["rebuilt_predictions"] = int(pipeline_result.rebuilt_predictions)
        result["skipped_predictions"] = int(pipeline_result.skipped_predictions)
    _invalidate_read_caches()
    post_result = run_post_pipeline_steps(
        connect_fn=connect,
        policy=PropPostProcessPolicy(
            settle_recent_finals=True,
            refresh_covers_context=True,
            publish_mode="full",
            settle_recent_finals_message="Settling recent completed games.",
            covers_refresh_start_message="Refreshing Covers matchup context for H2H and team history.",
            covers_refresh_done_message="Covers matchup context refreshed.",
            publish_start_message="Publishing refreshed odds payloads.",
            publish_done_message=str(result.get("message") or "Odds import finished."),
        ),
        progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
            stage=stage,
            stage_index=4 if stage == "settling_recent_finals" else 5 if stage == "refreshing_covers_context" else 6,
            stage_total=total_stages,
            current=current,
            total=total,
            message=message,
        ),
        settle_recent_finals_fn=_settle_recent_completed_games,
        refresh_covers_context_fn=lambda conn: import_covers_props(
            conn,
            selected_date=_local_today_iso(),
            force_refresh=True,
            sync_props=False,
            update_game_markets=False,
        ),
        publish_post_mutation_payloads_fn=lambda conn, _policy: _publish_post_mutation_read_payloads(conn),
    )
    result["recent_finals_settlement"] = post_result.recent_finals_settlement
    covers_result = post_result.covers_context
    covers_error = post_result.covers_context_error
    result["published_payloads"] = post_result.published_payloads
    if covers_result is not None:
        result["covers_context"] = covers_result
        if covers_result.get("message"):
            result["message"] = f"{str(result.get('message') or 'Odds import finished.')} {covers_result['message']}"
    if covers_error:
        result["covers_context_error"] = covers_error
    return result


def _queue_odds_import_job(force_refresh: bool, request: Request | None = None) -> dict[str, Any]:
    with _PROP_SYNC_LOCK:
        if _PROP_SYNC_STATE["running"]:
            return {
                "status": "busy",
                "started_at": _PROP_SYNC_STATE["started_at"],
                "scope": _PROP_SYNC_STATE.get("scope"),
                "target_game_ids": list(_PROP_SYNC_STATE.get("target_game_ids") or []),
            }
    started_at = _begin_prop_sync_job(
        "odds_import",
        stage="queued",
        message="Odds import queued for background processing.",
    )
    job_run_id = _create_job_run(
        "odds_import",
        request=request,
        trigger_action="api.odds.import" if request is not None else "internal.odds_import",
        source_path=request.url.path if request is not None else "/api/odds/import",
        metadata={"started_at": started_at, "force_refresh": force_refresh},
    )
    _append_job_run_event(job_run_id, "job.queued", "Odds import queued.", details={"force_refresh": force_refresh})

    def _run() -> None:
        try:
            _append_job_run_event(job_run_id, "job.running", "Odds import started.", details={"force_refresh": force_refresh})
            result = _run_odds_import_job(force_refresh)
            if result.get("status") in {"missing_api_key", "missing_cache", "stale_cache", "provider_error"}:
                _append_job_run_event(job_run_id, "job.provider_failed", str(result.get("message") or "Odds import failed."), level="warning", details=result)
                _finish_job_run(job_run_id, status="failed", result=result, error_text=str(result.get("message") or "Odds import failed."))
                return
            _mutate_prop_sync_state(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_result=result,
                last_error=None,
                status="completed",
                scope="odds_import",
                message=str(result.get("message") or "Odds import finished."),
            )
            _finish_job_run(job_run_id, status="completed", result=result)
        except Exception as exc:
            _mutate_prop_sync_state(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_error=str(exc),
                status="failed",
                scope="odds_import",
                message=str(exc),
            )
            _append_job_run_event(job_run_id, "job.failed", f"Odds import failed: {exc}", level="error")
            _finish_job_run(job_run_id, status="failed", error_text=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return {
        "status": "queued",
        "started_at": started_at,
        "scope": "odds_import",
        "target_game_ids": [],
        "force_refresh": force_refresh,
        "message": "Odds import queued. Follow the Live Pipeline card for progress.",
    }


@app.post("/api/props/repair-current-slate", dependencies=[Depends(_protect_mutation)])
def repair_current_slate_props(request: Request) -> dict[str, Any]:
    return _run_audited_mutation(
        request,
        "props.repair_current_slate",
        lambda: _queue_current_slate_repair_job(request=request),
    )


@app.post("/api/settle-props", dependencies=[Depends(_protect_mutation)])
def settle_props(
    request: Request,
    selected_date: str | None = None,
    selected_dates: Annotated[list[str] | None, Query()] = None,
) -> dict:
    return _run_audited_mutation(
        request,
        "props.settle",
        lambda: _settle_props_impl(selected_date=selected_date, selected_dates=selected_dates),
        details={"selected_date": selected_date, "selected_dates": selected_dates or []},
    )


def _settle_props_impl(
    *,
    selected_date: str | None = None,
    selected_dates: list[str] | None = None,
) -> dict[str, Any]:
    with connect() as conn:
        props = settle_completed_props(conn, selected_date=selected_date, selected_dates=selected_dates)
        games = settle_completed_game_predictions(conn, selected_date=selected_date, selected_dates=selected_dates)
        special = settle_stocks(conn, selected_date=selected_date, selected_dates=selected_dates)
        gems = _sync_gem_snapshot_settlements(conn)
        watchlist = _sync_watchlist_snapshot_settlements(conn)
        scheduled_repair = _maybe_repair_current_slate_after_settlement(conn)
    _invalidate_read_caches()
    with connect() as conn:
        published_payloads = _publish_post_mutation_read_payloads(
            conn,
            matchup_game_ids=list(scheduled_repair.get("target_game_ids") or []) if isinstance(scheduled_repair, dict) else [],
            full_matchup_refresh=False,
        )
    return {
        "props": props,
        "games": games,
        "special": special,
        "gems": gems,
        "watchlist": watchlist,
        "scheduled_repair": scheduled_repair,
        "published_payloads": published_payloads,
        "selected_date": selected_date,
        "selected_dates": selected_dates or [],
    }


@app.get("/api/admin/unsettled-props", dependencies=[Depends(_protect_mutation)])
def unsettled_props_audit() -> dict[str, Any]:
    """List final prop lines that cannot yet be settled, grouped for DNP review."""
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                g.id AS game_id,
                g.game_date,
                home.abbreviation AS home_team,
                away.abbreviation AS away_team,
                p.id AS player_id,
                p.full_name AS player_name,
                COUNT(DISTINCT pl.id) AS prop_count,
                GROUP_CONCAT(DISTINCT pl.market) AS markets,
                CASE WHEN EXISTS (
                    SELECT 1 FROM player_game_stats pgs
                    WHERE pgs.game_id = pl.game_id AND pgs.player_id = pl.player_id
                ) THEN 1 ELSE 0 END AS has_boxscore,
                COALESCE((
                    SELECT MAX(pga.did_not_play)
                    FROM player_game_availability pga
                    WHERE pga.game_id = pl.game_id AND pga.player_id = pl.player_id
                ), 0) AS explicit_dnp,
                (
                    SELECT pga.status_reason
                    FROM player_game_availability pga
                    WHERE pga.game_id = pl.game_id AND pga.player_id = pl.player_id
                      AND trim(COALESCE(pga.status_reason, '')) <> ''
                    ORDER BY pga.did_not_play DESC, pga.observed_at DESC
                    LIMIT 1
                ) AS availability_reason
            FROM prop_lines pl
            JOIN games g ON g.id = pl.game_id
            JOIN players p ON p.id = pl.player_id
            JOIN teams home ON home.id = g.home_team_id
            JOIN teams away ON away.id = g.away_team_id
            LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
            WHERE g.status = 'final' AND sp.id IS NULL
            GROUP BY pl.game_id, pl.player_id
            ORDER BY g.game_date DESC, p.full_name
            """
        ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["markets"] = sorted(filter(None, str(item.get("markets") or "").split(",")))
        item["review_status"] = (
            "confirmed_dnp" if item["explicit_dnp"] else "missing_boxscore" if not item["has_boxscore"] else "settlement_context_missing"
        )
        items.append(item)
    return {"count": len(items), "items": items}


@app.post("/api/admin/unsettled-props/void-dnp", dependencies=[Depends(_protect_mutation)])
def void_dnp_props(request: Request, game_id: int, player_id: int, confirmation: str) -> dict[str, Any]:
    return _run_audited_mutation(
        request,
        "admin.unsettled_props.void_dnp",
        lambda: _void_dnp_props_impl(game_id=game_id, player_id=player_id, confirmation=confirmation),
        target={"game_id": game_id, "player_id": player_id},
    )


def _void_dnp_props_impl(*, game_id: int, player_id: int, confirmation: str) -> dict[str, Any]:
    if confirmation.strip() != "VOID DNP":
        raise HTTPException(status_code=400, detail="Confirmation must be VOID DNP.")
    with connect() as conn:
        game = conn.execute(
            "SELECT id FROM games WHERE id = ? AND status = 'final'", (int(game_id),)
        ).fetchone()
        player = conn.execute("SELECT team_id FROM players WHERE id = ?", (int(player_id),)).fetchone()
        if game is None or player is None:
            raise HTTPException(status_code=404, detail="Final game or player not found.")
        team = conn.execute(
            "SELECT team_id FROM player_team_history WHERE game_id = ? AND player_id = ? ORDER BY id DESC LIMIT 1",
            (int(game_id), int(player_id)),
        ).fetchone()
        team_id = int(team["team_id"]) if team else int(player["team_id"])
        conn.execute(
            """
            INSERT INTO player_game_availability (
                player_id, game_id, team_id, source, is_active, did_not_play, status_reason, minutes_text, observed_at
            ) VALUES (?, ?, ?, 'manual_review', 0, 1, 'Admin-confirmed DNP', NULL, ?)
            ON CONFLICT(player_id, game_id, source) DO UPDATE SET
                team_id = excluded.team_id, is_active = 0, did_not_play = 1,
                status_reason = excluded.status_reason, observed_at = excluded.observed_at
            """,
            (int(player_id), int(game_id), team_id, datetime.now(timezone.utc).isoformat()),
        )
        prop_rows = conn.execute(
            """
            SELECT pl.id FROM prop_lines pl
            LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
            WHERE pl.game_id = ? AND pl.player_id = ? AND sp.id IS NULL
            """,
            (int(game_id), int(player_id)),
        ).fetchall()
        prop_ids = [int(row["id"]) for row in prop_rows]
        if prop_ids:
            placeholders = ",".join("?" for _ in prop_ids)
            params = tuple(prop_ids)
            conn.execute(f"DELETE FROM watchlist_snapshot_items WHERE prop_line_id IN ({placeholders})", params)
            conn.execute(f"DELETE FROM gem_snapshot_items WHERE prop_line_id IN ({placeholders})", params)
            conn.execute(f"DELETE FROM prop_predictions WHERE prop_line_id IN ({placeholders})", params)
            conn.execute(f"DELETE FROM prop_lines WHERE id IN ({placeholders})", params)
        conn.commit()
    _invalidate_read_caches()
    return {"game_id": game_id, "player_id": player_id, "voided_prop_lines": len(prop_ids), "status": "voided_dnp"}


@app.get("/api/value-board", dependencies=[Depends(_protect_force_refresh)])
def value_board(response: Response, force_refresh: bool = False) -> list[dict]:
    cached_injury_refresh = _cached_rotowire_refresh_metadata()
    if force_refresh:
        started = datetime.now(timezone.utc)
        with connect() as conn:
            _publish_matchup_snapshot_aggregates(conn, injury_refresh=cached_injury_refresh)
            payload = _read_cached_payload(VALUE_BOARD_CACHE_NAME) or []
        compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        _set_observability_headers(response, VALUE_BOARD_CACHE_NAME, "BYPASS", round(compute_ms, 2))
        return payload
    def compute() -> list[dict]:
        with connect() as conn:
            aggregates = _aggregate_matchup_snapshot_payloads(conn)
            if aggregates is not None:
                return aggregates[0]
            _publish_matchup_snapshot_aggregates(conn, injury_refresh=cached_injury_refresh)
            return _read_cached_payload(VALUE_BOARD_CACHE_NAME) or []
    payload, status, compute_ms = _read_through_cache_with_meta(
        VALUE_BOARD_CACHE_NAME,
        VALUE_BOARD_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, VALUE_BOARD_CACHE_NAME, status, compute_ms)
    return payload


@app.get("/api/watchlist")
def watchlist(response: Response) -> list[dict]:
    def compute() -> list[dict]:
        with connect() as conn:
            return _watchlist_payload(conn)

    payload, status, compute_ms = _read_through_cache_with_meta(
        WATCHLIST_CACHE_NAME,
        WATCHLIST_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, WATCHLIST_CACHE_NAME, status, compute_ms)
    return payload


@app.get("/api/dfs/first-half")
def dfs_first_half(response: Response) -> list[dict[str, Any]]:
    started = datetime.now(timezone.utc)
    with connect() as conn:
        payload = build_current_dfs_first_half_estimates(
            conn,
            recent_values_fn=_recent_market_values,
            recent_minutes_fn=_recent_minutes_played,
        )
    compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
    _set_observability_headers(response, "dfs_first_half.json", "BYPASS", round(compute_ms, 2))
    return payload


@app.get("/api/player-first-half-history")
def player_first_half_history(
    player_name: str = Query(..., min_length=2),
    team: str | None = Query(None),
    opponent_team: str | None = Query(None),
    limit: int = Query(20, ge=1, le=100),
) -> list[dict[str, Any]]:
    with connect() as conn:
        params: list[Any] = [f"%{player_name.strip().lower()}%"]
        clauses = ["LOWER(p.full_name) LIKE ?"]
        if team:
            normalized_team = normalize_team_abbreviation(team)
            if normalized_team:
                clauses.append("upper(t.abbreviation) = ?")
                params.append(normalized_team.upper())
        if opponent_team:
            normalized_opponent = normalize_team_abbreviation(opponent_team)
            if normalized_opponent:
                clauses.append("upper(opp.abbreviation) = ?")
                params.append(normalized_opponent.upper())
        params.append(limit)
        rows = conn.execute(
            f"""
            SELECT
                g.game_date,
                g.id AS game_id,
                p.id AS player_id,
                p.full_name AS player,
                t.abbreviation AS team,
                opp.abbreviation AS opponent_team,
                pfh.first_half_points,
                pfh.first_half_rebounds,
                pfh.first_half_assists,
                pfh.first_half_threes,
                pfh.first_half_steals,
                pfh.first_half_blocks,
                pfh.first_half_turnovers,
                pfh.first_half_minutes,
                pfh.minutes_source,
                (
                    SELECT json_group_array(
                        json_object(
                            'market', pl.market,
                            'line', pl.line,
                            'sportsbook', pl.sportsbook,
                            'actual_result', sp.actual_result,
                            'winning_side', sp.winning_side
                        )
                    )
                    FROM settled_props sp
                    JOIN prop_lines pl ON pl.id = sp.prop_line_id
                    WHERE pl.game_id = pfh.game_id
                      AND pl.player_id = pfh.player_id
                ) AS settled_lines_json
            FROM player_first_half_stats pfh
            JOIN players p ON p.id = pfh.player_id
            JOIN games g ON g.id = pfh.game_id
            JOIN teams t ON t.id = pfh.team_id
            JOIN teams opp ON opp.id = pfh.opponent_team_id
            WHERE {' AND '.join(clauses)}
            ORDER BY g.game_date DESC, pfh.first_half_points DESC, pfh.player_id ASC
            LIMIT ?
            """,
            tuple(params),
        ).fetchall()
    payload: list[dict[str, Any]] = []
    for row in rows:
        payload.append(
            {
                "game_date": row["game_date"],
                "game_id": int(row["game_id"]),
                "player_id": int(row["player_id"]),
                "player": str(row["player"]),
                "team": str(row["team"]),
                "opponent_team": str(row["opponent_team"]),
                "first_half_points": int(row["first_half_points"] or 0),
                "first_half_rebounds": int(row["first_half_rebounds"] or 0),
                "first_half_assists": int(row["first_half_assists"] or 0),
                "first_half_threes": int(row["first_half_threes"] or 0),
                "first_half_steals": int(row["first_half_steals"] or 0),
                "first_half_blocks": int(row["first_half_blocks"] or 0),
                "first_half_turnovers": int(row["first_half_turnovers"] or 0),
                "first_half_minutes": float(row["first_half_minutes"] or 0.0),
                "minutes_source": str(row["minutes_source"] or ""),
                "settled_lines": json.loads(row["settled_lines_json"]) if row["settled_lines_json"] else [],
            }
        )
    return payload


@app.get("/api/player-first-half-lines")
def player_first_half_lines(limit: int = Query(200, ge=1, le=1000)) -> list[dict[str, Any]]:
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                g.game_date,
                g.id AS game_id,
                p.id AS player_id,
                p.full_name AS player,
                p.position,
                t.abbreviation AS team,
                t.logo_url AS team_logo_url,
                opp.abbreviation AS opponent_team,
                pl.sportsbook,
                pl.market,
                pl.line,
                sp.actual_result,
                sp.winning_side,
                pfh.first_half_points,
                pfh.first_half_rebounds,
                pfh.first_half_assists,
                pfh.first_half_threes,
                pfh.first_half_steals,
                pfh.first_half_blocks,
                pfh.first_half_turnovers,
                pfh.first_half_minutes,
                pfh.minutes_source
            FROM settled_props sp
            JOIN prop_lines pl ON pl.id = sp.prop_line_id
            JOIN games g ON g.id = pl.game_id
            JOIN players p ON p.id = pl.player_id
            JOIN player_first_half_stats pfh
              ON pfh.game_id = pl.game_id
             AND pfh.player_id = pl.player_id
            JOIN teams t ON t.id = pfh.team_id
            JOIN teams opp ON opp.id = pfh.opponent_team_id
            WHERE g.status = 'final'
            ORDER BY g.game_date DESC, pfh.first_half_points DESC, pl.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
    payload: list[dict[str, Any]] = []
    for row in rows:
        market = str(row["market"] or "").strip().lower()
        first_half_result = (
            float(row["first_half_points"] or 0.0) if market == "points"
            else float(row["first_half_rebounds"] or 0.0) if market == "rebounds"
            else float(row["first_half_assists"] or 0.0) if market == "assists"
            else float(row["first_half_threes"] or 0.0) if market == "threes"
            else float(row["first_half_steals"] or 0.0) if market == "steals"
            else float(row["first_half_blocks"] or 0.0) if market == "blocks"
            else float(row["first_half_blocks"] or 0.0) + float(row["first_half_steals"] or 0.0) if market == "blocks_steals"
            else float(row["first_half_points"] or 0.0) + float(row["first_half_rebounds"] or 0.0) if market == "points_rebounds"
            else float(row["first_half_points"] or 0.0) + float(row["first_half_assists"] or 0.0) if market == "points_assists"
            else float(row["first_half_rebounds"] or 0.0) + float(row["first_half_assists"] or 0.0) if market == "rebounds_assists"
            else float(row["first_half_points"] or 0.0) + float(row["first_half_rebounds"] or 0.0) + float(row["first_half_assists"] or 0.0) if market == "points_rebounds_assists"
            else None
        )
        if first_half_result is None:
            continue
        line = float(row["line"] or 0.0)
        expected_halfway_line = line * 0.5
        pace_ratio = first_half_result / expected_halfway_line if abs(expected_halfway_line) > 1e-9 else None
        halftime_margin_to_line = first_half_result - expected_halfway_line
        payload.append(
            {
                "game_date": row["game_date"],
                "game_id": int(row["game_id"]),
                "player_id": int(row["player_id"]),
                "player": str(row["player"]),
                "position": str(row["position"]) if row["position"] is not None else None,
                "team": str(row["team"]),
                "team_logo_url": str(row["team_logo_url"]) if row["team_logo_url"] is not None else None,
                "opponent_team": str(row["opponent_team"]),
                "sportsbook": str(row["sportsbook"]),
                "market": market,
                "line": line,
                "actual_result": float(row["actual_result"] or 0.0),
                "winning_side": str(row["winning_side"] or ""),
                "first_half_result": float(first_half_result),
                "expected_halfway_line": expected_halfway_line,
                "pace_ratio": pace_ratio,
                "halftime_margin_to_line": halftime_margin_to_line,
                "on_track_by_half": bool(pace_ratio is not None and pace_ratio >= 1.0),
                "first_half_points": int(row["first_half_points"] or 0),
                "first_half_rebounds": int(row["first_half_rebounds"] or 0),
                "first_half_assists": int(row["first_half_assists"] or 0),
                "first_half_threes": int(row["first_half_threes"] or 0),
                "first_half_steals": int(row["first_half_steals"] or 0),
                "first_half_blocks": int(row["first_half_blocks"] or 0),
                "first_half_turnovers": int(row["first_half_turnovers"] or 0),
                "first_half_minutes": float(row["first_half_minutes"] or 0.0),
                "minutes_source": str(row["minutes_source"] or ""),
            }
        )
    return payload


@app.get("/api/model-performance")
def model_performance(response=None) -> dict:
    def compute() -> dict:
        with connect() as conn:
            return _model_performance_payload(conn)

    if response is None:
        return compute()
    payload, status, compute_ms = _read_through_cache_with_meta(
        MODEL_PERFORMANCE_CACHE_NAME,
        MODEL_PERFORMANCE_TTL_SECONDS,
        compute,
    )
    if response is not None:
        _set_observability_headers(response, MODEL_PERFORMANCE_CACHE_NAME, status, compute_ms)
    return payload


@app.get("/api/gem-performance")
def gem_performance(response: Response) -> dict:
        def compute() -> dict:
                with connect() as conn:
                        return _gem_performance_payload(conn)

        payload, status, compute_ms = _read_through_cache_with_meta(
                GEM_PERFORMANCE_CACHE_NAME,
                GEM_PERFORMANCE_TTL_SECONDS,
                compute,
        )
        _set_observability_headers(response, GEM_PERFORMANCE_CACHE_NAME, status, compute_ms)
        return payload


@app.get("/api/watchlist-performance")
def watchlist_performance(response: Response) -> dict:
    def compute() -> dict:
        with connect() as conn:
            return _watchlist_performance_payload(conn)

    payload, status, compute_ms = _read_through_cache_with_meta(
        WATCHLIST_PERFORMANCE_CACHE_NAME,
        WATCHLIST_PERFORMANCE_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, WATCHLIST_PERFORMANCE_CACHE_NAME, status, compute_ms)
    return payload


def _snapshot_watchlist(conn, snapshot_date: str) -> dict:
    now = datetime.now(timezone.utc).isoformat()
    min_ev = 0.02
    min_edge = 0.05
    max_edge = LOW_CONFIDENCE_EDGE_MIN
    rows = _watchlist_payload(conn, min_ev=min_ev, min_edge=min_edge)
    existing = conn.execute(
        "SELECT id FROM watchlist_snapshots WHERE snapshot_date = ?",
        (snapshot_date,),
    ).fetchone()
    if existing:
        snapshot_id = int(existing["id"])
        conn.execute(
            """
            UPDATE watchlist_snapshots
            SET updated_at = ?, min_ev = ?, min_edge = ?, max_edge = ?, item_count = ?
            WHERE id = ?
            """,
            (now, min_ev, min_edge, max_edge, len(rows), snapshot_id),
        )
        conn.execute("DELETE FROM watchlist_snapshot_items WHERE snapshot_id = ?", (snapshot_id,))
    else:
        cursor = conn.execute(
            """
            INSERT INTO watchlist_snapshots (
                snapshot_date, created_at, updated_at, min_ev, min_edge, max_edge, item_count, settled_count, wins_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (snapshot_date, now, now, min_ev, min_edge, max_edge, len(rows)),
        )
        snapshot_id = int(cursor.lastrowid)
    if rows:
        conn.executemany(
            """
            INSERT INTO watchlist_snapshot_items (
                snapshot_id, prop_line_id, prediction_id, game_id, player_id, market, side, line, edge, expected_value, confidence
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_id,
                    int(row["prop_line_id"]),
                    int(row["id"]),
                    int(row["game_id"]),
                    int(row["player_id"]),
                    str(row["market"]),
                    str(row["recommended_side"]),
                    float(row["line"]),
                    float(row["edge"]),
                    float(row["expected_value"]),
                    str(row["confidence"]),
                )
                for row in rows
            ],
        )
    conn.commit()
    settlement = _sync_watchlist_snapshot_settlements(conn, snapshot_id=snapshot_id)
    return {
        "snapshot_id": snapshot_id,
        "snapshot_date": snapshot_date,
        "tracked": len(rows),
        "settled": settlement["settled_count"],
        "wins": settlement["wins_count"],
    }


def _sync_watchlist_snapshot_settlements(conn, snapshot_id: int | None = None) -> dict:
    params: tuple = ()
    filter_clause = ""
    if snapshot_id is not None:
        filter_clause = "WHERE i.snapshot_id = ?"
        params = (snapshot_id,)
    rows = conn.execute(
        f"""
        SELECT i.id, i.snapshot_id, i.side, sp.winning_side, sp.settled_at
        FROM watchlist_snapshot_items i
        LEFT JOIN settled_props sp ON sp.prop_line_id = i.prop_line_id
        {filter_clause}
        """,
        params,
    ).fetchall()
    updated = 0
    for row in rows:
        is_settled = 1 if row["winning_side"] is not None else 0
        conn.execute(
            """
            UPDATE watchlist_snapshot_items
            SET is_settled = ?, winning_side = ?, settled_at = ?
            WHERE id = ?
            """,
            (is_settled, row["winning_side"], row["settled_at"], int(row["id"])),
        )
        updated += 1
    target_params: tuple = () if snapshot_id is None else (snapshot_id,)
    target_clause = "" if snapshot_id is None else "WHERE s.id = ?"
    summary_rows = conn.execute(
        f"""
        SELECT
            s.id AS snapshot_id,
            COUNT(i.id) AS item_count,
            SUM(CASE WHEN i.is_settled = 1 THEN 1 ELSE 0 END) AS settled_count,
            SUM(CASE WHEN i.is_settled = 1 AND i.side = i.winning_side THEN 1 ELSE 0 END) AS wins_count
        FROM watchlist_snapshots s
        LEFT JOIN watchlist_snapshot_items i ON i.snapshot_id = s.id
        {target_clause}
        GROUP BY s.id
        """,
        target_params,
    ).fetchall()
    for row in summary_rows:
        conn.execute(
            """
            UPDATE watchlist_snapshots
            SET item_count = ?, settled_count = ?, wins_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(row["item_count"] or 0),
                int(row["settled_count"] or 0),
                int(row["wins_count"] or 0),
                datetime.now(timezone.utc).isoformat(),
                int(row["snapshot_id"]),
            ),
        )
    conn.commit()
    settled_count = sum(int(row["settled_count"] or 0) for row in summary_rows)
    wins_count = sum(int(row["wins_count"] or 0) for row in summary_rows)
    return {
        "updated_items": updated,
        "snapshots": len(summary_rows),
        "settled_count": settled_count,
        "wins_count": wins_count,
    }


def _gem_preset_config(preset: str) -> dict:
    key = (preset or "balanced").strip().lower()
    mapping = {
        "conservative": {"min_ev": 0.03, "min_edge": 0.08, "min_score": 0.55, "allow_low": False, "limit": 20},
        "balanced": {"min_ev": 0.02, "min_edge": 0.05, "min_score": 0.42, "allow_low": True, "limit": 24},
        "aggressive": {"min_ev": 0.01, "min_edge": 0.035, "min_score": 0.32, "allow_low": True, "limit": 30},
    }
    if key not in mapping:
        raise HTTPException(status_code=400, detail=f"Invalid preset: {preset}")
    return {"name": key, **mapping[key]}


def _build_current_gems(conn, preset: str) -> list[dict]:
    cfg = _gem_preset_config(preset)
    props = _value_board_payload(conn)
    discrepancies = line_discrepancies(conn)
    disc_map = {}
    for item in discrepancies:
        disc_map[(item["game_id"], item["player_name"], item["market"], item["side"])] = item
    scored = []
    for prop in props:
        disc = disc_map.get((prop["game_id"], prop["player"], prop["market"], prop["recommended_side"]))
        if not disc:
            continue
        edge = float(prop["edge"] or 0.0)
        ev = float(prop["expected_value"] or 0.0)
        confidence = str(prop["confidence"] or "").strip().lower()
        line_gap = float(disc.get("line_gap") or 0.0)
        price_gap = float(disc.get("price_gap") or 0.0)
        edge_norm = min(abs(edge) / 0.12, 1.0)
        ev_norm = min(max(ev, 0.0) / 0.08, 1.0)
        disc_norm = min(((line_gap * 0.7) + (price_gap / 40.0)) / 2.0, 1.0)
        confidence_factor = 1.0 if confidence == "high" else (0.92 if confidence == "medium" else 0.82)
        gem_score = round(((0.45 * edge_norm) + (0.35 * ev_norm) + (0.20 * disc_norm)) * confidence_factor, 3)
        if ev < cfg["min_ev"] or abs(edge) < cfg["min_edge"]:
            continue
        if (not cfg["allow_low"]) and confidence == "low":
            continue
        if gem_score < cfg["min_score"]:
            continue
        scored.append(
            {
                "prop_line_id": int(prop["prop_line_id"]),
                "game_id": int(prop["game_id"]),
                "player_id": int(prop["player_id"]),
                "player": str(prop["player"]),
                "position": prop.get("position"),
                "market": str(prop["market"]),
                "side": str(prop["recommended_side"]),
                "line": float(prop["line"]),
                "edge": edge,
                "expected_value": ev,
                "confidence": confidence,
                "line_gap": line_gap,
                "price_gap": int(price_gap),
                "gem_score": gem_score,
            }
        )
    scored.sort(key=lambda item: (item["gem_score"], item["expected_value"], item["edge"]), reverse=True)
    filtered: list[dict] = []
    primary_side_by_key: dict[tuple[int, int, str], str] = {}
    for item in scored:
        key = (int(item["game_id"]), int(item["player_id"]), str(item["market"]))
        primary_side = primary_side_by_key.get(key)
        if primary_side is None:
            primary_side_by_key[key] = str(item["side"])
            filtered.append(item)
            continue
        if str(item["side"]) == primary_side:
            filtered.append(item)
    return filtered[: int(cfg["limit"])]


def _snapshot_gems(conn, snapshot_date: str, preset: str) -> dict:
    cfg = _gem_preset_config(preset)
    now = datetime.now(timezone.utc).isoformat()
    rows = _build_current_gems(conn, cfg["name"])
    existing = conn.execute(
        "SELECT id FROM gem_snapshots WHERE snapshot_date = ? AND preset = ?",
        (snapshot_date, cfg["name"]),
    ).fetchone()
    if existing:
        snapshot_id = int(existing["id"])
        conn.execute(
            """
            UPDATE gem_snapshots
            SET updated_at = ?, min_ev = ?, min_edge = ?, allow_low = ?, min_score = ?, item_count = ?
            WHERE id = ?
            """,
            (now, float(cfg["min_ev"]), float(cfg["min_edge"]), 1 if cfg["allow_low"] else 0, float(cfg["min_score"]), len(rows), snapshot_id),
        )
        conn.execute("DELETE FROM gem_snapshot_items WHERE snapshot_id = ?", (snapshot_id,))
    else:
        cursor = conn.execute(
            """
            INSERT INTO gem_snapshots (
                snapshot_date, preset, created_at, updated_at, min_ev, min_edge, allow_low, min_score, item_count, settled_count, wins_count
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 0, 0)
            """,
            (snapshot_date, cfg["name"], now, now, float(cfg["min_ev"]), float(cfg["min_edge"]), 1 if cfg["allow_low"] else 0, float(cfg["min_score"]), len(rows)),
        )
        snapshot_id = int(cursor.lastrowid)
    if rows:
        conn.executemany(
            """
            INSERT INTO gem_snapshot_items (
                snapshot_id, prop_line_id, game_id, player_id, market, side, line, edge, expected_value, confidence, line_gap, price_gap, gem_score
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            [
                (
                    snapshot_id,
                    row["prop_line_id"],
                    row["game_id"],
                    row["player_id"],
                    row["market"],
                    row["side"],
                    row["line"],
                    row["edge"],
                    row["expected_value"],
                    row["confidence"],
                    row["line_gap"],
                    row["price_gap"],
                    row["gem_score"],
                )
                for row in rows
            ],
        )
    conn.commit()
    settlement = _sync_gem_snapshot_settlements(conn, snapshot_id=snapshot_id)
    return {
        "snapshot_id": snapshot_id,
        "snapshot_date": snapshot_date,
        "preset": cfg["name"],
        "tracked": len(rows),
        "settled": settlement["settled_count"],
        "wins": settlement["wins_count"],
    }


def _sync_gem_snapshot_settlements(conn, snapshot_id: int | None = None) -> dict:
    params: tuple = ()
    filter_clause = ""
    if snapshot_id is not None:
        filter_clause = "WHERE i.snapshot_id = ?"
        params = (snapshot_id,)
    rows = conn.execute(
        f"""
        SELECT i.id, i.snapshot_id, i.side, sp.winning_side, sp.settled_at
        FROM gem_snapshot_items i
        LEFT JOIN settled_props sp ON sp.prop_line_id = i.prop_line_id
        {filter_clause}
        """,
        params,
    ).fetchall()
    updated = 0
    for row in rows:
        is_settled = 1 if row["winning_side"] is not None else 0
        conn.execute(
            """
            UPDATE gem_snapshot_items
            SET is_settled = ?, winning_side = ?, settled_at = ?
            WHERE id = ?
            """,
            (is_settled, row["winning_side"], row["settled_at"], int(row["id"])),
        )
        updated += 1
    target_params: tuple = () if snapshot_id is None else (snapshot_id,)
    target_clause = "" if snapshot_id is None else "WHERE s.id = ?"
    summary_rows = conn.execute(
        f"""
        SELECT
            s.id AS snapshot_id,
            COUNT(i.id) AS item_count,
            SUM(CASE WHEN i.is_settled = 1 THEN 1 ELSE 0 END) AS settled_count,
            SUM(CASE WHEN i.is_settled = 1 AND i.side = i.winning_side THEN 1 ELSE 0 END) AS wins_count
        FROM gem_snapshots s
        LEFT JOIN gem_snapshot_items i ON i.snapshot_id = s.id
        {target_clause}
        GROUP BY s.id
        """,
        target_params,
    ).fetchall()
    for row in summary_rows:
        conn.execute(
            """
            UPDATE gem_snapshots
            SET item_count = ?, settled_count = ?, wins_count = ?, updated_at = ?
            WHERE id = ?
            """,
            (
                int(row["item_count"] or 0),
                int(row["settled_count"] or 0),
                int(row["wins_count"] or 0),
                datetime.now(timezone.utc).isoformat(),
                int(row["snapshot_id"]),
            ),
        )
    conn.commit()
    settled_count = sum(int(row["settled_count"] or 0) for row in summary_rows)
    wins_count = sum(int(row["wins_count"] or 0) for row in summary_rows)
    return {
        "updated_items": updated,
        "snapshots": len(summary_rows),
        "settled_count": settled_count,
        "wins_count": wins_count,
    }


@app.post("/api/gems/snapshot", dependencies=[Depends(_protect_mutation)])
def create_gem_snapshot(snapshot_date: str | None = None, preset: str = "balanced") -> dict:
    date_text = snapshot_date or datetime.now(LOCAL_TZ).date().isoformat()
    _parse_iso_date(date_text, "snapshot_date")
    with connect() as conn:
        payload = _snapshot_gems(conn, date_text, preset)
    _invalidate_read_caches()
    return payload


@app.post("/api/gems/sync-settlements", dependencies=[Depends(_protect_mutation)])
def sync_gem_settlements() -> dict:
    with connect() as conn:
        payload = _sync_gem_snapshot_settlements(conn)
    _invalidate_read_caches()
    return payload


@app.post("/api/watchlist/snapshot", dependencies=[Depends(_protect_mutation)])
def create_watchlist_snapshot(snapshot_date: str | None = None) -> dict:
    date_text = snapshot_date or datetime.now(LOCAL_TZ).date().isoformat()
    _parse_iso_date(date_text, "snapshot_date")
    with connect() as conn:
        payload = _snapshot_watchlist(conn, date_text)
    _invalidate_read_caches()
    return payload


@app.post("/api/watchlist/sync-settlements", dependencies=[Depends(_protect_mutation)])
def sync_watchlist_settlements() -> dict:
    with connect() as conn:
        payload = _sync_watchlist_snapshot_settlements(conn)
    _invalidate_read_caches()
    return payload


@app.get("/api/gems/snapshots")
def list_gem_snapshots(limit: int = 30, preset: str | None = None) -> list[dict]:
    max_limit = max(1, min(limit, 180))
    with connect() as conn:
        if preset:
            cfg = _gem_preset_config(preset)
            rows = conn.execute(
                """
                SELECT
                    id, snapshot_date, preset, created_at, updated_at,
                    item_count, settled_count, wins_count
                FROM gem_snapshots
                WHERE preset = ?
                ORDER BY snapshot_date DESC, id DESC
                LIMIT ?
                """,
                (cfg["name"], max_limit),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT
                    id, snapshot_date, preset, created_at, updated_at,
                    item_count, settled_count, wins_count
                FROM gem_snapshots
                ORDER BY snapshot_date DESC, id DESC
                LIMIT ?
                """,
                (max_limit,),
            ).fetchall()
    payload = []
    for row in rows:
        settled = int(row["settled_count"] or 0)
        wins = int(row["wins_count"] or 0)
        payload.append(
            {
                **dict(row),
                "win_rate": round(wins / settled, 4) if settled else None,
            }
        )
    return payload


@app.get("/api/watchlist/snapshots")
def list_watchlist_snapshots(limit: int = 30) -> list[dict]:
    max_limit = max(1, min(limit, 180))
    with connect() as conn:
        rows = conn.execute(
            """
            SELECT
                id, snapshot_date, created_at, updated_at,
                min_ev, min_edge, max_edge,
                item_count, settled_count, wins_count
            FROM watchlist_snapshots
            ORDER BY snapshot_date DESC, id DESC
            LIMIT ?
            """,
            (max_limit,),
        ).fetchall()
    payload = []
    for row in rows:
        settled = int(row["settled_count"] or 0)
        wins = int(row["wins_count"] or 0)
        payload.append(
            {
                **dict(row),
                "win_rate": round(wins / settled, 4) if settled else None,
            }
        )
    return payload


@app.get("/api/performance/timeline")
def performance_timeline(limit: int = 30) -> list[dict]:
    max_limit = max(1, min(limit, 180))
    with connect() as conn:
        model_rows = conn.execute(
            """
            WITH ranked AS (
              SELECT
                pp.prop_line_id,
                pp.recommended_side,
                DATE(sp.settled_at) AS settled_date,
                sp.winning_side,
                ROW_NUMBER() OVER (
                  PARTITION BY pp.prop_line_id
                  ORDER BY pp.prediction_time DESC, pp.id DESC
                ) AS rn
              FROM prop_predictions pp
              JOIN settled_props sp ON sp.prop_line_id = pp.prop_line_id
            )
            SELECT
              settled_date,
              COUNT(*) AS n,
              SUM(CASE WHEN lower(recommended_side) = lower(winning_side) THEN 1 ELSE 0 END) AS wins
            FROM ranked
            WHERE rn = 1
            GROUP BY settled_date
            ORDER BY settled_date DESC
            LIMIT ?
            """,
            (max_limit,),
        ).fetchall()
        gem_rows = conn.execute(
            """
            SELECT
              snapshot_date,
              settled_count,
              wins_count
            FROM gem_snapshots
            ORDER BY snapshot_date DESC
            LIMIT ?
            """,
            (max_limit,),
        ).fetchall()
        watch_rows = conn.execute(
            """
            SELECT
              snapshot_date,
              settled_count,
              wins_count
            FROM watchlist_snapshots
            ORDER BY snapshot_date DESC
            LIMIT ?
            """,
            (max_limit,),
        ).fetchall()
    by_date: dict[str, dict] = {}
    for row in model_rows:
        d = str(row["settled_date"])
        item = by_date.setdefault(d, {"date": d})
        n = int(row["n"] or 0)
        wins = int(row["wins"] or 0)
        item["model_settled"] = n
        item["model_wins"] = wins
        item["model_win_rate"] = round(wins / n, 4) if n else None
    for row in gem_rows:
        d = str(row["snapshot_date"])
        item = by_date.setdefault(d, {"date": d})
        n = int(row["settled_count"] or 0)
        wins = int(row["wins_count"] or 0)
        item["gem_settled"] = n
        item["gem_wins"] = wins
        item["gem_win_rate"] = round(wins / n, 4) if n else None
    for row in watch_rows:
        d = str(row["snapshot_date"])
        item = by_date.setdefault(d, {"date": d})
        n = int(row["settled_count"] or 0)
        wins = int(row["wins_count"] or 0)
        item["watchlist_settled"] = n
        item["watchlist_wins"] = wins
        item["watchlist_win_rate"] = round(wins / n, 4) if n else None
    return sorted(by_date.values(), key=lambda item: item["date"], reverse=True)[:max_limit]


@app.get("/api/model-diagnostics")
def model_diagnostics(model_version: str = MODEL_VERSION, windows: str = "7,14,30") -> dict:
    parsed_windows = []
    for item in windows.split(","):
        token = item.strip()
        if not token:
            continue
        try:
            days = int(token)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid window value: {token}") from exc
        if days > 0 and days not in parsed_windows:
            parsed_windows.append(days)
    if not parsed_windows:
        parsed_windows = [7, 14, 30]

    with connect() as conn:
        overall = build_accuracy_report(conn, model_version=model_version)
        by_window = {
            f"{days}d": build_accuracy_report_for_days(conn, days, model_version=model_version)
            for days in parsed_windows
        }
    return {
        "model_version": model_version,
        "overall": {
            "total_predictions": overall.total_predictions,
            "mae": overall.mae,
            "rmse": overall.rmse,
            "bias": overall.bias,
            "directional_accuracy": overall.directional_accuracy,
            "market_breakdown": overall.market_breakdown,
        },
        "windows": {
            key: {
                "total_predictions": report.total_predictions,
                "mae": report.mae,
                "rmse": report.rmse,
                "bias": report.bias,
                "directional_accuracy": report.directional_accuracy,
                "market_breakdown": report.market_breakdown,
            }
            for key, report in by_window.items()
        },
    }


def _minutes_metric_summary(rows: list[dict[str, float | str | int | None]]) -> dict[str, Any]:
    if not rows:
        return {
            "rows": 0,
            "production_mae": None,
            "learned_mae": None,
            "heuristic_mae": None,
            "recent5_mae": None,
            "recent10_mae": None,
            "recent_blend_mae": None,
            "production_bias": None,
            "learned_bias": None,
            "heuristic_bias": None,
        }
    count = len(rows)
    production_mae = round(sum(abs(float(row["learned_error"])) for row in rows) / count, 3)
    production_bias = round(sum(float(row["learned_error"]) for row in rows) / count, 3)
    return {
        "rows": count,
        "production_mae": production_mae,
        "learned_mae": production_mae,
        "heuristic_mae": round(sum(abs(float(row["heuristic_error"])) for row in rows) / count, 3),
        "recent5_mae": round(sum(abs(float(row["recent5_error"])) for row in rows) / count, 3),
        "recent10_mae": round(sum(abs(float(row["recent10_error"])) for row in rows) / count, 3),
        "recent_blend_mae": round(sum(abs(float(row["recent_blend_error"])) for row in rows) / count, 3),
        "production_bias": production_bias,
        "learned_bias": production_bias,
        "heuristic_bias": round(sum(float(row["heuristic_error"]) for row in rows) / count, 3),
    }


def _minutes_trend_bucket(value: float) -> str:
    if value <= -4.0:
        return "drop"
    if value >= 3.5:
        return "rise"
    return "stable"


def _minutes_volatility_bucket(value: float) -> str:
    if value >= 8.0:
        return "high"
    if value >= 5.0:
        return "medium"
    return "low"


def _minutes_context_bucket(row: dict[str, float | str | int | None]) -> str:
    if int(_row_value(row, "recent_transfer", 0) or 0) == 1:
        return "recent_transfer"
    if float(_row_value(row, "recent_absence_days", 0.0) or 0.0) >= 7.0:
        return "absence_return"
    unavailable_minutes = float(_row_value(row, "same_position_unavailable_minutes", 0.0) or 0.0)
    key_out_count = float(_row_value(row, "same_position_key_out_count", 0.0) or 0.0)
    if unavailable_minutes >= 18.0 and key_out_count >= 1.0:
        return "vacancy"
    if unavailable_minutes >= 10.0 or key_out_count >= 1.0:
        return "soft_vacancy"
    return "stable_context"


def _minutes_breakdown_entry(rows: list[dict[str, float | str | int | None]]) -> dict[str, Any]:
    summary = _minutes_metric_summary(rows)
    if not rows:
        return {
            **summary,
            "delta_vs_recent_blend": None,
            "overpredict_rate": None,
            "underpredict_rate": None,
            "avg_learned_minus_recent_blend": None,
        }
    count = len(rows)
    overpredict_rate = sum(1 for row in rows if float(row["learned_error"]) > 0.0) / count
    underpredict_rate = sum(1 for row in rows if float(row["learned_error"]) < 0.0) / count
    avg_learned_minus_recent_blend = sum(
        float(row["learned_minutes"]) - float(row["recent_blend_minutes"])
        for row in rows
    ) / count
    return {
        **summary,
        "delta_vs_recent_blend": round(float(summary["production_mae"]) - float(summary["recent_blend_mae"]), 3),
        "overpredict_rate": round(overpredict_rate, 3),
        "underpredict_rate": round(underpredict_rate, 3),
        "avg_learned_minus_recent_blend": round(avg_learned_minus_recent_blend, 3),
        "avg_post_blend_minus_recent_blend": round(
            sum(float(row["post_blend_projection"]) - float(row["recent_blend_minutes"]) for row in rows) / count,
            3,
        ),
        "avg_post_anchor_minus_recent_blend": round(
            sum(float(row["post_anchor_projection"]) - float(row["recent_blend_minutes"]) for row in rows) / count,
            3,
        ),
        "avg_post_baseline_minus_recent_blend": round(
            sum(float(row["post_baseline_projection"]) - float(row["recent_blend_minutes"]) for row in rows) / count,
            3,
        ),
        "avg_post_recency_floor_minus_recent_blend": round(
            sum(float(row["post_recency_floor_projection"]) - float(row["recent_blend_minutes"]) for row in rows) / count,
            3,
        ),
        "avg_post_bounds_minus_recent_blend": round(
            sum(float(row["post_bounds_projection"]) - float(row["recent_blend_minutes"]) for row in rows) / count,
            3,
        ),
        "avg_post_hard_rules_minus_recent_blend": round(
            sum(float(row["post_hard_rules_projection"]) - float(row["recent_blend_minutes"]) for row in rows) / count,
            3,
        ),
        "avg_post_rebound_guard_minus_recent_blend": round(
            sum(float(row["post_rebound_guard_projection"]) - float(row["recent_blend_minutes"]) for row in rows) / count,
            3,
        ),
        "avg_final_minus_recent_blend": round(
            sum(float(row["final_projection"]) - float(row["recent_blend_minutes"]) for row in rows) / count,
            3,
        ),
    }


def _minutes_slice_report(
    rows: list[dict[str, float | str | int | None]],
    *,
    key_name: str,
    min_rows: int,
) -> dict[str, dict[str, Any]]:
    grouped: dict[str, list[dict[str, float | str | int | None]]] = {}
    for row in rows:
        grouped.setdefault(str(row[key_name]), []).append(row)
    report: dict[str, dict[str, Any]] = {}
    for key, bucket_rows in sorted(grouped.items()):
        if len(bucket_rows) < min_rows:
            continue
        report[key] = _minutes_breakdown_entry(bucket_rows)
    return report


def _minutes_biggest_regressions(
    rows: list[dict[str, float | str | int | None]],
    *,
    min_rows: int,
    limit: int,
) -> list[dict[str, Any]]:
    candidates: list[dict[str, Any]] = []
    for dimension in ("role_bucket", "trend_bucket", "volatility_bucket", "context_bucket"):
        grouped: dict[str, list[dict[str, float | str | int | None]]] = {}
        for row in rows:
            grouped.setdefault(str(row[dimension]), []).append(row)
        for key, bucket_rows in grouped.items():
            if len(bucket_rows) < min_rows:
                continue
            entry = _minutes_breakdown_entry(bucket_rows)
            delta = entry["delta_vs_recent_blend"]
            if delta is None:
                continue
            candidates.append({
                "dimension": dimension,
                "label": key,
                **entry,
            })
    candidates.sort(key=lambda item: (float(item["delta_vs_recent_blend"]), item["rows"]), reverse=True)
    return candidates[:limit]


def _minutes_top_loss_rows(
    rows: list[dict[str, float | str | int | None]],
    *,
    role_bucket: str | None = None,
    limit: int = 10,
) -> list[dict[str, Any]]:
    filtered = [
        row for row in rows
        if role_bucket is None or str(row["role_bucket"]) == role_bucket
    ]
    filtered.sort(
        key=lambda row: (
            abs(float(row["learned_error"])) - abs(float(row["recent_blend_error"])),
            abs(float(row["learned_error"])),
        ),
        reverse=True,
    )
    top_rows: list[dict[str, Any]] = []
    for row in filtered[:limit]:
        top_rows.append({
            "player_id": int(row["player_id"]),
            "game_id": int(row["game_id"]),
            "game_date": str(row["game_date"]),
            "role_bucket": str(row["role_bucket"]),
            "trend_bucket": str(row["trend_bucket"]),
            "volatility_bucket": str(row["volatility_bucket"]),
            "context_bucket": str(row["context_bucket"]),
            "actual_minutes": round(float(row["actual_minutes"]), 3),
            "recent_blend_minutes": round(float(row["recent_blend_minutes"]), 3),
            "learned_minutes": round(float(row["learned_minutes"]), 3),
            "recent_blend_error": round(float(row["recent_blend_error"]), 3),
            "learned_error": round(float(row["learned_error"]), 3),
            "delta_vs_recent_blend_abs": round(
                abs(float(row["learned_error"])) - abs(float(row["recent_blend_error"])),
                3,
            ),
            "post_blend_projection": round(float(row["post_blend_projection"]), 3),
            "post_anchor_projection": round(float(row["post_anchor_projection"]), 3),
            "post_baseline_projection": round(float(row["post_baseline_projection"]), 3),
            "post_recency_floor_projection": round(float(row["post_recency_floor_projection"]), 3),
            "post_bounds_projection": round(float(row["post_bounds_projection"]), 3),
            "post_hard_rules_projection": round(float(row["post_hard_rules_projection"]), 3),
            "post_rebound_guard_projection": round(float(row["post_rebound_guard_projection"]), 3),
            "final_projection": round(float(row["final_projection"]), 3),
            "minutes_trend": round(float(row["minutes_trend"]), 3),
            "minute_volatility": round(float(row["minute_volatility"]), 3),
            "recent_absence_days": (
                round(float(row["recent_absence_days"]), 3)
                if row["recent_absence_days"] is not None
                else None
            ),
            "same_position_unavailable_minutes": round(float(row["same_position_unavailable_minutes"]), 3),
            "same_position_key_out_count": round(float(row["same_position_key_out_count"]), 3),
        })
    return top_rows


def _build_minutes_eval_rows(conn: Any, effective_start: str) -> list[dict[str, float | str | int | None]]:
    rows = conn.execute("SELECT id FROM players ORDER BY id").fetchall()
    eval_rows: list[dict[str, float | str | int | None]] = []
    for player in rows:
        player_id = int(player["id"])
        training_rows = _player_training_rows(conn, player_id)
        minutes_history = [float(row["minutes"] or 0.0) for row in training_rows]
        for idx in range(5, len(training_rows)):
            current = training_rows[idx]
            game_date = str(current["game_date"] or "")
            if game_date[:10] < str(effective_start):
                continue
            newest_minutes = list(reversed(minutes_history[max(0, idx - 10):idx]))
            if len(newest_minutes) < 5:
                continue
            previous_game_date = str(training_rows[idx - 1]["game_date"]) if idx > 0 else None
            context = _historical_game_context(current)
            minute_volatility = _minute_volatility(newest_minutes)
            minutes_trend = _recent_trend(newest_minutes)
            ewma_minutes = _ewma_newest_first(newest_minutes, alpha=0.38)
            recent_minutes_avg = sum(newest_minutes[:5]) / min(len(newest_minutes), 5)
            last_10_minutes_avg = sum(newest_minutes) / len(newest_minutes)
            recent_absence_days = _days_between_game_dates(previous_game_date, game_date)
            blowout = _blowout_adjustment(conn, context, str(current["rotation_role"] or "starter"))
            team_transition = _team_transition_features_from_player_rows(
                conn,
                player_id=player_id,
                team_id=int(current["team_id"]),
                player_rows=training_rows,
                row_index=idx,
            )
            lineup_context = _minutes_lineup_context_from_player_rows(
                conn,
                player_id=player_id,
                team_id=int(current["team_id"]),
                player_rows=training_rows,
                row_index=idx,
            )
            opportunity_context = _historical_minutes_opportunity_context(
                conn,
                player_id=player_id,
                game_id=int(current["game_id"]),
                team_id=int(current["team_id"]),
                position=str(current["position"] or ""),
                before_game_date=game_date,
            )
            learned_minutes, _note, stage_details = _project_minutes(
                conn,
                player_id=player_id,
                game_id=int(current["game_id"]),
                rotation_role=str(current["rotation_role"] or "starter"),
                ewma_minutes=ewma_minutes,
                minutes_trend=minutes_trend,
                recent_minutes_avg=recent_minutes_avg,
                last_10_minutes_avg=last_10_minutes_avg,
                minute_volatility=minute_volatility,
                context=context,
                blowout_delta=float(blowout["minutes_delta"]),
                injury_delta=0.0,
                injury_status="available",
                recent_absence_days=recent_absence_days,
                team_transition=team_transition,
                lineup_context=lineup_context,
                opportunity_context=opportunity_context,
                before_game_date=game_date,
                allow_training=False,
                include_stage_details=True,
            )
            role_state = _classify_minutes_role(
                rotation_role=str(current["rotation_role"] or "starter"),
                recent_minutes_avg=recent_minutes_avg,
                last_10_minutes_avg=last_10_minutes_avg,
                ewma_minutes=ewma_minutes,
                minutes_trend=minutes_trend,
                minute_volatility=minute_volatility,
                injury_status="available",
                injury_delta=0.0,
                recent_absence_days=recent_absence_days,
                lineup_context=lineup_context,
                opportunity_context=opportunity_context,
            )
            heuristic_minutes = max(ewma_minutes + (0.35 * minutes_trend), 4.0)
            recent_blend = (0.65 * recent_minutes_avg) + (0.35 * last_10_minutes_avg)
            actual_minutes = float(current["minutes"] or 0.0)
            eval_rows.append({
                "player_id": player_id,
                "game_id": int(current["game_id"]),
                "game_date": game_date,
                "season": game_date[:4],
                "role_bucket": role_state.bucket,
                "recent_transfer": 1 if float(team_transition[0]) > 0.0 and float(team_transition[0]) <= 10.0 else 0,
                "recent_absence_days": recent_absence_days,
                "minutes_trend": minutes_trend,
                "minute_volatility": minute_volatility,
                "same_position_unavailable_minutes": float(opportunity_context[0]) if opportunity_context else 0.0,
                "same_position_key_out_count": float(opportunity_context[1]) if opportunity_context else 0.0,
                "trend_bucket": _minutes_trend_bucket(minutes_trend),
                "volatility_bucket": _minutes_volatility_bucket(minute_volatility),
                "learned_minutes": learned_minutes,
                "recent_blend_minutes": recent_blend,
                "actual_minutes": actual_minutes,
                "post_blend_projection": float(stage_details.get("post_blend_projection") or learned_minutes),
                "post_anchor_projection": float(stage_details.get("post_anchor_projection") or learned_minutes),
                "post_baseline_projection": float(stage_details.get("post_baseline_projection") or learned_minutes),
                "post_recency_floor_projection": float(stage_details.get("post_recency_floor_projection") or learned_minutes),
                "post_bounds_projection": float(stage_details.get("post_bounds_projection") or learned_minutes),
                "post_hard_rules_projection": float(stage_details.get("post_hard_rules_projection") or learned_minutes),
                "post_rebound_guard_projection": float(stage_details.get("post_rebound_guard_projection") or learned_minutes),
                "final_projection": float(stage_details.get("final_projection") or learned_minutes),
                "heuristic_error": heuristic_minutes - actual_minutes,
                "learned_error": learned_minutes - actual_minutes,
                "recent5_error": recent_minutes_avg - actual_minutes,
                "recent10_error": last_10_minutes_avg - actual_minutes,
                "recent_blend_error": recent_blend - actual_minutes,
            })
    for row in eval_rows:
        row["context_bucket"] = _minutes_context_bucket(row)
    return eval_rows


@app.get("/api/minutes-diagnostics")
def minutes_diagnostics(start_date: str | None = None) -> dict[str, Any]:
    evaluation_start = (start_date or "").strip()
    if evaluation_start:
        try:
            datetime.fromisoformat(evaluation_start[:10])
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid start_date: {evaluation_start}") from exc

    with connect() as conn:
        effective_start = evaluation_start or conn.execute("SELECT DATE(?) AS d", (_training_start_date(),)).fetchone()["d"]
        eval_rows = _build_minutes_eval_rows(conn, str(effective_start))

    by_role: dict[str, list[dict[str, float | str | int | None]]] = {}
    by_season: dict[str, list[dict[str, float | str | int | None]]] = {}
    recent_transfer_rows: list[dict[str, float | str | int | None]] = []
    for row in eval_rows:
        by_role.setdefault(str(row["role_bucket"]), []).append(row)
        by_season.setdefault(str(row["season"]), []).append(row)
        if int(row["recent_transfer"] or 0) == 1:
            recent_transfer_rows.append(row)

    return {
        "model_version": MODEL_VERSION,
        "evaluation_start": effective_start,
        "overall": _minutes_metric_summary(eval_rows),
        "recent_transfer": _minutes_metric_summary(recent_transfer_rows),
        "by_role": {
            role: _minutes_metric_summary(role_rows)
            for role, role_rows in sorted(by_role.items())
        },
        "by_season": {
            season: _minutes_metric_summary(season_rows)
            for season, season_rows in sorted(by_season.items())
        },
        "breakdown": {
            "by_trend": _minutes_slice_report(eval_rows, key_name="trend_bucket", min_rows=25),
            "by_volatility": _minutes_slice_report(eval_rows, key_name="volatility_bucket", min_rows=25),
            "by_context": _minutes_slice_report(eval_rows, key_name="context_bucket", min_rows=25),
            "biggest_regressions": _minutes_biggest_regressions(eval_rows, min_rows=25, limit=8),
            "top_loss_rows": {
                "overall": _minutes_top_loss_rows(eval_rows, limit=10),
                "rotation": _minutes_top_loss_rows(eval_rows, role_bucket="rotation", limit=10),
                "starter_volatile": _minutes_top_loss_rows(eval_rows, role_bucket="starter_volatile", limit=10),
            },
        },
    }


@app.get("/api/model-loss-breakdown")
def model_loss_breakdown(model_version: str = MODEL_VERSION, top_n_players: int = 15) -> dict:
    with connect() as conn:
        rows = conn.execute(
            """
            WITH ranked AS (
              SELECT
                pp.*,
                ROW_NUMBER() OVER (
                  PARTITION BY pp.prop_line_id
                  ORDER BY pp.prediction_time DESC, pp.id DESC
                ) AS rn
              FROM prop_predictions pp
              WHERE pp.model_version = ?
            )
            SELECT
              pl.market,
              r.confidence,
              r.recommended_side,
              sp.winning_side,
              p.full_name,
              t.abbreviation AS team,
              g.spread_home,
              sp.player_minutes
            FROM settled_props sp
            JOIN ranked r ON r.prop_line_id = sp.prop_line_id AND r.rn = 1
            JOIN prop_lines pl ON pl.id = sp.prop_line_id
            JOIN players p ON p.id = pl.player_id
            JOIN teams t ON t.id = p.team_id
            JOIN games g ON g.id = pl.game_id
            """
            ,
            (model_version,),
        ).fetchall()

    total = len(rows)
    losses = [row for row in rows if row["recommended_side"] != row["winning_side"]]
    wins = total - len(losses)

    def spread_bucket(value) -> str:
        if value is None:
            return "none"
        absolute = abs(float(value))
        if absolute < 4.5:
            return "<4.5"
        if absolute < 8.5:
            return "4.5-8.4"
        if absolute < 12.5:
            return "8.5-12.4"
        return "12.5+"

    def minutes_bucket(value) -> str:
        if value is None:
            return "unknown"
        minutes = float(value)
        if minutes < 10:
            return "0-9"
        if minutes < 20:
            return "10-19"
        if minutes < 30:
            return "20-29"
        return "30+"

    return {
        "model_version": model_version,
        "total_settled": total,
        "wins": wins,
        "losses": len(losses),
        "win_rate": round((wins / total), 4) if total else None,
        "losses_by_market": Counter(row["market"] for row in losses).most_common(),
        "losses_by_confidence": Counter(row["confidence"] for row in losses).most_common(),
        "losses_by_spread_bucket": Counter(spread_bucket(row["spread_home"]) for row in losses).most_common(),
        "losses_by_minutes_bucket": Counter(minutes_bucket(row["player_minutes"]) for row in losses).most_common(),
        "top_players_by_losses": Counter(row["full_name"] for row in losses).most_common(top_n_players),
        "teams_by_losses": Counter(row["team"] for row in losses).most_common(),
    }


@app.post("/api/models/train", dependencies=[Depends(_protect_mutation)])
def train_model(request: Request) -> dict:
    return _run_audited_mutation(request, "models.train", lambda: _queue_model_training_job(request))


@app.post("/api/models/tune", dependencies=[Depends(_protect_mutation)])
def tune_model(request: Request) -> dict:
    return _run_audited_mutation(request, "models.tune", _tune_model_impl)


def _tune_model_impl() -> dict:
    with connect() as conn:
        return run_parameter_tuning(conn)


@app.post("/api/odds/import", dependencies=[Depends(_protect_mutation)])
def import_odds(request: Request, force_refresh: bool = False) -> dict:
    return _run_audited_mutation(
        request,
        "odds.import",
        lambda: _queue_odds_import_job(force_refresh, request),
        details={"force_refresh": force_refresh},
    )


@app.post("/api/covers/import", dependencies=[Depends(_protect_mutation)])
def import_covers(request: Request, selected_date: str | None = None, force_refresh: bool = False) -> dict:
    return _run_audited_mutation(
        request,
        "covers.import",
        lambda: _import_covers_impl(request=request, selected_date=selected_date, force_refresh=force_refresh),
        details={"selected_date": selected_date, "force_refresh": force_refresh},
    )


def _import_covers_impl(*, request: Request | None, selected_date: str | None, force_refresh: bool) -> dict[str, Any]:
    with connect() as conn:
        result = _import_covers_provider_rows(conn, selected_date=selected_date, force_refresh=force_refresh, update_game_markets=True)
    ingestion = build_covers_provider_ingestion(result)
    sync_started = False
    if ingestion.prop_sync_eligible:
        sync_started = _start_prop_sync_if_needed("covers_import", request=request)
    result["sync_started"] = sync_started
    if sync_started:
        base_message = str(ingestion.message or "").strip()
        if base_message:
            result["message"] = f"{base_message} Prop sync queued in background."
        else:
            result["message"] = "Covers import completed. Prop sync queued in background."
    _invalidate_read_caches()
    if not sync_started:
        with connect() as conn:
            result["published_payloads"] = _publish_post_mutation_read_payloads(conn)
    return result


@app.post("/api/injuries/import/rotowire", dependencies=[Depends(_protect_mutation)])
def import_rotowire_injuries(request: Request, force_refresh: bool = False) -> dict:
    return _run_audited_mutation(
        request,
        "injuries.rotowire.import",
        lambda: _import_rotowire_injuries_impl(request=request, force_refresh=force_refresh),
        details={"force_refresh": force_refresh},
    )


def _import_rotowire_injuries_impl(*, request: Request | None, force_refresh: bool) -> dict[str, Any]:
    with connect() as conn:
        result = import_rotowire_lineups(conn, force_refresh=force_refresh)
        result["affected_game_ids"] = _scheduled_game_ids_for_teams(conn, result.get("affected_team_ids", []))
        delete_json_cache(ROSTER_CACHE_NAME)
        delete_json_cache(MATCHUPS_CACHE_NAME)
        roster_payload = _roster_payload(conn, refresh_lineups=False)
        write_json_cache(ROSTER_CACHE_NAME, _cache_envelope(roster_payload, ROSTER_TTL_SECONDS))
        result["published_payloads"] = {ROSTER_CACHE_NAME: len(roster_payload)}
        result["roster"] = roster_payload
        result["roster_cache_refresh"] = {"status": "ready"}
    affected_game_ids = list(result.get("affected_game_ids") or [])
    roster_changed = bool(result.get("roster_changed"))
    repair_ingestion = build_no_provider_ingestion(
        scope="injury_update",
        target_game_ids=affected_game_ids if roster_changed else [],
    )
    result["repair"] = (
        _queue_current_slate_repair_job(repair_ingestion.target_game_ids, request=request)
        if repair_ingestion.status == "ready" and repair_ingestion.target_game_ids
        else {
            "status": "not_needed",
            "scope": "injury_update",
            "target_game_ids": repair_ingestion.target_game_ids,
        }
    )
    result["specials"] = (
        {
            "status": "queued" if queue_prepare_stocks_games(affected_game_ids) else "busy",
            "target_game_ids": affected_game_ids,
        }
        if roster_changed and affected_game_ids
        else {
            "status": "not_needed",
            "target_game_ids": affected_game_ids if roster_changed else [],
        }
    )
    result["predictions"] = 0
    return result


@app.post("/api/roster/import/espn", dependencies=[Depends(_protect_mutation)])
def import_espn_rosters(request: Request) -> dict[str, Any]:
    return _run_audited_mutation(request, "roster.espn.import", _import_espn_rosters_impl)


def _import_espn_rosters_impl() -> dict[str, Any]:
    with connect() as conn:
        result = sync_espn_rosters(conn)
        result["affected_game_ids"] = _scheduled_game_ids_for_teams(
            conn,
            result.get("changed_team_ids", []),
        )
    _invalidate_read_caches()
    return result


@app.post("/api/history/import/espn", dependencies=[Depends(_protect_mutation)])
def import_espn_history(
    request: Request,
    season: int | None = None,
    force_refresh: bool = False,
    include_player_stats: bool = True,
    include_previous_season: bool = False,
    missing_only: bool = False,
    auto_backfill_gaps: bool = False,
    selected_date: str | None = None,
    selected_dates: Annotated[list[str] | None, Query()] = None,
) -> dict:
    return _run_audited_mutation(
        request,
        "history.espn.import",
        lambda: _import_espn_history_impl(
            season=season,
            force_refresh=force_refresh,
            include_player_stats=include_player_stats,
            include_previous_season=include_previous_season,
            missing_only=missing_only,
            auto_backfill_gaps=auto_backfill_gaps,
            selected_date=selected_date,
            selected_dates=selected_dates,
        ),
        details={
            "season": season,
            "force_refresh": force_refresh,
            "include_player_stats": include_player_stats,
            "include_previous_season": include_previous_season,
            "missing_only": missing_only,
            "auto_backfill_gaps": auto_backfill_gaps,
            "selected_date": selected_date,
            "selected_dates": selected_dates or [],
        },
    )


def _import_espn_history_impl(
    *,
    season: int | None = None,
    force_refresh: bool = False,
    include_player_stats: bool = True,
    include_previous_season: bool = False,
    missing_only: bool = False,
    auto_backfill_gaps: bool = False,
    selected_date: str | None = None,
    selected_dates: list[str] | None = None,
) -> dict[str, Any]:
    target_season = season or datetime.now().year
    seasons = [target_season - 1, target_season] if include_previous_season else [target_season]
    unique_seasons = sorted(set(seasons))
    daily_dates = _selected_espn_dates(selected_date, selected_dates)
    if not daily_dates and not force_refresh and not include_previous_season:
        daily_dates = _default_espn_daily_dates()
    if not _ESPN_HISTORY_IMPORT_LOCK.acquire(blocking=False):
        raise HTTPException(
            status_code=409,
            detail="An ESPN history import is already running. Wait for it to finish before starting another import.",
        )

    try:
        with connect() as conn:
            scoreboards = []
            errors = []
            if daily_dates:
                for item in daily_dates:
                    try:
                        scoreboards.append(
                            import_espn_scoreboard(conn, _season_for_date(item), force_refresh=force_refresh, selected_date=item)
                        )
                    except Exception as exc:
                        errors.append(
                            {
                                "stage": "scoreboard",
                                "selected_date": item,
                                "season": _season_for_date(item),
                                "error": str(exc),
                            }
                        )
            else:
                for item in unique_seasons:
                    try:
                        scoreboards.append(import_espn_scoreboard(conn, item, force_refresh=force_refresh))
                    except Exception as exc:
                        errors.append(
                            {
                                "stage": "scoreboard",
                                "selected_date": None,
                                "season": item,
                                "error": str(exc),
                            }
                        )
            player_stats = []
            if include_player_stats:
                if daily_dates:
                    for item in daily_dates:
                        try:
                            player_stats.append(
                                import_espn_player_boxscores(
                                    conn,
                                    _season_for_date(item),
                                    force_refresh=force_refresh,
                                    missing_only=missing_only,
                                    selected_date=item,
                                )
                            )
                        except Exception as exc:
                            errors.append(
                                {
                                    "stage": "boxscore",
                                    "selected_date": item,
                                    "season": _season_for_date(item),
                                    "error": str(exc),
                                }
                            )
                else:
                    for item in unique_seasons:
                        try:
                            player_stats.append(
                                import_espn_player_boxscores(
                                    conn,
                                    item,
                                    force_refresh=force_refresh,
                                    missing_only=missing_only,
                                )
                            )
                        except Exception as exc:
                            errors.append(
                                {
                                    "stage": "boxscore",
                                    "selected_date": None,
                                    "season": item,
                                    "error": str(exc),
                                }
                            )

        with connect() as conn:
            ats_backfill = _recompute_team_results_from_game_lines(conn)
            settlements = settle_completed_props(conn, selected_dates=daily_dates)
            game_settlements = settle_completed_game_predictions(conn, selected_dates=daily_dates)
            special_settlements = settle_stocks(conn, selected_dates=daily_dates)
            gem_settlements = _sync_gem_snapshot_settlements(conn)
            watchlist_settlements = _sync_watchlist_snapshot_settlements(conn)
            _clear_scheduled_prop_state(conn, clear_source_rows=True)
            synced_props = 0
            projections = []
            watchlist_snapshot = _snapshot_watchlist(conn, datetime.now(LOCAL_TZ).date().isoformat())
            gap_audit = _espn_stats_gap_audit(conn)
            backfill_result: dict[str, Any] | None = None
            if auto_backfill_gaps and include_player_stats and gap_audit["missing_dates"]:
                backfill_result = _backfill_espn_stats_gaps(
                    conn,
                    gap_audit["missing_dates"],
                    force_refresh=force_refresh,
                )
                gap_audit = _espn_stats_gap_audit(conn)
    except Exception as exc:
        message = str(exc)
        if "turso" in message.lower() or "httpsconnectionpool" in message.lower() or "nameresolutionerror" in message.lower():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Unable to connect to Turso while refreshing ESPN history. "
                    "Check TURSO_DATABASE_URL/TURSO_AUTH_TOKEN and network/DNS access, "
                    "or disable Turso by setting USE_TURSO=false."
                ),
            ) from exc
        raise
    finally:
        _ESPN_HISTORY_IMPORT_LOCK.release()
    _clear_prop_scrape_caches()
    _invalidate_read_caches()
    with connect() as conn:
        published_payloads = _publish_post_mutation_read_payloads(conn)
    return {
        "season": target_season,
        "seasons": unique_seasons,
        "selected_date": daily_dates[0] if len(daily_dates) == 1 else None,
        "selected_dates": daily_dates,
        "scoreboards": scoreboards,
        "player_stats": player_stats,
        "ats_backfill": ats_backfill,
        "synced_props": synced_props,
        "settlements": settlements,
        "game_settlements": game_settlements,
        "special_settlements": special_settlements,
        "gem_settlements": gem_settlements,
        "watchlist_settlements": watchlist_settlements,
        "watchlist_snapshot": watchlist_snapshot,
        "predictions": len(projections),
        "missing_only": missing_only,
        "source": "espn",
        "errors": errors,
        "gap_audit": gap_audit,
        "gap_backfill": backfill_result,
        "published_payloads": published_payloads,
    }


@app.get("/api/history/missing/espn")
def missing_espn_history_dates(limit: int = 30) -> dict:
    with connect() as conn:
        payload = _missing_espn_scores_payload(conn, limit=max(1, min(limit, 180)))
    return payload


@app.post("/api/history/import/espn-missing", dependencies=[Depends(_protect_mutation)])
def import_missing_espn_history(
    request: Request,
    force_refresh: bool = True,
    include_player_stats: bool = True,
    missing_only: bool = True,
    limit: int = 30,
) -> dict:
    return _run_audited_mutation(
        request,
        "history.espn.import_missing",
        lambda: _import_missing_espn_history_impl(
            force_refresh=force_refresh,
            include_player_stats=include_player_stats,
            missing_only=missing_only,
            limit=limit,
        ),
        details={
            "force_refresh": force_refresh,
            "include_player_stats": include_player_stats,
            "missing_only": missing_only,
            "limit": limit,
        },
    )


def _import_missing_espn_history_impl(
    *,
    force_refresh: bool = True,
    include_player_stats: bool = True,
    missing_only: bool = True,
    limit: int = 30,
) -> dict[str, Any]:
    with connect() as conn:
        payload = _missing_espn_scores_payload(conn, limit=max(1, min(limit, 180)))
    selected_dates = payload.get("dates", [])
    if not selected_dates:
        return {
            "selected_dates": [],
            "missing_games": [],
            "missing_count": 0,
            "source": "espn",
            "message": "No incomplete completed ESPN games found.",
        }
    result = _import_espn_history_impl(
        force_refresh=force_refresh,
        include_player_stats=include_player_stats,
        include_previous_season=False,
        missing_only=missing_only,
        selected_dates=selected_dates,
    )
    result["missing_dates"] = selected_dates
    result["missing_count"] = len(payload.get("games", []))
    return result


@app.get("/api/history/audit/espn-gaps")
def audit_espn_history_gaps(
    start_date: str | None = None,
    end_date: str | None = None,
    limit_missing_games: int = 200,
) -> dict:
    with connect() as conn:
        return _espn_stats_gap_audit(
            conn,
            start_date=start_date,
            end_date=end_date,
            limit_missing_games=max(1, min(limit_missing_games, 1000)),
        )


@app.post("/api/history/backfill/espn-gaps", dependencies=[Depends(_protect_mutation)])
def backfill_espn_history_gaps(
    request: Request,
    start_date: str | None = None,
    end_date: str | None = None,
    force_refresh: bool = True,
) -> dict:
    return _run_audited_mutation(
        request,
        "history.espn.backfill_gaps",
        lambda: _backfill_espn_history_gaps_impl(start_date=start_date, end_date=end_date, force_refresh=force_refresh),
        details={"start_date": start_date, "end_date": end_date, "force_refresh": force_refresh},
    )


def _backfill_espn_history_gaps_impl(
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    force_refresh: bool = True,
) -> dict[str, Any]:
    with connect() as conn:
        before = _espn_stats_gap_audit(conn, start_date=start_date, end_date=end_date, limit_missing_games=1000)
        result = _backfill_espn_stats_gaps(
            conn,
            before["missing_dates"],
            force_refresh=force_refresh,
        )
        ats_backfill = _recompute_team_results_from_game_lines(conn)
        settlements = settle_completed_props(conn, selected_dates=before["missing_dates"])
        game_settlements = settle_completed_game_predictions(conn, selected_dates=before["missing_dates"])
        special_settlements = settle_stocks(conn, selected_dates=before["missing_dates"])
        gem_settlements = _sync_gem_snapshot_settlements(conn)
        watchlist_settlements = _sync_watchlist_snapshot_settlements(conn)
        _clear_scheduled_prop_state(conn, clear_source_rows=True)
        synced_props = 0
        projections = []
        watchlist_snapshot = _snapshot_watchlist(conn, datetime.now(LOCAL_TZ).date().isoformat())
        after = _espn_stats_gap_audit(conn, start_date=start_date, end_date=end_date, limit_missing_games=1000)
    _clear_prop_scrape_caches()
    _invalidate_read_caches()
    with connect() as conn:
        published_payloads = _publish_post_mutation_read_payloads(conn)
    return {
        "source": "espn",
        "backfill": result,
        "gap_audit_before": before,
        "gap_audit_after": after,
        "ats_backfill": ats_backfill,
        "settlements": settlements,
        "game_settlements": game_settlements,
        "special_settlements": special_settlements,
        "gem_settlements": gem_settlements,
        "watchlist_settlements": watchlist_settlements,
        "watchlist_snapshot": watchlist_snapshot,
        "synced_props": synced_props,
        "predictions": len(projections),
        "published_payloads": published_payloads,
    }


@app.post("/api/history/recompute-ats", dependencies=[Depends(_protect_mutation)])
def recompute_ats_from_game_lines(request: Request) -> dict:
    return _run_audited_mutation(request, "history.recompute_ats", _recompute_ats_from_game_lines_impl)


def _recompute_ats_from_game_lines_impl() -> dict[str, Any]:
    with connect() as conn:
        result = _recompute_team_results_from_game_lines(conn)
    _invalidate_read_caches()
    with connect() as conn:
        published_payloads = _publish_post_mutation_read_payloads(conn)
    return {"source": "game_lines", "published_payloads": published_payloads, **result}


@app.post("/api/history/backfill-covers-lines", dependencies=[Depends(_protect_mutation)])
def backfill_covers_lines(
    request: Request,
    start_date: str,
    end_date: str | None = None,
    force_refresh: bool = True,
    max_days: int = 45,
) -> dict:
    return _run_audited_mutation(
        request,
        "history.backfill_covers_lines",
        lambda: _backfill_covers_lines_impl(
            start_date=start_date,
            end_date=end_date,
            force_refresh=force_refresh,
            max_days=max_days,
        ),
        details={"start_date": start_date, "end_date": end_date, "force_refresh": force_refresh, "max_days": max_days},
    )


def _backfill_covers_lines_impl(
    *,
    start_date: str,
    end_date: str | None = None,
    force_refresh: bool = True,
    max_days: int = 45,
) -> dict[str, Any]:
    start = _parse_iso_date(start_date, "start_date")
    end = _parse_iso_date(end_date or start_date, "end_date")
    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
    total_days = (end - start).days + 1
    if total_days > max_days:
        raise HTTPException(status_code=400, detail=f"Date range too large: {total_days} days (max {max_days})")

    selected_dates = [(start + timedelta(days=offset)).isoformat() for offset in range(total_days)]
    imported_events = 0
    imported_rows = 0
    synced_props = 0
    errors = []
    with connect() as conn:
        for day in selected_dates:
            try:
                result = import_covers_props(conn, selected_date=day, force_refresh=force_refresh)
                imported_events += int(result.get("events", 0) or 0)
                imported_rows += int(result.get("imported", 0) or 0)
                synced_props += int(result.get("synced_props", 0) or 0)
                if result.get("status") == "failed":
                    errors.append({"date": day, "error": str(result.get("message") or "failed")})
            except Exception as exc:
                errors.append({"date": day, "error": str(exc)})
        ats = _recompute_team_results_from_game_lines(conn)
    _invalidate_read_caches()
    with connect() as conn:
        published_payloads = _publish_post_mutation_read_payloads(conn)
    return {
        "source": "covers",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "dates": selected_dates,
        "imported_events": imported_events,
        "imported_rows": imported_rows,
        "synced_props": synced_props,
        "ats_backfill": ats,
        "errors": errors,
        "published_payloads": published_payloads,
    }


@app.post("/api/history/backfill-odds-lines", dependencies=[Depends(_protect_mutation)])
def backfill_historical_odds_lines(
    request: Request,
    start_date: str,
    end_date: str | None = None,
    snapshot_time_utc: str = "16:00:00Z",
    force_refresh: bool = False,
    max_days: int = 14,
) -> dict:
    return _run_audited_mutation(
        request,
        "history.backfill_odds_lines",
        lambda: _backfill_historical_odds_lines_impl(
            start_date=start_date,
            end_date=end_date,
            snapshot_time_utc=snapshot_time_utc,
            force_refresh=force_refresh,
            max_days=max_days,
        ),
        details={
            "start_date": start_date,
            "end_date": end_date,
            "snapshot_time_utc": snapshot_time_utc,
            "force_refresh": force_refresh,
            "max_days": max_days,
        },
    )


def _backfill_historical_odds_lines_impl(
    *,
    start_date: str,
    end_date: str | None = None,
    snapshot_time_utc: str = "16:00:00Z",
    force_refresh: bool = False,
    max_days: int = 14,
) -> dict[str, Any]:
    start = _parse_iso_date(start_date, "start_date")
    end = _parse_iso_date(end_date or start_date, "end_date")
    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")
    total_days = (end - start).days + 1
    if total_days > max_days:
        raise HTTPException(status_code=400, detail=f"Date range too large: {total_days} days (max {max_days})")

    selected_dates = [(start + timedelta(days=offset)).isoformat() for offset in range(total_days)]
    fetched_events = 0
    matched_games = 0
    updated_games = 0
    skipped_events = 0
    errors = []
    with connect() as conn:
        for day in selected_dates:
            try:
                result = import_historical_odds_api_game_markets(
                    conn,
                    selected_date=day,
                    snapshot_time_utc=snapshot_time_utc,
                    force_refresh=force_refresh,
                )
                fetched_events += int(result.get("fetched_events", 0) or 0)
                matched_games += int(result.get("matched_games", 0) or 0)
                updated_games += int(result.get("updated_games", 0) or 0)
                skipped_events += int(result.get("skipped_events", 0) or 0)
                for item in result.get("errors", []) or []:
                    errors.append({"date": day, **item})
                if result.get("status") == "missing_api_key":
                    errors.append({"date": day, "error": str(result.get("message") or "missing_api_key")})
                    break
            except Exception as exc:
                errors.append({"date": day, "error": str(exc)})
        ats = _recompute_team_results_from_game_lines(conn)
    _invalidate_read_caches()
    with connect() as conn:
        published_payloads = _publish_post_mutation_read_payloads(conn)
    return {
        "source": "historical_odds_api",
        "start_date": start.isoformat(),
        "end_date": end.isoformat(),
        "dates": selected_dates,
        "snapshot_time_utc": snapshot_time_utc,
        "fetched_events": fetched_events,
        "matched_games": matched_games,
        "updated_games": updated_games,
        "skipped_events": skipped_events,
        "ats_backfill": ats,
        "errors": errors,
        "published_payloads": published_payloads,
    }


def _selected_espn_dates(selected_date: str | None, selected_dates: list[str] | None) -> list[str]:
    values = [selected_date] if selected_date else []
    values.extend(selected_dates or [])
    dates = []
    seen = set()
    for value in values:
        if not value:
            continue
        for item in str(value).split(","):
            date_text = item.strip()
            if not date_text:
                continue
            try:
                parsed = datetime.strptime(date_text, "%Y-%m-%d").date().isoformat()
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=f"Invalid selected date: {date_text}") from exc
            if parsed not in seen:
                dates.append(parsed)
                seen.add(parsed)
    return dates


def _scheduled_game_ids_for_teams(conn, team_ids: list[int] | tuple[int, ...]) -> list[int]:
    normalized = sorted({int(team_id) for team_id in team_ids if int(team_id) > 0})
    if not normalized:
        return []
    active_game_ids = set(_active_slate_game_ids(conn))
    if active_game_ids:
        placeholders = ",".join("?" for _ in normalized)
        rows = conn.execute(
            f"""
            SELECT id
            FROM games
            WHERE status = 'scheduled'
              AND id IN ({",".join("?" for _ in active_game_ids)})
              AND (home_team_id IN ({placeholders}) OR away_team_id IN ({placeholders}))
            ORDER BY game_date, start_time, id
            """,
            tuple(sorted(active_game_ids)) + tuple(normalized) + tuple(normalized),
        ).fetchall()
        filtered_active = [int(row["id"]) for row in rows]
        if filtered_active:
            return filtered_active

    today_iso = _local_today_iso()
    placeholders = ",".join("?" for _ in normalized)
    next_date_row = conn.execute(
        f"""
        SELECT MIN(game_date) AS next_game_date
        FROM games
        WHERE status = 'scheduled'
          AND game_date >= ?
          AND (home_team_id IN ({placeholders}) OR away_team_id IN ({placeholders}))
        """,
        (today_iso, *normalized, *normalized),
    ).fetchone()
    target_date = str(next_date_row["next_game_date"] or "").strip() if next_date_row is not None else ""
    if not target_date:
        fallback_row = conn.execute(
            f"""
            SELECT MIN(game_date) AS next_game_date
            FROM games
            WHERE status = 'scheduled'
              AND (home_team_id IN ({placeholders}) OR away_team_id IN ({placeholders}))
            """,
            tuple(normalized) + tuple(normalized),
        ).fetchone()
        target_date = str(fallback_row["next_game_date"] or "").strip() if fallback_row is not None else ""
    if not target_date:
        return []
    rows = conn.execute(
        f"""
        SELECT id
        FROM games
        WHERE status = 'scheduled'
          AND game_date = ?
          AND (home_team_id IN ({placeholders}) OR away_team_id IN ({placeholders}))
        ORDER BY game_date, start_time, id
        """,
        (target_date, *normalized, *normalized),
    ).fetchall()
    return [int(row["id"]) for row in rows]


def _default_espn_daily_dates(today_local=None) -> list[str]:
    today = today_local or datetime.now(LOCAL_TZ).date()
    previous = today - timedelta(days=1)
    return [previous.isoformat(), today.isoformat()]


def _parse_iso_date(value: str, field: str):
    try:
        return datetime.strptime(value, "%Y-%m-%d").date()
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=f"Invalid {field}: {value}") from exc


def _espn_stats_gap_audit(
    conn,
    *,
    start_date: str | None = None,
    end_date: str | None = None,
    limit_missing_games: int = 200,
) -> dict:
    today = datetime.now(LOCAL_TZ).date()
    if end_date:
        end = _parse_iso_date(end_date, "end_date")
    else:
        end = today
    if start_date:
        start = _parse_iso_date(start_date, "start_date")
    else:
        start = max(end - timedelta(days=45), datetime(end.year, 1, 1).date())
    if end < start:
        raise HTTPException(status_code=400, detail="end_date must be on or after start_date")

    coverage_rows = conn.execute(
        """
        SELECT
            g.game_date,
            COUNT(*) AS final_games,
            SUM(CASE WHEN EXISTS (SELECT 1 FROM player_game_stats s WHERE s.game_id = g.id) THEN 1 ELSE 0 END) AS games_with_stats,
            SUM(CASE WHEN EXISTS (SELECT 1 FROM player_game_stats s WHERE s.game_id = g.id) THEN 0 ELSE 1 END) AS games_missing_stats
        FROM games g
        WHERE g.status = 'final'
          AND g.game_date >= ?
          AND g.game_date <= ?
        GROUP BY g.game_date
        ORDER BY g.game_date DESC
        """,
        (start.isoformat(), end.isoformat()),
    ).fetchall()

    missing_games = conn.execute(
        """
        SELECT
            g.game_date,
            g.id AS game_id,
            ht.abbreviation AS home_team,
            at.abbreviation AS away_team
        FROM games g
        JOIN teams ht ON ht.id = g.home_team_id
        JOIN teams at ON at.id = g.away_team_id
        WHERE g.status = 'final'
          AND g.game_date >= ?
          AND g.game_date <= ?
          AND NOT EXISTS (SELECT 1 FROM player_game_stats s WHERE s.game_id = g.id)
        ORDER BY g.game_date DESC, g.id DESC
        LIMIT ?
        """,
        (start.isoformat(), end.isoformat(), int(limit_missing_games)),
    ).fetchall()

    latest_final = conn.execute(
        """
        SELECT MAX(game_date) AS latest_final_date
        FROM games
        WHERE status = 'final' AND game_date <= ?
        """,
        (today.isoformat(),),
    ).fetchone()
    latest_stats = conn.execute(
        """
        SELECT MAX(g.game_date) AS latest_stats_date
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        WHERE g.game_date <= ?
        """,
        (today.isoformat(),),
    ).fetchone()

    latest_final_date = latest_final["latest_final_date"] if latest_final else None
    latest_stats_date = latest_stats["latest_stats_date"] if latest_stats else None
    lag_days = None
    if latest_final_date and latest_stats_date:
        lag_days = (datetime.strptime(latest_final_date, "%Y-%m-%d").date() - datetime.strptime(latest_stats_date, "%Y-%m-%d").date()).days

    missing_dates = sorted({str(row["game_date"]) for row in missing_games})
    strict_ok = len(missing_dates) == 0 and (lag_days is None or lag_days <= 0)
    return {
        "window_start": start.isoformat(),
        "window_end": end.isoformat(),
        "latest_final_game_date": latest_final_date,
        "latest_stats_game_date": latest_stats_date,
        "stats_lag_days": lag_days,
        "missing_dates": missing_dates,
        "missing_dates_count": len(missing_dates),
        "missing_games_count": len(missing_games),
        "missing_games": [dict(row) for row in missing_games],
        "coverage_by_date": [dict(row) for row in coverage_rows],
        "strict_ok": strict_ok,
    }


def _backfill_espn_stats_gaps(conn, missing_dates: list[str], *, force_refresh: bool = True) -> dict:
    dates = sorted({date for date in missing_dates if date})
    scoreboards = []
    boxscores = []
    errors = []
    for date_text in dates:
        season = _season_for_date(date_text)
        try:
            scoreboards.append(
                import_espn_scoreboard(
                    conn,
                    season,
                    force_refresh=force_refresh,
                    selected_date=date_text,
                )
            )
        except Exception as exc:
            errors.append({"stage": "scoreboard", "selected_date": date_text, "season": season, "error": str(exc)})
            continue
        try:
            boxscores.append(
                import_espn_player_boxscores(
                    conn,
                    season,
                    force_refresh=force_refresh,
                    missing_only=True,
                    selected_date=date_text,
                )
            )
        except Exception as exc:
            errors.append({"stage": "boxscore", "selected_date": date_text, "season": season, "error": str(exc)})
    return {
        "dates_attempted": dates,
        "dates_attempted_count": len(dates),
        "scoreboards": scoreboards,
        "player_stats": boxscores,
        "errors": errors,
    }


def _recompute_team_results_from_game_lines(conn) -> dict:
    rows = conn.execute(
        """
        SELECT
            tgr.team_id,
            tgr.game_id,
            tgr.is_home,
            tgr.points,
            tgr.opponent_points,
            tgr.possessions,
            tgr.closing_spread,
            tgr.closing_total,
            tgr.ats_result,
            tgr.total_result,
            g.spread_home,
            g.game_total
        FROM team_game_results tgr
        JOIN games g ON g.id = tgr.game_id
        """
    ).fetchall()
    updated = 0
    skipped = 0
    for row in rows:
        if row["spread_home"] is None:
            skipped += 1
            continue
        game_total = row["game_total"] if _is_real_total(row["game_total"]) else (row["points"] + row["opponent_points"])
        computed_ats = _team_ats_result(row)
        computed_total = _game_total_result({**dict(row), "game_total": game_total})
        if computed_ats == "none" or computed_total == "none":
            skipped += 1
            continue
        is_home = int(row["is_home"]) == 1
        closing_spread = float(row["spread_home"]) if is_home else -float(row["spread_home"])
        if (
            float(row["closing_spread"]) == float(closing_spread)
            and float(row["closing_total"]) == float(game_total)
            and str(row["ats_result"]) == computed_ats
            and str(row["total_result"]) == computed_total
        ):
            continue
        conn.execute(
            """
            UPDATE team_game_results
            SET closing_spread = ?, closing_total = ?, ats_result = ?, total_result = ?
            WHERE team_id = ? AND game_id = ? AND is_home = ?
            """,
            (
                closing_spread,
                float(game_total),
                computed_ats,
                computed_total,
                int(row["team_id"]),
                int(row["game_id"]),
                int(row["is_home"]),
            ),
        )
        updated += 1
    conn.commit()
    return {"updated_rows": updated, "skipped_rows": skipped, "scanned_rows": len(rows)}


def _season_for_date(value: str) -> int:
    return datetime.strptime(value, "%Y-%m-%d").year


def _missing_espn_scores_payload(conn, limit: int = 30) -> dict:
    cutoff = datetime.now(timezone.utc) - timedelta(hours=COMPLETED_GAME_GRACE_HOURS)
    rows = conn.execute(
        """
        SELECT
            g.id,
            g.game_date,
            g.start_time,
            g.status,
            g.espn_event_id,
            home.abbreviation AS home_team,
            away.abbreviation AS away_team,
            (SELECT COUNT(*) FROM team_game_results r WHERE r.game_id = g.id) AS team_results_count,
            (SELECT COUNT(*) FROM player_game_stats s WHERE s.game_id = g.id) AS player_stats_count,
            (SELECT COUNT(*) FROM team_game_boxscores b WHERE b.game_id = g.id) AS team_boxscores_count
        FROM games g
        JOIN teams home ON home.id = g.home_team_id
        JOIN teams away ON away.id = g.away_team_id
        WHERE g.game_date IS NOT NULL
        ORDER BY g.game_date DESC, g.start_time DESC
        """,
    ).fetchall()
    missing_games = []
    for row in rows:
        if not _is_missing_completed_score(row, cutoff):
            continue
        team_results_count = int(row["team_results_count"] or 0)
        player_stats_count = int(row["player_stats_count"] or 0)
        team_boxscores_count = int(row["team_boxscores_count"] or 0)
        missing_reasons: list[str] = []
        if team_results_count < 2:
            missing_reasons.append("team_results")
        if player_stats_count <= 0:
            missing_reasons.append("player_stats")
        if team_boxscores_count < 2:
            missing_reasons.append("team_boxscores")
        missing_games.append(
            {
                "id": int(row["id"]),
                "game_date": row["game_date"],
                "start_time": row["start_time"],
                "status": row["status"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "espn_event_id": row["espn_event_id"],
                "has_team_results": team_results_count >= 2,
                "has_player_stats": player_stats_count > 0,
                "has_team_boxscores": team_boxscores_count >= 2,
                "team_results_count": team_results_count,
                "player_stats_count": player_stats_count,
                "team_boxscores_count": team_boxscores_count,
                "missing_reasons": missing_reasons,
            }
        )
    dates = []
    seen = set()
    for item in missing_games:
        value = str(item["game_date"])
        if value in seen:
            continue
        seen.add(value)
        dates.append(value)
        if len(dates) >= limit:
            break
    filtered_games = [item for item in missing_games if item["game_date"] in set(dates)]
    return {
        "dates": sorted(dates),
        "games": filtered_games,
        "count": len(filtered_games),
        "source": "espn",
    }


def _is_missing_completed_score(row, cutoff: datetime) -> bool:
    start = _parse_game_start(row["start_time"])
    if start is None:
        return False
    if start >= cutoff:
        return False
    status = str(row["status"] or "").lower()
    if status != "final":
        return False
    team_results_count = int(row["team_results_count"] or 0)
    player_stats_count = int(row["player_stats_count"] or 0)
    team_boxscores_count = int(row["team_boxscores_count"] or 0)
    return team_results_count < 2 or player_stats_count <= 0 or team_boxscores_count < 2


@app.get("/api/sportsbook-props")
def sportsbook_props(game_id: int | None = None) -> list[dict]:
    with connect() as conn:
        payload = list_sportsbook_props(conn, game_id)
    write_json_cache("sportsbook_props.json", payload)
    return payload


@app.get("/api/odds/cache")
def odds_cache() -> dict:
    return odds_cache_summary()


@app.get("/api/line-discrepancies", dependencies=[Depends(_protect_force_refresh)])
def discrepancies(response: Response, game_id: int | None = None, force_refresh: bool = False) -> list[dict]:
    if game_id is not None:
        started = datetime.now(timezone.utc)
        with connect() as conn:
            payload = _line_discrepancies_payload(conn, game_id)
        compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        _set_observability_headers(response, f"{LINE_DISCREPANCIES_CACHE_NAME}:game_id={game_id}", "BYPASS", round(compute_ms, 2))
        return payload
    if force_refresh:
        started = datetime.now(timezone.utc)
        with connect() as conn:
            payload = _line_discrepancies_payload(conn, None)
        compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        write_json_cache(LINE_DISCREPANCIES_CACHE_NAME, _cache_envelope(payload, LINE_DISCREPANCIES_TTL_SECONDS))
        _set_observability_headers(response, LINE_DISCREPANCIES_CACHE_NAME, "BYPASS", round(compute_ms, 2))
        return payload
    def compute() -> list[dict]:
        with connect() as conn:
            return _line_discrepancies_payload(conn, None)
    payload, status, compute_ms = _read_through_cache_with_meta(
        LINE_DISCREPANCIES_CACHE_NAME,
        LINE_DISCREPANCIES_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, LINE_DISCREPANCIES_CACHE_NAME, status, compute_ms)
    return payload


@app.get("/api/roster")
def roster(response: Response) -> list[dict]:
    def compute() -> list[dict]:
        with connect() as conn:
            return _roster_payload(conn, refresh_lineups=False)

    payload, status, compute_ms = _read_through_cache_with_meta(
        ROSTER_CACHE_NAME,
        ROSTER_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, ROSTER_CACHE_NAME, status, compute_ms)
    return payload


@app.get("/api/models/runs")
@app.get("/api/model-runs")
def model_runs(response: Response) -> dict:
    def compute() -> dict:
        with connect() as conn:
            return _model_runs_payload(conn)

    payload, status, compute_ms = _read_through_cache_with_meta(
        MODEL_RUNS_CACHE_NAME,
        MODEL_RUNS_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, MODEL_RUNS_CACHE_NAME, status, compute_ms)
    return payload


@app.get("/api/matchups", dependencies=[Depends(_protect_force_refresh)])
def matchups(response: Response, force_refresh: bool = False) -> list[dict]:
    cached_injury_refresh = _cached_rotowire_refresh_metadata()
    def compute() -> list[dict]:
        with connect() as conn:
            aggregates = _aggregate_matchup_snapshot_payloads(conn)
            if aggregates is None:
                _publish_matchup_snapshot_aggregates(conn, injury_refresh=cached_injury_refresh)
                return _read_cached_payload(MATCHUPS_CACHE_NAME) or []
            return aggregates[1]

    if force_refresh:
        started = datetime.now(timezone.utc)
        try:
            payload = compute()
            write_json_cache(MATCHUPS_CACHE_NAME, _cache_envelope(payload, MATCHUPS_TTL_SECONDS))
        except sqlite3.OperationalError as exc:
            if _is_sqlite_locked_error(exc):
                cached = _read_cached_payload(MATCHUPS_CACHE_NAME, allow_stale=True)
                if cached is not None:
                    compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
                    print(f"[cache] {MATCHUPS_CACHE_NAME} serving stale payload after sqlite lock: {exc}")
                    _set_observability_headers(response, MATCHUPS_CACHE_NAME, "STALE", round(compute_ms, 2))
                    return cached
            raise
        compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        _set_observability_headers(response, MATCHUPS_CACHE_NAME, "BYPASS", round(compute_ms, 2))
        return payload

    payload, status, compute_ms = _read_through_cache_with_meta(
        MATCHUPS_CACHE_NAME,
        MATCHUPS_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, MATCHUPS_CACHE_NAME, status, compute_ms)
    return payload


def _start_prop_sync_if_needed(source: str, request: Request | None = None) -> bool:
    with _PROP_SYNC_LOCK:
        if _PROP_SYNC_STATE["running"]:
            return False
    _begin_prop_sync_job(source)
    job_run_id = _create_job_run(
        "background_prop_sync",
        request=request,
        trigger_action=f"internal.{source}" if request is None else f"api.{source}",
        source_path=request.url.path if request is not None else None,
        metadata={"source": source},
    )
    _append_job_run_event(job_run_id, "job.queued", "Background prop sync queued.", details={"source": source})

    def _run() -> None:
        try:
            _append_job_run_event(job_run_id, "job.running", "Background prop sync started.", details={"source": source})
            with connect() as conn:
                pipeline_result = run_prop_sync_pipeline(
                    conn,
                    request_source=source,
                    sync_props=True,
                    rebuild_mode="changed_props",
                    skip_rebuild_message="No prop-line changes; skipped prediction rebuild.",
                    sync_props_fn=sync_prop_lines_from_sportsbook,
                    rebuild_predictions_fn=rebuild_predictions_live,
                    progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
                        stage=stage,
                        stage_index=1 if stage == "syncing_props" else 2,
                        stage_total=3,
                        current=current,
                        total=total,
                        message=message,
                    ),
                )
                touched_game_ids = list(pipeline_result.target_game_ids)
                _set_prop_sync_progress(
                    scope=source,
                    target_game_ids=touched_game_ids,
                )
            _invalidate_read_caches()
            post_result = run_post_pipeline_steps(
                connect_fn=connect,
                policy=PropPostProcessPolicy(
                    publish_mode="targeted",
                    matchup_game_ids=touched_game_ids,
                    full_matchup_refresh=False,
                    include_performance=False,
                    publish_start_message="Publishing synced payloads.",
                    publish_done_message="Background prop sync finished.",
                ),
                progress_callback=lambda stage, current, total, message: _set_prop_sync_progress(
                    stage=stage,
                    stage_index=3,
                    stage_total=3,
                    current=current,
                    total=total,
                    message=message,
                ),
                publish_post_mutation_payloads_fn=lambda conn, policy: _publish_post_mutation_read_payloads(
                    conn,
                    matchup_game_ids=list(policy.matchup_game_ids or []),
                    full_matchup_refresh=policy.full_matchup_refresh,
                    include_performance=policy.include_performance,
                ),
            )
            published_payloads = post_result.published_payloads or {}
            _mutate_prop_sync_state(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_result={
                    "source": source,
                    "synced_props": int(pipeline_result.scanned_props),
                    "changed_props": int(pipeline_result.synced_props),
                    "rebuilt_predictions": int(pipeline_result.rebuilt_predictions),
                    "attempted_predictions": int(pipeline_result.attempted_predictions),
                    "skipped_predictions": int(pipeline_result.skipped_predictions),
                    "target_game_ids": touched_game_ids,
                    "published_payloads": published_payloads,
                },
                last_error=None,
                status="completed",
                scope=source,
                target_game_ids=touched_game_ids,
                message="Background prop sync finished.",
            )
            _finish_job_run(
                job_run_id,
                status="completed",
                result={
                    "source": source,
                    "synced_props": int(pipeline_result.scanned_props),
                    "changed_props": int(pipeline_result.synced_props),
                    "rebuilt_predictions": int(pipeline_result.rebuilt_predictions),
                    "attempted_predictions": int(pipeline_result.attempted_predictions),
                    "skipped_predictions": int(pipeline_result.skipped_predictions),
                    "target_game_ids": touched_game_ids,
                    "published_payloads": published_payloads,
                },
            )
        except Exception as exc:
            _mutate_prop_sync_state(
                running=False,
                finished_at=datetime.now(timezone.utc).isoformat(),
                last_error=str(exc),
                status="failed",
                message=str(exc),
            )
            _append_job_run_event(job_run_id, "job.failed", f"Background prop sync failed: {exc}", level="error", details={"source": source})
            _finish_job_run(job_run_id, status="failed", error_text=str(exc))

    threading.Thread(target=_run, daemon=True).start()
    return True


def _covers_records_by_game(conn) -> dict[int, dict]:
    payload = read_json_cache("covers_props_raw.json")
    today_local = datetime.now(LOCAL_TZ).date().isoformat()
    if isinstance(payload, dict) and payload.get("cache_date") != today_local:
        delete_json_cache("covers_props_raw.json")
    records_by_game = {}
    if isinstance(payload, dict):
        for item in payload.get("games", []):
            if not isinstance(item, dict) or item.get("game_id") is None:
                continue
            records = item.get("records")
            if isinstance(records, dict):
                records_by_game[int(item["game_id"])] = records
    if records_by_game:
        return records_by_game

    pages_payload = read_json_cache("covers_pages_raw.json")
    if not isinstance(pages_payload, dict):
        return {}
    if pages_payload.get("cache_date") != today_local:
        delete_json_cache("covers_pages_raw.json")
        return {}
    for item in pages_payload.get("games", []):
        if not isinstance(item, dict):
            continue
        event_id = item.get("event_id")
        matchup_page = item.get("matchup_page")
        if event_id is None or not isinstance(matchup_page, str) or not matchup_page:
            continue
        try:
            metadata = _metadata_from_page(
                conn,
                CoversGame(
                    event_id=str(event_id),
                    odds_url=str(item.get("odds_url") or ""),
                    matchup_url=item.get("matchup_url"),
                ),
                matchup_page,
                fallback_page=item.get("odds_page") if isinstance(item.get("odds_page"), str) else None,
            )
        except Exception:
            continue
        if metadata.game_id is not None and isinstance(metadata.records, dict):
            records_by_game[int(metadata.game_id)] = metadata.records
    return records_by_game


def _covers_market_odds_by_game() -> dict[int, dict]:
    payload = read_json_cache("covers_props_raw.json")
    if not isinstance(payload, dict):
        return {}
    cache_date = payload.get("cache_date")
    today_local = datetime.now(LOCAL_TZ).date().isoformat()
    if cache_date != today_local:
        delete_json_cache("covers_props_raw.json")
        return {}
    pages_payload = read_json_cache("covers_pages_raw.json")
    pages_by_event_id = {}
    if isinstance(pages_payload, dict):
        for item in pages_payload.get("games", []):
            if isinstance(item, dict) and item.get("event_id") is not None:
                pages_by_event_id[str(item["event_id"])] = item
    odds_by_game = {}
    for item in payload.get("games", []):
        if not isinstance(item, dict) or item.get("game_id") is None:
            continue
        market = {
            "away_spread": item.get("away_spread"),
            "away_spread_price": item.get("away_spread_price"),
            "spread_home": item.get("spread_home"),
            "game_total": item.get("game_total"),
            "home_spread_price": item.get("home_spread_price"),
            "over_total": item.get("over_total"),
            "over_price": item.get("over_price"),
            "under_total": item.get("under_total"),
            "under_price": item.get("under_price"),
            "away_moneyline": item.get("away_moneyline"),
            "home_moneyline": item.get("home_moneyline"),
        }
        if any(value is None for value in market.values()):
            page_item = pages_by_event_id.get(str(item.get("provider_event_id")))
            if isinstance(page_item, dict):
                parsed_market = _game_market_from_page(page_item.get("odds_page", ""), str(item.get("home_team") or ""), str(item.get("away_team") or ""))
                market = {**market, **{key: parsed_market.get(key) for key in market}}
        odds_by_game[int(item["game_id"])] = {
            "spread_home": market.get("spread_home"),
            "game_total": market.get("game_total"),
            "home_moneyline": market.get("home_moneyline"),
            "away_moneyline": market.get("away_moneyline"),
            "spread_market": {
                "away_line": market.get("away_spread"),
                "away_price": market.get("away_spread_price"),
                "home_line": market.get("spread_home"),
                "home_price": market.get("home_spread_price"),
            },
            "total_market": {
                "over_line": market.get("over_total"),
                "over_price": market.get("over_price"),
                "under_line": market.get("under_total"),
                "under_price": market.get("under_price"),
            },
            "moneyline_market": {
                "away_price": market.get("away_moneyline"),
                "home_price": market.get("home_moneyline"),
            },
        }
    return odds_by_game


def _matchup_snapshot_key(game) -> str:
    normalized_start = _normalized_start_key(_game_row_value(game, "start_time"))
    return "|".join(
        [
            normalized_start or str(_game_row_value(game, "start_time") or ""),
            str(int(_game_row_value(game, "home_team_id"))),
            str(int(_game_row_value(game, "away_team_id"))),
        ]
    )


def _coalesce_matchup_games(games) -> list[tuple[dict, list[int]]]:
    groups: dict[tuple, dict] = {}
    for game in games:
        normalized_start = _normalized_start_key(game["start_time"])
        key = (
            normalized_start or str(game["start_time"]),
            int(game["home_team_id"]),
            int(game["away_team_id"]),
        )
        item = groups.setdefault(key, {"game": game, "game_ids": []})
        item["game_ids"].append(int(game["id"]))
        if _game_row_score(game) > _game_row_score(item["game"]):
            item["game"] = game
    return [
        (_merged_game_row(item["game"], [game for game in games if int(game["id"]) in item["game_ids"]]), sorted(set(item["game_ids"])))
        for item in groups.values()
    ]


def _merged_game_row(primary, group_games) -> dict:
    merged = dict(primary)
    for field, validator in (
        ("spread_home", _is_real_spread),
        ("game_total", _is_real_total),
        ("home_moneyline", _is_real_moneyline),
        ("away_moneyline", _is_real_moneyline),
        ("rest_days_home", _is_real_rest_days),
        ("rest_days_away", _is_real_rest_days),
    ):
        if validator(merged.get(field)):
            continue
        replacement = next((game[field] for game in group_games if validator(game[field])), None)
        if replacement is not None:
            merged[field] = replacement
    if not _is_real_total(merged.get("game_total")):
        merged["game_total"] = None
    return merged


def _game_row_value(game, field: str) -> Any:
    if isinstance(game, dict):
        return game.get(field)
    return game[field]


def _game_row_score(game) -> tuple[int, int, int, int, int, int]:
    return (
        1 if _is_real_spread(_game_row_value(game, "spread_home")) else 0,
        1 if _is_real_total(_game_row_value(game, "game_total")) else 0,
        1 if _is_real_moneyline(_game_row_value(game, "home_moneyline")) else 0,
        1 if _is_real_moneyline(_game_row_value(game, "away_moneyline")) else 0,
        1 if str(_game_row_value(game, "start_time")).endswith("Z") or "+" in str(_game_row_value(game, "start_time")) else 0,
        int(_game_row_value(game, "id")),
    )


def _is_real_spread(value) -> bool:
    return value is not None


def _is_real_total(value) -> bool:
    return value is not None and float(value) > 0


def _is_real_moneyline(value) -> bool:
    return value is not None


def _is_real_rest_days(value) -> bool:
    return value is not None


def _normalized_start_key(value: str | None) -> str | None:
    parsed = _parse_game_start(value)
    if parsed is None:
        return None
    return parsed.astimezone(timezone.utc).replace(second=0, microsecond=0).isoformat()


def _value_board_payload_for_games(conn, game_ids: list[int], include_filtered_only: bool = True) -> list[dict]:
    if not game_ids:
        return []
    payload = _value_board_payload(conn, game_ids=game_ids, include_filtered_only=include_filtered_only)
    best_by_leg = {}
    for item in payload:
        key = (
            item.get("player_id") or item["player"],
            item["market"],
            item["recommended_side"],
        )
        current = best_by_leg.get(key)
        if current is None or _value_prop_rank(item) > _value_prop_rank(current):
            best_by_leg[key] = item
    return sorted(best_by_leg.values(), key=_value_prop_rank, reverse=True)


def _value_prop_rank(item: dict) -> tuple[float, float, int, int]:
    recommended_price = item["over_odds"] if item["recommended_side"] == "over" else item["under_odds"]
    return (
        float(item["expected_value"]),
        float(item["edge"]),
        int(recommended_price),
        int(item["id"]),
    )


def _sportsbook_props_for_games(conn, game_ids: list[int]) -> list[dict]:
    payload = []
    for game_id in game_ids:
        payload.extend(list_sportsbook_props(conn, game_id))
    return sorted(payload, key=lambda item: (item["commence_time"], item["player_name"], item["market"], item["side"], item["sportsbook"]))


def _resolve_player_team_for_game(conn, *, player_id: int, game_id: int) -> int | None:
    row = conn.execute(
        """
        SELECT COALESCE(
            (
                SELECT h.team_id
                FROM player_team_history h
                LEFT JOIN games hg ON hg.id = h.game_id
                WHERE h.player_id = p.id
                  AND h.team_id IN (g.home_team_id, g.away_team_id)
                  AND h.game_id IS NOT NULL
                  AND hg.game_date IS NOT NULL
                  AND hg.game_date <= g.game_date
                ORDER BY hg.game_date DESC, h.id DESC
                LIMIT 1
            ),
            (
                SELECT CASE
                    WHEN SUM(CASE WHEN recent.home_team_id = g.home_team_id OR recent.away_team_id = g.home_team_id THEN 1 ELSE 0 END)
                       > SUM(CASE WHEN recent.home_team_id = g.away_team_id OR recent.away_team_id = g.away_team_id THEN 1 ELSE 0 END)
                    THEN g.home_team_id
                    WHEN SUM(CASE WHEN recent.home_team_id = g.home_team_id OR recent.away_team_id = g.home_team_id THEN 1 ELSE 0 END)
                       < SUM(CASE WHEN recent.home_team_id = g.away_team_id OR recent.away_team_id = g.away_team_id THEN 1 ELSE 0 END)
                    THEN g.away_team_id
                    ELSE NULL
                END
                FROM (
                    SELECT rg.home_team_id, rg.away_team_id
                    FROM player_game_stats s2
                    JOIN games rg ON rg.id = s2.game_id
                    WHERE s2.player_id = p.id
                      AND (rg.game_date < g.game_date OR (rg.game_date = g.game_date AND s2.game_id < g.id))
                    ORDER BY rg.game_date DESC, s2.game_id DESC
                    LIMIT 8
                ) recent
            ),
            (
                SELECT h.team_id
                FROM player_team_history h
                LEFT JOIN games hg ON hg.id = h.game_id
                WHERE h.player_id = p.id
                  AND h.team_id IN (g.home_team_id, g.away_team_id)
                  AND h.game_id IS NOT NULL
                  AND hg.game_date IS NOT NULL
                ORDER BY hg.game_date DESC, h.id DESC
                LIMIT 1
            ),
            CASE
                WHEN p.team_id IN (g.home_team_id, g.away_team_id) THEN p.team_id
                ELSE NULL
            END,
            g.home_team_id
        ) AS resolved_team_id
        FROM players p
        JOIN games g ON g.id = ?
        WHERE p.id = ?
        """,
        (int(game_id), int(player_id)),
    ).fetchone()
    if row is None or row["resolved_team_id"] is None:
        return None
    return int(row["resolved_team_id"])


def _resolve_discrepancy_player_context(
    conn,
    *,
    game_id: int,
    player_name: str,
) -> tuple[int | None, int | None]:
    player_rows = conn.execute(
        """
        SELECT DISTINCT spl.provider_player_id AS player_id
        FROM sportsbook_prop_lines spl
        WHERE spl.game_id = ?
          AND lower(spl.player_name) = lower(?)
          AND spl.provider_player_id IS NOT NULL
        """,
        (int(game_id), str(player_name)),
    ).fetchall()
    player_ids = [int(row["player_id"]) for row in player_rows if row["player_id"] is not None]
    player_id: int | None = None
    if len(player_ids) == 1:
        player_id = player_ids[0]
    else:
        candidate_rows = conn.execute(
            """
            SELECT DISTINCT p.id, p.full_name
            FROM players p
            JOIN games g ON g.id = ?
            WHERE p.team_id IN (g.home_team_id, g.away_team_id)
              AND EXISTS (
                  SELECT 1
                  FROM player_game_stats stats
                  WHERE stats.player_id = p.id
              )
            ORDER BY p.id
            """,
            (int(game_id),),
        ).fetchall()
        fuzzy_matches = [
            int(row["id"])
            for row in candidate_rows
            if _fuzzy_player_name_match(str(row["full_name"] or ""), str(player_name or ""))
        ]
        if len(fuzzy_matches) == 1:
            player_id = fuzzy_matches[0]
    if player_id is None:
        return None, None
    return player_id, _resolve_player_team_for_game(conn, player_id=player_id, game_id=game_id)


def _line_discrepancies_payload(conn, game_id: int | None) -> list[dict]:
    payload = line_discrepancies(conn, game_id)
    context_cache: dict[tuple[int, str], tuple[int | None, int | None]] = {}
    for item in payload:
        item["recent_values"] = []
        item["h2h_opponent"] = None
        item["h2h_values"] = []
        resolved_game_id = item.get("game_id")
        if resolved_game_id is None:
            continue
        cache_key = (int(resolved_game_id), str(item.get("player_name") or ""))
        player_id, resolved_team_id = context_cache.get(cache_key, (None, None))
        if cache_key not in context_cache:
            player_id, resolved_team_id = _resolve_discrepancy_player_context(
                conn,
                game_id=int(resolved_game_id),
                player_name=str(item.get("player_name") or ""),
            )
            context_cache[cache_key] = (player_id, resolved_team_id)
        if player_id is None:
            continue
        item["recent_values"] = _recent_market_values(
            conn,
            player_id=player_id,
            market=str(item["market"]),
            game_id=int(resolved_game_id),
            limit=5,
        )
        if resolved_team_id is None:
            continue
        item.update(
            _recent_h2h_market_history(
                conn,
                player_id=player_id,
                market=str(item["market"]),
                game_id=int(resolved_game_id),
                resolved_team_id=int(resolved_team_id),
                limit=5,
            )
        )
    return payload


def _line_discrepancies_for_games(conn, game_ids: list[int]) -> list[dict]:
    payload = []
    for game_id in game_ids:
        payload.extend(_line_discrepancies_payload(conn, game_id))
    return sorted(payload, key=lambda item: (item["line_gap"], item["price_gap"]), reverse=True)


def _best_side_sportsbooks_for_prop_lines(conn, prop_line_ids: list[int]) -> dict[int, dict[str, str | None]]:
    normalized_ids = sorted({int(prop_line_id) for prop_line_id in prop_line_ids if int(prop_line_id) > 0})
    if not normalized_ids:
        return {}
    placeholders = ",".join("?" for _ in normalized_ids)
    rows = conn.execute(
        """
        SELECT
            pl.id AS prop_line_id,
            pl.over_odds,
            pl.under_odds,
            spl.provider,
            spl.sportsbook,
            spl.side,
            spl.price
        FROM prop_lines pl
        JOIN players p ON p.id = pl.player_id
        JOIN sportsbook_prop_lines spl ON spl.game_id = pl.game_id
          AND spl.market = pl.market
          AND spl.line = pl.line
          AND (
              (spl.provider_player_id IS NOT NULL AND spl.provider_player_id = p.id)
              OR """
        + _player_name_match_clause("p.full_name", "spl.player_name")
        + """
          )
        WHERE pl.id IN ("""
        + placeholders
        + """)
          AND spl.side IN ('over', 'under')
        ORDER BY pl.id, spl.side, spl.price DESC, spl.sportsbook
        """,
        tuple(normalized_ids),
    ).fetchall()
    grouped: dict[int, list[dict[str, Any]]] = {}
    for row in rows:
        prop_line_id = int(row["prop_line_id"])
        grouped.setdefault(prop_line_id, []).append(dict(row))
    result: dict[int, dict[str, str | None]] = {}
    for prop_line_id, group_rows in grouped.items():
        has_covers = any(str(row["provider"]) == "covers" for row in group_rows)
        label_map: dict[str, str | None] = {"over": None, "under": None}
        for side, odds_key in (("over", "over_odds"), ("under", "under_odds")):
            target_price = next(
                (int(row[odds_key]) for row in group_rows if row.get(odds_key) is not None),
                None,
            )
            if target_price is None:
                continue
            candidates = [
                row
                for row in group_rows
                if str(row["side"]) == side
                and int(row["price"]) == target_price
                and ((not has_covers) or str(row["provider"]) == "covers")
            ]
            if not candidates:
                candidates = [
                    row
                    for row in group_rows
                    if str(row["side"]) == side and int(row["price"]) == target_price
                ]
            if candidates:
                chosen = sorted(
                    candidates,
                    key=lambda row: (
                        0 if str(row["provider"]) == "covers" else 1,
                        str(row["sportsbook"]),
                    ),
                )[0]
                label_map[side] = str(chosen["sportsbook"])
        result[prop_line_id] = {
            "best_over_sportsbook": label_map["over"],
            "best_under_sportsbook": label_map["under"],
        }
    return result


def _value_board_payload(
    conn,
    game_id: int | None = None,
    game_ids: list[int] | None = None,
    include_filtered_only: bool = True,
) -> list[dict]:
    if game_ids:
        placeholders = ",".join("?" for _ in game_ids)
        game_filter = f" AND pl.game_id IN ({placeholders})"
        params = tuple(game_ids)
    elif game_id is not None:
        game_filter = " AND pl.game_id = ?"
        params = (game_id,)
    else:
        game_filter = ""
        params = ()
    rows = conn.execute(
        f"""
        WITH raw_props AS (
            SELECT
                pp.id,
                pp.prop_line_id,
                pl.game_id,
                p.full_name AS player,
                p.id AS player_id,
                p.position,
                rt.abbreviation AS team,
                rt.logo_url AS team_logo_url,
                pl.sportsbook,
                pl.market,
                pl.line,
                pl.over_odds,
                pl.under_odds,
                pp.projection,
                pp.recommended_side,
                pp.model_probability,
                pp.implied_probability,
                pp.edge,
                pp.expected_value,
                pp.confidence,
                pp.reason,
                pp.prediction_time,
                g.start_time,
                g.game_date,
                p.rotation_role,
                g.spread_home,
                g.game_total,
                rt.id AS resolved_team_id
            FROM prop_predictions pp
            JOIN prop_lines pl ON pl.id = pp.prop_line_id
            JOIN players p ON p.id = pl.player_id
            JOIN games g ON g.id = pl.game_id
            LEFT JOIN settled_props sp ON sp.prop_line_id = pp.prop_line_id
            JOIN teams rt ON rt.id = (
                SELECT COALESCE(
                    (
                        SELECT h.team_id
                        FROM player_team_history h
                        LEFT JOIN games hg ON hg.id = h.game_id
                        WHERE h.player_id = p.id
                          AND h.team_id IN (g.home_team_id, g.away_team_id)
                          AND h.game_id IS NOT NULL
                          AND hg.game_date IS NOT NULL
                          AND hg.game_date <= g.game_date
                        ORDER BY hg.game_date DESC, h.id DESC
                        LIMIT 1
                    ),
                    (
                        SELECT CASE
                            WHEN SUM(CASE WHEN recent.home_team_id = g.home_team_id OR recent.away_team_id = g.home_team_id THEN 1 ELSE 0 END)
                               > SUM(CASE WHEN recent.home_team_id = g.away_team_id OR recent.away_team_id = g.away_team_id THEN 1 ELSE 0 END)
                            THEN g.home_team_id
                            WHEN SUM(CASE WHEN recent.home_team_id = g.home_team_id OR recent.away_team_id = g.home_team_id THEN 1 ELSE 0 END)
                               < SUM(CASE WHEN recent.home_team_id = g.away_team_id OR recent.away_team_id = g.away_team_id THEN 1 ELSE 0 END)
                            THEN g.away_team_id
                            ELSE NULL
                        END
                        FROM (
                            SELECT rg.home_team_id, rg.away_team_id
                            FROM player_game_stats s2
                            JOIN games rg ON rg.id = s2.game_id
                            WHERE s2.player_id = p.id
                              AND (rg.game_date < g.game_date OR (rg.game_date = g.game_date AND s2.game_id < g.id))
                            ORDER BY rg.game_date DESC, s2.game_id DESC
                            LIMIT 8
                        ) recent
                    ),
                    (
                        SELECT h.team_id
                        FROM player_team_history h
                        LEFT JOIN games hg ON hg.id = h.game_id
                        WHERE h.player_id = p.id
                          AND h.team_id IN (g.home_team_id, g.away_team_id)
                          AND h.game_id IS NOT NULL
                          AND hg.game_date IS NOT NULL
                        ORDER BY hg.game_date DESC, h.id DESC
                        LIMIT 1
                    ),
                    CASE
                        WHEN p.team_id IN (g.home_team_id, g.away_team_id) THEN p.team_id
                        ELSE NULL
                    END,
                    g.home_team_id
                )
            )
            WHERE sp.id IS NULL
            {game_filter}
        ),
        ranked_props AS (
            SELECT
                rp.*,
                CASE
                    WHEN rp.resolved_team_id = g.home_team_id THEN g.rest_days_home
                    ELSE g.rest_days_away
                END AS rest_days,
                CASE
                    WHEN rp.resolved_team_id = g.home_team_id THEN g.spread_home
                    ELSE -g.spread_home
                END AS team_spread,
                ROW_NUMBER() OVER (
                    PARTITION BY rp.game_id, rp.player_id, rp.market, rp.line
                    ORDER BY
                        CASE
                            WHEN rp.recommended_side = 'over' THEN rp.over_odds
                            ELSE rp.under_odds
                        END DESC,
                        rp.expected_value DESC,
                        rp.id DESC
                ) AS rn
            FROM raw_props rp
            JOIN games g ON g.id = rp.game_id
        )
        SELECT * FROM ranked_props WHERE rn = 1
        ORDER BY expected_value DESC, edge DESC
        """,
        params,
    ).fetchall()
    best_book_map = _best_side_sportsbooks_for_prop_lines(
        conn,
        [int(row["prop_line_id"]) for row in rows if row["prop_line_id"] is not None],
    )
    payload = []
    fallback_payload = []
    freshness_cache: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        is_active_time = _is_active_game_time(row["start_time"])
        item = dict(row)
        item["prop_line_id"] = int(item["prop_line_id"])
        best_books = best_book_map.get(item["prop_line_id"], {})
        item["best_over_sportsbook"] = best_books.get("best_over_sportsbook")
        item["best_under_sportsbook"] = best_books.get("best_under_sportsbook")
        item["display_sportsbook"] = (
            item["best_over_sportsbook"]
            if str(item.get("recommended_side")) == "over"
            else item["best_under_sportsbook"]
        ) or item.get("sportsbook")
        item = _repair_prediction_item_if_needed(conn, item)
        if _player_is_unavailable(conn, int(item["player_id"])):
            continue
        if include_filtered_only and not _include_value_board_pick(item):
            continue
        if ENABLE_PLAYER_FRESHNESS_GATE:
            freshness_key = (int(item["player_id"]), int(item["game_id"]))
            freshness = freshness_cache.get(freshness_key)
            if freshness is None:
                freshness = _player_data_freshness(
                    conn,
                    player_id=freshness_key[0],
                    game_id=freshness_key[1],
                    max_lag_days=PLAYER_DATA_MAX_LAG_DAYS,
                    min_recent_games=PLAYER_DATA_MIN_RECENT_GAMES,
                    recent_window_days=PLAYER_DATA_RECENT_WINDOW_DAYS,
                )
                freshness_cache[freshness_key] = freshness
            if not freshness["is_fresh"]:
                continue
        item["recent_values"] = _recent_market_values(
            conn,
            player_id=int(item["player_id"]),
            market=str(item["market"]),
            game_id=int(item["game_id"]),
            limit=5,
        )
        item["recent_minutes"] = _recent_minutes_played(
            conn,
            player_id=int(item["player_id"]),
            game_id=int(item["game_id"]),
            limit=5,
        )
        h2h_history = _recent_h2h_market_history(
            conn,
            player_id=int(item["player_id"]),
            market=str(item["market"]),
            game_id=int(item["game_id"]),
            resolved_team_id=int(item["resolved_team_id"]),
            limit=5,
        )
        item.update(h2h_history)
        item["increased_role"] = _has_increased_role(reason=item.get("reason"))
        item.update(_blowout_display(item["team_spread"], item["rotation_role"]))
        fallback_payload.append(item)
        if game_id is None and not is_active_time:
            continue
        payload.append(item)
    if payload:
        return payload
    return fallback_payload


def _recent_market_values(conn, *, player_id: int, market: str, game_id: int, limit: int = 5) -> list[float]:
    rows = conn.execute(
        """
        SELECT
            s.points,
            s.rebounds,
            s.assists,
            s.threes,
            s.steals,
            s.blocks
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN games target ON target.id = ?
        WHERE s.player_id = ?
          AND (g.game_date < target.game_date OR (g.game_date = target.game_date AND s.game_id < target.id))
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT ?
        """,
        (int(game_id), int(player_id), int(limit)),
    ).fetchall()
    values: list[float] = []
    for row in rows:
        value = _market_value_from_stats_row(row, market)
        if value is not None:
            values.append(round(float(value), 1))
    return values


def _recent_minutes_played(conn, *, player_id: int, game_id: int, limit: int = 5) -> list[float]:
    rows = conn.execute(
        """
        SELECT s.minutes
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN games target ON target.id = ?
        WHERE s.player_id = ?
          AND (g.game_date < target.game_date OR (g.game_date = target.game_date AND s.game_id < target.id))
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT ?
        """,
        (int(game_id), int(player_id), int(limit)),
    ).fetchall()
    return [round(float(row["minutes"] or 0.0), 1) for row in rows]


def _recent_h2h_market_history(
    conn,
    *,
    player_id: int,
    market: str,
    game_id: int,
    resolved_team_id: int,
    limit: int = 5,
) -> dict[str, Any]:
    """Return the player's prior market results against the target opponent."""
    opponent = conn.execute(
        """
        SELECT
            CASE WHEN g.home_team_id = ? THEN g.away_team_id ELSE g.home_team_id END AS opponent_id,
            CASE WHEN g.home_team_id = ? THEN away.abbreviation ELSE home.abbreviation END AS opponent
        FROM games g
        JOIN teams home ON home.id = g.home_team_id
        JOIN teams away ON away.id = g.away_team_id
        WHERE g.id = ?
          AND ? IN (g.home_team_id, g.away_team_id)
        """,
        (int(resolved_team_id), int(resolved_team_id), int(game_id), int(resolved_team_id)),
    ).fetchone()
    if opponent is None:
        return {"h2h_opponent": None, "h2h_values": [], "h2h_minutes": []}

    rows = conn.execute(
        """
        SELECT
            s.points,
            s.rebounds,
            s.assists,
            s.threes,
            s.steals,
            s.blocks,
            s.minutes
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        JOIN games target ON target.id = ?
        WHERE s.player_id = ?
          AND (g.game_date < target.game_date OR (g.game_date = target.game_date AND s.game_id < target.id))
          AND ? IN (g.home_team_id, g.away_team_id)
          AND COALESCE(
              (
                  SELECT h.team_id
                  FROM player_team_history h
                  WHERE h.player_id = s.player_id AND h.game_id = s.game_id
                  ORDER BY h.confidence DESC, h.id DESC
                  LIMIT 1
              ),
              ?
          ) != ?
        ORDER BY g.game_date DESC, s.game_id DESC
        LIMIT ?
        """,
        (
            int(game_id),
            int(player_id),
            int(opponent["opponent_id"]),
            int(resolved_team_id),
            int(opponent["opponent_id"]),
            int(limit),
        ),
    ).fetchall()
    values: list[float] = []
    minutes: list[float] = []
    for row in rows:
        value = _market_value_from_stats_row(row, market)
        if value is None:
            continue
        values.append(round(float(value), 1))
        minutes.append(round(float(row["minutes"] or 0.0), 1))
    return {
        "h2h_opponent": str(opponent["opponent"]),
        "h2h_values": values,
        "h2h_minutes": minutes,
    }


def _special_target_pair_params(pairs: list[tuple[int, int]]) -> tuple[str, tuple[int, ...]]:
    placeholders = ",".join("(?, ?)" for _ in pairs)
    params = tuple(value for pair in pairs for value in pair)
    return placeholders, params


def _active_special_prop_pairs(conn, pairs: list[tuple[int, int]]) -> set[tuple[int, int]]:
    if not pairs:
        return set()
    placeholders, params = _special_target_pair_params(pairs)
    rows = conn.execute(
        f"""
        WITH target_pairs(game_id, player_id) AS (
            VALUES {placeholders}
        )
        SELECT DISTINCT tp.game_id, tp.player_id
        FROM target_pairs tp
        JOIN prop_lines pl
          ON pl.game_id = tp.game_id
         AND pl.player_id = tp.player_id
        """,
        params,
    ).fetchall()
    return {(int(row["game_id"]), int(row["player_id"])) for row in rows}


def _special_game_rows_by_pair(conn, pairs: list[tuple[int, int]]) -> dict[tuple[int, int], sqlite3.Row]:
    if not pairs:
        return {}
    placeholders, params = _special_target_pair_params(pairs)
    rows = conn.execute(
        f"""
        WITH target_pairs(game_id, player_id) AS (
            VALUES {placeholders}
        )
        SELECT
            tp.game_id,
            tp.player_id,
            g.status,
            g.game_date,
            g.start_time,
            home.abbreviation AS home_team,
            away.abbreviation AS away_team,
            p.position,
            rt.abbreviation AS team,
            rt.logo_url AS team_logo_url
        FROM target_pairs tp
        JOIN games g ON g.id = tp.game_id
        JOIN players p ON p.id = tp.player_id
        JOIN teams home ON home.id = g.home_team_id
        JOIN teams away ON away.id = g.away_team_id
        LEFT JOIN teams rt ON rt.id = (
            SELECT COALESCE(
                (
                    SELECT h.team_id
                    FROM player_team_history h
                    WHERE h.player_id = p.id
                      AND h.game_id = g.id
                    ORDER BY h.id DESC
                    LIMIT 1
                ),
                (
                    SELECT h.team_id
                    FROM player_team_history h
                    WHERE h.player_id = p.id
                      AND h.team_id IN (g.home_team_id, g.away_team_id)
                    ORDER BY h.id DESC
                    LIMIT 1
                ),
                (
                    CASE
                        WHEN p.team_id IN (g.home_team_id, g.away_team_id) THEN p.team_id
                        ELSE NULL
                    END
                ),
                p.team_id
            )
        )
        """,
        params,
    ).fetchall()
    return {(int(row["game_id"]), int(row["player_id"])): row for row in rows}


def _special_recent_history_by_pair(
    conn,
    pairs: list[tuple[int, int]],
    *,
    limit: int,
) -> dict[tuple[int, int], dict[str, list[float]]]:
    if not pairs:
        return {}
    placeholders, pair_params = _special_target_pair_params(pairs)
    rows = conn.execute(
        f"""
        WITH target_pairs(game_id, player_id) AS (
            VALUES {placeholders}
        ),
        ranked_history AS (
            SELECT
                tp.game_id AS target_game_id,
                tp.player_id,
                s.points,
                s.rebounds,
                s.assists,
                s.threes,
                s.steals,
                s.blocks,
                s.minutes,
                ROW_NUMBER() OVER (
                    PARTITION BY tp.game_id, tp.player_id
                    ORDER BY g.game_date DESC, s.game_id DESC
                ) AS history_rank
            FROM target_pairs tp
            JOIN games target ON target.id = tp.game_id
            JOIN player_game_stats s ON s.player_id = tp.player_id
            JOIN games g ON g.id = s.game_id
            WHERE g.game_date < target.game_date
               OR (g.game_date = target.game_date AND s.game_id < target.id)
        )
        SELECT
            target_game_id AS game_id,
            player_id,
            points,
            rebounds,
            assists,
            threes,
            steals,
            blocks,
            minutes,
            history_rank
        FROM ranked_history
        WHERE history_rank <= ?
        ORDER BY game_id, player_id, history_rank
        """,
        pair_params + (int(limit),),
    ).fetchall()
    history: dict[tuple[int, int], dict[str, list[float]]] = {
        (game_id, player_id): {"recent_values": [], "recent_minutes": []}
        for game_id, player_id in pairs
    }
    for row in rows:
        key = (int(row["game_id"]), int(row["player_id"]))
        market_value = _market_value_from_stats_row(row, "blocks_steals")
        if market_value is not None:
            history[key]["recent_values"].append(round(float(market_value), 1))
        history[key]["recent_minutes"].append(round(float(row["minutes"] or 0.0), 1))
    return history


def _player_data_freshness(
    conn,
    *,
    player_id: int,
    game_id: int,
    max_lag_days: int,
    min_recent_games: int,
    recent_window_days: int,
) -> dict[str, Any]:
    target = conn.execute("SELECT game_date FROM games WHERE id = ?", (int(game_id),)).fetchone()
    if not target or not target["game_date"]:
        return {"is_fresh": False, "reason": "missing_target_game"}
    target_date = datetime.strptime(str(target["game_date"]), "%Y-%m-%d").date()
    latest_row = conn.execute(
        """
        SELECT MAX(g.game_date) AS latest_game_date
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        WHERE s.player_id = ?
          AND (g.game_date < ? OR (g.game_date = ? AND s.game_id < ?))
        """,
        (int(player_id), target_date.isoformat(), target_date.isoformat(), int(game_id)),
    ).fetchone()
    if not latest_row or not latest_row["latest_game_date"]:
        return {"is_fresh": False, "reason": "no_history"}
    latest_date = datetime.strptime(str(latest_row["latest_game_date"]), "%Y-%m-%d").date()
    lag_days = (target_date - latest_date).days
    recent_count_row = conn.execute(
        """
        SELECT COUNT(*) AS c
        FROM player_game_stats s
        JOIN games g ON g.id = s.game_id
        WHERE s.player_id = ?
          AND g.game_date >= ?
          AND (g.game_date < ? OR (g.game_date = ? AND s.game_id < ?))
        """,
        (
            int(player_id),
            (target_date - timedelta(days=max(1, int(recent_window_days)))).isoformat(),
            target_date.isoformat(),
            target_date.isoformat(),
            int(game_id),
        ),
    ).fetchone()
    recent_games = int((recent_count_row["c"] if recent_count_row else 0) or 0)
    is_fresh = lag_days <= max(0, int(max_lag_days)) and recent_games >= max(1, int(min_recent_games))
    return {
        "is_fresh": is_fresh,
        "lag_days": lag_days,
        "recent_games": recent_games,
        "latest_game_date": latest_date.isoformat(),
    }


def _market_value_from_stats_row(row, market: str) -> float | None:
    key = str(market or "").strip().lower()
    points = float(row["points"] or 0.0)
    rebounds = float(row["rebounds"] or 0.0)
    assists = float(row["assists"] or 0.0)
    threes = float(row["threes"] or 0.0)
    steals = float(row["steals"] or 0.0)
    blocks = float(row["blocks"] or 0.0)
    mapping = {
        "points": points,
        "rebounds": rebounds,
        "assists": assists,
        "threes": threes,
        "steals": steals,
        "blocks": blocks,
        "points_rebounds": points + rebounds,
        "points_assists": points + assists,
        "rebounds_assists": rebounds + assists,
        "points_rebounds_assists": points + rebounds + assists,
        "blocks_steals": blocks + steals,
    }
    return mapping.get(key)


def _has_increased_role(
    *,
    reason: str | None,
) -> bool:
    text = str(reason or "")
    injury_match = re.search(
        r"injury\s+([a-z_]+)\s+\(avail\s+([0-9]+(?:\.[0-9]+)?),\s+team usage\s+([0-9]+(?:\.[0-9]+)?),\s+min\s+([+-]?[0-9]+(?:\.[0-9]+)?)\)",
        text,
        flags=re.IGNORECASE,
    )
    if not injury_match:
        return False
    status = str(injury_match.group(1) or "").strip().lower()
    usage_multiplier = float(injury_match.group(3))
    minutes_delta = float(injury_match.group(4))
    if status in _UNAVAILABLE_PLAYER_STATUSES:
        return False
    return bool(
        usage_multiplier >= INCREASED_ROLE_USAGE_THRESHOLD
        or minutes_delta >= INCREASED_ROLE_MINUTES_THRESHOLD
    )


def _watchlist_payload(conn, min_ev: float = 0.02, min_edge: float = 0.05, limit: int = 60) -> list[dict]:
    rows = conn.execute(
        """
        WITH raw_props AS (
            SELECT
                pp.id,
                pp.prop_line_id,
                pl.game_id,
                p.full_name AS player,
                p.id AS player_id,
                p.position,
                rt.abbreviation AS team,
                rt.logo_url AS team_logo_url,
                away.abbreviation AS away_team,
                home.abbreviation AS home_team,
                pl.sportsbook,
                pl.market,
                pl.line,
                pl.over_odds,
                pl.under_odds,
                pp.projection,
                pp.recommended_side,
                pp.model_probability,
                pp.implied_probability,
                pp.edge,
                pp.expected_value,
                pp.confidence,
                pp.reason,
                pp.prediction_time,
                g.start_time,
                g.game_date,
                p.rotation_role,
                g.spread_home,
                g.game_total,
                rt.id AS resolved_team_id
            FROM prop_predictions pp
            JOIN prop_lines pl ON pl.id = pp.prop_line_id
            JOIN players p ON p.id = pl.player_id
            JOIN games g ON g.id = pl.game_id
            JOIN teams rt ON rt.id = (
                SELECT COALESCE(
                    (
                        SELECT h.team_id
                        FROM player_team_history h
                        LEFT JOIN games hg ON hg.id = h.game_id
                        WHERE h.player_id = p.id
                          AND h.team_id IN (g.home_team_id, g.away_team_id)
                          AND h.game_id IS NOT NULL
                          AND hg.game_date IS NOT NULL
                          AND hg.game_date <= g.game_date
                        ORDER BY hg.game_date DESC, h.id DESC
                        LIMIT 1
                    ),
                    (
                        SELECT CASE
                            WHEN SUM(CASE WHEN recent.home_team_id = g.home_team_id OR recent.away_team_id = g.home_team_id THEN 1 ELSE 0 END)
                               > SUM(CASE WHEN recent.home_team_id = g.away_team_id OR recent.away_team_id = g.away_team_id THEN 1 ELSE 0 END)
                            THEN g.home_team_id
                            WHEN SUM(CASE WHEN recent.home_team_id = g.home_team_id OR recent.away_team_id = g.home_team_id THEN 1 ELSE 0 END)
                               < SUM(CASE WHEN recent.home_team_id = g.away_team_id OR recent.away_team_id = g.away_team_id THEN 1 ELSE 0 END)
                            THEN g.away_team_id
                            ELSE NULL
                        END
                        FROM (
                            SELECT rg.home_team_id, rg.away_team_id
                            FROM player_game_stats s2
                            JOIN games rg ON rg.id = s2.game_id
                            WHERE s2.player_id = p.id
                              AND (rg.game_date < g.game_date OR (rg.game_date = g.game_date AND s2.game_id < g.id))
                            ORDER BY rg.game_date DESC, s2.game_id DESC
                            LIMIT 8
                        ) recent
                    ),
                    (
                        SELECT h.team_id
                        FROM player_team_history h
                        LEFT JOIN games hg ON hg.id = h.game_id
                        WHERE h.player_id = p.id
                          AND h.team_id IN (g.home_team_id, g.away_team_id)
                          AND h.game_id IS NOT NULL
                          AND hg.game_date IS NOT NULL
                        ORDER BY hg.game_date DESC, h.id DESC
                        LIMIT 1
                    ),
                    CASE
                        WHEN p.team_id IN (g.home_team_id, g.away_team_id) THEN p.team_id
                        ELSE NULL
                    END,
                    g.home_team_id
                )
            )
            JOIN teams away ON away.id = g.away_team_id
            JOIN teams home ON home.id = g.home_team_id
            LEFT JOIN settled_props sp ON sp.prop_line_id = pl.id
            WHERE g.status = 'scheduled'
              AND sp.id IS NULL
        ),
        ranked_props AS (
            SELECT
                rp.*,
                CASE
                    WHEN rp.resolved_team_id = g.home_team_id THEN g.rest_days_home
                    ELSE g.rest_days_away
                END AS rest_days,
                CASE
                    WHEN rp.resolved_team_id = g.home_team_id THEN g.spread_home
                    ELSE -g.spread_home
                END AS team_spread,
                ROW_NUMBER() OVER (
                    PARTITION BY rp.game_id, rp.player_id, rp.market, rp.line
                    ORDER BY
                        CASE
                            WHEN rp.recommended_side = 'over' THEN rp.over_odds
                            ELSE rp.under_odds
                        END DESC,
                        rp.expected_value DESC,
                        rp.id DESC
                ) AS rn
            FROM raw_props rp
            JOIN games g ON g.id = rp.game_id
        )
        SELECT *
        FROM ranked_props
        WHERE rn = 1
          AND lower(confidence) = 'low'
          AND expected_value >= ?
          AND ABS(edge) >= ?
        ORDER BY expected_value DESC, edge DESC
        """,
        (float(min_ev), float(min_edge)),
    ).fetchall()
    best_book_map = _best_side_sportsbooks_for_prop_lines(
        conn,
        [int(row["prop_line_id"]) for row in rows if row["prop_line_id"] is not None],
    )
    value_board_prediction_ids = {int(item["id"]) for item in _value_board_payload(conn)}
    gem_prop_line_ids = {int(item["prop_line_id"]) for item in _build_current_gems(conn, "balanced")}
    payload = []
    freshness_cache: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
        item["prop_line_id"] = int(item["prop_line_id"])
        best_books = best_book_map.get(item["prop_line_id"], {})
        item["best_over_sportsbook"] = best_books.get("best_over_sportsbook")
        item["best_under_sportsbook"] = best_books.get("best_under_sportsbook")
        item["display_sportsbook"] = (
            item["best_over_sportsbook"]
            if str(item.get("recommended_side")) == "over"
            else item["best_under_sportsbook"]
        ) or item.get("sportsbook")
        item = _repair_prediction_item_if_needed(conn, item)
        if _player_is_unavailable(conn, int(item["player_id"])):
            continue
        if not _include_watchlist_pick(item):
            continue
        if int(item["id"]) in value_board_prediction_ids:
            continue
        if int(item["prop_line_id"]) in gem_prop_line_ids:
            continue
        if not _is_active_game_time(item["start_time"]):
            continue
        if ENABLE_PLAYER_FRESHNESS_GATE:
            freshness_key = (int(item["player_id"]), int(item["game_id"]))
            freshness = freshness_cache.get(freshness_key)
            if freshness is None:
                freshness = _player_data_freshness(
                    conn,
                    player_id=freshness_key[0],
                    game_id=freshness_key[1],
                    max_lag_days=PLAYER_DATA_MAX_LAG_DAYS,
                    min_recent_games=PLAYER_DATA_MIN_RECENT_GAMES,
                    recent_window_days=PLAYER_DATA_RECENT_WINDOW_DAYS,
                )
                freshness_cache[freshness_key] = freshness
            if not freshness["is_fresh"]:
                continue
        item["recent_values"] = _recent_market_values(
            conn,
            player_id=int(item["player_id"]),
            market=str(item["market"]),
            game_id=int(item["game_id"]),
            limit=5,
        )
        item["recent_minutes"] = _recent_minutes_played(
            conn,
            player_id=int(item["player_id"]),
            game_id=int(item["game_id"]),
            limit=5,
        )
        item.update(
            _recent_h2h_market_history(
                conn,
                player_id=int(item["player_id"]),
                market=str(item["market"]),
                game_id=int(item["game_id"]),
                resolved_team_id=int(item["resolved_team_id"]),
                limit=5,
            )
        )
        item["increased_role"] = _has_increased_role(reason=item.get("reason"))
        item.update(_blowout_display(item["team_spread"], item["rotation_role"]))
        payload.append(item)
    return payload[: max(1, min(int(limit), 200))]


def _include_watchlist_pick(item: dict) -> bool:
    confidence = str(item.get("confidence") or "").strip().lower()
    market = str(item.get("market") or "").strip().lower()
    side = str(item.get("recommended_side") or "").strip().lower()
    try:
        edge = abs(float(item.get("edge") or 0.0))
        ev = float(item.get("expected_value") or 0.0)
    except (TypeError, ValueError):
        return False

    if confidence != "low":
        return False
    if ev < 0.02 or edge < 0.05 or edge >= LOW_CONFIDENCE_EDGE_MIN:
        return False
    if _requires_stricter_over_edge(market, side) and edge < 0.08:
        return False

    # Settled watchlist history supports a tighter rebound floor and
    # excluding low-confidence points+assists from this fallback cohort.
    if market == "rebounds":
        return edge >= 0.06
    if market == "points_assists":
        return False
    return True


def _include_value_board_pick(item: dict) -> bool:
    confidence = str(item.get("confidence") or "").strip().lower()
    market = str(item.get("market") or "").strip().lower()
    side = str(item.get("recommended_side") or "").strip().lower()
    try:
        edge = abs(float(item.get("edge") or 0.0))
        ev = float(item.get("expected_value") or 0.0)
    except (TypeError, ValueError):
        return False

    # Settled history shows weak overs in scoring-heavy markets underperforming;
    # require a stronger edge before surfacing them on the board.
    if _requires_stricter_over_edge(market, side) and edge < 0.08:
        return False

    # Medium confidence has underperformed in rebounds; require stricter admission.
    if confidence == "medium":
        if market == "rebounds":
            return edge >= 0.14 and edge < LOW_CONFIDENCE_EDGE_MAX
        return edge >= 0.12 and edge < LOW_CONFIDENCE_EDGE_MAX
    if confidence == "high":
        return edge >= 0.10 and edge < max(LOW_CONFIDENCE_EDGE_MAX, 0.25)
    if confidence != "low":
        return False

    # Market-aware low-confidence admission gates, tuned from settled calibration.
    if market == "points":
        return edge >= 0.05 and edge < 0.12
    if market == "rebounds":
        return edge >= 0.08 and edge < LOW_CONFIDENCE_EDGE_MAX
    if market == "threes":
        return (edge >= 0.05 and edge < 0.08) or (edge >= 0.12 and edge < LOW_CONFIDENCE_EDGE_MAX)
    if market in {"assists", "points_assists", "points_rebounds", "rebounds_assists", "points_rebounds_assists"}:
        return ev >= 0.07 and edge >= 0.04 and edge < LOW_CONFIDENCE_EDGE_MAX

    return edge >= LOW_CONFIDENCE_EDGE_MIN and edge < LOW_CONFIDENCE_EDGE_MAX


def _requires_stricter_over_edge(market: str, side: str) -> bool:
    if side != "over":
        return False
    return market in {"points", "threes", "points_rebounds", "points_assists"}


def _blowout_display(team_spread, role: str | None) -> dict:
    if team_spread is None:
        return {
            "blowout_risk": "unknown",
            "blowout_probability": 0.0,
            "blowout_minutes_impact": 0.0,
        }
    spread_abs = abs(float(team_spread))
    probability = _blowout_probability(spread_abs)
    role_delta = {
        "star": -4.0,
        "starter": -3.0,
        "rotation": -1.5,
        "bench": 2.0,
    }.get(role or "starter", -2.0)
    return {
        "blowout_risk": _blowout_label(probability),
        "blowout_probability": round(probability, 2),
        "blowout_minutes_impact": round(probability * role_delta, 1),
    }


def _blowout_probability(spread_abs: float) -> float:
    if spread_abs < 4.5:
        return 0.08
    if spread_abs < 8.5:
        return 0.18
    if spread_abs < 12.5:
        return 0.34
    if spread_abs < 16.5:
        return 0.48
    return 0.60


def _blowout_label(probability: float) -> str:
    if probability >= 0.45:
        return "very high"
    if probability >= 0.30:
        return "high"
    if probability >= 0.15:
        return "medium"
    return "low"


def _team_last_10_summary(conn, team_id: int) -> dict:
    rows = conn.execute(
        """
        SELECT
            r.*,
            g.game_date,
            g.start_time,
            g.spread_home,
            g.game_total,
            opponent.abbreviation AS opponent,
            CASE
                WHEN g.home_team_id = r.team_id THEN segments.home_q1_points
                ELSE segments.away_q1_points
            END AS q1_points,
            CASE
                WHEN g.home_team_id = r.team_id THEN segments.away_q1_points
                ELSE segments.home_q1_points
            END AS q1_allowed,
            CASE
                WHEN g.home_team_id = r.team_id THEN segments.home_1h_points
                ELSE segments.away_1h_points
            END AS first_half_points,
            CASE
                WHEN g.home_team_id = r.team_id THEN segments.away_1h_points
                ELSE segments.home_1h_points
            END AS first_half_allowed
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        JOIN teams opponent ON opponent.id = CASE
            WHEN g.home_team_id = r.team_id THEN g.away_team_id
            ELSE g.home_team_id
        END
        LEFT JOIN game_segment_results segments ON segments.game_id = g.id
        WHERE r.team_id = ?
        ORDER BY g.game_date DESC, g.start_time DESC
        LIMIT 10
        """,
        (team_id,),
    ).fetchall()

    recent_games = []
    count = len(rows)
    wins = 0
    ats_wins = 0
    ats_losses = 0
    ats_pushes = 0
    overs = 0
    unders = 0
    total_pushes = 0
    home_games = 0
    total_pts = 0
    total_opp_pts = 0
    segment_buckets = {
        "overall": {"games": 0, "q1_for": 0.0, "q1_against": 0.0, "first_half_for": 0.0, "first_half_against": 0.0},
        "home": {"games": 0, "q1_for": 0.0, "q1_against": 0.0, "first_half_for": 0.0, "first_half_against": 0.0},
        "away": {"games": 0, "q1_for": 0.0, "q1_against": 0.0, "first_half_for": 0.0, "first_half_against": 0.0},
    }

    for row in rows:
        points = row["points"]
        opponent_points = row["opponent_points"]

        if points > opponent_points:
            wins += 1
        if row["is_home"]:
            home_games += 1

        ats_result = _team_ats_result(row)
        total_result = _game_total_result(row)
        item = dict(row)
        item["ats_result"] = ats_result
        item["total_result"] = total_result
        recent_games.append(item)

        if ats_result == "cover":
            ats_wins += 1
        elif ats_result == "no_cover":
            ats_losses += 1
        elif ats_result == "push":
            ats_pushes += 1

        if total_result == "over":
            overs += 1
        elif total_result == "under":
            unders += 1
        elif total_result == "push":
            total_pushes += 1

        total_pts += points
        total_opp_pts += opponent_points

        if (
            row["q1_points"] is not None
            and row["q1_allowed"] is not None
            and row["first_half_points"] is not None
            and row["first_half_allowed"] is not None
        ):
            bucket_key = "home" if row["is_home"] else "away"
            for key in ("overall", bucket_key):
                segment_buckets[key]["games"] += 1
                segment_buckets[key]["q1_for"] += float(row["q1_points"])
                segment_buckets[key]["q1_against"] += float(row["q1_allowed"])
                segment_buckets[key]["first_half_for"] += float(row["first_half_points"])
                segment_buckets[key]["first_half_against"] += float(row["first_half_allowed"])

    def _segment_average_payload(bucket: dict[str, float]) -> dict[str, float | int | None]:
        games = int(bucket["games"])
        if games <= 0:
            return {
                "games": 0,
                "avg_q1_points_for": None,
                "avg_q1_points_against": None,
                "avg_first_half_points_for": None,
                "avg_first_half_points_against": None,
                "avg_q1_total": None,
                "avg_first_half_total": None,
            }
        avg_q1_for = round(bucket["q1_for"] / games, 1)
        avg_q1_against = round(bucket["q1_against"] / games, 1)
        avg_first_half_for = round(bucket["first_half_for"] / games, 1)
        avg_first_half_against = round(bucket["first_half_against"] / games, 1)
        return {
            "games": games,
            "avg_q1_points_for": avg_q1_for,
            "avg_q1_points_against": avg_q1_against,
            "avg_first_half_points_for": avg_first_half_for,
            "avg_first_half_points_against": avg_first_half_against,
            "avg_q1_total": round(avg_q1_for + avg_q1_against, 1),
            "avg_first_half_total": round(avg_first_half_for + avg_first_half_against, 1),
        }

    return {
        "games": count,
        "wins": wins,
        "losses": count - wins,
        "home_games": home_games,
        "away_games": count - home_games,
        "ats_wins": ats_wins,
        "ats_losses": ats_losses,
        "ats_pushes": ats_pushes,
        "overs": overs,
        "unders": unders,
        "total_pushes": total_pushes,
        "avg_points_for": round(total_pts / count, 1) if count > 0 else 0,
        "avg_points_against": round(total_opp_pts / count, 1) if count > 0 else 0,
        "segment_averages": {
            "overall": _segment_average_payload(segment_buckets["overall"]),
            "home": _segment_average_payload(segment_buckets["home"]),
            "away": _segment_average_payload(segment_buckets["away"]),
        },
        "recent_games": recent_games,
    }


def _h2h_segment_summary(
    conn,
    *,
    away_team_id: int,
    home_team_id: int,
    scheduled_start_time: str,
    scheduled_game_id: int,
) -> dict[str, float | int | None] | None:
    rows = conn.execute(
        """
        SELECT
            g.id,
            g.start_time,
            g.home_team_id,
            g.away_team_id,
            segments.home_q1_points,
            segments.away_q1_points,
            segments.home_1h_points,
            segments.away_1h_points
        FROM games g
        JOIN game_segment_results segments ON segments.game_id = g.id
        WHERE (
            (g.home_team_id = ? AND g.away_team_id = ?)
            OR
            (g.home_team_id = ? AND g.away_team_id = ?)
        )
          AND g.id != ?
          AND COALESCE(g.start_time, '') < COALESCE(?, '')
          AND segments.home_q1_points IS NOT NULL
          AND segments.away_q1_points IS NOT NULL
          AND segments.home_1h_points IS NOT NULL
          AND segments.away_1h_points IS NOT NULL
        ORDER BY g.start_time DESC
        LIMIT 10
        """,
        (home_team_id, away_team_id, away_team_id, home_team_id, scheduled_game_id, scheduled_start_time),
    ).fetchall()
    if not rows:
        return None

    meeting_count = len(rows)
    away_q1_points = 0.0
    home_q1_points = 0.0
    away_first_half_points = 0.0
    home_first_half_points = 0.0
    q1_total = 0.0
    first_half_total = 0.0

    for row in rows:
        if int(row["away_team_id"]) == away_team_id:
            away_game_q1 = float(row["away_q1_points"])
            home_game_q1 = float(row["home_q1_points"])
            away_game_first_half = float(row["away_1h_points"])
            home_game_first_half = float(row["home_1h_points"])
        else:
            away_game_q1 = float(row["home_q1_points"])
            home_game_q1 = float(row["away_q1_points"])
            away_game_first_half = float(row["home_1h_points"])
            home_game_first_half = float(row["away_1h_points"])

        away_q1_points += away_game_q1
        home_q1_points += home_game_q1
        away_first_half_points += away_game_first_half
        home_first_half_points += home_game_first_half
        q1_total += away_game_q1 + home_game_q1
        first_half_total += away_game_first_half + home_game_first_half

    return {
        "meetings": meeting_count,
        "away_avg_q1_points": round(away_q1_points / meeting_count, 1),
        "home_avg_q1_points": round(home_q1_points / meeting_count, 1),
        "avg_q1_total": round(q1_total / meeting_count, 1),
        "away_avg_first_half_points": round(away_first_half_points / meeting_count, 1),
        "home_avg_first_half_points": round(home_first_half_points / meeting_count, 1),
        "avg_first_half_total": round(first_half_total / meeting_count, 1),
    }


def _team_ratings_by_team(conn, team_ids: set[int]) -> dict[int, dict[str, Any]]:
    normalized_ids = sorted({int(team_id) for team_id in team_ids if int(team_id) > 0})
    if not normalized_ids:
        return {}
    if not hasattr(conn, "execute"):
        return {}

    latest_row = conn.execute(
        """
        SELECT MAX(substr(g.game_date, 1, 4)) AS season_year
        FROM games g
        JOIN team_game_results r ON r.game_id = g.id
        """
    ).fetchone()
    season_year = str(latest_row["season_year"] or "").strip() if latest_row is not None else ""
    if not season_year:
        return {}

    rows = conn.execute(
        """
        SELECT
            r.team_id,
            r.is_home,
            r.points,
            r.opponent_points,
            r.possessions,
            g.game_date,
            g.start_time
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        WHERE substr(g.game_date, 1, 4) = ?
        ORDER BY g.game_date DESC, g.start_time DESC, r.game_id DESC, r.id DESC
        """,
        (season_year,),
    ).fetchall()

    by_team: dict[int, list[Any]] = {}
    for row in rows:
        team_id = int(row["team_id"])
        by_team.setdefault(team_id, []).append(row)

    def _window_payload(window_rows: list[Any]) -> dict[str, Any] | None:
        games = len(window_rows)
        if games <= 0:
            return None
        possessions = sum(float(row["possessions"] or 0.0) for row in window_rows)
        if possessions <= 0.0:
            return None
        points = sum(float(row["points"] or 0.0) for row in window_rows)
        opponent_points = sum(float(row["opponent_points"] or 0.0) for row in window_rows)
        return {
            "games": games,
            "possessions": round(possessions, 1),
            "off_rating": round((100.0 * points) / possessions, 1),
            "def_rating": round((100.0 * opponent_points) / possessions, 1),
            "net_rating": round((100.0 * (points - opponent_points)) / possessions, 1),
            "pace": round(possessions / games, 1),
        }

    payload_by_team: dict[int, dict[str, Any]] = {}
    for team_id, team_rows in by_team.items():
        season = _window_payload(team_rows)
        last_10 = _window_payload(team_rows[:10])
        home = _window_payload([row for row in team_rows if int(row["is_home"] or 0) == 1][:10])
        away = _window_payload([row for row in team_rows if int(row["is_home"] or 0) != 1][:10])
        payload_by_team[team_id] = {
            "season": season,
            "last_10": last_10,
            "home": home,
            "away": away,
        }

    ranked_season = [
        (team_id, payload["season"])
        for team_id, payload in payload_by_team.items()
        if isinstance(payload.get("season"), dict)
    ]
    off_rank = {
        team_id: idx + 1
        for idx, (team_id, _payload) in enumerate(
            sorted(ranked_season, key=lambda item: float(item[1]["off_rating"]), reverse=True)
        )
    }
    def_rank = {
        team_id: idx + 1
        for idx, (team_id, _payload) in enumerate(
            sorted(ranked_season, key=lambda item: float(item[1]["def_rating"]))
        )
    }
    net_rank = {
        team_id: idx + 1
        for idx, (team_id, _payload) in enumerate(
            sorted(ranked_season, key=lambda item: float(item[1]["net_rating"]), reverse=True)
        )
    }
    pace_rank = {
        team_id: idx + 1
        for idx, (team_id, _payload) in enumerate(
            sorted(ranked_season, key=lambda item: float(item[1]["pace"]), reverse=True)
        )
    }

    result: dict[int, dict[str, Any]] = {}
    for team_id in normalized_ids:
        payload = payload_by_team.get(team_id)
        if payload is None:
            continue
        result[team_id] = {
            **payload,
            "off_rank": off_rank.get(team_id),
            "def_rank": def_rank.get(team_id),
            "net_rank": net_rank.get(team_id),
            "pace_rank": pace_rank.get(team_id),
        }
    return result


def _matchup_rating_differentials(
    home_ratings: dict[str, Any] | None,
    away_ratings: dict[str, Any] | None,
) -> dict[str, float | None]:
    home_season = home_ratings.get("season") if isinstance(home_ratings, dict) else None
    away_season = away_ratings.get("season") if isinstance(away_ratings, dict) else None
    home_last_10 = home_ratings.get("last_10") if isinstance(home_ratings, dict) else None
    away_last_10 = away_ratings.get("last_10") if isinstance(away_ratings, dict) else None

    def _diff(left: Any, right: Any, key: str) -> float | None:
        if not isinstance(left, dict) or not isinstance(right, dict):
            return None
        left_value = left.get(key)
        right_value = right.get(key)
        if not isinstance(left_value, (int, float)) or not isinstance(right_value, (int, float)):
            return None
        return round(float(left_value) - float(right_value), 1)

    return {
        "season_net_diff": _diff(home_season, away_season, "net_rating"),
        "season_off_diff": _diff(home_season, away_season, "off_rating"),
        "season_def_diff": _diff(home_season, away_season, "def_rating"),
        "last_10_net_diff": _diff(home_last_10, away_last_10, "net_rating"),
        "pace_diff": _diff(home_season, away_season, "pace"),
    }


def _team_ats_result(row) -> str:
    spread_home = row["spread_home"]
    if spread_home is None:
        return "unknown"
    team_spread = float(spread_home) if row["is_home"] else -float(spread_home)
    margin = float(row["points"]) - float(row["opponent_points"]) + team_spread
    if margin == 0:
        return "push"
    return "cover" if margin > 0 else "no_cover"


def _game_total_result(row) -> str:
    game_total = row["game_total"]
    if game_total is None or float(game_total) <= 0:
        return "unknown"
    total_score = float(row["points"]) + float(row["opponent_points"])
    if total_score == float(game_total):
        return "push"
    return "over" if total_score > float(game_total) else "under"


def _rest_days_before_game(conn, team_id: int, start_time: str, game_date: str | None = None) -> int | None:
    current_date = _parse_game_date(game_date) if game_date else None
    if current_date is None:
        current_start = _parse_game_start(start_time)
        if current_start is None:
            return None
        current_date = current_start.astimezone(LOCAL_TZ).date()
    else:
        current_start = _parse_game_start(start_time)
    rows = conn.execute(
        """
        SELECT g.start_time, g.game_date
        FROM games g
        WHERE (g.home_team_id = ? OR g.away_team_id = ?)
          AND lower(g.status) NOT IN ('canceled', 'cancelled')
        ORDER BY g.start_time DESC
        """,
        (team_id, team_id),
    ).fetchall()
    previous_dates = []
    for row in rows:
        previous_start = _parse_game_start(row["start_time"])
        if current_start is not None and (previous_start is None or previous_start >= current_start):
            continue
        previous_date = _parse_game_date(row["game_date"])
        if previous_date is None and previous_start is not None:
            previous_date = previous_start.astimezone(LOCAL_TZ).date()
        if previous_date and previous_date < current_date:
            previous_dates.append(previous_date)
    if not previous_dates:
        return None
    rest_days = max((current_date - max(previous_dates)).days, 0)
    if rest_days > 14:
        return None
    return rest_days


def _parse_game_date(value: str):
    try:
        return datetime.fromisoformat(str(value)[:10]).date()
    except ValueError:
        return None


def _parse_game_start(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=LOCAL_TZ)
    return parsed.astimezone(timezone.utc)


def _is_active_game_time(value: str | None) -> bool:
    if not value:
        return False
    try:
        start_time = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=timezone.utc)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=COMPLETED_GAME_GRACE_HOURS)
    return start_time.astimezone(timezone.utc) >= cutoff


def _is_today_active_game_time(value: str | None) -> bool:
    if not value:
        return False
    try:
        start_time = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return False
    if start_time.tzinfo is None:
        start_time = start_time.replace(tzinfo=LOCAL_TZ)
    local_start = start_time.astimezone(LOCAL_TZ)
    today = datetime.now(LOCAL_TZ).date()
    if local_start.date() != today:
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(hours=COMPLETED_GAME_GRACE_HOURS)
    return start_time.astimezone(timezone.utc) >= cutoff
