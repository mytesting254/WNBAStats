from __future__ import annotations

from backend.app import db


class FakeResponse:
    def __init__(self, results: list[dict] | None = None):
        self._results = results

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        if self._results is not None:
            return {"results": self._results}
        return {
            "results": [
                {
                    "type": "ok",
                    "response": {
                        "result": {
                            "cols": [{"name": "id"}, {"name": "name"}],
                            "rows": [
                                [
                                    {"type": "integer", "value": "7"},
                                    {"type": "text", "value": "NY"},
                                ]
                            ],
                            "last_insert_rowid": "7",
                        }
                    },
                }
            ]
        }


def test_turso_http_connection_executes_parameterized_query(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        return FakeResponse()

    monkeypatch.setattr(db.requests, "post", fake_post)

    conn = db.TursoHttpConnection("libsql://wnba-test-org.turso.io", "test-token")
    cursor = conn.execute("SELECT * FROM teams WHERE id = ? AND spread = ?", (7, -8.0))
    row = cursor.fetchone()

    assert calls[0][0] == "https://wnba-test-org.turso.io/v2/pipeline"
    assert calls[0][1]["json"]["requests"][0]["stmt"]["args"] == [
        {"type": "integer", "value": "7"},
        {"type": "float", "value": -8.0},
    ]
    assert row["id"] == 7
    assert dict(row) == {"id": 7, "name": "NY"}
    assert cursor.lastrowid == 7


def test_turso_http_connection_batches_executemany(monkeypatch):
    calls = []

    def fake_post(url, **kwargs):
        calls.append((url, kwargs))
        execute_count = sum(1 for request in kwargs["json"]["requests"] if request["type"] == "execute")
        return FakeResponse(
            [
                {
                    "type": "ok",
                    "response": {
                        "result": {
                            "cols": [],
                            "rows": [],
                            "last_insert_rowid": str(index + 1),
                        }
                    },
                }
                for index in range(execute_count)
            ]
            + [{"type": "ok", "response": {"result": {"cols": [], "rows": []}}}]
        )

    monkeypatch.setattr(db.requests, "post", fake_post)

    conn = db.TursoHttpConnection("libsql://wnba-test-org.turso.io", "test-token")
    conn.EXECUTEMANY_BATCH_SIZE = 10
    cursor = conn.executemany("INSERT INTO teams (id, name) VALUES (?, ?)", [(idx, f"T{idx}") for idx in range(25)])

    assert len(calls) == 3
    assert [sum(1 for request in call[1]["json"]["requests"] if request["type"] == "execute") for call in calls] == [10, 10, 5]
    assert calls[0][1]["json"]["requests"][0]["stmt"]["args"] == [
        {"type": "integer", "value": "0"},
        {"type": "text", "value": "T0"},
    ]
    assert cursor.lastrowid == 5
