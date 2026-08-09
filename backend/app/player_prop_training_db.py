from __future__ import annotations

import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path

from .paths import get_training_db_path

PLAYER_PROP_TRAINING_DB_VERSION = "v4"
_PLAYER_PROP_TRAINING_DB_LOCK = threading.RLock()

MARKET_SAMPLE_CURATION_POLICY: dict[str, dict[str, dict[str, float | bool]]] = {}


def ensure_player_prop_training_db(
    conn: sqlite3.Connection,
    *,
    force: bool = False,
    allow_rebuild: bool = True,
) -> dict[str, object]:
    training_db_path = get_training_db_path()
    training_db_path.parent.mkdir(parents=True, exist_ok=True)
    expected_signature = _source_signature(conn)

    if not training_db_path.exists():
        if not allow_rebuild:
            return {
                "path": str(training_db_path),
                "rebuilt": False,
                "included_rows": 0,
                "candidate_rows": 0,
                "excluded_rows": 0,
                "built_at": None,
                "source_signature": expected_signature,
                "stale": True,
                "missing": True,
            }

    with _PLAYER_PROP_TRAINING_DB_LOCK:
        with _connect_training_db(training_db_path) as training_conn:
            _init_training_db(training_conn)
            metadata = _read_metadata(training_conn)
            current_row_count = int(metadata.get("included_rows") or 0)
            if (
                not force
                and metadata.get("source_signature") == expected_signature
                and metadata.get("db_version") == PLAYER_PROP_TRAINING_DB_VERSION
                and current_row_count > 0
            ):
                return {
                    "path": str(training_db_path),
                    "rebuilt": False,
                    "included_rows": current_row_count,
                    "candidate_rows": int(metadata.get("candidate_rows") or 0),
                    "excluded_rows": int(metadata.get("excluded_rows") or 0),
                    "built_at": metadata.get("built_at"),
                    "source_signature": expected_signature,
                }

            if not allow_rebuild:
                return {
                    "path": str(training_db_path),
                    "rebuilt": False,
                    "included_rows": current_row_count,
                    "candidate_rows": int(metadata.get("candidate_rows") or 0),
                    "excluded_rows": int(metadata.get("excluded_rows") or 0),
                    "built_at": metadata.get("built_at"),
                    "source_signature": str(metadata.get("source_signature") or ""),
                    "stale": True,
                    "missing": False,
                }

            _rebuild_player_prop_training_examples(
                source_conn=conn,
                training_conn=training_conn,
                source_signature=expected_signature,
            )
            metadata = _read_metadata(training_conn)
            return {
                "path": str(training_db_path),
                "rebuilt": True,
                "included_rows": int(metadata.get("included_rows") or 0),
                "candidate_rows": int(metadata.get("candidate_rows") or 0),
                "excluded_rows": int(metadata.get("excluded_rows") or 0),
                "built_at": metadata.get("built_at"),
                "source_signature": expected_signature,
            }


