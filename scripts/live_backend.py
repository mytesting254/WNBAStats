#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sqlite3
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


def _docker_inspect(container: str) -> dict:
    proc = _run(["docker", "inspect", container])
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or f"docker inspect failed for {container}")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"docker inspect returned invalid JSON for {container}") from exc
    if not isinstance(payload, list) or not payload or not isinstance(payload[0], dict):
        raise RuntimeError(f"docker inspect returned no container data for {container}")
    return payload[0]


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


def _runtime_mount(container: str, destination: str = "/data") -> dict[str, str]:
    payload = _docker_inspect(container)
    mounts = payload.get("Mounts")
    if not isinstance(mounts, list):
        raise RuntimeError(f"docker inspect returned no mounts for {container}")
    for mount in mounts:
        if not isinstance(mount, dict):
            continue
        if str(mount.get("Destination") or "") != destination:
            continue
        source = str(mount.get("Source") or "")
        if not source:
            break
        return {
            "container": container,
            "destination": destination,
            "source": source,
            "type": str(mount.get("Type") or ""),
            "name": str(mount.get("Name") or ""),
        }
    raise RuntimeError(f"Could not find a mount for {destination} on {container}")


def _docker_exec(container: str, command: list[str], *, workdir: str = "/app") -> int:
    proc = subprocess.run(["docker", "exec", "-i", "-w", workdir, container, *command], cwd=ROOT, check=False)
    return int(proc.returncode)


def _db_summary(path: Path) -> dict[str, object]:
    result: dict[str, object] = {
        "path": str(path),
        "exists": path.exists(),
    }
    if not path.exists():
        return result

    stat = path.stat()
    result["size_bytes"] = int(stat.st_size)
    result["modified_at"] = stat.st_mtime

    try:
        conn = sqlite3.connect(path)
        try:
            result["table_count"] = int(
                conn.execute("SELECT COUNT(*) FROM sqlite_master WHERE type='table'").fetchone()[0]
            )
            result["latest_final_game_date"] = conn.execute(
                "SELECT MAX(game_date) FROM games WHERE status = 'final'"
            ).fetchone()[0]
            result["final_games"] = int(
                conn.execute("SELECT COUNT(*) FROM games WHERE status = 'final'").fetchone()[0]
            )
            result["latest_team_boxscore_game_date"] = conn.execute(
                "SELECT MAX(g.game_date) FROM team_game_boxscores t JOIN games g ON g.id = t.game_id"
            ).fetchone()[0]
            result["team_boxscore_rows"] = int(
                conn.execute("SELECT COUNT(*) FROM team_game_boxscores").fetchone()[0]
            )
            result["latest_team_results_game_date"] = conn.execute(
                "SELECT MAX(g.game_date) FROM team_game_results r JOIN games g ON g.id = r.game_id"
            ).fetchone()[0]
            result["team_results_rows"] = int(
                conn.execute("SELECT COUNT(*) FROM team_game_results").fetchone()[0]
            )
            job_runs_table = conn.execute(
                "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='job_runs'"
            ).fetchone()[0]
            if job_runs_table:
                row = conn.execute(
                    "SELECT COUNT(*), MAX(started_at), MAX(finished_at) FROM job_runs"
                ).fetchone()
                result["job_runs"] = int(row[0])
                result["latest_job_started"] = row[1]
                result["latest_job_finished"] = row[2]
        finally:
            conn.close()
    except sqlite3.Error as exc:
        result["error"] = str(exc)
    return result


def _comparison(label: str, candidate: dict[str, object], live: dict[str, object]) -> dict[str, object]:
    output: dict[str, object] = {
        "label": label,
        "path": candidate["path"],
        "exists": candidate["exists"],
    }
    if not candidate["exists"]:
        output["status"] = "missing"
        return output
    if candidate.get("error"):
        output["status"] = "error"
        output["error"] = candidate["error"]
        return output

    mismatches: list[str] = []
    for key in (
        "latest_final_game_date",
        "final_games",
        "latest_team_boxscore_game_date",
        "team_boxscore_rows",
        "latest_team_results_game_date",
        "team_results_rows",
    ):
        if candidate.get(key) != live.get(key):
            mismatches.append(key)

    if str(candidate["path"]) == str(live["path"]):
        output["status"] = "live"
    elif mismatches:
        output["status"] = "different"
        output["diff_keys"] = mismatches
    else:
        output["status"] = "matches_live"
    return output


