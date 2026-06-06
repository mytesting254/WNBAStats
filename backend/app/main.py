from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import Counter
import os
import sqlite3
import threading
import time
from typing import Any, Callable
from typing import Annotated

from fastapi import Depends, FastAPI, Header, HTTPException, Query, Request, Response
from fastapi.middleware.cors import CORSMiddleware

from .ball_dont_lie import fetch_team_history
from .accuracy_analysis import build_accuracy_report, build_accuracy_report_for_days
from .bootstrap import ensure_teams
from .cache import delete_json_cache, read_json_cache, write_json_cache
from .covers_import import CoversGame, RAW_CACHE_NAME as COVERS_RAW_CACHE_NAME, _game_market_from_page, _metadata_from_page, import_covers_props
from .db import connect, init_db
from .espn_history import import_espn_player_boxscores, import_espn_scoreboard
from .game_prediction_tracking import save_game_prediction, settle_completed_game_predictions
from .game_predictions import project_game
from .odds_import import RAW_CACHE_NAME as ODDS_RAW_CACHE_NAME, import_the_odds_api_props, line_discrepancies, list_sportsbook_props, odds_cache_summary, sync_prop_lines_from_sportsbook
from .projections import rebuild_predictions
from .rotowire_import import RAW_CACHE_NAME as ROTOWIRE_RAW_CACHE_NAME, import_rotowire_lineups
from .settlement import settle_completed_props
from .training import latest_model_run, list_model_runs, run_parameter_tuning, run_walk_forward_training


app = FastAPI(title="WNBA Prop Value API")
COMPLETED_GAME_GRACE_HOURS = 4
LOCAL_TZ = timezone(timedelta(hours=-4))
LOW_CONFIDENCE_EDGE_MIN = float(os.getenv("LOW_CONFIDENCE_EDGE_MIN", "0.08"))
LOW_CONFIDENCE_EDGE_MAX = float(os.getenv("LOW_CONFIDENCE_EDGE_MAX", "0.18"))
PLAYER_DATA_MAX_LAG_DAYS = int(os.getenv("PLAYER_DATA_MAX_LAG_DAYS", "5"))
PLAYER_DATA_MIN_RECENT_GAMES = int(os.getenv("PLAYER_DATA_MIN_RECENT_GAMES", "3"))
PLAYER_DATA_RECENT_WINDOW_DAYS = int(os.getenv("PLAYER_DATA_RECENT_WINDOW_DAYS", "30"))
ENABLE_PLAYER_FRESHNESS_GATE = os.getenv("ENABLE_PLAYER_FRESHNESS_GATE", "0").strip().lower() in {"1", "true", "yes"}
VALUE_BOARD_CACHE_NAME = "current_value_board.json"
MATCHUPS_CACHE_NAME = "current_matchups.json"
LINE_DISCREPANCIES_CACHE_NAME = "line_discrepancies.json"
MODEL_PERFORMANCE_CACHE_NAME = "model_performance.json"
MODEL_RUNS_CACHE_NAME = "model_runs.json"
ROSTER_CACHE_NAME = "roster.json"
GEM_MIN_EV = float(os.getenv("GEM_MIN_EV", "0.02"))
GEM_MIN_EDGE = float(os.getenv("GEM_MIN_EDGE", "0.05"))
READ_CACHE_VERSION = 1
VALUE_BOARD_TTL_SECONDS = int(os.getenv("VALUE_BOARD_TTL_SECONDS", "300"))
LINE_DISCREPANCIES_TTL_SECONDS = int(os.getenv("LINE_DISCREPANCIES_TTL_SECONDS", "300"))
MATCHUPS_TTL_SECONDS = int(os.getenv("MATCHUPS_TTL_SECONDS", "300"))
MODEL_PERFORMANCE_TTL_SECONDS = int(os.getenv("MODEL_PERFORMANCE_TTL_SECONDS", "300"))
MODEL_RUNS_TTL_SECONDS = int(os.getenv("MODEL_RUNS_TTL_SECONDS", "300"))
ROSTER_TTL_SECONDS = int(os.getenv("ROSTER_TTL_SECONDS", "300"))
RATE_LIMIT_MUTATION_CAPACITY = float(os.getenv("RATE_LIMIT_MUTATION_CAPACITY", "10"))
RATE_LIMIT_MUTATION_REFILL_PER_SEC = float(os.getenv("RATE_LIMIT_MUTATION_REFILL_PER_SEC", "0.5"))
RATE_LIMIT_REFRESH_CAPACITY = float(os.getenv("RATE_LIMIT_REFRESH_CAPACITY", "6"))
RATE_LIMIT_REFRESH_REFILL_PER_SEC = float(os.getenv("RATE_LIMIT_REFRESH_REFILL_PER_SEC", "0.33"))
_RATE_BUCKETS: dict[tuple[str, str], tuple[float, float]] = {}
_RATE_LOCK = threading.Lock()
_PROP_SYNC_LOCK = threading.Lock()
_PROP_SYNC_STATE: dict[str, Any] = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "last_error": None,
    "last_result": None,
}

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
    if not _is_dev_env() and not _configured_api_key():
        raise RuntimeError("API_KEY is required when ENV is not dev/local/test.")
    if _is_dev_env() and not _configured_api_key():
        print("[security] API_KEY not set; mutating endpoints are open in dev/test mode.")
    init_db()
    with connect() as conn:
        ensure_teams(conn)
    _invalidate_read_caches()


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/ops/health")
def ops_health() -> dict[str, Any]:
    with _PROP_SYNC_LOCK:
        sync_state = dict(_PROP_SYNC_STATE)
    return {
        "status": "ok",
        "prop_sync": sync_state,
    }


