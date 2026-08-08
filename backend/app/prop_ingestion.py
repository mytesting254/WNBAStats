from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Any, Callable, Literal

from .odds_import import SyncPropLinesResult, sync_prop_lines_from_sportsbook
from .projections import LiveRebuildResult, rebuild_predictions_live


@dataclass(frozen=True)
class PropIngestionResult:
    source: Literal["odds_api", "covers", "none"]
    status: str
    message: str | None
    prop_sync_eligible: bool
    target_game_ids: list[int]
    rebuild_mode: Literal["changed_props", "target_games", "skip"]
    fallback_target_game_rebuild_when_unchanged: bool = False
    initial_sync_message: str | None = None
    skip_sync_message: str | None = None
    metadata: dict[str, Any] | None = None


@dataclass(frozen=True)
class PropPipelineResult:
    request_source: str
    target_game_ids: list[int]
    scanned_props: int
    synced_props: int
    changed_prop_line_ids: list[int]
    attempted_predictions: int
    rebuilt_predictions: int
    skipped_predictions: int
    rebuild_errors: list[str]
    attempted_game_predictions: int
    rebuilt_game_predictions: int
    watchlist_snapshot: dict[str, Any] | None = None
    dfs_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class PropPostProcessPolicy:
    settle_recent_finals: bool = False
    refresh_covers_context: bool = False
    publish_mode: Literal["full", "targeted", "current_read", "skip"] = "skip"
    matchup_game_ids: list[int] | None = None
    full_matchup_refresh: bool = True
    include_performance: bool = True
    publish_start_message: str | None = None
    publish_done_message: str | None = None
    covers_refresh_start_message: str | None = None
    covers_refresh_done_message: str | None = None
    settle_recent_finals_message: str | None = None


@dataclass(frozen=True)
class PropPostProcessResult:
    recent_finals_settlement: dict[str, Any] | None = None
    published_payloads: dict[str, int] | None = None
    covers_context: dict[str, Any] | None = None
    covers_context_error: str | None = None


def build_odds_provider_ingestion(result: dict[str, Any]) -> PropIngestionResult:
    status = str(result.get("status") or "")
    message = str(result.get("message")) if result.get("message") is not None else None
    target_game_ids = [int(game_id) for game_id in (result.get("target_game_ids") or []) if int(game_id) > 0]
    return PropIngestionResult(
        source="odds_api",
        status=status,
        message=message,
        prop_sync_eligible=bool(result.get("prop_sync_eligible")),
        target_game_ids=target_game_ids,
        rebuild_mode="changed_props",
        fallback_target_game_rebuild_when_unchanged=False,
        metadata=dict(result),
    )


def build_covers_provider_ingestion(result: dict[str, Any]) -> PropIngestionResult:
    status = str(result.get("status") or "")
    message = str(result.get("message")) if result.get("message") is not None else None
    target_game_ids = [int(game_id) for game_id in (result.get("target_game_ids") or []) if int(game_id) > 0]
    return PropIngestionResult(
        source="covers",
        status=status,
        message=message,
        prop_sync_eligible=bool(result.get("prop_sync_eligible")),
        target_game_ids=target_game_ids,
        rebuild_mode="changed_props",
        fallback_target_game_rebuild_when_unchanged=False,
        metadata=dict(result),
    )


def build_no_provider_ingestion(
    *,
    scope: Literal["current_slate", "injury_update", "legacy_recalculate"],
    target_game_ids: list[int] | None,
) -> PropIngestionResult:
    normalized_target_game_ids = sorted({int(game_id) for game_id in (target_game_ids or []) if int(game_id) > 0})
    if scope == "injury_update":
        return PropIngestionResult(
            source="none",
            status="ready" if normalized_target_game_ids else "skipped",
            message=(
                f"Skipped sportsbook sync for injury update; rebuilding all projections for {len(normalized_target_game_ids)} affected games."
                if normalized_target_game_ids
                else "No scheduled games were available for repair."
            ),
            prop_sync_eligible=False,
            target_game_ids=normalized_target_game_ids,
            rebuild_mode="target_games",
            skip_sync_message=(
                f"Skipped sportsbook sync for injury update; rebuilding all projections for {len(normalized_target_game_ids)} affected games."
                if normalized_target_game_ids
                else "No scheduled games were available for repair."
            ),
            metadata={"scope": scope},
        )
    return PropIngestionResult(
        source="none",
        status="ready" if normalized_target_game_ids else "skipped",
        message=(
            f"Syncing props for {len(normalized_target_game_ids)} games."
            if normalized_target_game_ids
            else "No scheduled games were available for repair."
        ),
        prop_sync_eligible=True,
        target_game_ids=normalized_target_game_ids,
        rebuild_mode="changed_props",
        fallback_target_game_rebuild_when_unchanged=True,
        initial_sync_message=(
            f"Syncing props for {len(normalized_target_game_ids)} games."
            if normalized_target_game_ids
            else "No scheduled games were available for repair."
        ),
        metadata={"scope": scope},
    )


