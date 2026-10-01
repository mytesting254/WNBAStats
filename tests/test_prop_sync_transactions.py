import sqlite3

import pytest

from backend.app import main


@pytest.fixture
def connections(tmp_path):
    path = tmp_path / "progress.sqlite"
    writer = sqlite3.connect(path, timeout=0.1)
    writer.execute("PRAGMA journal_mode=WAL")
    columns = (
        "scope status started_at finished_at stage stage_index stage_total "
        "current_count total_count percent message last_error last_result_json "
        "target_game_ids_json updated_at"
    ).split()
    writer.execute("CREATE TABLE prop_sync_jobs (id INTEGER PRIMARY KEY, " + ", ".join(columns) + ")")
    writer.execute("INSERT INTO prop_sync_jobs (id, status) VALUES (1, 'queued')")
    writer.commit()
    observer = sqlite3.connect(path, timeout=0.1)
    yield writer, observer
    writer.close()
    observer.close()


def test_progress_releases_writer_before_projection_work(connections):
    writer, observer = connections
    main._persist_prop_sync_job_snapshot({"job_id": 1, "status": "running"}, conn=writer)
    assert not writer.in_transaction
    assert observer.execute("SELECT status FROM prop_sync_jobs WHERE id=1").fetchone()[0] == "running"
    # Another pipeline/cache writer must work while projection calculation runs.
    observer.execute("BEGIN IMMEDIATE")
    observer.rollback()


def test_progress_preserves_existing_business_transaction(connections):
    writer, observer = connections
    writer.execute("UPDATE prop_sync_jobs SET message='pending business change' WHERE id=1")
    main._persist_prop_sync_job_snapshot({"job_id": 1, "status": "running"}, conn=writer)
    assert writer.in_transaction
    assert observer.execute("SELECT status FROM prop_sync_jobs WHERE id=1").fetchone()[0] == "queued"
    writer.rollback()
    assert writer.execute("SELECT message FROM prop_sync_jobs WHERE id=1").fetchone()[0] is None


def test_progress_failure_releases_owned_connection(monkeypatch):
    class FailingConnection:
        in_transaction = False
        rolled_back = False
        closed = False

        def execute(self, *args):
            raise sqlite3.OperationalError("database is locked")

        def rollback(self):
            self.rolled_back = True

        def close(self):
            self.closed = True

    conn = FailingConnection()
    monkeypatch.setattr(main, "connect", lambda: conn)
    main._persist_prop_sync_job_snapshot({"job_id": 1})
    assert conn.rolled_back
    assert conn.closed
