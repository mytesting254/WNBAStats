from __future__ import annotations

import math
import os
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .db import connect
from .paths import get_db_path
from .player_prop_model import MODEL_VERSION, predict_player_prop


_SNAPSHOT_JOB_LOCK = threading.Lock()
_SNAPSHOT_JOB_RUNNING = False


def get_tracking_db_path() -> Path:
    configured = os.getenv("WNBA_STOCKS_TRACKING_DB", "").strip()
    if configured:
        return Path(configured)
    return get_db_path().with_name("stocks_tracking.sqlite")


def ensure_tracking_schema() -> Path:
    path = get_tracking_db_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS projection_snapshots (
                id INTEGER PRIMARY KEY,
                game_id INTEGER NOT NULL,
                player_id INTEGER NOT NULL,
                player_name TEXT NOT NULL,
                game_date TEXT NOT NULL,
                captured_at TEXT NOT NULL,
                model_version TEXT NOT NULL,
                projected_steals REAL NOT NULL,
                projected_blocks REAL NOT NULL,
                projected_stocks REAL NOT NULL,
                steal_prob_1_plus REAL NOT NULL,
                steal_prob_2_plus REAL NOT NULL,
                block_prob_1_plus REAL NOT NULL,
                block_prob_2_plus REAL NOT NULL,
                stocks_prob_2_plus REAL NOT NULL,
                data_quality TEXT NOT NULL,
                UNIQUE(game_id, player_id, model_version, captured_at)
            );
            CREATE TABLE IF NOT EXISTS settlements (
                snapshot_id INTEGER PRIMARY KEY,
                actual_steals INTEGER NOT NULL,
                actual_blocks INTEGER NOT NULL,
                settled_at TEXT NOT NULL,
                FOREIGN KEY(snapshot_id) REFERENCES projection_snapshots(id)
            );
            CREATE INDEX IF NOT EXISTS idx_stocks_snapshots_game ON projection_snapshots(game_id, player_id);
            """
        )
    return path


def _open_tracking_connection() -> sqlite3.Connection:
    path = ensure_tracking_schema()
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=30000")
    return conn


def _poisson_at_least(mean: float, threshold: int) -> float:
    safe_mean = max(0.0, float(mean))
    return 1 - sum(math.exp(-safe_mean) * safe_mean**k / math.factorial(k) for k in range(threshold))


def snapshot_stocks(
    conn: sqlite3.Connection,
    game_ids: list[int] | None = None,
    runtime_cache: dict[str, dict[tuple, object]] | None = None,
) -> int:
    tracking = _open_tracking_connection()
    try:
        shared_runtime_cache: dict[str, dict[tuple, object]] = runtime_cache if runtime_cache is not None else {}
        filter_sql = ""
        params: tuple[int, ...] = ()
        if game_ids:
            normalized_game_ids = tuple(sorted({int(game_id) for game_id in game_ids if int(game_id) > 0}))
            if normalized_game_ids:
                filter_sql = " AND g.id IN (" + ",".join("?" for _ in normalized_game_ids) + ")"
                params = normalized_game_ids
        rows = conn.execute(
            """
            SELECT DISTINCT
                g.id,
                g.game_date,
                p.id,
                p.full_name
            FROM prop_lines pl
            JOIN games g ON g.id = pl.game_id
            JOIN players p ON p.id = pl.player_id
            WHERE g.status = 'scheduled'
            """
            + filter_sql
            + """
            ORDER BY g.start_time, p.full_name
            """,
            params,
        ).fetchall()
        now = datetime.now(timezone.utc).isoformat()
        rows_to_insert: list[tuple[object, ...]] = []
        for game_id, game_date, player_id, player_name in rows:
            steals, _, _ = predict_player_prop(
                conn,
                int(player_id),
                "steals",
                int(game_id),
                runtime_cache=shared_runtime_cache,
                allow_training=False,
            )
            blocks, _, _ = predict_player_prop(
                conn,
                int(player_id),
                "blocks",
                int(game_id),
                runtime_cache=shared_runtime_cache,
                allow_training=False,
            )
            projected_stocks = float(steals + blocks)
            rows_to_insert.append(
                (
                    int(game_id),
                    int(player_id),
                    str(player_name),
                    str(game_date),
                    now,
                    MODEL_VERSION,
                    float(steals),
                    float(blocks),
                    projected_stocks,
                    _poisson_at_least(float(steals), 1),
                    _poisson_at_least(float(steals), 2),
                    _poisson_at_least(float(blocks), 1),
                    _poisson_at_least(float(blocks), 2),
                    _poisson_at_least(projected_stocks, 2),
                    "model_only",
                )
            )
        if rows_to_insert:
            tracking.executemany(
                """
                INSERT OR IGNORE INTO projection_snapshots (
                    game_id,
                    player_id,
                    player_name,
                    game_date,
                    captured_at,
                    model_version,
                    projected_steals,
                    projected_blocks,
                    projected_stocks,
                    steal_prob_1_plus,
                    steal_prob_2_plus,
                    block_prob_1_plus,
                    block_prob_2_plus,
                    stocks_prob_2_plus,
                    data_quality
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                rows_to_insert,
            )
            written = tracking.total_changes
        else:
            written = 0
        tracking.commit()
        return written
    finally:
        tracking.close()