def run_prop_sync_pipeline(
    conn: sqlite3.Connection,
    *,
    request_source: str,
    target_game_ids: list[int] | None = None,
    sync_props: bool = True,
    fast_fail: bool = False,
    rebuild_mode: Literal["changed_props", "target_games", "skip"] = "changed_props",
    fallback_target_game_rebuild_when_unchanged: bool = False,
    rebuild_chunk_size: int = 30,
    initial_sync_message: str | None = None,
    skip_sync_message: str | None = None,
    skip_rebuild_message: str | None = None,
    post_game_rebuild_message: str | None = None,
    watchlist_snapshot_date: str | None = None,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
    sync_props_fn: Callable[..., int | SyncPropLinesResult] = sync_prop_lines_from_sportsbook,
    rebuild_predictions_fn: Callable[..., LiveRebuildResult] = rebuild_predictions_live,
    rebuild_games_fn: Callable[..., dict[str, Any]] | None = None,
    snapshot_watchlist_fn: Callable[[sqlite3.Connection, str], dict[str, Any]] | None = None,
    snapshot_dfs_fn: Callable[[sqlite3.Connection, list[int]], dict[str, Any]] | None = None,
) -> PropPipelineResult:
    normalized_target_game_ids = sorted({int(game_id) for game_id in (target_game_ids or []) if int(game_id) > 0})
    scanned = 0
    synced = 0
    changed_prop_line_ids: list[int] = []
    touched_game_ids = list(normalized_target_game_ids)

    if sync_props:
        if progress_callback is not None and initial_sync_message:
            progress_callback("syncing_props", 0, 1, initial_sync_message)
        sync_result = sync_props_fn(
            conn,
            game_ids=normalized_target_game_ids or None,
            fast_fail=fast_fail,
            rebuild_predictions_after=False,
            include_change_details=True,
            progress_callback=lambda current, total, message: progress_callback("syncing_props", current, total, message)
            if progress_callback is not None
            else None,
        )
        if isinstance(sync_result, SyncPropLinesResult):
            scanned = int(sync_result.synced_props)
            synced = int(sync_result.changed_props)
            changed_prop_line_ids = list(sync_result.changed_prop_line_ids)
            touched_game_ids = list(sync_result.touched_game_ids or touched_game_ids)
        else:
            scanned = int(sync_result)
            synced = int(sync_result)
    else:
        if progress_callback is not None:
            progress_callback("syncing_props", 1, 1, skip_sync_message or "Skipped sportsbook prop sync.")

    rebuild_result = LiveRebuildResult(projections=[], attempted=0, written=0, skipped=0, errors=[])
    if rebuild_mode == "target_games" and touched_game_ids:
        rebuild_result = rebuild_predictions_fn(
            conn,
            game_ids=touched_game_ids,
            prop_line_ids=None,
            chunk_size=rebuild_chunk_size,
            progress_callback=lambda current, total, message: progress_callback("rebuilding_predictions", current, total, message)
            if progress_callback is not None
            else None,
        )
    elif rebuild_mode == "changed_props" and changed_prop_line_ids:
        rebuild_result = rebuild_predictions_fn(
            conn,
            game_ids=None,
            prop_line_ids=changed_prop_line_ids,
            chunk_size=rebuild_chunk_size,
            progress_callback=lambda current, total, message: progress_callback("rebuilding_predictions", current, total, message)
            if progress_callback is not None
            else None,
        )
    elif rebuild_mode == "changed_props" and fallback_target_game_rebuild_when_unchanged and touched_game_ids:
        rebuild_result = rebuild_predictions_fn(
            conn,
            game_ids=touched_game_ids,
            prop_line_ids=None,
            chunk_size=rebuild_chunk_size,
            progress_callback=lambda current, total, message: progress_callback("rebuilding_predictions", current, total, message)
            if progress_callback is not None
            else None,
        )
    elif progress_callback is not None:
        progress_callback("rebuilding_predictions", 0, 0, skip_rebuild_message or "No prop-line changes; skipped prediction rebuild.")

    attempted_game_predictions = 0
    rebuilt_game_predictions = 0
    if rebuild_games_fn is not None and touched_game_ids:
        game_rebuild_result = rebuild_games_fn(
            conn,
            game_ids=touched_game_ids,
            progress_callback=lambda current, total, message: progress_callback("rebuilding_games", current, total, message)
            if progress_callback is not None
            else None,
        )
        attempted_game_predictions = int(game_rebuild_result["attempted"])
        rebuilt_game_predictions = int(game_rebuild_result["written"])
        if progress_callback is not None:
            progress_callback(
                "rebuilding_games",
                attempted_game_predictions,
                attempted_game_predictions,
                post_game_rebuild_message or "Refreshing watchlist snapshot.",
            )

    dfs_snapshot = None
    if snapshot_dfs_fn is not None and touched_game_ids:
        dfs_snapshot = snapshot_dfs_fn(conn, touched_game_ids)

    watchlist_snapshot = None
    if snapshot_watchlist_fn is not None and watchlist_snapshot_date is not None:
        watchlist_snapshot = snapshot_watchlist_fn(conn, watchlist_snapshot_date)

    return PropPipelineResult(
        request_source=request_source,
        target_game_ids=touched_game_ids,
        scanned_props=scanned,
        synced_props=synced,
        changed_prop_line_ids=changed_prop_line_ids,
        attempted_predictions=int(rebuild_result.attempted),
        rebuilt_predictions=int(rebuild_result.written),
        skipped_predictions=int(rebuild_result.skipped),
        rebuild_errors=list(rebuild_result.errors[:20]),
        attempted_game_predictions=attempted_game_predictions,
        rebuilt_game_predictions=rebuilt_game_predictions,
        watchlist_snapshot=watchlist_snapshot,
        dfs_snapshot=dfs_snapshot,
    )


