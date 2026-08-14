export type ValueProp = {
  id: number;
  game_id: number;
  player: string;
  position?: string | null;
  team: string;
  team_logo_url?: string | null;
  sportsbook: string;
  best_over_sportsbook?: string | null;
  best_under_sportsbook?: string | null;
  display_sportsbook?: string | null;
  market: string;
  line: number;
  over_odds: number;
  under_odds: number;
  projection: number;
  recommended_side: "over" | "under";
  model_probability: number;
  implied_probability: number;
  edge: number;
  expected_value: number;
  confidence: "low" | "medium" | "high";
  reason: string;
  prediction_time: string;
  start_time: string;
  rest_days: number;
  rotation_role: string;
  spread_home: number | null;
  game_total: number | null;
  team_spread: number | null;
  blowout_risk: string;
  blowout_probability: number;
  blowout_minutes_impact: number;
  increased_role?: boolean;
  recent_values?: number[];
  recent_minutes?: number[];
  h2h_opponent?: string | null;
  h2h_values?: number[];
  h2h_minutes?: number[];
};

export type WatchlistProp = ValueProp & {
  prop_line_id: number;
  away_team?: string;
  home_team?: string;
};

export type DfsFirstHalfEstimate = {
  prop_line_id: number;
  game_id: number;
  player_id: number;
  player: string;
  position?: string | null;
  team: string;
  team_logo_url?: string | null;
  sportsbook: string;
  market: string;
  line: number;
  over_odds: number;
  under_odds: number;
  full_game_projection: number;
  estimated_first_half_result: number;
  expected_halfway_line: number;
  pace_ratio?: number | null;
  halftime_margin_to_line?: number | null;
  on_track_probability?: number | null;
  recommended_side: "over" | "under";
  model_version: string;
  model_rows: number;
  team_first_half_share: number;
  estimated_first_half_minutes_share: number;
  projected_first_half_total?: number | null;
  game_total?: number | null;
  start_time?: string | null;
  confidence: "low" | "medium" | "high";
  recent_values?: number[];
  recent_minutes?: number[];
  h2h_opponent?: string | null;
  h2h_values?: number[];
  h2h_minutes?: number[];
};

export type PlayerFirstHalfLine = {
  game_date: string;
  game_id: number;
  player_id: number;
  player: string;
  position?: string | null;
  team: string;
  team_logo_url?: string | null;
  opponent_team: string;
  sportsbook: string;
  market: string;
  line: number;
  actual_result: number;
  winning_side: string;
  first_half_result: number;
  expected_halfway_line: number;
  pace_ratio?: number | null;
  halftime_margin_to_line?: number | null;
  on_track_by_half: boolean;
  first_half_points: number;
  first_half_rebounds: number;
  first_half_assists: number;
  first_half_threes: number;
  first_half_steals: number;
  first_half_blocks: number;
  first_half_turnovers: number;
  first_half_minutes: number;
  minutes_source?: string | null;
};

export type SpecialStocksSnapshot = {
  id: number;
  game_id: number;
  player_id: number;
  player_name: string;
  game_date: string;
  captured_at: string;
  model_version: string;
  projected_steals: number;
  projected_blocks: number;
  projected_stocks: number;
  steal_prob_1_plus: number;
  steal_prob_2_plus: number;
  block_prob_1_plus: number;
  block_prob_2_plus: number;
  stocks_prob_2_plus: number;
  stocks_prob_3_plus: number;
  data_quality: string;
  start_time?: string | null;
  home_team?: string | null;
  away_team?: string | null;
  position?: string | null;
  team?: string | null;
  team_logo_url?: string | null;
  board_player_count?: number;
  board_candidate_count_50_plus?: number;
  board_candidate_threshold?: number;
  board_candidate_count_threshold?: number;
  board_avg_prob_2_plus?: number;
  board_avg_prob_3_plus?: number;
  board_top_projected_stocks?: number;
  board_top_prob_2_plus?: number;
  board_top_player_name?: string | null;
  recent_values?: number[];
  recent_minutes?: number[];
  h2h_opponent?: string | null;
  h2h_values?: number[];
  h2h_minutes?: number[];
  actual_steals?: number | null;
  actual_blocks?: number | null;
  actual_stocks?: number | null;
  settled_at?: string | null;
};