def queue_snapshot_stocks(game_ids: list[int] | None = None) -> bool:
    global _SNAPSHOT_JOB_RUNNING
    normalized_game_ids = sorted({int(game_id) for game_id in (game_ids or []) if int(game_id) > 0})
    with _SNAPSHOT_JOB_LOCK:
        if _SNAPSHOT_JOB_RUNNING:
            return False
        _SNAPSHOT_JOB_RUNNING = True

    def _run() -> None:
        global _SNAPSHOT_JOB_RUNNING
        try:
            with connect() as conn:
                snapshot_stocks(conn, game_ids=normalized_game_ids or None, runtime_cache=None)
        except Exception:
            # Model-only tracking is best-effort and must never block sportsbook rebuilds.
            pass
        finally:
            with _SNAPSHOT_JOB_LOCK:
                _SNAPSHOT_JOB_RUNNING = False

    threading.Thread(target=_run, daemon=True).start()
    return True


def list_special_stocks(*, include_history: bool = False) -> list[dict[str, Any]]:
    tracking = _open_tracking_connection()
    try:
        if include_history:
            rows = tracking.execute(
                """
                SELECT
                    ps.*,
                    st.actual_steals,
                    st.actual_blocks,
                    CASE
                        WHEN st.snapshot_id IS NULL THEN NULL
                        ELSE st.actual_steals + st.actual_blocks
                    END AS actual_stocks,
                    st.settled_at
                FROM projection_snapshots ps
                LEFT JOIN settlements st ON st.snapshot_id = ps.id
                ORDER BY ps.game_date DESC, ps.projected_stocks DESC, ps.captured_at DESC
                """
            ).fetchall()
        else:
            rows = tracking.execute(
                """
                WITH latest AS (
                    SELECT
                        ps.*,
                        ROW_NUMBER() OVER (
                            PARTITION BY ps.game_id, ps.player_id
                            ORDER BY ps.captured_at DESC, ps.id DESC
                        ) AS snapshot_rank
                    FROM projection_snapshots ps
                )
                SELECT
                    latest.*,
                    st.actual_steals,
                    st.actual_blocks,
                    CASE
                        WHEN st.snapshot_id IS NULL THEN NULL
                        ELSE st.actual_steals + st.actual_blocks
                    END AS actual_stocks,
                    st.settled_at
                FROM latest
                LEFT JOIN settlements st ON st.snapshot_id = latest.id
                WHERE latest.snapshot_rank = 1
                ORDER BY latest.game_date, latest.projected_stocks DESC, latest.player_name
                """
            ).fetchall()
        return [dict(row) for row in rows]
    finally:
        tracking.close()


def settle_stocks(
    conn: sqlite3.Connection,
    *,
    selected_date: str | None = None,
    selected_dates: list[str] | None = None,
) -> dict[str, Any]:
    target_dates = sorted(
        {
            str(value).strip()
            for value in ([selected_date] if selected_date else []) + list(selected_dates or [])
            if str(value).strip()
        }
    )
    tracking = _open_tracking_connection()
    try:
        filter_sql = ""
        params: tuple[str, ...] = ()
        if target_dates:
            placeholders = ",".join("?" for _ in target_dates)
            filter_sql = f" AND ps.game_date IN ({placeholders})"
            params = tuple(target_dates)
        snapshot_rows = tracking.execute(
            """
            SELECT
                ps.id,
                ps.game_id,
                ps.player_id,
                ps.game_date
            FROM projection_snapshots ps
            LEFT JOIN settlements st ON st.snapshot_id = ps.id
            WHERE st.snapshot_id IS NULL
            """
            + filter_sql,
            params,
        ).fetchall()
        settled_at = datetime.now(timezone.utc).isoformat()
        inserts: list[tuple[int, int, int, str]] = []
        for row in snapshot_rows:
            game_row = conn.execute("SELECT status FROM games WHERE id = ?", (int(row["game_id"]),)).fetchone()
            if game_row is None or str(game_row["status"] or "").lower() != "final":
                continue
            stat_row = conn.execute(
                """
                SELECT steals, blocks
                FROM player_game_stats
                WHERE game_id = ? AND player_id = ?
                """,
                (int(row["game_id"]), int(row["player_id"])),
            ).fetchone()
            if stat_row is None:
                continue
            inserts.append(
                (
                    int(row["id"]),
                    int(stat_row["steals"] or 0),
                    int(stat_row["blocks"] or 0),
                    settled_at,
                )
            )
        if inserts:
            tracking.executemany(
                """
                INSERT INTO settlements (snapshot_id, actual_steals, actual_blocks, settled_at)
                VALUES (?, ?, ?, ?)
                """,
                inserts,
            )
            tracking.commit()
        return {
            "eligible": len(snapshot_rows),
            "settled": len(inserts),
            "settled_at": settled_at,
            "selected_date": target_dates[0] if len(target_dates) == 1 else None,
            "selected_dates": target_dates,
        }
    finally:
        tracking.close()
