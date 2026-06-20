from __future__ import annotations

import json

from backend.app.main import _cache_file_is_stale


def test_stale_payload_audit_prefers_captured_at_local_date(tmp_path) -> None:
    cache_file = tmp_path / "rotowire_lineups_raw.json"
    cache_file.write_text(
        json.dumps(
            {
                "source": "rotowire",
                "captured_at": "2026-06-20T00:30:00+00:00",
                "cache_date": "2026-06-20",
                "rows": [{"team": "NY", "player_name": "Test Player", "status": "OUT"}],
            }
        ),
        encoding="utf-8",
    )

    assert _cache_file_is_stale(cache_file, "2026-06-19") is False


def test_stale_payload_audit_falls_back_to_cache_date_without_captured_at(tmp_path) -> None:
    cache_file = tmp_path / "covers_props_raw.json"
    cache_file.write_text(
        json.dumps(
            {
                "source": "covers",
                "cache_date": "2026-06-18",
                "rows": [],
            }
        ),
        encoding="utf-8",
    )

    assert _cache_file_is_stale(cache_file, "2026-06-19") is True