export type SpecialStocksPerformance = {
  total_latest: number;
  settled_count: number;
  pending_count: number;
  hits_2_plus: number;
  hit_rate_2_plus: number | null;
  avg_prob_2_plus: number | null;
  hits_3_plus: number;
  hit_rate_3_plus: number | null;
  avg_prob_3_plus: number | null;
  candidate_count_50_plus: number;
  candidate_hits_2_plus: number;
  candidate_hit_rate_2_plus: number | null;
  recommended_candidate_threshold: number;
  recommended_candidate_count: number;
  recommended_candidate_hits_2_plus: number;
  recommended_candidate_hit_rate_2_plus: number | null;
  threshold_recommendations: {
    status: string;
    high_confidence_threshold: number;
    watch_threshold: number;
    settled_rows: number;
    evaluated_thresholds: Array<{
      threshold: number;
      support: number;
      hits: number;
      hit_rate: number | null;
      eligible: boolean;
    }>;
  };
  calibration_buckets: Array<{
    label: string;
    min_prob: number;
    max_prob: number | null;
    count: number;
    hits: number;
    avg_prob: number | null;
    hit_rate: number | null;
  }>;
  calibration_buckets_3_plus: Array<{
    label: string;
    min_prob: number;
    max_prob: number | null;
    count: number;
    hits: number;
    avg_prob: number | null;
    hit_rate: number | null;
  }>;
};

export type SportsbookProp = {
  id: number;
  game_id: number | null;
  provider: string;
  provider_event_id: string;
  commence_time: string;
  home_team: string;
  away_team: string;
  sportsbook: string;
  market: string;
  player_name: string;
  side: "over" | "under";
  line: number;
  price: number;
  captured_at: string;
};

export type LineDiscrepancy = {
  game_id: number | null;
  matchup: string;
  commence_time: string;
  player_name: string;
  market: string;
  side: "over" | "under";
  books: number;
  line_gap: number;
  price_gap: number;
  recent_values?: number[];
  h2h_opponent?: string | null;
  h2h_values?: number[];
  best_price: {
    sportsbook: string;
    line: number;
    price: number;
  };
  low_line: {
    sportsbook: string;
    line: number;
    price: number;
  };
  high_line: {
    sportsbook: string;
    line: number;
    price: number;
  };
  book_lines: Array<{
    sportsbook: string;
    line: number;
    price: number;
  }>;
};

export type ModelPerformance = {
  total_settled?: number;
  settled: number;
  wins: number;
  win_rate: number | null;
  average_ev: number | null;
  message?: string;
  game_totals?: {
    total_settled: number;
    settled: number;
    wins: number;
    win_rate: number | null;
    average_absolute_error: number | null;
    average_absolute_edge: number | null;
    by_edge: Array<{
      label: string;
      settled: number;
      wins: number;
      win_rate: number | null;
    }>;
    by_confidence: Array<{
      label: string;
      settled: number;
      wins: number;
      win_rate: number | null;
    }>;
    message?: string;
  };
};

export type GemPerformance = {
  qualified: number;
  wins: number;
  win_rate: number | null;
  current_open_conservative?: number;
  current_open_balanced?: number;
  current_open_aggressive?: number;
  message?: string;
};

export type WatchlistPerformance = {
  qualified: number;
  wins: number;
  win_rate: number | null;
  message?: string;
};

export type AuthState = {
  authenticated: boolean;
  user: {
    username: string;
    is_admin: boolean;
  } | null;
  csrf_token: string | null;
};

export type OpsHealth = {
  status: string;
  prop_sync: PropSyncHealth;
  prop_sync_job?: PropSyncHealth | null;
  model_training: AsyncJobHealth;
  runtime_storage?: {
    db_path: string;
    training_db_path: string;
    cache_dir: string;
    snapshot_dir: string;
    source_of_truth: string;
    training_store: string;
    team_boxscores_table: string;
    team_results_table: string;
    team_boxscores_flow: string;
  } | null;
};

export type PropSyncHealth = {
  job_id?: number | null;
  running: boolean;
  started_at: string | null;
  finished_at: string | null;
  last_error: string | null;
  last_result: Record<string, unknown> | null;
  status?: string | null;
  scope?: string | null;
  target_game_ids?: number[];
  stage?: string | null;
  stage_index?: number;
  stage_total?: number;
  current?: number;
  total?: number;
  percent?: number;
  message?: string | null;
  updated_at?: string | null;
};

export type AsyncJobHealth = {
  running: boolean;
  started_at: string | null;
  finished_at: string | null;
  last_error: string | null;
  last_result: Record<string, unknown> | null;
  status?: string | null;
  message?: string | null;
  updated_at?: string | null;
};

export type CacheViewStatus = {
  cache_name?: string;
  exists: boolean;
  cached_at: string | null;
  cache_date?: string | null;
  ttl_seconds?: number | null;
  source?: string | null;
  is_fresh: boolean;
  age_seconds?: number | null;
};