def run_post_pipeline_steps(
    *,
    connect_fn: Callable[[], Any],
    policy: PropPostProcessPolicy,
    progress_callback: Callable[[str, int, int, str | None], None] | None = None,
    settle_recent_finals_fn: Callable[[Any], dict[str, Any]] | None = None,
    refresh_covers_context_fn: Callable[[Any], dict[str, Any]] | None = None,
    publish_post_mutation_payloads_fn: Callable[[Any, PropPostProcessPolicy], dict[str, int]] | None = None,
    publish_current_read_payloads_fn: Callable[[Any], Any] | None = None,
) -> PropPostProcessResult:
    recent_finals_settlement: dict[str, Any] | None = None
    published_payloads: dict[str, int] | None = None
    covers_context: dict[str, Any] | None = None
    covers_context_error: str | None = None

    if policy.settle_recent_finals and settle_recent_finals_fn is not None:
        with connect_fn() as conn:
            if progress_callback is not None:
                progress_callback(
                    "settling_recent_finals",
                    0,
                    1,
                    policy.settle_recent_finals_message or "Settling recent completed games.",
                )
            recent_finals_settlement = settle_recent_finals_fn(conn)

    if policy.refresh_covers_context and refresh_covers_context_fn is not None:
        try:
            if progress_callback is not None:
                progress_callback(
                    "refreshing_covers_context",
                    0,
                    1,
                    policy.covers_refresh_start_message or "Refreshing Covers context.",
                )
            with connect_fn() as conn:
                covers_context = refresh_covers_context_fn(conn)
            if progress_callback is not None:
                progress_callback(
                    "refreshing_covers_context",
                    1,
                    1,
                    policy.covers_refresh_done_message or "Covers context refreshed.",
                )
        except Exception as exc:
            covers_context_error = str(exc)
            if progress_callback is not None:
                progress_callback(
                    "refreshing_covers_context",
                    1,
                    1,
                    f"{policy.covers_refresh_start_message or 'Refreshing Covers context.'} failed: {covers_context_error}",
                )

    if policy.publish_mode != "skip":
        if progress_callback is not None:
            progress_callback(
                "publishing_payloads",
                0,
                1,
                policy.publish_start_message or "Publishing refreshed payloads.",
            )
        with connect_fn() as conn:
            if policy.publish_mode == "current_read" and publish_current_read_payloads_fn is not None:
                publish_current_read_payloads_fn(conn)
                published_payloads = {}
            elif publish_post_mutation_payloads_fn is not None:
                published_payloads = publish_post_mutation_payloads_fn(conn, policy)
            else:
                published_payloads = {}
        if progress_callback is not None:
            progress_callback(
                "publishing_payloads",
                1,
                1,
                policy.publish_done_message or "Publishing finished.",
            )

    return PropPostProcessResult(
        recent_finals_settlement=recent_finals_settlement,
        published_payloads=published_payloads,
        covers_context=covers_context,
        covers_context_error=covers_context_error,
    )
