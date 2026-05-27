from __future__ import annotations

from datetime import datetime, timedelta, timezone
from collections import Counter
import os
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
from .covers_import import import_covers_props
from .db import connect, init_db
from .espn_history import import_espn_player_boxscores, import_espn_scoreboard
from .game_prediction_tracking import save_game_prediction, settle_completed_game_predictions
from .game_predictions import project_game
from .odds_import import import_the_odds_api_props, line_discrepancies, list_sportsbook_props, odds_cache_summary, sync_prop_lines_from_sportsbook
from .projections import rebuild_predictions
from .rotowire_import import RAW_CACHE_NAME as ROTOWIRE_RAW_CACHE_NAME, import_rotowire_lineups
from .settlement import settle_completed_props
from .training import latest_model_run, list_model_runs, run_walk_forward_training


app = FastAPI(title="WNBA Prop Value API")
COMPLETED_GAME_GRACE_HOURS = 4
LOCAL_TZ = timezone(timedelta(hours=-4))
LOW_CONFIDENCE_EDGE_MIN = 0.12
VALUE_BOARD_CACHE_NAME = "current_value_board.json"
MATCHUPS_CACHE_NAME = "current_matchups.json"
LINE_DISCREPANCIES_CACHE_NAME = "line_discrepancies.json"
MODEL_PERFORMANCE_CACHE_NAME = "model_performance.json"
MODEL_RUNS_CACHE_NAME = "model_runs.json"
ROSTER_CACHE_NAME = "roster.json"
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


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


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


def _set_observability_headers(response: Response, cache_name: str, cache_status: str, compute_ms: float) -> None:
    if os.getenv("EXPOSE_DEBUG_HEADERS", "").strip().lower() not in {"1", "true", "yes"}:
        return
    response.headers["X-Cache"] = cache_status
    response.headers["X-Cache-Key"] = f"{cache_name}:v{READ_CACHE_VERSION}"
    response.headers["X-Compute-Ms"] = f"{compute_ms:.2f}"
    print(f"[cache] {cache_name} status={cache_status} compute_ms={compute_ms:.2f}")


@app.post("/api/recalculate", dependencies=[Depends(_protect_mutation)])
def recalculate() -> dict[str, int]:
    with connect() as conn:
        projections = rebuild_predictions(conn)
        settlements = settle_completed_props(conn)
        game_settlements = settle_completed_game_predictions(conn)
    _invalidate_read_caches()
    return {"predictions": len(projections), "settled": settlements["settled"], "game_settled": game_settlements["settled"]}


@app.post("/api/settle-props", dependencies=[Depends(_protect_mutation)])
def settle_props() -> dict:
    with connect() as conn:
        props = settle_completed_props(conn)
        games = settle_completed_game_predictions(conn)
    _invalidate_read_caches()
    return {"props": props, "games": games}


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


@app.get("/api/model-performance")
def model_performance(response: Response) -> dict:
    def compute() -> dict:
        with connect() as conn:
            total_settled_row = conn.execute(
                "SELECT COUNT(*) AS count FROM settled_props"
            ).fetchone()
            total_settled = int(total_settled_row["count"] or 0)
            rows = conn.execute(
                """
                SELECT
                    pp.recommended_side,
                    pp.expected_value,
                    sp.winning_side
                FROM prop_predictions pp
                JOIN settled_props sp ON sp.prop_line_id = pp.prop_line_id
                """
            ).fetchall()
        evaluated = len(rows)
        if total_settled == 0:
            return {
                "settled": 0,
                "wins": 0,
                "win_rate": None,
                "average_ev": None,
                "message": "No settled props yet. Settle completed games to evaluate the model.",
            }
        if evaluated == 0:
            return {
                "settled": 0,
                "wins": 0,
                "win_rate": None,
                "average_ev": None,
                "message": f"{total_settled} settled prop{'s' if total_settled != 1 else ''} exist, but there are no matching model predictions.",
            }
        wins = sum(1 for row in rows if row["recommended_side"] == row["winning_side"])
        avg_ev = sum(float(row["expected_value"]) for row in rows) / evaluated
        return {
            "settled": evaluated,
            "wins": wins,
            "win_rate": round(wins / evaluated, 4),
            "average_ev": round(avg_ev, 4),
            "message": f"Evaluated {evaluated} settled model prediction{'s' if evaluated != 1 else ''}.",
        }

    payload, status, compute_ms = _read_through_cache_with_meta(
        MODEL_PERFORMANCE_CACHE_NAME,
        MODEL_PERFORMANCE_TTL_SECONDS,
        compute,
    )
    _set_observability_headers(response, MODEL_PERFORMANCE_CACHE_NAME, status, compute_ms)
    return payload


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