export type CacheStatus = {
  status: string;
  views: {
    props: CacheViewStatus;
    watchlist: CacheViewStatus;
    matchups: CacheViewStatus;
    insights?: CacheViewStatus;
    parlays: {
      matchups: CacheViewStatus;
      props: CacheViewStatus;
    };
  };
};

export type StalePayloadAudit = {
  stale: number;
  checked: number;
  files: string[];
  message: string;
};

export type DbLockAudit = {
  engine: string;
  status: string;
  locked: boolean;
  message: string;
  db_path?: string;
  db_exists?: boolean;
  writable?: boolean;
  recovered?: boolean;
  checkpoint?: {
    busy: number;
    log_frames: number;
    checkpointed_frames: number;
  } | null;
  wal?: {
    path: string;
    exists: boolean;
    size_bytes: number;
  };
  shm?: {
    path: string;
    exists: boolean;
    size_bytes: number;
  };
};

const API_KEY = (window.__APP_CONFIG__?.apiKey ?? "").trim();
let csrfToken = "";

export function setCsrfToken(nextToken: string | null | undefined) {
  csrfToken = (nextToken ?? "").trim();
}

function apiHeaders(headers?: HeadersInit, includeApiKey = false): Headers {
  const merged = new Headers(headers);
  if (includeApiKey && API_KEY) {
    merged.set("X-API-Key", API_KEY);
  }
  if (csrfToken) {
    merged.set("X-CSRF-Token", csrfToken);
  }
  return merged;
}

async function apiFetch(input: string, init?: RequestInit & { includeApiKey?: boolean }): Promise<Response> {
  const { includeApiKey = false, headers, ...rest } = init ?? {};
  return fetch(input, {
    ...rest,
    cache: "no-store",
    credentials: "same-origin",
    headers: apiHeaders(headers, includeApiKey),
  });
}

async function readErrorDetail(response: Response): Promise<string> {
  const contentType = response.headers.get("content-type") ?? "";
  if (contentType.includes("application/json")) {
    try {
      const payload = await response.json();
      if (typeof payload?.detail === "string" && payload.detail.trim()) {
        return payload.detail.trim();
      }
    } catch {
      // Fall through to text parsing.
    }
  }
  try {
    const text = (await response.text()).trim();
    if (text) return text;
  } catch {
    // Ignore parse errors and use status text fallback below.
  }
  return response.statusText || "Request failed";
}

export async function fetchAuthState(): Promise<AuthState> {
  const response = await apiFetch("/api/auth/me");
  if (!response.ok) {
    throw new Error("Failed to load auth state");
  }
  return response.json();
}

export async function loginAdmin(username: string, password: string): Promise<AuthState> {
  const response = await apiFetch("/api/auth/login", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ username, password })
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to sign in");
  }
  return response.json();
}

export async function logoutAdmin(): Promise<AuthState> {
  const response = await apiFetch("/api/auth/logout", { method: "POST" });
  if (!response.ok) {
    throw new Error("Failed to sign out");
  }
  return response.json();
}

export async function fetchOpsHealth(): Promise<OpsHealth> {
  const response = await apiFetch("/api/ops/health");
  if (!response.ok) {
    throw new Error("Failed to load operations health");
  }
  return response.json();
}

export async function fetchCacheStatus(): Promise<CacheStatus> {
  const response = await apiFetch("/api/cache/status");
  if (!response.ok) {
    throw new Error("Failed to load cache status");
  }
  return response.json();
}

async function waitForPropSyncCompletion(startedAt?: string | null): Promise<{
  status?: string;
  started_at?: string | null;
  scope?: string | null;
  target_game_ids?: number[];
  scanned_props?: number;
  synced_props?: number;
  rebuilt_predictions?: number;
  predictions?: number;
}> {
  const deadline = Date.now() + 10 * 60 * 1000;

  while (Date.now() < deadline) {
    const health = await fetchOpsHealth();
    const propSync = health.prop_sync;

    if (propSync.last_error) {
      throw new Error(propSync.last_error);
    }

    if (!propSync.running && propSync.last_result) {
      const result = propSync.last_result as {
        status?: string;
        started_at?: string | null;
        scope?: string | null;
        target_game_ids?: number[];
        scanned_props?: number;
        synced_props?: number;
        rebuilt_predictions?: number;
        predictions?: number;
      };

      if (!startedAt || !health.prop_sync.started_at || health.prop_sync.started_at === startedAt) {
        return result;
      }
    }

    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }

  throw new Error("Timed out waiting for current-slate repair to finish.");
}

