#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _run(cmd: list[str], *, capture: bool = True) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        cwd=ROOT,
        text=True,
        capture_output=capture,
        check=False,
    )


def _git_head() -> tuple[str, str]:
    proc = _run(["git", "rev-parse", "HEAD"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "Unable to read git HEAD")
    full = proc.stdout.strip()
    return full, full[:7]


def _docker_ps() -> list[dict[str, str]]:
    proc = _run(["docker", "ps", "--format", "{{.Names}}\t{{.Image}}\t{{.Status}}"])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "docker ps failed")
    rows: list[dict[str, str]] = []
    for line in proc.stdout.splitlines():
        name, image, status = (line.split("\t", 2) + ["", ""])[:3]
        rows.append({"name": name, "image": image, "status": status})
    return rows


def _backend_candidates() -> list[dict[str, str]]:
    rows = _docker_ps()
    candidates: list[dict[str, str]] = []
    for row in rows:
        name = row["name"]
        image = row["image"]
        if name.startswith("backend-") or name == "wnbastats-backend-1" or "_backend:" in image:
            candidates.append(row)
    return candidates


def _select_container(explicit: str | None = None) -> str:
    if explicit:
        return explicit

    full_head, short_head = _git_head()
    candidates = _backend_candidates()
    if not candidates:
        raise RuntimeError("No running backend containers found")

    def score(row: dict[str, str]) -> tuple[int, int]:
        score_value = 0
        image = row["image"]
        status = row["status"].lower()
        if full_head in image or short_head in image:
            score_value += 100
        if "healthy" in status:
            score_value += 20
        if row["name"].startswith("backend-"):
            score_value += 5
        return score_value, -candidates.index(row)

    best = max(candidates, key=score)
    if score(best)[0] <= 0 and len(candidates) > 1:
        names = ", ".join(row["name"] for row in candidates)
        raise RuntimeError(
            "Could not confidently identify the active backend container for this repo. "
            f"Candidates: {names}. Pass --container explicitly."
        )
    return best["name"]


def _docker_exec(container: str, command: list[str], *, workdir: str = "/app") -> int:
    proc = subprocess.run(["docker", "exec", "-i", "-w", workdir, container, *command], cwd=ROOT, check=False)
    return int(proc.returncode)


def _cmd_container(args: argparse.Namespace) -> int:
    print(_select_container(args.container))
    return 0


def _cmd_exec(args: argparse.Namespace) -> int:
    if not args.command:
        raise RuntimeError("exec requires a command after --")
    container = _select_container(args.container)
    return _docker_exec(container, args.command, workdir=args.workdir)


def _cmd_runtime_info(args: argparse.Namespace) -> int:
    container = _select_container(args.container)
    payload = {
        "container": container,
        "git_head": _git_head()[0],
    }
    print(json.dumps(payload, indent=2))
    return _docker_exec(
        container,
        [
            "python",
            "-c",
            (
                "import json, sqlite3; "
                "from backend.app.paths import get_cache_dir, get_db_path, get_snapshot_dir; "
                "db_path = get_db_path(); "
                "conn = sqlite3.connect(db_path); "
                "tables = conn.execute(\"SELECT COUNT(*) FROM sqlite_master WHERE type='table'\").fetchone()[0]; "
                "print(json.dumps({"
                "'db_path': str(db_path), "
                "'cache_dir': str(get_cache_dir()), "
                "'snapshot_dir': str(get_snapshot_dir()), "
                "'table_count': int(tables)"
                "}, indent=2))"
            ),
        ],
    )


def _cmd_recalculate(args: argparse.Namespace) -> int:
    container = _select_container(args.container)
    return _docker_exec(
        container,
        [
            "python",
            "-c",
            (
                "from fastapi import Response; "
                "from backend.app.main import recalculate; "
                "response = Response(); "
                "result = recalculate(response); "
                "print(result); "
                "print(dict(response.headers))"
            ),
        ],
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run maintenance commands inside the active deployed backend container.")
    parser.add_argument("--container", help="Override auto-detection and target a specific container name.")
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    container_parser = subparsers.add_parser("container", help="Print the detected backend container name.")
    container_parser.set_defaults(func=_cmd_container)

    info_parser = subparsers.add_parser("runtime-info", help="Print the detected runtime container and active DB/cache paths.")
    info_parser.set_defaults(func=_cmd_runtime_info)

    recalc_parser = subparsers.add_parser("recalculate", help="Run the backend recalculate path inside the active container.")
    recalc_parser.set_defaults(func=_cmd_recalculate)

    exec_parser = subparsers.add_parser("exec", help="Run an arbitrary command inside the active backend container.")
    exec_parser.add_argument("--workdir", default="/app", help="Container working directory. Default: /app")
    exec_parser.add_argument("command", nargs=argparse.REMAINDER, help="Command to run inside the container.")
    exec_parser.set_defaults(func=_cmd_exec)
    return parser


def main() -> int:
    parser = _build_parser()
    args = parser.parse_args()
    try:
        return int(args.func(args))
    except RuntimeError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
