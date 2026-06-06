from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from backend.app.paths import get_db_path, get_snapshot_dir


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _schema_version(path: Path) -> int:
    conn = sqlite3.connect(path)
    try:
        row = conn.execute("PRAGMA user_version").fetchone()
        return int(row[0] if row else 0)
    finally:
        conn.close()


def _load_manifest(manifest_path: Path) -> dict:
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("Snapshot manifest must be a JSON object.")
    return payload


def restore_snapshot(db_path: Path, snapshot_db: Path, manifest_path: Path, force: bool = False) -> Path:
    if not snapshot_db.exists():
        raise FileNotFoundError(f"Snapshot DB not found: {snapshot_db}")
    if not manifest_path.exists():
        raise FileNotFoundError(f"Snapshot manifest not found: {manifest_path}")

    manifest = _load_manifest(manifest_path)
    expected_sha = str(manifest.get("sha256") or "").strip().lower()
    actual_sha = _sha256(snapshot_db).lower()
    if expected_sha and expected_sha != actual_sha:
        raise RuntimeError("Snapshot checksum mismatch. Refusing restore.")

    snapshot_schema = _schema_version(snapshot_db)
    current_schema = _schema_version(db_path) if db_path.exists() else snapshot_schema
    manifest_schema = manifest.get("schema_version")
    if manifest_schema is not None and int(manifest_schema) != snapshot_schema:
        raise RuntimeError("Manifest schema_version does not match snapshot DB schema version.")
    if not force and current_schema != snapshot_schema:
        raise RuntimeError(
            f"Schema mismatch: current={current_schema}, snapshot={snapshot_schema}. "
            "Use --force to override."
        )

    db_path.parent.mkdir(parents=True, exist_ok=True)
    if db_path.exists():
        backup = db_path.with_name(f"{db_path.stem}.backup-{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}{db_path.suffix}")
        shutil.copy2(db_path, backup)
    else:
        backup = db_path.with_name(f"{db_path.stem}.backup-none")

    wal = db_path.with_suffix(db_path.suffix + "-wal")
    shm = db_path.with_suffix(db_path.suffix + "-shm")
    if wal.exists():
        wal.unlink()
    if shm.exists():
        shm.unlink()

    temp_path = db_path.with_name(f"{db_path.name}.restore-tmp")
    shutil.copy2(snapshot_db, temp_path)
    os.replace(temp_path, db_path)
    return backup


def _resolve_snapshot_paths(snapshot: str, snapshot_dir: Path) -> tuple[Path, Path]:
    snapshot_path = Path(snapshot)
    if not snapshot_path.is_absolute():
        snapshot_path = (snapshot_dir / snapshot_path).resolve()
    manifest = snapshot_path.with_suffix(".manifest.json")
    return snapshot_path, manifest


def main() -> None:
    parser = argparse.ArgumentParser(description="Restore local SQLite DB from a snapshot with checksum/schema validation.")
    parser.add_argument("snapshot", help="Snapshot .sqlite file path (absolute or relative to --snapshot-dir).")
    parser.add_argument("--db-path", default=str(get_db_path()))
    parser.add_argument("--snapshot-dir", default=str(get_snapshot_dir()))
    parser.add_argument("--force", action="store_true", help="Allow schema-version mismatch restore.")
    args = parser.parse_args()

    db_path = Path(args.db_path).expanduser().resolve()
    snapshot_dir = Path(args.snapshot_dir).expanduser().resolve()
    snapshot_db, manifest_path = _resolve_snapshot_paths(args.snapshot, snapshot_dir)
    backup = restore_snapshot(db_path, snapshot_db, manifest_path, force=args.force)
    print(f"Restored DB from: {snapshot_db}")
    print(f"Manifest used:    {manifest_path}")
    print(f"Backup created:   {backup}")


if __name__ == "__main__":
    main()