export async function waitForModelTrainingCompletion(startedAt?: string | null): Promise<Record<string, unknown> | null> {
  const deadline = Date.now() + 20 * 60 * 1000;

  while (Date.now() < deadline) {
    const health = await fetchOpsHealth();
    const job = health.model_training;

    if (job.started_at !== startedAt) {
      await new Promise((resolve) => window.setTimeout(resolve, 1000));
      continue;
    }

    if (job.last_error) {
      throw new Error(job.last_error);
    }

    if (!job.running) {
      return job.last_result;
    }

    await new Promise((resolve) => window.setTimeout(resolve, 1000));
  }

  throw new Error("Timed out waiting for model training to finish.");
}

export async function auditStalePayloads(): Promise<StalePayloadAudit> {
  const response = await apiFetch("/api/cache/stale-payloads");
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to audit stale payloads");
  }
  return response.json();
}

export async function deleteStalePayloads(acknowledgement: string): Promise<{
  deleted: number;
  checked: number;
  files: string[];
  message: string;
}> {
  const response = await apiFetch("/api/cache/stale-payloads/delete", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ acknowledgement }),
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to delete stale payloads");
  }
  return response.json();
}

export async function auditDbLock(): Promise<DbLockAudit> {
  const response = await apiFetch("/api/db/lock");
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to audit database lock state");
  }
  return response.json();
}

export async function recoverDbLock(): Promise<DbLockAudit> {
  const response = await apiFetch("/api/db/unlock", { method: "POST" });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to run database recovery");
  }
  return response.json();
}