def load_player_prop_training_samples(
    conn: sqlite3.Connection,
    *,
    market: str,
    sample_kind: str,
    force_rebuild: bool = False,
    allow_rebuild: bool = True,
) -> tuple[list[object], object]:
    from .player_prop_model import TrainingSample, TrainingSampleDiagnostics

    info = ensure_player_prop_training_db(conn, force=force_rebuild, allow_rebuild=allow_rebuild)
    training_db_path = Path(str(info["path"]))
    if not training_db_path.exists():
        return [], TrainingSampleDiagnostics()
    with _connect_training_db(training_db_path) as training_conn:
        training_conn.row_factory = sqlite3.Row
        try:
            rows = training_conn.execute(
                """
                SELECT *
                FROM player_prop_training_examples
                WHERE market = ?
                  AND sample_kind = ?
                ORDER BY game_date ASC, source_game_id ASC, id ASC
                """,
                (str(market), str(sample_kind)),
            ).fetchall()
            metadata = _read_metadata(training_conn)
        except sqlite3.OperationalError:
            return [], TrainingSampleDiagnostics()

    diagnostics_by_market = json.loads(metadata.get("diagnostics_by_market") or "{}")
    diag_payload = diagnostics_by_market.get(str(market), {}).get(str(sample_kind), {})
    diagnostics = TrainingSampleDiagnostics(
        candidate_rows=int(diag_payload.get("candidate_rows") or 0),
        included_rows=int(diag_payload.get("included_rows") or 0),
        skipped_before_training_start=int(diag_payload.get("skipped_before_training_start") or 0),
        skipped_missing_history_window=int(diag_payload.get("skipped_missing_history_window") or 0),
        skipped_incomplete_context=int(diag_payload.get("skipped_incomplete_context") or 0),
        skipped_ambiguous_team_identity=int(diag_payload.get("skipped_ambiguous_team_identity") or 0),
        skipped_missing_snapshot=int(diag_payload.get("skipped_missing_snapshot") or 0),
        skipped_market_curation=int(diag_payload.get("skipped_market_curation") or 0),
    )
    samples = [
        TrainingSample(
            source_prop_line_id=_nullable_int(row["source_prop_line_id"]),
            source_player_id=int(row["source_player_id"]),
            source_game_id=int(row["source_game_id"]),
            features=list(json.loads(str(row["features_json"]))),
            target=float(row["target_value"]),
            game_date=str(row["game_date"]),
            season=str(row["season"]),
            segment=str(row["segment"]),
            baseline=float(row["baseline_value"] or 0.0),
            is_recent_transfer=bool(int(row["is_recent_transfer"] or 0)),
            sample_count=int(row["sample_count"] or 0),
            avg_minutes=float(row["avg_minutes"] or 0.0),
        )
        for row in rows
    ]
    return samples, diagnostics


def load_player_prop_final_projection_samples(
    conn: sqlite3.Connection,
    *,
    market: str,
    force_rebuild: bool = False,
    allow_rebuild: bool = True,
) -> tuple[list[object], object]:
    from .player_prop_model import FinalProjectionSample, TrainingSampleDiagnostics

    info = ensure_player_prop_training_db(conn, force=force_rebuild, allow_rebuild=allow_rebuild)
    training_db_path = Path(str(info["path"]))
    if not training_db_path.exists():
        return [], TrainingSampleDiagnostics()
    with _connect_training_db(training_db_path) as training_conn:
        training_conn.row_factory = sqlite3.Row
        try:
            rows = training_conn.execute(
                """
                SELECT *
                FROM player_prop_training_examples
                WHERE market = ?
                  AND sample_kind = 'final'
                ORDER BY game_date ASC, source_game_id ASC, id ASC
                """,
                (str(market),),
            ).fetchall()
            metadata = _read_metadata(training_conn)
        except sqlite3.OperationalError:
            return [], TrainingSampleDiagnostics()

    diagnostics_by_market = json.loads(metadata.get("diagnostics_by_market") or "{}")
    diag_payload = diagnostics_by_market.get(str(market), {}).get("final", {})
    diagnostics = TrainingSampleDiagnostics(
        candidate_rows=int(diag_payload.get("candidate_rows") or 0),
        included_rows=int(diag_payload.get("included_rows") or 0),
        skipped_before_training_start=int(diag_payload.get("skipped_before_training_start") or 0),
        skipped_missing_history_window=int(diag_payload.get("skipped_missing_history_window") or 0),
        skipped_incomplete_context=int(diag_payload.get("skipped_incomplete_context") or 0),
        skipped_ambiguous_team_identity=int(diag_payload.get("skipped_ambiguous_team_identity") or 0),
        skipped_missing_snapshot=int(diag_payload.get("skipped_missing_snapshot") or 0),
        skipped_market_curation=int(diag_payload.get("skipped_market_curation") or 0),
    )
    samples = [
        FinalProjectionSample(
            source_prop_line_id=_nullable_int(row["source_prop_line_id"]),
            source_player_id=int(row["source_player_id"]),
            source_game_id=int(row["source_game_id"]),
            features=list(json.loads(str(row["features_json"]))),
            target=float(row["target_value"]),
            game_date=str(row["game_date"]),
            season=str(row["season"]),
            segment=str(row["segment"]),
            component_projection=float(row["component_projection"] or 0.0),
            line=float(row["line_value"]) if row["line_value"] is not None else None,
            over_odds=_nullable_int(row["over_odds"]),
            under_odds=_nullable_int(row["under_odds"]),
            sample_count=int(row["sample_count"] or 0),
            avg_minutes=float(row["avg_minutes"] or 0.0),
        )
        for row in rows
    ]
    return samples, diagnostics


