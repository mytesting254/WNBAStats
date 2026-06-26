from __future__ import annotations

import re
import sqlite3
import unicodedata
from typing import Any

from .bootstrap import normalize_team_abbreviation


def normalize_player_lookup_name(name: str | None) -> str:
    raw = str(name or "").strip()
    if not raw:
        return ""
    ascii_name = (
        unicodedata.normalize("NFKD", raw)
        .encode("ascii", "ignore")
        .decode("ascii")
        .lower()
    )
    return re.sub(r"[^a-z0-9]+", " ", ascii_name).strip()


def player_lookup_parts(name: str | None) -> tuple[str, str | None, str | None]:
    normalized = normalize_player_lookup_name(name)
    if not normalized:
        return "", None, None
    parts = normalized.split()
    if not parts:
        return normalized, None, None
    return normalized, (parts[0][0] if parts[0] else None), parts[-1]


def player_is_skeletal(player: dict[str, Any] | None) -> bool:
    if not player:
        return True
    position = str(player.get("position") or "").strip()
    role = str(player.get("rotation_role") or "").strip()
    return not position or not role


def _player_rows(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    return conn.execute(
        """
        SELECT
            p.id AS player_id,
            p.full_name,
            p.team_id,
            t.abbreviation AS team_abbreviation,
            p.rotation_role,
            p.position
        FROM players p
        LEFT JOIN teams t ON t.id = p.team_id
        """
    ).fetchall()


def _team_score_for_candidates(
    conn: sqlite3.Connection,
    candidates: list[sqlite3.Row],
    preferred_team: str | None,
) -> dict[int, tuple[int, float, int]]:
    scores = {int(candidate["player_id"]): (0, 0.0, 0) for candidate in candidates}
    if not preferred_team or not candidates:
        return scores
    candidate_ids = [int(candidate["player_id"]) for candidate in candidates]
    placeholders = ",".join("?" for _ in candidate_ids)
    history_rows = conn.execute(
        f"""
        SELECT
            h.player_id,
            COUNT(*) AS hits,
            MAX(h.confidence) AS best_confidence
        FROM player_team_history h
        JOIN teams t ON t.id = h.team_id
        WHERE upper(t.abbreviation) = ?
          AND h.player_id IN ({placeholders})
        GROUP BY h.player_id
        """,
        [preferred_team, *candidate_ids],
    ).fetchall()
    history_by_player = {
        int(row["player_id"]): (int(row["hits"] or 0), float(row["best_confidence"] or 0.0))
        for row in history_rows
    }
    for candidate in candidates:
        player_id = int(candidate["player_id"])
        current_team = str(candidate["team_abbreviation"] or "").upper()
        hits, confidence = history_by_player.get(player_id, (0, 0.0))
        current_bonus = 1 if current_team == preferred_team else 0
        scores[player_id] = (current_bonus, confidence, hits)
    return scores


def _pick_best_candidate(
    conn: sqlite3.Connection,
    candidates: list[sqlite3.Row],
    *,
    preferred_team: str | None,
    prefer_rich: bool = True,
) -> dict[str, Any] | None:
    if not candidates:
        return None
    pool = candidates
    if prefer_rich:
        rich = [candidate for candidate in candidates if not player_is_skeletal(dict(candidate))]
        if rich:
            pool = rich
    if len(pool) == 1:
        return dict(pool[0])

    team_scores = _team_score_for_candidates(conn, pool, preferred_team)
    ranked = sorted(
        pool,
        key=lambda candidate: team_scores.get(int(candidate["player_id"]), (0, 0.0, 0)),
        reverse=True,
    )
    best = team_scores.get(int(ranked[0]["player_id"]), (0, 0.0, 0))
    second = team_scores.get(int(ranked[1]["player_id"]), (0, 0.0, 0)) if len(ranked) > 1 else None
    if second is None or best > second:
        return dict(ranked[0])
    return None


def resolve_player_identity(
    conn: sqlite3.Connection,
    team_abbreviation: str,
    player_name: str,
    *,
    exclude_player_id: int | None = None,
    prefer_rich: bool = True,
) -> dict[str, Any] | None:
    normalized_team = normalize_team_abbreviation(team_abbreviation) or team_abbreviation.upper()
    normalized_name, first_initial, last_name = player_lookup_parts(player_name)
    rows = _player_rows(conn)
    filtered = [
        row for row in rows
        if exclude_player_id is None or int(row["player_id"]) != int(exclude_player_id)
    ]

    exact_team = [
        row for row in filtered
        if str(row["team_abbreviation"] or "").upper() == normalized_team
        and str(row["full_name"] or "").strip().lower() == str(player_name or "").strip().lower()
    ]
    candidate = _pick_best_candidate(conn, exact_team, preferred_team=normalized_team, prefer_rich=prefer_rich)
    if candidate:
        return candidate

    team_matches: list[sqlite3.Row] = []
    if normalized_name:
        for row in filtered:
            if str(row["team_abbreviation"] or "").upper() != normalized_team:
                continue
            candidate_name, candidate_initial, candidate_last = player_lookup_parts(row["full_name"])
            if not candidate_name:
                continue
            if candidate_name == normalized_name:
                team_matches.append(row)
                continue
            if first_initial and last_name and candidate_initial == first_initial and candidate_last == last_name:
                team_matches.append(row)
    candidate = _pick_best_candidate(conn, team_matches, preferred_team=normalized_team, prefer_rich=prefer_rich)
    if candidate:
        return candidate

    global_matches: list[sqlite3.Row] = []
    if normalized_name:
        for row in filtered:
            candidate_name, candidate_initial, candidate_last = player_lookup_parts(row["full_name"])
            if not candidate_name:
                continue
            if candidate_name == normalized_name:
                global_matches.append(row)
                continue
            if first_initial and last_name and candidate_initial == first_initial and candidate_last == last_name:
                global_matches.append(row)
    return _pick_best_candidate(conn, global_matches, preferred_team=normalized_team, prefer_rich=prefer_rich)


def repair_shadow_player_identities(conn: sqlite3.Connection) -> dict[str, int]:
    shadow_rows = conn.execute(
        """
        SELECT
            p.id AS player_id,
            p.full_name,
            p.team_id,
            t.abbreviation AS team_abbreviation,
            p.rotation_role,
            p.position
        FROM players p
        JOIN teams t ON t.id = p.team_id
        WHERE (p.position IS NULL OR trim(p.position) = '' OR p.rotation_role IS NULL OR trim(p.rotation_role) = '')
          AND p.full_name GLOB '[A-Za-z]. *'
        """
    ).fetchall()

    merged = 0
    for shadow in shadow_rows:
        shadow_id = int(shadow["player_id"])
        stat_count = conn.execute("SELECT COUNT(*) FROM player_game_stats WHERE player_id = ?", (shadow_id,)).fetchone()[0]
        prop_count = conn.execute("SELECT COUNT(*) FROM prop_lines WHERE player_id = ?", (shadow_id,)).fetchone()[0]
        if int(stat_count or 0) > 0 or int(prop_count or 0) > 0:
            continue
        target = resolve_player_identity(
            conn,
            str(shadow["team_abbreviation"]),
            str(shadow["full_name"]),
            exclude_player_id=shadow_id,
            prefer_rich=True,
        )
        if not target or player_is_skeletal(target):
            continue
        target_id = int(target["player_id"])
        if target_id == shadow_id:
            continue

        conn.execute("UPDATE injuries SET player_id = ? WHERE player_id = ?", (target_id, shadow_id))
        conn.execute("UPDATE manual_adjustments SET player_id = ? WHERE player_id = ?", (target_id, shadow_id))
        conn.execute("UPDATE gem_snapshot_items SET player_id = ? WHERE player_id = ?", (target_id, shadow_id))
        conn.execute("UPDATE watchlist_snapshot_items SET player_id = ? WHERE player_id = ?", (target_id, shadow_id))

        history_rows = conn.execute(
            """
            SELECT team_id, game_id, source, confidence, observed_at
            FROM player_team_history
            WHERE player_id = ?
            """,
            (shadow_id,),
        ).fetchall()
        for history in history_rows:
            conn.execute(
                """
                INSERT INTO player_team_history (player_id, team_id, game_id, source, confidence, observed_at)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(player_id, game_id, source) DO UPDATE SET
                    team_id = excluded.team_id,
                    confidence = MAX(player_team_history.confidence, excluded.confidence),
                    observed_at = CASE
                        WHEN player_team_history.observed_at >= excluded.observed_at THEN player_team_history.observed_at
                        ELSE excluded.observed_at
                    END
                """,
                (
                    target_id,
                    int(history["team_id"]),
                    history["game_id"],
                    str(history["source"]),
                    float(history["confidence"] or 0.0),
                    str(history["observed_at"]),
                ),
            )
        conn.execute("DELETE FROM player_team_history WHERE player_id = ?", (shadow_id,))
        conn.execute("DELETE FROM players WHERE id = ?", (shadow_id,))
        merged += 1

    return {"merged_players": merged}