export async function createGemSnapshot(snapshotDate?: string, preset = "balanced"): Promise<{
  snapshot_id: number;
  snapshot_date: string;
  preset: string;
  tracked: number;
  settled: number;
  wins: number;
}> {
  const params = new URLSearchParams({ preset });
  if (snapshotDate) {
    params.set("snapshot_date", snapshotDate);
  }
  const response = await apiFetch(`/api/gems/snapshot?${params.toString()}`, {
    method: "POST",
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to create gem snapshot");
  }
  return response.json();
}

export type TeamLast10 = {
  games: number;
  wins: number;
  losses: number;
  home_games: number;
  away_games: number;
  ats_wins: number;
  ats_losses: number;
  ats_pushes: number;
  overs: number;
  unders: number;
  total_pushes: number;
  avg_points_for: number;
  avg_points_against: number;
  segment_averages?: {
    overall?: {
      games: number;
      avg_q1_points_for: number | null;
      avg_q1_points_against: number | null;
      avg_first_half_points_for: number | null;
      avg_first_half_points_against: number | null;
      avg_q1_total: number | null;
      avg_first_half_total: number | null;
    } | null;
    home?: {
      games: number;
      avg_q1_points_for: number | null;
      avg_q1_points_against: number | null;
      avg_first_half_points_for: number | null;
      avg_first_half_points_against: number | null;
      avg_q1_total: number | null;
      avg_first_half_total: number | null;
    } | null;
    away?: {
      games: number;
      avg_q1_points_for: number | null;
      avg_q1_points_against: number | null;
      avg_first_half_points_for: number | null;
      avg_first_half_points_against: number | null;
      avg_q1_total: number | null;
      avg_first_half_total: number | null;
    } | null;
  } | null;
  recent_games: Array<{
    game_date: string;
    opponent: string;
    is_home: number;
    points: number;
    opponent_points: number;
    closing_spread: number;
    closing_total: number;
    ats_result: "cover" | "no_cover" | "push";
    total_result: "over" | "under" | "push";
  }>;
};

export type CoversRecordRow = {
  date: string;
  home?: string;
  winner?: string | null;
  opponent?: string;
  location?: "home" | "away";
  result?: string | null;
  score: string;
  ats: string;
  total: string;
};

export type CoversRecords = {
  team_table?: Array<{
    team: string;
    record: string;
    ats: string;
    ou: string;
    away: string;
    home: string;
  }>;
  head_to_head: CoversRecordRow[];
  away_last_10: CoversRecordRow[];
  home_last_10: CoversRecordRow[];
};

export type TeamRatingsWindow = {
  games: number;
  possessions: number;
  off_rating: number;
  def_rating: number;
  net_rating: number;
  pace: number;
};

export type TeamRatings = {
  season?: TeamRatingsWindow | null;
  last_10?: TeamRatingsWindow | null;
  home?: TeamRatingsWindow | null;
  away?: TeamRatingsWindow | null;
  off_rank?: number | null;
  def_rank?: number | null;
  net_rank?: number | null;
  pace_rank?: number | null;
};

export type MatchupInsightsWindow = {
  games: number;
  points?: number | null;
  points_allowed?: number | null;
  fg_pct?: number | null;
  fg_pct_allowed?: number | null;
  three_pct?: number | null;
  three_pct_allowed?: number | null;
  free_throw_pct?: number | null;
  free_throw_pct_allowed?: number | null;
  rebounds?: number | null;
  rebounds_allowed?: number | null;
  offensive_rebounds?: number | null;
  offensive_rebounds_allowed?: number | null;
  defensive_rebounds?: number | null;
  defensive_rebounds_allowed?: number | null;
  assists?: number | null;
  assists_allowed?: number | null;
  threes_made?: number | null;
  threes_made_allowed?: number | null;
  threes_attempted?: number | null;
  threes_attempted_allowed?: number | null;
  turnovers?: number | null;
  turnovers_forced?: number | null;
  steals?: number | null;
  steals_allowed?: number | null;
  blocks?: number | null;
  blocks_allowed?: number | null;
};

export type MatchupInsights = {
  context: "home" | "away";
  overall_last_5?: MatchupInsightsWindow | null;
  context_last_5?: MatchupInsightsWindow | null;
};

export type Matchup = {
  id: number;
  game_date: string;
  start_time: string;
  home_team: string;
  home_team_name: string;
  home_logo_url?: string | null;
  home_rest_days: number | null;
  away_team: string;
  away_team_name: string;
  away_logo_url?: string | null;
  away_rest_days: number | null;
  spread_home: number | null;
  game_total: number | null;
  home_moneyline: number | null;
  away_moneyline: number | null;
  home_spread_price?: number | null;
  away_spread_price?: number | null;
  over_price?: number | null;
  under_price?: number | null;
  model_prop_count?: number;
  prediction_count?: number;
  sportsbook_prop_count?: number;
  model_props_ready?: boolean;
  model_props_status?: "ready" | "pending" | "sportsbook_only" | "empty";
  spread_market?: {
    away_line: number | null;
    away_price: number | null;
    home_line: number | null;
    home_price: number | null;
  };
  total_market?: {
    over_line: number | null;
    over_price: number | null;
    under_line: number | null;
    under_price: number | null;
  };
  moneyline_market?: {
    away_price: number | null;
    home_price: number | null;
  };
  blowout_risk: string;
  home_projected_points: number | null;
  away_projected_points: number | null;
  projected_margin: number | null;
  projected_total: number | null;
  projected_q1_total?: number | null;
  projected_first_half_total?: number | null;
  winner_pick: string;
  ats_pick: string;
  ats_edge: number | null;
  total_pick: string;
  total_edge: number | null;
  game_confidence: string;
  total_confidence?: string;
  game_reason: string;
  total_reason?: string;
  home: TeamLast10;
  away: TeamLast10;
  home_team_ratings?: TeamRatings | null;
  away_team_ratings?: TeamRatings | null;
  home_team_insights?: MatchupInsights | null;
  away_team_insights?: MatchupInsights | null;
  rating_differentials?: {
    season_net_diff?: number | null;
    season_off_diff?: number | null;
    season_def_diff?: number | null;
    last_10_net_diff?: number | null;
    pace_diff?: number | null;
  } | null;
  h2h_segment_summary?: {
    meetings: number;
    away_avg_q1_points: number | null;
    home_avg_q1_points: number | null;
    avg_q1_total: number | null;
    away_avg_first_half_points: number | null;
    home_avg_first_half_points: number | null;
    avg_first_half_total: number | null;
  } | null;
  covers_records?: CoversRecords | null;
  props: ValueProp[];
  sportsbook_props: SportsbookProp[];
  line_discrepancies: LineDiscrepancy[];
};

export type RosterPlayer = {
  team: string;
  player_name: string;
  status: string;
  captured_at?: string | null;
  out_since?: string | null;
  player_id?: number;
  rotation_role?: string | null;
  position?: string | null;
  recent_minutes_avg?: number | null;
  recent_contribution_avg?: number | null;
  player_impact_score?: number | null;
  team_injury_factor?: number | null;
  team_missing_key_players?: number | null;
  team_penalty_points?: number | null;
};

export type ModelRunMetric = {
  training_sample_diagnostics?: {
    candidate_rows: number;
    included_rows: number;
    skipped_before_training_start: number;
    skipped_missing_history_window?: number;
    skipped_incomplete_context?: number;
    skipped_missing_snapshot?: number;
  };
  residual_training_sample_diagnostics?: {
    candidate_rows: number;
    included_rows: number;
    skipped_before_training_start: number;
    skipped_missing_history_window?: number;
    skipped_incomplete_context?: number;
    skipped_missing_snapshot?: number;
  };
  rows: number;
  mae: number | null;
  rmse: number | null;
  bias: number | null;
  directional_accuracy: number | null;
  segment_count?: number;
  skipped_segments?: number;
  baseline_mae?: number | null;
  baseline_rmse?: number | null;
  baseline_bias?: number | null;
  baseline_directional_accuracy?: number | null;
  mae_improvement?: number | null;
  rmse_improvement?: number | null;
  directional_accuracy_improvement?: number | null;
  settled_rows?: number;
  side_accuracy?: number | null;
  calibration_gap?: number | null;
  brier_score?: number | null;
  avg_edge?: number | null;
  avg_expected_value?: number | null;
  realized_roi?: number | null;
};

export type ModelRun = {
  id?: number;
  model_version: string;
  run_type: string;
  status: string;
  started_at: string;
  finished_at: string | null;
  training_rows: number;
  markets: string[];
  metrics: Record<string, ModelRunMetric>;
  notes?: string | null;
};

export type ModelTuningCandidate = {
  rank: number;
  config: {
    ridge_penalty: number;
    market_weight_scale: number;
    player_weight_scale: number;
    stabilization_scale: number;
  };
  summary: {
    total_rows: number;
    avg_mae: number | null;
    avg_rmse: number | null;
    avg_directional_accuracy: number | null;
  };
  metrics: Record<string, ModelRunMetric>;
  training_rows: number;
};

export type ModelTuningRun = {
  model_version: string;
  run_type: string;
  started_at: string;
  finished_at: string;
  candidate_count: number;
  default_config: ModelTuningCandidate["config"];
  best_candidate: ModelTuningCandidate | null;
  candidates: ModelTuningCandidate[];
};

export async function fetchValueBoard(): Promise<ValueProp[]> {
  const response = await apiFetch("/api/value-board");
  if (!response.ok) {
    throw new Error("Failed to load value board");
  }
  return response.json();
}

export async function fetchWatchlist(): Promise<WatchlistProp[]> {
  const response = await apiFetch("/api/watchlist");
  if (!response.ok) {
    throw new Error("Failed to load watchlist");
  }
  return response.json();
}

export async function fetchDfsFirstHalf(): Promise<DfsFirstHalfEstimate[]> {
  const response = await apiFetch("/api/dfs/first-half");
  if (!response.ok) {
    throw new Error("Failed to load DFS first-half estimates");
  }
  return response.json();
}

export async function fetchPlayerFirstHalfLines(limit = 200): Promise<PlayerFirstHalfLine[]> {
  const response = await apiFetch(`/api/player-first-half-lines?limit=${limit}`);
  if (!response.ok) {
    throw new Error("Failed to load player first-half line history");
  }
  return response.json();
}

export async function fetchSpecialStocks(): Promise<SpecialStocksSnapshot[]> {
  const response = await apiFetch("/api/special/stocks");
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || "Failed to load special props");
  }
  return response.json();
}