def _cmd_container(args: argparse.Namespace) -> int:
    print(_select_container(args.container))
    return 0


def _cmd_exec(args: argparse.Namespace) -> int:
    command = list(args.command)
    if command and command[0] == "--":
        command = command[1:]
    if not command:
        raise RuntimeError("exec requires a command after --")
    container = _select_container(args.container)
    return _docker_exec(container, command, workdir=args.workdir)


def _cmd_runtime_info(args: argparse.Namespace) -> int:
    container = _select_container(args.container)
    mount = _runtime_mount(container)
    payload = {
        "note": "This is the active deployed backend runtime. Prefer these resolved paths over host /data or repo-local data/ paths.",
        "container": container,
        "git_head": _git_head()[0],
        "host_runtime_root": mount["source"],
        "container_runtime_root": mount["destination"],
        "mount_type": mount["type"],
        "mount_name": mount["name"],
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


def _cmd_host_runtime_info(args: argparse.Namespace) -> int:
    container = _select_container(args.container)
    mount = _runtime_mount(container)
    payload = {
        "note": "These host-side paths match the active backend container mount. Do not assume host /data/wnba.sqlite or repo-local data/wnba.sqlite is the live runtime DB.",
        "container": container,
        "git_head": _git_head()[0],
        "host_runtime_root": mount["source"],
        "container_runtime_root": mount["destination"],
        "db_path": f"{mount['source']}/wnba.sqlite",
        "cache_dir": f"{mount['source']}/cache",
        "snapshot_dir": f"{mount['source']}/snapshots",
        "mount_type": mount["type"],
        "mount_name": mount["name"],
    }
    print(json.dumps(payload, indent=2))
    return 0


def _cmd_host_env(args: argparse.Namespace) -> int:
    container = _select_container(args.container)
    mount = _runtime_mount(container)
    source = mount["source"]
    print(f"export WNBA_DATA_DIR={source}")
    print(f"export WNBA_DB_PATH={source}/wnba.sqlite")
    print(f"export WNBA_CACHE_DIR={source}/cache")
    print(f"export WNBA_SNAPSHOT_DIR={source}/snapshots")
    return 0


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


def _cmd_doctor(args: argparse.Namespace) -> int:
    container = _select_container(args.container)
    mount = _runtime_mount(container)
    live_db = Path(mount["source"]) / "wnba.sqlite"
    repo_local_db = ROOT / "data" / "wnba.sqlite"
    host_data_db = Path("/data/wnba.sqlite")

    live_summary = _db_summary(live_db)
    repo_summary = _db_summary(repo_local_db)
    host_summary = _db_summary(host_data_db)

    payload = {
        "note": "The active deployment source of truth is the mounted-volume DB behind the selected backend container.",
        "container": container,
        "git_head": _git_head()[0],
        "live_runtime": {
            "container_db_path": "/data/wnba.sqlite",
            "host_runtime_root": mount["source"],
            "mount_name": mount["name"],
            "mount_type": mount["type"],
            "summary": live_summary,
        },
        "comparisons": [
            _comparison("repo_local", repo_summary, live_summary),
            _comparison("host_data", host_summary, live_summary),
        ],
    }
    print(json.dumps(payload, indent=2))
    return 0


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run maintenance commands inside the active deployed backend container.")
    parser.add_argument("--container", help="Override auto-detection and target a specific container name.")
    subparsers = parser.add_subparsers(dest="command_name", required=True)

    container_parser = subparsers.add_parser("container", help="Print the detected backend container name.")
    container_parser.set_defaults(func=_cmd_container)

    info_parser = subparsers.add_parser("runtime-info", help="Print the detected runtime container and active DB/cache paths.")
    info_parser.set_defaults(func=_cmd_runtime_info)

    host_info_parser = subparsers.add_parser(
        "host-runtime-info",
        help="Print the detected live host-side runtime root and the DB/cache/snapshot paths that match the active backend container.",
    )
    host_info_parser.set_defaults(func=_cmd_host_runtime_info)

    host_env_parser = subparsers.add_parser(
        "host-env",
        help="Print shell export lines for host-side commands that should target the active backend runtime volume.",
    )
    host_env_parser.set_defaults(func=_cmd_host_env)

    doctor_parser = subparsers.add_parser(
        "doctor",
        help="Compare the active live runtime DB against repo-local data/wnba.sqlite and host /data/wnba.sqlite.",
    )
    doctor_parser.set_defaults(func=_cmd_doctor)

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
