import sqlite3
from contextlib import contextmanager
from types import SimpleNamespace

import pytest

from backend.app import covers_import as covers


def test_covers_releases_writer_before_next_http_fetch(tmp_path, monkeypatch):
    path = tmp_path / "covers.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE writes (event_id TEXT)")
        conn.commit()
        games = [covers.CoversGame(str(i), f"https://test/{i}/odds") for i in range(2)]
        fetches = []

        def fetch(url):
            # A second writer must be able to proceed during every HTTP request.
            with sqlite3.connect(path, timeout=0) as other:
                other.execute("INSERT INTO writes VALUES ('http')")
            fetches.append(url)
            return "page"

        def metadata(conn, game, page, fallback_page=None):
            conn.execute("INSERT INTO writes VALUES (?)", (game.event_id,))
            return covers.CoversMetadata(game.event_id, None, "2026-09-30", "2026-09-30T20:00:00Z", "IND", "NY")

        monkeypatch.setattr(covers, "covers_matchup_links", lambda _: games)
        monkeypatch.setattr(covers, "_fetch_odds_board", lambda _: {})
        monkeypatch.setattr(covers, "_fetch_text", fetch)
        monkeypatch.setattr(covers, "_fetch_market_fragments", lambda _: [])
        monkeypatch.setattr(covers, "_metadata_from_page", metadata)
        monkeypatch.setattr(covers, "_event_rows", lambda *args: [])
        monkeypatch.setattr(covers, "read_json_cache", lambda _: None)
        monkeypatch.setattr(covers, "write_json_cache", lambda *args: None)

        result = covers._import_covers_provider_rows(conn, force_refresh=True)

        assert result["status"] == "imported_game_markets_only"
        assert result["events"] == 2
        assert len(fetches) == 4
        assert not conn.in_transaction


def test_covers_retries_external_writer_before_resolving_metadata(tmp_path, monkeypatch):
    path = tmp_path / "covers.sqlite"
    conn = sqlite3.connect(path, timeout=0)
    other = sqlite3.connect(path)
    try:
        conn.execute("CREATE TABLE writes (value TEXT)")
        conn.commit()
        other.execute("INSERT INTO writes VALUES ('external')")
        delays = []

        def release(seconds):
            delays.append(seconds)
            other.commit()

        monkeypatch.setattr(covers.time, "sleep", release)
        covers._covers_db_write(conn, lambda: conn.execute("INSERT INTO writes VALUES ('covers')"))

        assert delays == [2.0]
        assert conn.execute("SELECT value FROM writes ORDER BY rowid").fetchall() == [("external",), ("covers",)]
        assert not conn.in_transaction
    finally:
        conn.close()
        other.close()


@pytest.mark.parametrize("error", ["database is locked", "database table is locked", "database schema is locked"])
def test_covers_rolls_back_partial_writes_before_retry(error, monkeypatch):
    with sqlite3.connect(":memory:") as conn:
        conn.execute("CREATE TABLE writes (value TEXT)")
        attempts = []
        monkeypatch.setattr(covers.time, "sleep", lambda _: None)

        def write():
            attempts.append(1)
            conn.execute("INSERT INTO writes VALUES ('covers')")
            if len(attempts) == 1:
                raise sqlite3.OperationalError(error)

        covers._covers_db_write(conn, write)
        assert len(attempts) == 2
        assert conn.execute("SELECT COUNT(*) FROM writes").fetchone()[0] == 1


@pytest.mark.parametrize("error, expected_attempts", [("database is locked", 4), ("no such table: missing", 1)])
def test_covers_limits_retries_and_propagates_db_errors(error, expected_attempts, monkeypatch):
    with sqlite3.connect(":memory:") as conn:
        attempts = []
        monkeypatch.setattr(covers.time, "sleep", lambda _: None)

        def write():
            attempts.append(1)
            raise sqlite3.OperationalError(error)

        with pytest.raises(sqlite3.OperationalError, match=error):
            covers._covers_db_write(conn, write)
        assert len(attempts) == expected_attempts
        assert not conn.in_transaction


def test_covers_worker_commits_sync_before_separate_audit_write(tmp_path, monkeypatch):
    from backend.app import main

    path = tmp_path / "covers.sqlite"
    with sqlite3.connect(path) as conn:
        conn.execute("CREATE TABLE writes (value TEXT)")

    @contextmanager
    def connect():
        conn = sqlite3.connect(path, timeout=0)
        try:
            with conn:
                yield conn
        finally:
            conn.close()

    class ImmediateThread:
        def __init__(self, *, target, **kwargs):
            self.target = target

        def start(self):
            self.target()

    events = []
    finished = []

    def audit(job_id, event, message, **kwargs):
        with connect() as conn:
            conn.execute("INSERT INTO writes VALUES (?)", (event,))
        events.append(event)

    def pipeline(conn, **kwargs):
        conn.execute("INSERT INTO writes VALUES ('sync')")
        return SimpleNamespace(target_game_ids=[1], scanned_props=1, synced_props=1,
                               changed_prop_line_ids=[1], attempted_predictions=1,
                               rebuilt_predictions=1, skipped_predictions=0, dfs_snapshot=None)

    def progress(*, conn=None, **kwargs):
        if conn is not None:
            conn.execute("INSERT INTO writes VALUES ('progress')")

    monkeypatch.setattr(main, "connect", connect)
    monkeypatch.setattr(main.threading, "Thread", ImmediateThread)
    monkeypatch.setattr(main, "_current_prop_sync_state", lambda: {"running": False})
    monkeypatch.setattr(main, "_begin_prop_sync_job", lambda *args, **kwargs: ("now", 1))
    monkeypatch.setattr(main, "_create_job_run", lambda *args, **kwargs: 1)
    monkeypatch.setattr(main, "_append_job_run_event", audit)
    monkeypatch.setattr(main, "_set_prop_sync_progress", progress)
    monkeypatch.setattr(main, "_mutate_prop_sync_state", lambda **kwargs: None)
    monkeypatch.setattr(main, "_finish_job_run", lambda _, **kwargs: finished.append(kwargs))
    monkeypatch.setattr(main, "_import_covers_provider_rows", lambda *args, **kwargs: {"prop_sync_eligible": True, "target_game_ids": [1]})
    monkeypatch.setattr(main, "run_prop_sync_pipeline", pipeline)
    monkeypatch.setattr(main, "_invalidate_read_caches", lambda: None)
    monkeypatch.setattr(main, "run_post_pipeline_steps", lambda **kwargs: SimpleNamespace(published_payloads={}))

    assert main._start_covers_refresh_if_needed(selected_date=None)
    assert finished[-1]["status"] == "completed"
    assert "pipeline.sync.done" in events
    assert "job.failed" not in events
