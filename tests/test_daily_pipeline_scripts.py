from __future__ import annotations

import json
from pathlib import Path
import subprocess


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "live_daily_props.sh"


def _training_change_count(payload: dict, kind: str) -> int:
    command = f'source "{SCRIPT}"; training_change_count {kind}'
    result = subprocess.run(
        ["bash", "-c", command],
        input=json.dumps(payload),
        text=True,
        capture_output=True,
        check=True,
    )
    return int(result.stdout.strip())


def test_break_day_payload_skips_training() -> None:
    payload = {
        "scoreboards": [{"inserted_team_game_results": 0}],
        "player_stats": [{"inserted_player_game_stats": 0}],
        "settlements": {"settled": 0, "repaired": 0},
        "game_settlements": {"settled": 0},
    }

    assert _training_change_count(payload, "history") == 0
    script = SCRIPT.read_text(encoding="utf-8")
    assert "no new completed-game, player-stat, or settlement data; skipping model training" in script
    assert "/api/models/train?include_dfs=false&disposable_process=true" in script


def test_new_player_stats_trigger_training() -> None:
    payload = {
        "scoreboards": [],
        "player_stats": [{"inserted_player_game_stats": 21}],
        "settlements": {"settled": 0},
    }

    assert _training_change_count(payload, "history") == 21


def test_new_settlements_trigger_training() -> None:
    payload = {
        "props": {"settled": 14, "repaired": 2},
        "games": {"settled": 1},
        "special": {"settled": 3},
        "dfs": {"settled": 4},
    }

    assert _training_change_count(payload, "settlement") == 24
