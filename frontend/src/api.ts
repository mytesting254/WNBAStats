export type ValueProp = {
  id: number;
  game_id: number;
  player: string;
  position?: string | null;
  team: string;
  team_logo_url?: string | null;
  sportsbook: string;
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
  recent_values?: number[];
  recent_minutes?: number[];
};

export type WatchlistProp = ValueProp & {
  prop_line_id: number;
  away_team?: string;
  home_team?: string;
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
  prop_sync: {
    running: boolean;
    started_at: string | null;
    finished_at: string | null;
    last_error: string | null;
    last_result: Record<string, unknown> | null;
  };
};

const RUNTIME_API_KEY = (window.__APP_CONFIG__?.apiKey ?? "").trim();
const BUILD_API_KEY = (import.meta.env.VITE_API_KEY ?? "").trim();
const API_KEY = RUNTIME_API_KEY || BUILD_API_KEY;
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
  winner_pick: string;
  ats_pick: string;
  ats_edge: number | null;
  total_pick: string;
  total_edge: number | null;
  game_confidence: string;
  game_reason: string;
  home: TeamLast10;
  away: TeamLast10;
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
  player_id?: number;
  rotation_role?: string | null;
  recent_minutes_avg?: number | null;
  recent_contribution_avg?: number | null;
  player_impact_score?: number | null;
  team_injury_factor?: number | null;
  team_missing_key_players?: number | null;
  team_penalty_points?: number | null;
};

export type ModelRunMetric = {
  rows: number;
  mae: number | null;
  rmse: number | null;
  bias: number | null;
  directional_accuracy: number | null;
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
    throw new Error("Failed to load roster");
  }
  return response.json();
}

export type OddsImportResult = {
  status: string;
  imported?: number;
  synced_props?: number;
  message?: string;
  source?: string;
  errors?: Array<{ event_id?: string; error: string }>;
};

export async function importOdds(forceRefresh = false): Promise<OddsImportResult> {
  const response = await apiFetch(`/api/odds/import?force_refresh=${forceRefresh ? "true" : "false"}`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error("Failed to import sportsbook odds");
  }
  return response.json();
}

export async function importCoversOdds(forceRefresh = false): Promise<OddsImportResult> {
  const response = await apiFetch(`/api/covers/import?force_refresh=${forceRefresh ? "true" : "false"}`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error("Failed to import Covers odds");
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
}> {
  const response = await apiFetch(`/api/injuries/import/rotowire?force_refresh=${forceRefresh ? "true" : "false"}`, {
    method: "POST",
  });
  if (!response.ok) {
    throw new Error("Failed to import Rotowire lineups");
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
  synced_props?: number;
  rebuilt_predictions?: number;
}> {
  const response = await apiFetch("/api/props/repair-current-slate", { method: "POST" });
  if (!response.ok) {
    let detail = "";
    try {
      const payload = await response.json();
      detail = typeof payload?.detail === "string" ? payload.detail : "";
    } catch {
      detail = "";
    }
    throw new Error(detail || "Failed to repair current slate projections");
  }
  return response.json();
}

export async function settleProps(
  selectedDate?: string,
  selectedDates?: string[]
): Promise<{
  props: { settled: number; selected_date?: string | null; selected_dates?: string[] };
  games: { settled: number; selected_date?: string | null; selected_dates?: string[] };
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

export async function fetchModelRuns(): Promise<{ latest: ModelRun | null; runs: ModelRun[] }> {
  const response = await fetch("/api/models/runs");
  if (!response.ok) {
    throw new Error("Failed to load model runs");
  }
  return response.json();
}

export async function trainModel(): Promise<ModelRun> {
  const response = await apiFetch("/api/models/train", { method: "POST" });
  if (!response.ok) {
    throw new Error("Failed to train model");
  }
  return response.json();
}

export async function tuneModel(): Promise<ModelTuningRun> {
  const response = await apiFetch("/api/models/tune", { method: "POST" });
  if (!response.ok) {
    throw new Error("Failed to tune model");
  }
  return response.json();
}