export async function fetchSpecialStocksPerformance(): Promise<SpecialStocksPerformance> {
  const response = await apiFetch("/api/special/stats");
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || "Failed to load special stats");
  }
  return response.json();
}

export async function generateSpecialStocks(): Promise<{ generated: number }> {
  const response = await apiFetch("/api/special/stocks/generate", { method: "POST" });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || "Failed to generate special props");
  }
  return response.json();
}

export async function fetchPerformance(): Promise<ModelPerformance> {
  const response = await apiFetch("/api/model-performance");
  if (!response.ok) {
    throw new Error("Failed to load model performance");
  }
  return response.json();
}

export async function fetchGemPerformance(): Promise<GemPerformance> {
  const response = await apiFetch("/api/gem-performance");
  if (!response.ok) {
    throw new Error("Failed to load gem performance");
  }
  return response.json();
}

export async function fetchWatchlistPerformance(): Promise<WatchlistPerformance> {
  const response = await apiFetch("/api/watchlist-performance");
  if (!response.ok) {
    throw new Error("Failed to load watchlist performance");
  }
  return response.json();
}

export async function fetchMatchups(): Promise<Matchup[]> {
  const response = await apiFetch("/api/matchups");
  if (!response.ok) {
    throw new Error("Failed to load matchups");
  }
  return response.json();
}

export async function fetchLineDiscrepancies(): Promise<LineDiscrepancy[]> {
  const response = await apiFetch("/api/line-discrepancies");
  if (!response.ok) {
    throw new Error("Failed to load line discrepancies");
  }
  return response.json();
}

export async function fetchRoster(): Promise<RosterPlayer[]> {
  const response = await apiFetch("/api/roster");
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || "Failed to load roster");
  }
  return response.json();
}