@app.post("/api/odds/import", dependencies=[Depends(_protect_mutation)])
def import_odds(force_refresh: bool = False) -> dict:
    with connect() as conn:
        result = import_the_odds_api_props(conn, force_refresh=force_refresh)
    _invalidate_read_caches()
    return result


@app.post("/api/covers/import", dependencies=[Depends(_protect_mutation)])
def import_covers(selected_date: str | None = None, force_refresh: bool = False) -> dict:
    with connect() as conn:
        result = import_covers_props(conn, selected_date=selected_date, force_refresh=force_refresh)
    _invalidate_read_caches()
    return result


@app.post("/api/injuries/import/rotowire", dependencies=[Depends(_protect_mutation)])
def import_rotowire_injuries(force_refresh: bool = False) -> dict:
    with connect() as conn:
        result = import_rotowire_lineups(conn, force_refresh=force_refresh)
        projections = rebuild_predictions(conn)
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
    selected_date: str | None = None,
    selected_dates: Annotated[list[str] | None, Query()] = None,
) -> dict:
    target_season = season or datetime.now().year
    seasons = [target_season - 1, target_season] if include_previous_season else [target_season]
    unique_seasons = sorted(set(seasons))
    daily_dates = _selected_espn_dates(selected_date, selected_dates)
    if not daily_dates and not force_refresh and not include_previous_season:
        daily_dates = [datetime.now(LOCAL_TZ).date().isoformat()]

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
            settlements = settle_completed_props(conn)
            game_settlements = settle_completed_game_predictions(conn)
            synced_props = sync_prop_lines_from_sportsbook(conn)
            projections = rebuild_predictions(conn)
    except Exception as exc:
        message = str(exc)
        if "turso" in message.lower() or "httpsconnectionpool" in message.lower() or "nameresolutionerror" in message.lower():
            raise HTTPException(
                status_code=503,
                detail=(
                    "Unable to connect to Turso while refreshing ESPN history. "
                    "Check TURSO_DATABASE_URL/TURSO_AUTH_TOKEN and network/DNS access, or set USE_LOCAL_DB=true."
                ),
            ) from exc
        raise
    return {
        "season": target_season,
        "seasons": unique_seasons,
        "selected_date": daily_dates[0] if len(daily_dates) == 1 else None,
        "selected_dates": daily_dates,
        "scoreboards": scoreboards,
        "player_stats": player_stats,
        "synced_props": synced_props,
        "settlements": settlements,
        "game_settlements": game_settlements,
        "predictions": len(projections),
        "missing_only": missing_only,
        "source": "espn",
        "errors": errors,
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
    if start is None:
        game_day = _parse_game_date(row["game_date"])
        if game_day is None:
            return False
        return game_day < datetime.now(LOCAL_TZ).date()
    if start >= cutoff:
        return False
    status = str(row["status"] or "").lower()
    has_team_results = bool(row["has_team_results"])
    if status == "final" and not has_team_results:
        return True
    if status == "scheduled":
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
            FROM games g
            JOIN teams home ON home.id = g.home_team_id
            JOIN teams away ON away.id = g.away_team_id
            WHERE g.status = 'scheduled'
            ORDER BY g.start_time
            """
        ).fetchall()
        games = [game for game in games if _is_today_active_game_time(game["start_time"])]
        game_groups = _coalesce_matchup_games(games)
        covers_records = _covers_records_by_game()
        payload = []
        for game, game_ids in game_groups:
            home_summary = _team_last_10_summary(conn, int(game["home_team_id"]))
            away_summary = _team_last_10_summary(conn, int(game["away_team_id"]))
            game_id = int(game["id"])
            home_rest_days = _rest_days_before_game(conn, int(game["home_team_id"]), game["start_time"], game["game_date"])
            away_rest_days = _rest_days_before_game(conn, int(game["away_team_id"]), game["start_time"], game["game_date"])
            game_context = dict(game)
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
                    "spread_home": game["spread_home"],
                    "game_total": game["game_total"],
                    "blowout_risk": _blowout_display(game["spread_home"], "starter")["blowout_risk"],
                    **prediction,
                    "home": home_summary,
                    "away": away_summary,
                    "covers_records": covers_records.get(game_id),
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


def _covers_records_by_game() -> dict[int, dict]:
    payload = read_json_cache("covers_props_raw.json")
    if not isinstance(payload, dict):
        return {}
    records_by_game = {}
    for item in payload.get("games", []):
        if not isinstance(item, dict) or item.get("game_id") is None:
            continue
        records = item.get("records")
        if isinstance(records, dict):
            records_by_game[int(item["game_id"])] = records
    return records_by_game


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


def _game_row_score(game) -> tuple[int, int, int, int]:
    return (
        1 if _is_real_spread(game["spread_home"]) else 0,
        1 if _is_real_total(game["game_total"]) else 0,
        1 if str(game["start_time"]).endswith("Z") or "+" in str(game["start_time"]) else 0,
        int(game["id"]),
    )


def _is_real_spread(value) -> bool:
    return value is not None


def _is_real_total(value) -> bool:
    return value is not None and float(value) > 0


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
        WITH ranked_props AS (
            SELECT
                pp.id,
                pl.game_id,
                p.full_name AS player,
                p.id AS player_id,
                t.abbreviation AS team,
                t.logo_url AS team_logo_url,
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
                CASE
                    WHEN p.team_id = g.home_team_id THEN g.rest_days_home
                    ELSE g.rest_days_away
                END AS rest_days,
                p.rotation_role,
                g.spread_home,
                g.game_total,
                CASE
                    WHEN p.team_id = g.home_team_id THEN g.spread_home
                    ELSE -g.spread_home
                END AS team_spread,
                ROW_NUMBER() OVER (
                    PARTITION BY pl.game_id, pl.player_id, pl.market, pl.line
                    ORDER BY
                        CASE
                            WHEN pp.recommended_side = 'over' THEN pl.over_odds
                            ELSE pl.under_odds
                        END DESC,
                        pp.expected_value DESC,
                        pp.id DESC
                ) AS rn
            FROM prop_predictions pp
            JOIN prop_lines pl ON pl.id = pp.prop_line_id
            JOIN players p ON p.id = pl.player_id
            JOIN teams t ON t.id = p.team_id
            JOIN games g ON g.id = pl.game_id
            {game_filter}
        )
        SELECT * FROM ranked_props WHERE rn = 1
        ORDER BY expected_value DESC, edge DESC
        """,
        params,
    ).fetchall()
    payload = []
    for row in rows:
        if game_id is None and not _is_active_game_time(row["start_time"]):
            continue
        item = dict(row)
        if not _include_value_board_pick(item):
            continue
        item.update(_blowout_display(item["team_spread"], item["rotation_role"]))
        payload.append(item)
    return payload


def _include_value_board_pick(item: dict) -> bool:
    confidence = str(item.get("confidence") or "").strip().lower()
    if confidence != "low":
        return True
    try:
        return float(item.get("edge") or 0.0) >= LOW_CONFIDENCE_EDGE_MIN
    except (TypeError, ValueError):
        return False


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
        FROM team_game_results r
        JOIN games g ON g.id = r.game_id
        WHERE r.team_id = ?
          AND g.status = 'final'
        ORDER BY g.start_time DESC
        """,
        (team_id,),
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
    rest_days = max((current_date - max(previous_dates)).days - 1, 0)
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
