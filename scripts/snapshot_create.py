from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.paths import get_db_path, get_snapshot_dir

DEFAULT_SNAPSHOT_BASENAME = "wnba-runtime"
ROW_COUNT_TABLES = (
    "games",
    "players",
    "player_game_stats",
    "sportsbook_prop_lines",
    "prop_lines",
    "prop_predictions",
    "settled_props",
    "model_runs",
)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_commit_sha() -> str | None:
    try:
        result = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            cwd=ROOT_DIR,
            check=True,
            capture_output=True,
            text=True,
        )
    except Exception:
        return None
    value = result.stdout.strip()
    return value or None


def _row_counts(conn: sqlite3.Connection) -> dict[str, int]:
    counts: dict[str, int] = {}
    for table in ROW_COUNT_TABLES:
        row = conn.execute(f"SELECT COUNT(*) AS c FROM {table}").fetchone()
        counts[table] = int(row["c"] if row else 0)
    return counts


def _prune_old_snapshots(output_dir: Path, keep_latest: int) -> None:
    if keep_latest <= 0:
        return
    snapshot_files = sorted(output_dir.glob("wnba-*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old_db in snapshot_files[keep_latest:]:
        old_manifest = old_db.with_suffix(".manifest.json")
        old_db.unlink(missing_ok=True)
        old_manifest.unlink(missing_ok=True)


def create_snapshot(
    db_path: Path,
    output_dir: Path,
    snapshot_name: str,
    device: str | None,
    keep_latest: int,
) -> tuple[Path, Path]:
    if not db_path.exists():
        raise FileNotFoundError(f"Database file not found: {db_path}")

    output_dir.mkdir(parents=True, exist_ok=True)
    snapshot_db = output_dir / f"{snapshot_name}.sqlite"
    snapshot_manifest = output_dir / f"{snapshot_name}.manifest.json"

    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        conn.execute("PRAGMA wal_checkpoint(FULL)")
        schema_version_row = conn.execute("PRAGMA user_version").fetchone()
        schema_version = int(schema_version_row[0] if schema_version_row else 0)
        counts = _row_counts(conn)
    finally:
        conn.close()

    shutil.copy2(db_path, snapshot_db)
    checksum = _sha256(snapshot_db)

    manifest = {
        "snapshot_file": snapshot_db.name,
        "created_at": datetime.now(timezone.utc).isoformat(),
        "schema_version": schema_version,
        "app_commit_sha": _git_commit_sha(),
        "source_device": device or os.getenv("HOSTNAME") or os.getenv("COMPUTERNAME"),
        "db_path_source": str(db_path),
        "row_counts": counts,
        "sha256": checksum,
    }
    snapshot_manifest.write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    _prune_old_snapshots(output_dir, keep_latest)
    return snapshot_db, snapshot_manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Create a local SQLite snapshot with manifest and checksum.")
    parser.add_argument("--db-path", default=str(get_db_path()))
    parser.add_argument("--output-dir", default=str(get_snapshot_dir()))
    parser.add_argument("--label", default=None, help="Optional label for a rolling snapshot filename (for example 'auto-watch' => wnba-auto-watch.sqlite).")
    parser.add_argument("--name", default=None, help="Exact rolling snapshot basename without extension.")
    parser.add_argument("--device", default=None, help="Optional source device identifier.")
    parser.add_argument(
        "--keep-latest",
        type=int,
        default=5,
        help="Keep only the most recent N snapshots (SQLite + manifest). Use 0 to disable pruning.",
    )
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    output_dir = Path(args.output_dir).expanduser().resolve()
    snapshot_name = str(args.name or "").strip()
    if not snapshot_name:
        label = str(args.label or "").strip()
        snapshot_name = f"wnba-{label}" if label else DEFAULT_SNAPSHOT_BASENAME
    snapshot_db, snapshot_manifest = create_snapshot(
        db_path, output_dir, snapshot_name, args.device, args.keep_latest
    )
    print(f"Created snapshot DB: {snapshot_db}")
    print(f"Created manifest:   {snapshot_manifest}")


if __name__ == "__main__":
    main()