export type OddsImportResult = {
  status: string;
  imported?: number;
  synced_props?: number;
  message?: string;
  source?: string;
  sync_started?: boolean;
  sync_error?: string | null;
  errors?: Array<{ event_id?: string; error: string }>;
};

export async function importOdds(forceRefresh = false): Promise<OddsImportResult> {
  const response = await apiFetch(`/api/odds/import?force_refresh=${forceRefresh ? "true" : "false"}`, {
    method: "POST",
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || "Failed to import sportsbook odds");
  }
  return response.json();
}

export async function importCoversOdds(forceRefresh = false): Promise<OddsImportResult> {
  const response = await apiFetch(`/api/covers/import?force_refresh=${forceRefresh ? "true" : "false"}`, {
    method: "POST",
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || "Failed to import Covers odds");
  }
  return response.json();
}

export async function importRotowireInjuries(forceRefresh = false): Promise<{
  status?: string;
  message?: string | null;
  source: string;
  captured_at: string;
  parsed_rows: number;
  inserted: number;
  from_cache: boolean;
  used_fallback_cache?: boolean;
  fetch_error?: string | null;
  roster?: RosterPlayer[];
  roster_cache_refresh?: {
    status?: string;
    started_at?: string;
  };
}> {
  const response = await apiFetch(`/api/injuries/import/rotowire?force_refresh=${forceRefresh ? "true" : "false"}`, {
    method: "POST",
  });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || "Failed to import Rotowire lineups");
  }
  return response.json();
}

export async function importEspnHistory(
  forceRefresh = false,
  includePlayerStats = true,
  missingOnly = false,
  includePreviousSeason = false,
  selectedDate?: string,
  selectedDates?: string[]
): Promise<{
  season: number;
  seasons: number[];
  selected_date?: string | null;
  selected_dates?: string[];
  synced_props: number;
  predictions?: number;
  source: string;
  settlements?: { settled: number };
  game_settlements?: { settled: number };
}> {
  const params = new URLSearchParams({
    force_refresh: forceRefresh ? "true" : "false",
    include_player_stats: includePlayerStats ? "true" : "false",
    include_previous_season: includePreviousSeason ? "true" : "false",
    missing_only: missingOnly ? "true" : "false"
  });
  if (selectedDate) {
    params.set("selected_date", selectedDate);
  }
  selectedDates?.forEach((date) => {
    if (date) {
      params.append("selected_dates", date);
    }
  });
  const response = await apiFetch(`/api/history/import/espn?${params.toString()}`, {
    method: "POST",
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to refresh completed game results");
  }
  return response.json();
}

export type MissingEspnGame = {
  id: number;
  game_date: string;
  start_time: string;
  status: string;
  home_team: string;
  away_team: string;
  espn_event_id?: number | null;
  has_team_results: boolean;
  has_player_stats: boolean;
  has_team_boxscores: boolean;
  team_results_count: number;
  player_stats_count: number;
  team_boxscores_count: number;
  missing_reasons: string[];
};

export async function fetchMissingEspnScores(limit = 30): Promise<{
  dates: string[];
  games: MissingEspnGame[];
  count: number;
  source: string;
}> {
  const response = await fetch(`/api/history/missing/espn?limit=${limit}`);
  if (!response.ok) {
    throw new Error("Failed to load missing ESPN scores");
  }
  return response.json();
}

export async function importMissingEspnScores(
  forceRefresh = true,
  includePlayerStats = true,
  missingOnly = true,
  limit = 30
): Promise<{
  selected_dates?: string[];
  missing_dates?: string[];
  missing_count?: number;
  synced_props?: number;
  predictions?: number;
  settlements?: { settled: number };
  source: string;
  message?: string;
}> {
  const params = new URLSearchParams({
    force_refresh: forceRefresh ? "true" : "false",
    include_player_stats: includePlayerStats ? "true" : "false",
    missing_only: missingOnly ? "true" : "false",
    limit: String(limit)
  });
  const response = await apiFetch(`/api/history/import/espn-missing?${params.toString()}`, {
    method: "POST",
  });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to import missing ESPN scores");
  }
  return response.json();
}