def _connect_training_db(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA cache_size = -8000")
    conn.execute("PRAGMA temp_store = MEMORY")
    conn.execute("PRAGMA busy_timeout = 30000")
    return conn


def _init_training_db(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        """
        SELECT sql
        FROM sqlite_master
        WHERE type = 'table'
          AND name = 'player_prop_training_examples'
        """
    ).fetchone()
    create_sql = str(row["sql"] or "") if row is not None else ""
    if "UNIQUE(market, sample_kind, source_player_id, source_game_id)" in create_sql:
        conn.execute("DROP TABLE IF EXISTS player_prop_training_examples")
        conn.execute("DROP INDEX IF EXISTS idx_player_prop_training_market_kind")
        conn.execute("DROP INDEX IF EXISTS idx_player_prop_training_unique_sample")
    conn.executescript(
        """
        CREATE TABLE IF NOT EXISTS player_prop_training_metadata (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS player_prop_training_examples (
            id INTEGER PRIMARY KEY,
            market TEXT NOT NULL,
            sample_kind TEXT NOT NULL,
            source_prop_line_id INTEGER,
            source_player_id INTEGER NOT NULL,
            source_game_id INTEGER NOT NULL,
            game_date TEXT NOT NULL,
            season TEXT NOT NULL,
            segment TEXT NOT NULL,
            features_json TEXT NOT NULL,
            target_value REAL NOT NULL,
            baseline_value REAL,
            component_projection REAL,
            line_value REAL,
            over_odds INTEGER,
            under_odds INTEGER,
            sample_count INTEGER,
            avg_minutes REAL,
            is_recent_transfer INTEGER NOT NULL DEFAULT 0,
            created_at TEXT NOT NULL
        );

        CREATE TABLE IF NOT EXISTS player_prop_training_exclusions (
            id INTEGER PRIMARY KEY,
            market TEXT NOT NULL,
            sample_kind TEXT NOT NULL,
            reason_code TEXT NOT NULL,
            source_prop_line_id INTEGER,
            source_player_id INTEGER NOT NULL,
            source_game_id INTEGER NOT NULL,
            game_date TEXT NOT NULL,
            season TEXT NOT NULL,
            segment TEXT NOT NULL,
            sample_count INTEGER,
            avg_minutes REAL,
            is_recent_transfer INTEGER NOT NULL DEFAULT 0,
            details_json TEXT,
            created_at TEXT NOT NULL
        );

        CREATE INDEX IF NOT EXISTS idx_player_prop_training_market_kind
        ON player_prop_training_examples(market, sample_kind, game_date);

        CREATE INDEX IF NOT EXISTS idx_player_prop_training_exclusions_market_kind
        ON player_prop_training_exclusions(market, sample_kind, reason_code, game_date);

        CREATE UNIQUE INDEX IF NOT EXISTS idx_player_prop_training_unique_sample
        ON player_prop_training_examples(
            market,
            sample_kind,
            source_player_id,
            source_game_id,
            source_prop_line_id
        );
        """
    )
    _ensure_training_example_columns(conn)
    conn.commit()


def _ensure_training_example_columns(conn: sqlite3.Connection) -> None:
    columns = {
        str(row["name"])
        for row in conn.execute("PRAGMA table_info(player_prop_training_examples)").fetchall()
    }
    if "source_prop_line_id" not in columns:
        conn.execute("ALTER TABLE player_prop_training_examples ADD COLUMN source_prop_line_id INTEGER")


def _read_metadata(conn: sqlite3.Connection) -> dict[str, str]:
    rows = conn.execute("SELECT key, value FROM player_prop_training_metadata").fetchall()
    return {str(row["key"]): str(row["value"]) for row in rows}


def _write_metadata(conn: sqlite3.Connection, values: dict[str, object]) -> None:
    conn.executemany(
        """
        INSERT INTO player_prop_training_metadata (key, value)
        VALUES (?, ?)
        ON CONFLICT(key) DO UPDATE SET value = excluded.value
        """,
        [(key, json.dumps(value) if isinstance(value, (dict, list)) else str(value)) for key, value in values.items()],
    )


def _rebuild_player_prop_training_examples(
    *,
    source_conn: sqlite3.Connection,
    training_conn: sqlite3.Connection,
    source_signature: str,
) -> None:
    from .player_prop_model import (
        _build_final_projection_samples_inline,
        _build_residual_training_samples_inline,
        _build_training_samples_inline,
    )
    from .player_prop_model import TRAINING_MARKETS

    built_at = datetime.now(timezone.utc).isoformat()
    candidate_rows = 0
    included_rows = 0
    excluded_rows = 0
    diagnostic_summary: dict[str, dict[str, int]] = {}
    exclusion_summary: dict[str, dict[str, dict[str, int]]] = {}
    rows_to_insert: list[tuple[object, ...]] = []
    exclusion_rows_to_insert: list[tuple[object, ...]] = []
    training_conn.execute("DELETE FROM player_prop_training_examples")
    training_conn.execute("DELETE FROM player_prop_training_exclusions")

    for market in TRAINING_MARKETS:
        raw_samples, raw_diag = _build_training_samples_inline(source_conn, market)
        residual_samples, residual_diag = _build_residual_training_samples_inline(source_conn, market)
        final_samples, final_diag = _build_final_projection_samples_inline(source_conn, market)
        raw_samples, raw_diag, raw_exclusions = _apply_market_curation(market, "raw", raw_samples, raw_diag)
        residual_samples, residual_diag, residual_exclusions = _apply_market_curation(market, "residual", residual_samples, residual_diag)
        diag_map = {
            "raw": raw_diag.to_dict(),
            "residual": residual_diag.to_dict(),
            "final": final_diag.to_dict(),
        }
        diagnostic_summary[str(market)] = diag_map
        exclusion_summary[str(market)] = {
            "raw": _summarize_exclusion_rows(raw_exclusions),
            "residual": _summarize_exclusion_rows(residual_exclusions),
        }
        candidate_rows += sum(item["candidate_rows"] for item in diag_map.values())
        included_rows += (
            len(raw_samples)
            + len(residual_samples)
            + len(final_samples)
        )
        excluded_rows += sum(
            max(0, int(item["candidate_rows"]) - int(item["included_rows"]))
            for item in diag_map.values()
        )
        rows_to_insert.extend(_raw_rows_to_insert(market, raw_samples, built_at))
        rows_to_insert.extend(_residual_rows_to_insert(market, residual_samples, built_at))
        rows_to_insert.extend(_final_rows_to_insert(market, final_samples, built_at))
        exclusion_rows_to_insert.extend(_exclusion_rows_to_insert(market, "raw", raw_exclusions, built_at))
        exclusion_rows_to_insert.extend(_exclusion_rows_to_insert(market, "residual", residual_exclusions, built_at))

    training_conn.executemany(
        """
        INSERT INTO player_prop_training_examples (
            market, sample_kind, source_prop_line_id, source_player_id, source_game_id,
            game_date, season, segment, features_json, target_value,
            baseline_value, component_projection, line_value,
            over_odds, under_odds, sample_count, avg_minutes,
            is_recent_transfer, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        rows_to_insert,
    )
    training_conn.executemany(
        """
        INSERT INTO player_prop_training_exclusions (
            market, sample_kind, reason_code, source_prop_line_id, source_player_id,
            source_game_id, game_date, season, segment, sample_count,
            avg_minutes, is_recent_transfer, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        exclusion_rows_to_insert,
    )
    _write_metadata(
        training_conn,
        {
            "built_at": built_at,
            "db_version": PLAYER_PROP_TRAINING_DB_VERSION,
            "candidate_rows": candidate_rows,
            "included_rows": included_rows,
            "excluded_rows": excluded_rows,
            "diagnostics_by_market": diagnostic_summary,
            "exclusions_by_market": exclusion_summary,
            "source_signature": source_signature,
        },
    )
    training_conn.commit()


def _raw_rows_to_insert(market: str, samples: list[object], built_at: str) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for sample in samples:
        rows.append(
            (
                str(market),
                "raw",
                int(sample.source_prop_line_id or 0),
                int(getattr(sample, "source_player_id", 0) or 0),
                int(getattr(sample, "source_game_id", 0) or 0),
                str(sample.game_date),
                str(sample.season),
                str(sample.segment),
                json.dumps(list(sample.features)),
                float(sample.target),
                float(sample.baseline),
                None,
                None,
                None,
                None,
                None,
                None,
                1 if bool(sample.is_recent_transfer) else 0,
                built_at,
            )
        )
    return rows


def _residual_rows_to_insert(market: str, samples: list[object], built_at: str) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for sample in samples:
        rows.append(
            (
                str(market),
                "residual",
                int(sample.source_prop_line_id or 0),
                int(getattr(sample, "source_player_id", 0) or 0),
                int(getattr(sample, "source_game_id", 0) or 0),
                str(sample.game_date),
                str(sample.season),
                str(sample.segment),
                json.dumps(list(sample.features)),
                float(sample.target),
                float(sample.baseline),
                None,
                None,
                None,
                None,
                None,
                None,
                1 if bool(sample.is_recent_transfer) else 0,
                built_at,
            )
        )
    return rows


def _final_rows_to_insert(market: str, samples: list[object], built_at: str) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for sample in samples:
        rows.append(
            (
                str(market),
                "final",
                int(sample.source_prop_line_id or 0),
                int(getattr(sample, "source_player_id", 0) or 0),
                int(getattr(sample, "source_game_id", 0) or 0),
                str(sample.game_date),
                str(sample.season),
                str(sample.segment),
                json.dumps(list(sample.features)),
                float(sample.target),
                None,
                float(sample.component_projection),
                float(sample.line) if sample.line is not None else None,
                int(sample.over_odds) if sample.over_odds is not None else None,
                int(sample.under_odds) if sample.under_odds is not None else None,
                int(sample.sample_count),
                float(sample.avg_minutes),
                0,
                built_at,
            )
        )
    return rows


def _source_signature(conn: sqlite3.Connection) -> str:
    from .player_prop_model import (
        COMPONENT_MODEL_VERSION,
        FEATURE_NAMES,
        MINUTES_FEATURE_NAMES,
        MODEL_VERSION,
        _model_fingerprint,
        _training_start_date,
    )

    payload = {
        "model_fingerprint": _model_fingerprint(conn),
        "model_version": MODEL_VERSION,
        "component_model_version": COMPONENT_MODEL_VERSION,
        "feature_names": list(FEATURE_NAMES),
        "minutes_feature_names": list(MINUTES_FEATURE_NAMES),
        "training_start_date": _training_start_date(),
        "db_version": PLAYER_PROP_TRAINING_DB_VERSION,
        "market_sample_curation_policy": MARKET_SAMPLE_CURATION_POLICY,
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    import hashlib
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def player_prop_training_db_signature(
    conn: sqlite3.Connection,
    *,
    allow_rebuild: bool = False,
) -> str:
    info = ensure_player_prop_training_db(conn, force=False, allow_rebuild=allow_rebuild)
    payload = {
        "path": str(info["path"]),
        "source_signature": str(info["source_signature"]),
        "db_version": PLAYER_PROP_TRAINING_DB_VERSION,
        "included_rows": int(info["included_rows"]),
        "candidate_rows": int(info["candidate_rows"]),
        "excluded_rows": int(info["excluded_rows"]),
        "built_at": str(info["built_at"]),
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    import hashlib
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()[:16]


def _apply_market_curation(
    market: str,
    sample_kind: str,
    samples: list[object],
    diagnostics: object,
) -> tuple[list[object], object, list[tuple[str, object]]]:
    from .player_prop_model import TrainingSampleDiagnostics

    kept_samples: list[object] = []
    excluded_samples: list[tuple[str, object]] = []
    for sample in samples:
        reason = _market_curation_reason(market, sample_kind, sample)
        if reason is None:
            kept_samples.append(sample)
            continue
        excluded_samples.append((reason, sample))

    updated_diagnostics = TrainingSampleDiagnostics(
        candidate_rows=int(getattr(diagnostics, "candidate_rows", 0) or 0),
        included_rows=len(kept_samples),
        skipped_before_training_start=int(getattr(diagnostics, "skipped_before_training_start", 0) or 0),
        skipped_missing_history_window=int(getattr(diagnostics, "skipped_missing_history_window", 0) or 0),
        skipped_incomplete_context=int(getattr(diagnostics, "skipped_incomplete_context", 0) or 0),
        skipped_ambiguous_team_identity=int(getattr(diagnostics, "skipped_ambiguous_team_identity", 0) or 0),
        skipped_missing_snapshot=int(getattr(diagnostics, "skipped_missing_snapshot", 0) or 0),
        skipped_market_curation=len(excluded_samples),
    )
    return kept_samples, updated_diagnostics, excluded_samples


def _market_curation_reason(market: str, sample_kind: str, sample: object) -> str | None:
    policy = MARKET_SAMPLE_CURATION_POLICY.get(str(market), {}).get(str(sample_kind), {})
    if not policy:
        return None
    if bool(policy.get("exclude_recent_transfer")) and bool(getattr(sample, "is_recent_transfer", False)):
        return "recent_transfer"
    min_sample_count = int(policy.get("min_sample_count") or 0)
    if min_sample_count > 0 and int(getattr(sample, "sample_count", 0) or 0) < min_sample_count:
        return "low_sample_count"
    min_avg_minutes = float(policy.get("min_avg_minutes") or 0.0)
    if min_avg_minutes > 0.0 and float(getattr(sample, "avg_minutes", 0.0) or 0.0) < min_avg_minutes:
        return "low_avg_minutes"
    return None


def _summarize_exclusion_rows(exclusions: list[tuple[str, object]]) -> dict[str, int]:
    summary: dict[str, int] = {}
    for reason, _sample in exclusions:
        summary[reason] = int(summary.get(reason, 0)) + 1
    return summary


def _exclusion_rows_to_insert(
    market: str,
    sample_kind: str,
    exclusions: list[tuple[str, object]],
    built_at: str,
) -> list[tuple[object, ...]]:
    rows: list[tuple[object, ...]] = []
    for reason, sample in exclusions:
        details = {
            "policy": MARKET_SAMPLE_CURATION_POLICY.get(str(market), {}).get(str(sample_kind), {}),
        }
        rows.append(
            (
                str(market),
                str(sample_kind),
                str(reason),
                int(getattr(sample, "source_prop_line_id", 0) or 0),
                int(getattr(sample, "source_player_id", 0) or 0),
                int(getattr(sample, "source_game_id", 0) or 0),
                str(getattr(sample, "game_date", "")),
                str(getattr(sample, "season", "")),
                str(getattr(sample, "segment", "")),
                int(getattr(sample, "sample_count", 0) or 0),
                float(getattr(sample, "avg_minutes", 0.0) or 0.0),
                1 if bool(getattr(sample, "is_recent_transfer", False)) else 0,
                json.dumps(details, sort_keys=True),
                built_at,
            )
        )
    return rows


def _nullable_int(value: object) -> int | None:
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