def _is_dev_env() -> bool:
    return os.getenv("ENV", "dev").strip().lower() in {"dev", "local", "test"}


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


def _protect_mutation(
    request: Request,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    _consume_rate_limit(_client_key(request), "mutation", RATE_LIMIT_MUTATION_CAPACITY, RATE_LIMIT_MUTATION_REFILL_PER_SEC)
    _enforce_api_key(x_api_key, authorization)


def _protect_force_refresh(
    request: Request,
    force_refresh: bool = False,
    x_api_key: Annotated[str | None, Header(alias="X-API-Key")] = None,
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    if not force_refresh:
        return
    _consume_rate_limit(_client_key(request), "refresh", RATE_LIMIT_REFRESH_CAPACITY, RATE_LIMIT_REFRESH_REFILL_PER_SEC)
    _enforce_api_key(x_api_key, authorization)


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
        "ttl_seconds": ttl_seconds,
        "source": "api_read_cache",
        "payload": payload,
    }


def _read_cached_payload(cache_name: str) -> Any | None:
    cached = read_json_cache(cache_name)
    if not isinstance(cached, dict):
        return None
    if cached.get("cache_key_version") != READ_CACHE_VERSION:
        return None
    cached_at_raw = cached.get("cached_at")
    ttl_seconds = cached.get("ttl_seconds")
    if not isinstance(cached_at_raw, str) or not isinstance(ttl_seconds, int):
        return None
    try:
        cached_at = datetime.fromisoformat(cached_at_raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if cached_at.tzinfo is None:
        cached_at = cached_at.replace(tzinfo=timezone.utc)
    if datetime.now(timezone.utc) - cached_at > timedelta(seconds=ttl_seconds):
        return None
    return cached.get("payload")


def _read_through_cache(cache_name: str, ttl_seconds: int, compute: Callable[[], Any]) -> Any:
    cached_payload = _read_cached_payload(cache_name)
    if cached_payload is not None:
        return cached_payload
    payload = compute()
    write_json_cache(cache_name, _cache_envelope(payload, ttl_seconds))
    return payload


def _read_through_cache_with_meta(cache_name: str, ttl_seconds: int, compute: Callable[[], Any]) -> tuple[Any, str, float]:
    cached_payload = _read_cached_payload(cache_name)
    if cached_payload is not None:
        return cached_payload, "HIT", 0.0
    started = datetime.now(timezone.utc)
    payload = compute()
    compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
    write_json_cache(cache_name, _cache_envelope(payload, ttl_seconds))
    return payload, "MISS", round(compute_ms, 2)


def _invalidate_read_caches() -> None:
    for name in (
        VALUE_BOARD_CACHE_NAME,
        LINE_DISCREPANCIES_CACHE_NAME,
        MATCHUPS_CACHE_NAME,
        MODEL_PERFORMANCE_CACHE_NAME,
        MODEL_RUNS_CACHE_NAME,
        ROSTER_CACHE_NAME,
    ):
        delete_json_cache(name)


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
    if os.getenv("EXPOSE_DEBUG_HEADERS", "").strip().lower() not in {"1", "true", "yes"}:
        return
    response.headers["X-Cache"] = cache_status
    response.headers["X-Cache-Key"] = f"{cache_name}:v{READ_CACHE_VERSION}"
    response.headers["X-Compute-Ms"] = f"{compute_ms:.2f}"
    print(f"[cache] {cache_name} status={cache_status} compute_ms={compute_ms:.2f}")


@app.post("/api/recalculate", dependencies=[Depends(_protect_mutation)])
def recalculate() -> dict[str, int]:
    try:
        with connect() as conn:
            projections = rebuild_predictions(conn)
            settlements = settle_completed_props(conn)
            game_settlements = settle_completed_game_predictions(conn)
            _snapshot_watchlist(conn, datetime.now(LOCAL_TZ).date().isoformat())
    except sqlite3.OperationalError as exc:
        if "database is locked" in str(exc).lower():
            return {"predictions": 0, "settled": 0, "game_settled": 0, "status": "db_locked", "message": "Recalculate skipped because the database is busy. Try again in a few seconds."}
        raise
    _invalidate_read_caches()
    return {"predictions": len(projections), "settled": settlements["settled"], "game_settled": game_settlements["settled"], "status": "ok"}


@app.get("/api/props/sync-status")
def props_sync_status() -> dict:
    with _PROP_SYNC_LOCK:
        return dict(_PROP_SYNC_STATE)


@app.post("/api/settle-props", dependencies=[Depends(_protect_mutation)])
def settle_props() -> dict:
    with connect() as conn:
        props = settle_completed_props(conn)
        games = settle_completed_game_predictions(conn)
        gems = _sync_gem_snapshot_settlements(conn)
        watchlist = _sync_watchlist_snapshot_settlements(conn)
    _invalidate_read_caches()
    return {"props": props, "games": games, "gems": gems, "watchlist": watchlist}


@app.get("/api/value-board", dependencies=[Depends(_protect_force_refresh)])
def value_board(response: Response, force_refresh: bool = False) -> list[dict]:
    if force_refresh:
        started = datetime.now(timezone.utc)
        with connect() as conn:
            payload = _value_board_payload(conn)
        compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        write_json_cache(VALUE_BOARD_CACHE_NAME, _cache_envelope(payload, VALUE_BOARD_TTL_SECONDS))
        _set_observability_headers(response, VALUE_BOARD_CACHE_NAME, "BYPASS", round(compute_ms, 2))
        return payload
    def compute() -> list[dict]:
        with connect() as conn:
            return _value_board_payload(conn)
    payload, status, compute_ms = _read_through_cache_with_meta(
        VALUE_BOARD_CACHE_NAME,
        VALUE_BOARD_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, VALUE_BOARD_CACHE_NAME, status, compute_ms)
    return payload


@app.get("/api/watchlist")
def watchlist() -> list[dict]:
    with connect() as conn:
        return _watchlist_payload(conn)


@app.get("/api/model-performance")
def model_performance(response=None) -> dict:
    def compute() -> dict:
        with connect() as conn:
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
def gem_performance() -> dict:
    with connect() as conn:
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
        # Settled historical baseline (current metric)
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


@app.get("/api/watchlist-performance")
def watchlist_performance() -> dict:
    with connect() as conn:
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
                "prop_line_id": int(prop["id"]),
                "game_id": int(prop["game_id"]),
                "player_id": int(prop["player_id"]),
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
def model_diagnostics(model_version: str = "adaptive-context-v1", windows: str = "7,14,30") -> dict:
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


@app.get("/api/model-loss-breakdown")
def model_loss_breakdown(model_version: str = "adaptive-context-v1", top_n_players: int = 15) -> dict:
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
def train_model() -> dict:
    with connect() as conn:
        return run_walk_forward_training(conn)


@app.post("/api/models/tune", dependencies=[Depends(_protect_mutation)])
def tune_model() -> dict:
    with connect() as conn:
        return run_parameter_tuning(conn)


@app.post("/api/odds/import", dependencies=[Depends(_protect_mutation)])
def import_odds(force_refresh: bool = False) -> dict:
    with connect() as conn:
        result = import_the_odds_api_props(conn, force_refresh=force_refresh)
    if result.get("sync_error"):
        _start_prop_sync_if_needed("odds_import")
    _invalidate_read_caches()
    return result


@app.post("/api/covers/import", dependencies=[Depends(_protect_mutation)])
def import_covers(selected_date: str | None = None, force_refresh: bool = False) -> dict:
    with connect() as conn:
        result = import_covers_props(conn, selected_date=selected_date, force_refresh=force_refresh, sync_props=False)
    sync_started = _start_prop_sync_if_needed("covers_import")
    result["sync_started"] = sync_started
    if sync_started:
        base_message = str(result.get("message") or "").strip()
        if base_message:
            result["message"] = f"{base_message} Prop sync queued in background."
        else:
            result["message"] = "Covers import completed. Prop sync queued in background."
    _invalidate_read_caches()
    return result


@app.post("/api/injuries/import/rotowire", dependencies=[Depends(_protect_mutation)])
def import_rotowire_injuries(force_refresh: bool = False) -> dict:
    with connect() as conn:
        result = import_rotowire_lineups(conn, force_refresh=force_refresh)
        try:
            projections = rebuild_predictions(conn)
        except sqlite3.OperationalError as exc:
            if "database is locked" in str(exc).lower():
                result["status"] = "db_locked"
                result["message"] = "Roster refresh completed, but projection rebuild was skipped because the database is busy. Try again in a few seconds."
                result["predictions"] = 0
                _invalidate_read_caches()
                return result
            raise
    result["predictions"] = len(projections)
    _invalidate_read_caches()
    return result


@app.post("/api/history/import/espn", dependencies=[Depends(_protect_mutation)])
def import_espn_history(
    season: int | None = None,
    force_refresh: bool = False,
    include_player_stats: bool = True,
    include_previous_season: bool = False,
    missing_only: bool = False,
    auto_backfill_gaps: bool = False,
    selected_date: str | None = None,
    selected_dates: Annotated[list[str] | None, Query()] = None,
) -> dict:
    target_season = season or datetime.now().year
    seasons = [target_season - 1, target_season] if include_previous_season else [target_season]
    unique_seasons = sorted(set(seasons))
    daily_dates = _selected_espn_dates(selected_date, selected_dates)
    if not daily_dates and not force_refresh and not include_previous_season:
        daily_dates = _default_espn_daily_dates()

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
            ats_backfill = _recompute_team_results_from_game_lines(conn)
            settlements = settle_completed_props(conn)
            game_settlements = settle_completed_game_predictions(conn)
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
    _clear_prop_scrape_caches()
    _invalidate_read_caches()
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
        "gem_settlements": gem_settlements,
        "watchlist_settlements": watchlist_settlements,
        "watchlist_snapshot": watchlist_snapshot,
        "predictions": len(projections),
        "missing_only": missing_only,
        "source": "espn",
        "errors": errors,
        "gap_audit": gap_audit,
        "gap_backfill": backfill_result,
    }


@app.get("/api/history/missing/espn")
def missing_espn_history_dates(limit: int = 30) -> dict:
    with connect() as conn:
        payload = _missing_espn_scores_payload(conn, limit=max(1, min(limit, 180)))
    return payload


@app.post("/api/history/import/espn-missing", dependencies=[Depends(_protect_mutation)])
def import_missing_espn_history(
    force_refresh: bool = True,
    include_player_stats: bool = True,
    missing_only: bool = True,
    limit: int = 30,
) -> dict:
    with connect() as conn:
        payload = _missing_espn_scores_payload(conn, limit=max(1, min(limit, 180)))
    selected_dates = payload.get("dates", [])
    if not selected_dates:
        return {
            "selected_dates": [],
            "missing_games": [],
            "missing_count": 0,
            "source": "espn",
            "message": "No missing completed ESPN scores found.",
        }
    result = import_espn_history(
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
    start_date: str | None = None,
    end_date: str | None = None,
    force_refresh: bool = True,
) -> dict:
    with connect() as conn:
        before = _espn_stats_gap_audit(conn, start_date=start_date, end_date=end_date, limit_missing_games=1000)
        result = _backfill_espn_stats_gaps(
            conn,
            before["missing_dates"],
            force_refresh=force_refresh,
        )
        ats_backfill = _recompute_team_results_from_game_lines(conn)
        settlements = settle_completed_props(conn)
        game_settlements = settle_completed_game_predictions(conn)
        gem_settlements = _sync_gem_snapshot_settlements(conn)
        watchlist_settlements = _sync_watchlist_snapshot_settlements(conn)
        _clear_scheduled_prop_state(conn, clear_source_rows=True)
        synced_props = 0
        projections = []
        watchlist_snapshot = _snapshot_watchlist(conn, datetime.now(LOCAL_TZ).date().isoformat())
        after = _espn_stats_gap_audit(conn, start_date=start_date, end_date=end_date, limit_missing_games=1000)
    _clear_prop_scrape_caches()
    _invalidate_read_caches()
    return {
        "source": "espn",
        "backfill": result,
        "gap_audit_before": before,
        "gap_audit_after": after,
        "ats_backfill": ats_backfill,
        "settlements": settlements,
        "game_settlements": game_settlements,
        "gem_settlements": gem_settlements,
        "watchlist_settlements": watchlist_settlements,
        "watchlist_snapshot": watchlist_snapshot,
        "synced_props": synced_props,
        "predictions": len(projections),
    }


@app.post("/api/history/recompute-ats", dependencies=[Depends(_protect_mutation)])
def recompute_ats_from_game_lines() -> dict:
    with connect() as conn:
        result = _recompute_team_results_from_game_lines(conn)
    _invalidate_read_caches()
    return {"source": "game_lines", **result}


@app.post("/api/history/backfill-covers-lines", dependencies=[Depends(_protect_mutation)])
def backfill_covers_lines(
    start_date: str,
    end_date: str | None = None,
    force_refresh: bool = True,
    max_days: int = 45,
) -> dict:
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
            EXISTS(SELECT 1 FROM team_game_results r WHERE r.game_id = g.id) AS has_team_results
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
        missing_games.append(
            {
                "id": int(row["id"]),
                "game_date": row["game_date"],
                "start_time": row["start_time"],
                "status": row["status"],
                "home_team": row["home_team"],
                "away_team": row["away_team"],
                "espn_event_id": row["espn_event_id"],
                "has_team_results": bool(row["has_team_results"]),
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
    game_day = _parse_game_date(row["game_date"])
    today_local = datetime.now(LOCAL_TZ).date()
    if start is None:
        if game_day is None:
            return False
        return game_day < today_local
    if start >= cutoff:
        return False
    status = str(row["status"] or "").lower()
    has_team_results = bool(row["has_team_results"])
    if status == "final" and not has_team_results:
        return True
    # Avoid false positives from stale intraday schedule states. We only flag
    # scheduled games once the local game date has passed.
    if status == "scheduled" and game_day is not None and game_day < today_local:
        return True
    return False


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
            payload = line_discrepancies(conn, game_id)
        compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        _set_observability_headers(response, f"{LINE_DISCREPANCIES_CACHE_NAME}:game_id={game_id}", "BYPASS", round(compute_ms, 2))
        return payload
    if force_refresh:
        started = datetime.now(timezone.utc)
        with connect() as conn:
            payload = line_discrepancies(conn, None)
        compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
        write_json_cache(LINE_DISCREPANCIES_CACHE_NAME, _cache_envelope(payload, LINE_DISCREPANCIES_TTL_SECONDS))
        _set_observability_headers(response, LINE_DISCREPANCIES_CACHE_NAME, "BYPASS", round(compute_ms, 2))
        return payload
    def compute() -> list[dict]:
        with connect() as conn:
            return line_discrepancies(conn, None)
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
            try:
                import_rotowire_lineups(conn, force_refresh=False)
            except Exception:
                # Keep roster endpoint non-fatal so dashboard loads even if live Rotowire fetch fails.
                pass
        payload = read_json_cache(ROTOWIRE_RAW_CACHE_NAME)
        rows = payload.get("rows", []) if isinstance(payload, dict) else []
        captured_at = payload.get("captured_at") if isinstance(payload, dict) else None
        normalized = []
        for row in rows:
            if not isinstance(row, dict):
                continue
            team = str(row.get("team") or "").strip().upper()
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
        return sorted(normalized, key=lambda item: (item["team"], item["player_name"]))

    payload, status, compute_ms = _read_through_cache_with_meta(
        ROSTER_CACHE_NAME,
        ROSTER_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, ROSTER_CACHE_NAME, status, compute_ms)
    return payload


@app.get("/api/models/runs")
def model_runs(response: Response) -> dict:
    def compute() -> dict:
        with connect() as conn:
            return {
                "latest": latest_model_run(conn),
                "runs": list_model_runs(conn),
            }

    payload, status, compute_ms = _read_through_cache_with_meta(
        MODEL_RUNS_CACHE_NAME,
        MODEL_RUNS_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, MODEL_RUNS_CACHE_NAME, status, compute_ms)
    return payload


@app.get("/api/ball_dont_lie/history")
def ball_dont_lie_history(
    team: str,
    seasons: str | None = None,
    start_date: str | None = None,
    end_date: str | None = None,
    force_refresh: bool = False,
) -> dict:
    season_list: list[int] | None = None
    if seasons:
        try:
            season_list = [int(value.strip()) for value in seasons.split(",") if value.strip()]
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=f"Invalid seasons value: {exc}")

    try:
        return fetch_team_history(
            team=team,
            seasons=season_list,
            start_date=start_date,
            end_date=end_date,
            force_refresh=force_refresh,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))


@app.get("/api/matchups", dependencies=[Depends(_protect_force_refresh)])
def matchups(response: Response, force_refresh: bool = False) -> list[dict]:
    if not force_refresh:
        started = datetime.now(timezone.utc)
        cached = _read_cached_payload(MATCHUPS_CACHE_NAME)
        if cached is not None:
            _set_observability_headers(response, MATCHUPS_CACHE_NAME, "HIT", 0.0)
            return cached
    with connect() as conn:
        started = datetime.now(timezone.utc)
        injury_refresh = import_rotowire_lineups(conn, force_refresh=False)
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
            FROM games g
            JOIN teams home ON home.id = g.home_team_id
            JOIN teams away ON away.id = g.away_team_id
            WHERE g.status = 'scheduled'
            ORDER BY g.start_time
            """
        ).fetchall()
        games = [game for game in games if _is_today_active_game_time(game["start_time"])]
        game_groups = _coalesce_matchup_games(games)
        covers_records = _covers_records_by_game(conn)
        covers_market_odds = _covers_market_odds_by_game()
        payload = []
        for game, game_ids in game_groups:
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
            for field in ("spread_home", "game_total", "home_moneyline", "away_moneyline"):
                if market_override.get(field) is not None:
                    game_context[field] = market_override[field]
            game_context["rest_days_home"] = home_rest_days if home_rest_days is not None else 2
            game_context["rest_days_away"] = away_rest_days if away_rest_days is not None else 2
            prediction = project_game(conn, game_context)
            game_prediction_id = save_game_prediction(conn, game_context, prediction)
            payload.append(
                {
                    "id": game["id"],
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
                    **market_override,
                    "blowout_risk": _blowout_display(game_context["spread_home"], "starter")["blowout_risk"],
                    **prediction,
                    "home": home_summary,
                    "away": away_summary,
                    "covers_records": group_covers_records,
                    "props": _value_board_payload_for_games(conn, game_ids),
                    "sportsbook_props": _sportsbook_props_for_games(conn, game_ids),
                    "line_discrepancies": _line_discrepancies_for_games(conn, game_ids),
                    "injury_source": injury_refresh.get("source"),
                    "injury_captured_at": injury_refresh.get("captured_at"),
                    "injury_from_cache": injury_refresh.get("from_cache"),
                }
            )
    write_json_cache(MATCHUPS_CACHE_NAME, _cache_envelope(payload, MATCHUPS_TTL_SECONDS))
    compute_ms = (datetime.now(timezone.utc) - started).total_seconds() * 1000
    _set_observability_headers(response, MATCHUPS_CACHE_NAME, "BYPASS" if force_refresh else "MISS", round(compute_ms, 2))
    return payload


def _start_prop_sync_if_needed(source: str) -> bool:
    with _PROP_SYNC_LOCK:
        if _PROP_SYNC_STATE["running"]:
            return False
        _PROP_SYNC_STATE["running"] = True
        _PROP_SYNC_STATE["started_at"] = datetime.now(timezone.utc).isoformat()
        _PROP_SYNC_STATE["finished_at"] = None
        _PROP_SYNC_STATE["last_error"] = None

    def _run() -> None:
        try:
            with connect() as conn:
                synced = sync_prop_lines_from_sportsbook(conn)
            with _PROP_SYNC_LOCK:
                _PROP_SYNC_STATE["last_result"] = {
                    "source": source,
                    "synced_props": int(synced),
                }
        except Exception as exc:
            with _PROP_SYNC_LOCK:
                _PROP_SYNC_STATE["last_error"] = str(exc)
        finally:
            with _PROP_SYNC_LOCK:
                _PROP_SYNC_STATE["running"] = False
                _PROP_SYNC_STATE["finished_at"] = datetime.now(timezone.utc).isoformat()

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


def _value_board_payload_for_games(conn, game_ids: list[int]) -> list[dict]:
    if not game_ids:
        return []
    payload = _value_board_payload(conn, game_ids=game_ids)
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


def _line_discrepancies_for_games(conn, game_ids: list[int]) -> list[dict]:
    payload = []
    for game_id in game_ids:
        payload.extend(line_discrepancies(conn, game_id))
    return sorted(payload, key=lambda item: (item["line_gap"], item["price_gap"]), reverse=True)


def _value_board_payload(conn, game_id: int | None = None, game_ids: list[int] | None = None) -> list[dict]:
    if game_ids:
        placeholders = ",".join("?" for _ in game_ids)
        game_filter = f"WHERE pl.game_id IN ({placeholders})"
        params = tuple(game_ids)
    elif game_id is not None:
        game_filter = "WHERE pl.game_id = ?"
        params = (game_id,)
    else:
        game_filter = ""
        params = ()
    rows = conn.execute(
        f"""
        WITH raw_props AS (
            SELECT
                pp.id,
                pl.game_id,
                p.full_name AS player,
                p.id AS player_id,
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
    payload = []
    fallback_payload = []
    freshness_cache: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        is_active_time = _is_active_game_time(row["start_time"])
        item = dict(row)
        if not _include_value_board_pick(item):
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
    value_board_prediction_ids = {int(item["id"]) for item in _value_board_payload(conn)}
    gem_prop_line_ids = {int(item["prop_line_id"]) for item in _build_current_gems(conn, "balanced")}
    payload = []
    freshness_cache: dict[tuple[int, int], dict[str, Any]] = {}
    for row in rows:
        item = dict(row)
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
        item.update(_blowout_display(item["team_spread"], item["rotation_role"]))
        payload.append(item)
    return payload[: max(1, min(int(limit), 200))]


def _include_watchlist_pick(item: dict) -> bool:
    confidence = str(item.get("confidence") or "").strip().lower()
    market = str(item.get("market") or "").strip().lower()
    try:
        edge = abs(float(item.get("edge") or 0.0))
        ev = float(item.get("expected_value") or 0.0)
    except (TypeError, ValueError):
        return False

    if confidence != "low":
        return False
    if ev < 0.02 or edge < 0.05 or edge >= LOW_CONFIDENCE_EDGE_MIN:
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
    try:
        edge = abs(float(item.get("edge") or 0.0))
    except (TypeError, ValueError):
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

    return edge >= LOW_CONFIDENCE_EDGE_MIN and edge < LOW_CONFIDENCE_EDGE_MAX


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
            opponent.abbreviation AS opponent
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        JOIN teams opponent ON opponent.id = CASE
            WHEN g.home_team_id = r.team_id THEN g.away_team_id
            ELSE g.home_team_id
        END
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
        "recent_games": recent_games,
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