export async function repairCurrentSlateProps(): Promise<{
  status?: string;
  started_at?: string | null;
  scope?: string | null;
  target_game_ids?: number[];
  scanned_props?: number;
  synced_props?: number;
  rebuilt_predictions?: number;
}> {
  let response = await apiFetch("/api/props/repair-current-slate", { method: "POST" });

  // Backward compatibility for stale backend deployments that still expose only the legacy route.
  if (response.status === 404 || response.status === 405) {
    response = await apiFetch("/api/recalculate", { method: "POST" });
  }

  if (!response.ok) {
    const detail = await readErrorDetail(response);
    if (response.status === 401) {
      throw new Error("Unauthorized. Sign in on the Data tab and try Recalculate again.");
    }
    if (response.status === 403) {
      throw new Error(
        "Admin session verification failed. Sign out and sign back in, then retry Recalculate from the same public domain."
      );
    }
    throw new Error(detail || "Failed to repair current slate projections");
  }

  const payload = (await response.json()) as {
    status?: string;
    started_at?: string | null;
    scope?: string | null;
    target_game_ids?: number[];
    scanned_props?: number;
    synced_props?: number;
    rebuilt_predictions?: number;
    predictions?: number;
  };

  if (payload.status === "queued" || payload.status === "running" || payload.status === "busy") {
    return waitForPropSyncCompletion(payload.started_at);
  }

  // Normalize the legacy response shape to match the guarded current-slate response.
  if (typeof payload.predictions === "number" && payload.rebuilt_predictions == null) {
    return {
      status: "completed",
      rebuilt_predictions: payload.predictions,
      scanned_props: payload.scanned_props,
      synced_props: payload.synced_props,
      scope: payload.scope,
      started_at: payload.started_at,
      target_game_ids: payload.target_game_ids,
    };
  }

  return payload;
}

export async function settleProps(
  selectedDate?: string,
  selectedDates?: string[]
): Promise<{
  props: { settled: number; selected_date?: string | null; selected_dates?: string[] };
  games: { settled: number; selected_date?: string | null; selected_dates?: string[] };
  special?: { settled: number; selected_date?: string | null; selected_dates?: string[] };
  selected_date?: string | null;
  selected_dates?: string[];
}> {
  const params = new URLSearchParams();
  if (selectedDate) {
    params.set("selected_date", selectedDate);
  }
  selectedDates?.forEach((date) => {
    if (date) {
      params.append("selected_dates", date);
    }
  });
  const query = params.toString();
  const response = await apiFetch(`/api/settle-props${query ? `?${query}` : ""}`, { method: "POST" });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to settle props");
  }
  return response.json();
}

export type UnsettledPropAuditItem = {
  game_id: number;
  game_date: string;
  home_team: string;
  away_team: string;
  player_id: number;
  player_name: string;
  prop_count: number;
  markets: string[];
  has_boxscore: number;
  explicit_dnp: number;
  availability_reason?: string | null;
  review_status: "confirmed_dnp" | "missing_boxscore" | "settlement_context_missing";
};

export async function fetchUnsettledPropAudit(): Promise<{ count: number; items: UnsettledPropAuditItem[] }> {
  const response = await apiFetch("/api/admin/unsettled-props");
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    throw new Error(detail || "Failed to audit unsettled props");
  }
  return response.json();
}

export async function voidDnpProps(gameId: number, playerId: number): Promise<{ voided_prop_lines: number }> {
  const params = new URLSearchParams({ game_id: String(gameId), player_id: String(playerId), confirmation: "VOID DNP" });
  const response = await apiFetch(`/api/admin/unsettled-props/void-dnp?${params}`, { method: "POST" });
  if (!response.ok) throw new Error((await readErrorDetail(response)) || "Failed to void DNP props");
  return response.json();
}

export async function fetchModelRuns(): Promise<{ latest: ModelRun | null; runs: ModelRun[] }> {
  const response = await apiFetch("/api/models/runs");
  if (!response.ok) {
    throw new Error("Failed to load model runs");
  }
  return response.json();
}

export async function trainModel(): Promise<{
  status?: string;
  started_at?: string | null;
  message?: string;
}> {
  const response = await apiFetch("/api/models/train", { method: "POST" });
  if (!response.ok) {
    const detail = await readErrorDetail(response);
    if (response.status === 401) {
      throw new Error("Unauthorized. Sign in on the Data tab and try Train again.");
    }
    if (response.status === 403) {
      throw new Error(
        "Admin session verification failed. Sign out and sign back in, then retry Train from the same public domain."
      );
    }
    throw new Error(detail || "Failed to train model");
  }
  const payload = await response.json();
  if (payload?.status === "busy") {
    throw new Error(typeof payload?.message === "string" && payload.message ? payload.message : "Model training is already running.");
  }
  return payload;
}

export async function tuneModel(): Promise<ModelTuningRun> {
  const response = await apiFetch("/api/models/tune", { method: "POST" });
  if (!response.ok) {
    throw new Error("Failed to tune model");
  }
  return response.json();
}
