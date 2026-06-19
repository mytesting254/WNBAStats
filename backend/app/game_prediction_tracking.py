from __future__ import annotations

import sqlite3
from datetime import datetime, timezone
from typing import Mapping


MODEL_VERSION = "component-game-v2"


def save_game_prediction(conn: sqlite3.Connection, game: Mapping, prediction: Mapping) -> int:
    prediction_time = datetime.now(timezone.utc).isoformat()
    conn.execute(
        """
        INSERT INTO game_predictions (
            game_id, model_version, prediction_time,
            home_projected_points, away_projected_points, projected_margin, projected_total,
            winner_pick, ats_pick, ats_edge, total_pick, total_edge,
            confidence, reason, spread_home, game_total, home_rest_days, away_rest_days
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(game_id, model_version) DO UPDATE SET
            prediction_time = excluded.prediction_time,
            home_projected_points = excluded.home_projected_points,
            away_projected_points = excluded.away_projected_points,
            projected_margin = excluded.projected_margin,
            projected_total = excluded.projected_total,
            winner_pick = excluded.winner_pick,
            ats_pick = excluded.ats_pick,
            ats_edge = excluded.ats_edge,
            total_pick = excluded.total_pick,
            total_edge = excluded.total_edge,
            confidence = excluded.confidence,
            reason = excluded.reason,
            spread_home = excluded.spread_home,
            game_total = excluded.game_total,
            home_rest_days = excluded.home_rest_days,
            away_rest_days = excluded.away_rest_days
        """,
        (
            int(game["id"]),
            MODEL_VERSION,
            prediction_time,
            prediction.get("home_projected_points"),
            prediction.get("away_projected_points"),
            prediction.get("projected_margin"),
            prediction.get("projected_total"),
            str(prediction.get("winner_pick") or "N/A"),
            str(prediction.get("ats_pick") or "N/A"),
            prediction.get("ats_edge"),
            str(prediction.get("total_pick") or "N/A"),
            prediction.get("total_edge"),
            str(prediction.get("game_confidence") or "unknown"),
            str(prediction.get("game_reason") or ""),
            _value(game, "spread_home"),
            _value(game, "game_total"),
            _value(game, "rest_days_home"),
            _value(game, "rest_days_away"),
        ),
    )
    row = conn.execute(
        "SELECT id FROM game_predictions WHERE game_id = ? AND model_version = ?",
        (int(game["id"]), MODEL_VERSION),
    ).fetchone()
    return int(row["id"])


def settle_completed_game_predictions(
    conn: sqlite3.Connection,
    *,
    selected_date: str | None = None,
    selected_dates: list[str] | None = None,
) -> dict:
    target_dates = _normalized_dates(selected_date, selected_dates)
    date_filter = ""
    params: tuple[str, ...] = ()
    if target_dates:
        placeholders = ",".join("?" for _ in target_dates)
        date_filter = f" AND g.game_date IN ({placeholders})"
        params = tuple(target_dates)
    rows = conn.execute(
        """
        SELECT
            gp.*,
            g.home_team_id,
            g.away_team_id,
            home.abbreviation AS home_team,
            away.abbreviation AS away_team,
            home_result.points AS home_score,
            away_result.points AS away_score
        FROM game_predictions gp
        JOIN games g ON g.id = gp.game_id
        JOIN teams home ON home.id = g.home_team_id
        JOIN teams away ON away.id = g.away_team_id
        JOIN team_game_results home_result ON home_result.game_id = g.id AND home_result.team_id = g.home_team_id
        JOIN team_game_results away_result ON away_result.game_id = g.id AND away_result.team_id = g.away_team_id
        WHERE g.status = 'final'
        """
        + date_filter
        + """
          AND NOT EXISTS (
              SELECT 1
              FROM settled_game_predictions settled
              WHERE settled.game_prediction_id = gp.id
          )
        ORDER BY g.game_date, gp.id
        """,
        params,
    ).fetchall()

    settled_at = datetime.now(timezone.utc).isoformat()
    settlements = []
    for row in rows:
        home_score = int(row["home_score"])
        away_score = int(row["away_score"])
        actual_margin = float(home_score - away_score)
        actual_total = float(home_score + away_score)
        actual_winner = row["home_team"] if home_score >= away_score else row["away_team"]

        actual_ats_pick = _actual_ats_pick(row, actual_margin)
        actual_total_result = _actual_total_result(row, actual_total)
        winner_correct = int(str(row["winner_pick"]) == actual_winner)
        ats_correct = _pick_correct(str(row["ats_pick"]), actual_ats_pick)
        total_correct = _pick_correct(str(row["total_pick"]), actual_total_result)

        settlements.append(
            (
                row["id"],
                row["game_id"],
                home_score,
                away_score,
                actual_winner,
                actual_margin,
                actual_total,
                actual_ats_pick,
                actual_total_result,
                winner_correct,
                ats_correct,
                total_correct,
                settled_at,
            )
        )

    conn.executemany(
        """
        INSERT INTO settled_game_predictions (
            game_prediction_id, game_id, home_score, away_score,
            actual_winner, actual_margin, actual_total,
            actual_ats_pick, actual_total_result,
            winner_correct, ats_correct, total_correct, settled_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        settlements,
    )
    conn.commit()
    return {
        "eligible": len(rows),
        "settled": len(settlements),
        "settled_at": settled_at,
        "selected_date": target_dates[0] if len(target_dates) == 1 else None,
        "selected_dates": target_dates,
    }


def _actual_ats_pick(row, actual_margin: float) -> str | None:
    if row["spread_home"] is None:
        return None
    spread_home = float(row["spread_home"])
    spread_margin = actual_margin + spread_home
    if spread_margin == 0:
        return "push"
    return row["home_team"] if spread_margin > 0 else row["away_team"]


def _actual_total_result(row, actual_total: float) -> str | None:
    if row["game_total"] is None or float(row["game_total"]) <= 0:
        return None
    game_total = float(row["game_total"])
    if actual_total == game_total:
        return "push"
    return "Over" if actual_total > game_total else "Under"


def _pick_correct(predicted: str, actual: str | None) -> int | None:
    if actual is None or predicted == "N/A":
        return None
    if actual == "push":
        return int(predicted.lower() == "push")
    return int(predicted.split(" ", 1)[0] == actual)


def _value(row: Mapping, key: str):
    try:
        return row[key]
    except (KeyError, IndexError):
        return None


def _normalized_dates(selected_date: str | None, selected_dates: list[str] | None) -> list[str]:
    values = [selected_date] if selected_date else []
    values.extend(selected_dates or [])
    cleaned = sorted({str(value).strip() for value in values if str(value).strip()})
    return cleaned
