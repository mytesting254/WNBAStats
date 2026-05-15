from __future__ import annotations

from backend.app import db


class FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
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
