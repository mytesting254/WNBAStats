#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import shutil
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODEL_CACHE_PREFIX = "learned_prop_model"


def _db_marker(db_path: str) -> str:
    return hashlib.sha1(str(db_path).encode("utf-8")).hexdigest()[:12]


def _processed_name(path: Path, target_marker: str) -> str | None:
    if path.suffix != ".json":
        return None
    parts = path.stem.split("-")
    if len(parts) < 7 or parts[0] != MODEL_CACHE_PREFIX:
        return None
    parts[-4] = target_marker
    return "-".join(parts) + path.suffix


def main() -> int:
    parser = argparse.ArgumentParser(description="Rewrite WNBA learned model cache filenames for a target live DB path.")
    parser.add_argument("--source-cache-dir", type=Path, default=ROOT / "data" / "cache")
    parser.add_argument("--output-cache-dir", type=Path, default=ROOT / "data" / "processed_model_cache")
    parser.add_argument("--target-db-path", required=True, help="Live backend WNBA_DB_PATH value, for example /data/wnba.sqlite.")
    parser.add_argument("--clean", action="store_true", help="Delete existing processed learned model cache files first.")
    args = parser.parse_args()

    source_dir = args.source_cache_dir.resolve()
    output_dir = args.output_cache_dir.resolve()
    if not source_dir.exists():
        raise FileNotFoundError(f"Source cache directory not found: {source_dir}")

    output_dir.mkdir(parents=True, exist_ok=True)
    if args.clean:
        for existing in output_dir.glob(f"{MODEL_CACHE_PREFIX}-*.json"):
            existing.unlink()

    target_marker = _db_marker(args.target_db_path)
    processed = 0
    for path in sorted(source_dir.glob(f"{MODEL_CACHE_PREFIX}-*.json")):
        target_name = _processed_name(path, target_marker)
        if target_name is None:
            continue
        shutil.copy2(path, output_dir / target_name)
        processed += 1

    print(f"Processed {processed} WNBA model cache file(s) into {output_dir}")
    if processed == 0:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
