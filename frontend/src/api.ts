export type ValueProp = {
  id: number;
  game_id: number;
  player: string;
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
  const response = await fetch(`/api/gems/snapshot?${params.toString()}`, { method: "POST" });
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
};

export type ModelRunMetric = {
  rows: number;
  mae: number | null;
  rmse: number | null;
  bias: number | null;
  directional_accuracy: number | null;
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

export async function fetchValueBoard(): Promise<ValueProp[]> {
  const response = await fetch("/api/value-board");
  if (!response.ok) {
    throw new Error("Failed to load value board");
  }
  return response.json();
}

export async function fetchWatchlist(): Promise<WatchlistProp[]> {
  const response = await fetch("/api/watchlist");
  if (!response.ok) {
    throw new Error("Failed to load watchlist");
  }
  return response.json();
}

export async function fetchPerformance(): Promise<ModelPerformance> {
  const response = await fetch("/api/model-performance");
  if (!response.ok) {
    throw new Error("Failed to load model performance");
  }
  return response.json();
}

export async function fetchGemPerformance(): Promise<GemPerformance> {
  const response = await fetch("/api/gem-performance");
  if (!response.ok) {
    throw new Error("Failed to load gem performance");
  }
  return response.json();
}

export async function fetchWatchlistPerformance(): Promise<WatchlistPerformance> {
  const response = await fetch("/api/watchlist-performance");
  if (!response.ok) {
    throw new Error("Failed to load watchlist performance");
  }
  return response.json();
}

export async function fetchMatchups(): Promise<Matchup[]> {
  const response = await fetch("/api/matchups");
  if (!response.ok) {
    throw new Error("Failed to load matchups");
  }
  return response.json();
}

export async function fetchLineDiscrepancies(): Promise<LineDiscrepancy[]> {
  const response = await fetch("/api/line-discrepancies");
  if (!response.ok) {
    throw new Error("Failed to load line discrepancies");
  }
  return response.json();
}

export async function fetchRoster(): Promise<RosterPlayer[]> {
  const response = await fetch("/api/roster");
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
  const response = await fetch(`/api/odds/import?force_refresh=${forceRefresh ? "true" : "false"}`, { method: "POST" });
  if (!response.ok) {
    throw new Error("Failed to import sportsbook odds");
  }
  return response.json();
}

export async function importCoversOdds(forceRefresh = false): Promise<OddsImportResult> {
  const response = await fetch(`/api/covers/import?force_refresh=${forceRefresh ? "true" : "false"}`, { method: "POST" });
  if (!response.ok) {
    throw new Error("Failed to import Covers odds");
  }
  return response.json();
}

export async function importRotowireInjuries(forceRefresh = false): Promise<{
  source: string;
  captured_at: string;
  parsed_rows: number;
  inserted: number;
  from_cache: boolean;
}> {
  const response = await fetch(`/api/injuries/import/rotowire?force_refresh=${forceRefresh ? "true" : "false"}`, { method: "POST" });
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
  const response = await fetch(`/api/history/import/espn?${params.toString()}`, { method: "POST" });
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
  const response = await fetch(`/api/history/import/espn-missing?${params.toString()}`, { method: "POST" });
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

export async function recalculate(): Promise<void> {
  const response = await fetch("/api/recalculate", { method: "POST" });
  if (!response.ok) {
    throw new Error("Failed to recalculate projections");
  }
}

export async function fetchModelRuns(): Promise<{ latest: ModelRun | null; runs: ModelRun[] }> {
  const response = await fetch("/api/models/runs");
  if (!response.ok) {
    throw new Error("Failed to load model runs");
  }
  return response.json();
}

export async function trainModel(): Promise<ModelRun> {
  const response = await fetch("/api/models/train", { method: "POST" });
  if (!response.ok) {
    throw new Error("Failed to train model");
  }
  return response.json();
}
