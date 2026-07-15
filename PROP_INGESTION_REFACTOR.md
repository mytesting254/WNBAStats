# Prop Ingestion Refactor

## Goal

Split prop processing into two layers:

1. provider-specific ingestion
2. one shared downstream prop sync pipeline

This keeps The Odds API ingestion, Covers ingestion, and future providers separate at the fetch/materialization layer while eliminating duplicated sync, rebuild, publish, and progress orchestration.

## Current Problems

- route handlers and job runners decide independently when prop sync should run
- rebuild logic is split across odds import, covers import, current-slate repair, and legacy recalculate
- progress stages are remapped in multiple places
- success, cache fallback, and no-op cases do not share one result model
- provider-specific fetch code is mixed with downstream sync orchestration

## Target Architecture

### Layer 1: Provider Ingestion Adapters

Each adapter is responsible only for:

- fetching provider payloads or replaying saved cache
- writing raw cache artifacts
- materializing `sportsbook_prop_lines`
- updating provider-owned game-market context on `games`
- returning a normalized ingestion result

Adapters:

- `ingest_odds_api_props(...)`
- `ingest_covers_props(...)`
- `ingest_no_provider(...)`

`ingest_no_provider(...)` is the adapter used by recalculate and injury/roster-triggered repair paths when they should skip external fetches and operate only on existing local state.

### Layer 2: Shared Prop Sync Pipeline

The shared pipeline is responsible for:

- deciding whether `sportsbook_prop_lines -> prop_lines` sync should run
- diffing canonical sportsbook rows into model-ready `prop_lines`
- rebuilding affected prop predictions
- rebuilding affected game predictions when required
- publishing affected payloads
- emitting one consistent progress model

Shared entrypoint:

```python
run_prop_sync_pipeline(
    *,
    request_source: str,
    ingestion: PropIngestionResult,
    target_game_ids: list[int] | None = None,
    rebuild_mode: Literal["changed_props", "target_games", "skip"] = "changed_props",
    rebuild_games: bool = True,
    publish_mode: Literal["full", "targeted", "skip"] = "full",
    progress: PropPipelineProgressReporter | None = None,
    conn: sqlite3.Connection | None = None,
) -> PropPipelineResult
```

## Normalized Interfaces

### `PropIngestionResult`

```python
@dataclass(frozen=True)
class PropIngestionResult:
    source: Literal["odds_api", "covers", "none"]
    status: str
    message: str | None

    # Provider materialization results.
    imported_events: int
    imported_rows: int
    updated_game_markets: bool

    # If true, the downstream pipeline is allowed to run
    # sportsbook_prop_lines -> prop_lines sync.
    prop_sync_eligible: bool

    # Optional scope narrowing provided by the adapter.
    target_game_ids: list[int]

    # Raw adapter payload for logging, audit, or route response pass-through.
    metadata: dict[str, Any]
```
```

Rules:

- `prop_sync_eligible=True` only when the adapter produced or restored prop rows worth syncing
- hard provider failures set `prop_sync_eligible=False`
- game-markets-only imports set `prop_sync_eligible=False`
- cache fallbacks with valid prop rows set `prop_sync_eligible=True`

### `PropPipelineResult`

```python
@dataclass(frozen=True)
class PropPipelineResult:
    request_source: str
    status: Literal["completed", "failed", "skipped"]
    message: str | None

    target_game_ids: list[int]

    scanned_props: int
    synced_props: int
    changed_prop_line_ids: list[int]

    attempted_predictions: int
    rebuilt_predictions: int
    skipped_predictions: int

    attempted_game_predictions: int
    rebuilt_game_predictions: int

    published_payloads: dict[str, int]
    last_error: str | None
```
```

### `PropPipelineProgressReporter`

```python
class PropPipelineProgressReporter(Protocol):
    def __call__(
        self,
        stage: Literal[
            "provider_ingestion",
            "syncing_props",
            "rebuilding_predictions",
            "rebuilding_games",
            "settling_recent_finals",
            "refreshing_covers_context",
            "publishing_payloads",
        ],
        current: int,
        total: int,
        message: str | None = None,
    ) -> None: ...
```
```

The stage names above should become the only allowed stage vocabulary for prop-ingestion jobs.

## Adapter Mapping

### Odds API

Current source:

- `backend/app/odds_import.py`

Adapter output rules:

- `imported`, `loaded_from_cache`, `partial_import` with materialized rows:
  - `prop_sync_eligible=True`
- `missing_api_key`, `provider_error`:
  - `prop_sync_eligible=False`
- target games can be inferred from touched sportsbook rows after replacement, or left empty and resolved by sync

### Covers

Current source:

- `backend/app/covers_import.py`

Adapter output rules:

- `imported`, `loaded_from_cache`:
  - `prop_sync_eligible=True`
- `imported_game_markets_only`, `failed`:
  - `prop_sync_eligible=False`

### No Provider

Used by:

- `/api/props/repair-current-slate`
- `/api/recalculate`
- roster-triggered injury update repair

Adapter output rules:

- `prop_sync_eligible=False` for injury-targeted repair
- `prop_sync_eligible=True` for normal current-slate repair if existing sportsbook rows should be diffed again
- always carries `target_game_ids`

## Workflow Mapping

### `Refresh Odds`

1. `ingest_odds_api_props(...)`
2. `run_prop_sync_pipeline(...)`
3. optional `refreshing_covers_context`
4. publish

### `Refresh Covers`

1. `ingest_covers_props(...)`
2. `run_prop_sync_pipeline(...)` only if `prop_sync_eligible=True`
3. publish

### `Recalculate`

1. `ingest_no_provider(...)`
2. `run_prop_sync_pipeline(...)`
3. settle completed props and games if legacy route still needs settlement behavior
4. publish

### Roster / Injury Update

1. `ingest_no_provider(...)`
2. `run_prop_sync_pipeline(...)` with `rebuild_mode="target_games"`
3. publish targeted payloads

## Recommended First Refactor Step

Extract the shared downstream orchestration first, without changing provider fetchers yet.

Step 1:

- introduce `PropIngestionResult`
- introduce `PropPipelineResult`
- introduce `run_prop_sync_pipeline(...)`

Initial callers to migrate:

- `_start_prop_sync_if_needed()`
- `_run_current_slate_repair_job()`
- `_run_odds_import_job()`

This gets the duplicated stage mapping and rebuild/publish decisions into one place before touching more provider code.

## Recommended File Layout

Possible landing points:

- `backend/app/prop_ingestion.py`
  - dataclasses
  - provider adapter wrappers
  - shared pipeline runner
- `backend/app/odds_import.py`
  - Odds API fetch/materialization internals only
- `backend/app/covers_import.py`
  - Covers fetch/materialization internals only
- `backend/app/main.py`
  - routes, queue wiring, and job state only

## Migration Guardrails

- do not change `sync_prop_lines_from_sportsbook(...)` semantics during the first extraction
- move orchestration before changing provider row parsing
- keep current route payload shapes stable
- keep current progress stage names stable where already exposed in the frontend
- add tests for each adapter status to prove whether pipeline execution is allowed or skipped

## Exit Criteria

The refactor is successful when:

- Odds, Covers, recalculate, and injury repair all call one shared prop sync pipeline
- provider adapters no longer decide rebuild/publish behavior
- progress stage mapping exists in one place
- route handlers stop manually stitching together sync/rebuild/publish sequences
- failure and fallback states consistently decide whether prop sync should run
