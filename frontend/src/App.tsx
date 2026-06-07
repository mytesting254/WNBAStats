import { BrainCircuit, CalendarDays, Database, ListChecks, RefreshCw, ShieldCheck, SlidersHorizontal, TrendingUp } from "lucide-react";
import { useEffect, useMemo, useRef, useState } from "react";
import {
  fetchAuthState,
  fetchMissingEspnScores,
  fetchLineDiscrepancies,
  fetchGemPerformance,
  fetchMatchups,
  fetchModelRuns,
  fetchPerformance,
  fetchRoster,
  fetchValueBoard,
  fetchWatchlistPerformance,
  fetchWatchlist,
  importCoversOdds,
  createGemSnapshot,
  importEspnHistory,
  importMissingEspnScores,
  importOdds,
  importRotowireInjuries,
  loginAdmin,
  logoutAdmin,
  recalculate,
  setCsrfToken,
  trainModel,
  type AuthState,
  type CoversRecordRow,
  type GemPerformance,
  type LineDiscrepancy,
  type Matchup,
  type MissingEspnGame,
  type ModelPerformance,
  type ModelRun,
  type RosterPlayer,
  type TeamLast10,
  type ValueProp,
  type WatchlistPerformance,
  type WatchlistProp
} from "./api";

const markets = [
  { id: "all", label: "All" },
  { id: "points", label: "PTS" },
  { id: "rebounds", label: "REB" },
  { id: "assists", label: "AST" },
  { id: "points_rebounds", label: "P+R" },
  { id: "points_assists", label: "P+A" },
  { id: "rebounds_assists", label: "R+A" },
  { id: "points_rebounds_assists", label: "PRA" },
  { id: "threes", label: "3PM" },
  { id: "steals", label: "STL" },
  { id: "blocks", label: "BLK" },
  { id: "blocks_steals", label: "STL+BLK" }
];

const WNBA_TEAM_LOGOS: Record<string, string> = {
  ATL: "/team-logos/atl.png",
  CHI: "/team-logos/chi.png",
  CON: "/team-logos/conn.png",
  DAL: "/team-logos/dal.png",
  GS: "/team-logos/gs.png",
  IND: "/team-logos/ind.png",
  LV: "/team-logos/lv.png",
  LA: "/team-logos/la.png",
  MIN: "/team-logos/min.png",
  NY: "/team-logos/ny.png",
  PHX: "/team-logos/phx.png",
  SEA: "/team-logos/sea.png",
  TOR: "/team-logos/tor.png",
  WSH: "/team-logos/wsh.png",
  POR: "/team-logos/por.png",
  PDX: "/team-logos/por.png"
};

type DashboardTab = "props" | "gems" | "watchlist" | "matchups" | "parlays" | "discrepancies" | "roster" | "models" | "data";
type CandidateSortField = "expected_value" | "edge" | "projection" | "line" | "projection_gap" | "model_probability" | "confidence" | "player";
type DiscrepancySortField = "line_gap" | "price_gap" | "books" | "player_name";
type SortDirection = "desc" | "asc";

export function App() {
  const [props, setProps] = useState<ValueProp[]>([]);
  const [watchlist, setWatchlist] = useState<WatchlistProp[]>([]);
  const [matchups, setMatchups] = useState<Matchup[]>([]);
  const [discrepancies, setDiscrepancies] = useState<LineDiscrepancy[]>([]);
  const [roster, setRoster] = useState<RosterPlayer[]>([]);
  const [performance, setPerformance] = useState<ModelPerformance | null>(null);
  const [gemPerformance, setGemPerformance] = useState<GemPerformance | null>(null);
  const [watchlistPerformance, setWatchlistPerformance] = useState<WatchlistPerformance | null>(null);
  const [modelRuns, setModelRuns] = useState<ModelRun[]>([]);
  const [latestModelRun, setLatestModelRun] = useState<ModelRun | null>(null);
  const [training, setTraining] = useState(false);
  const [importingOdds, setImportingOdds] = useState(false);
  const [importingCoversOdds, setImportingCoversOdds] = useState(false);
  const [refreshingResults, setRefreshingResults] = useState(false);
  const [refreshingMissingScores, setRefreshingMissingScores] = useState(false);
  const [refreshingRoster, setRefreshingRoster] = useState(false);
  const [recalculating, setRecalculating] = useState(false);
  const [snapshottingGems, setSnapshottingGems] = useState(false);
  const [activeTab, setActiveTab] = useState<DashboardTab>("matchups");
  const [market, setMarket] = useState("all");
  const [confidence, setConfidence] = useState("all");
  const [modelProbabilityOrder, setModelProbabilityOrder] = useState<SortDirection>("desc");
  const [selected, setSelected] = useState<ValueProp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [operationStatus, setOperationStatus] = useState<string | null>(null);
  const [missingEspnDates, setMissingEspnDates] = useState<string[]>([]);
  const [missingEspnGames, setMissingEspnGames] = useState<MissingEspnGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [authState, setAuthState] = useState<AuthState>({ authenticated: false, user: null, csrf_token: null });
  const [authLoading, setAuthLoading] = useState(true);
  const [authSubmitting, setAuthSubmitting] = useState(false);
  const loadRequestIdRef = useRef(0);
  const [adminUsername, setAdminUsername] = useState("");
  const [adminPassword, setAdminPassword] = useState("");

  async function load() {
    const requestId = ++loadRequestIdRef.current;
    setLoading(true);
    setError(null);
    const [
      boardResult,
      performanceResult,
      gemPerformanceResult,
      watchlistPerformanceResult,
      watchlistResult,
      matchupsResult,
      discrepanciesResult,
      modelRunsResult,
      rosterResult
    ] = await Promise.allSettled([
      fetchValueBoard(),
      fetchPerformance(),
      fetchGemPerformance(),
      fetchWatchlistPerformance(),
      fetchWatchlist(),
      fetchMatchups(),
      fetchLineDiscrepancies(),
      fetchModelRuns(),
      fetchRoster()
    ]);

    const failures: string[] = [];

    if (requestId !== loadRequestIdRef.current) {
      return;
    }

    if (boardResult.status === "fulfilled") {
      setProps(boardResult.value);
      setSelected((current) => current ?? boardResult.value[0] ?? null);
    } else {
      failures.push("value board");
      setProps([]);
      setSelected(null);
    }

    if (performanceResult.status === "fulfilled") {
      setPerformance(performanceResult.value);
    } else {
      failures.push("model performance");
      setPerformance(null);
    }

    if (gemPerformanceResult.status === "fulfilled") {
      setGemPerformance(gemPerformanceResult.value);
    } else {
      failures.push("gem performance");
      setGemPerformance(null);
    }

    if (watchlistPerformanceResult.status === "fulfilled") {
      setWatchlistPerformance(watchlistPerformanceResult.value);
    } else {
      failures.push("watchlist performance");
      setWatchlistPerformance(null);
    }

    if (watchlistResult.status === "fulfilled") {
      setWatchlist(watchlistResult.value);
    } else {
      failures.push("watchlist");
      setWatchlist([]);
    }

    if (matchupsResult.status === "fulfilled") {
      setMatchups(matchupsResult.value);
    } else {
      failures.push("matchups");
      setMatchups([]);
    }

    if (discrepanciesResult.status === "fulfilled") {
      setDiscrepancies(discrepanciesResult.value);
    } else {
      failures.push("line discrepancies");
      setDiscrepancies([]);
    }

    if (modelRunsResult.status === "fulfilled") {
      setModelRuns(modelRunsResult.value.runs);
      setLatestModelRun(modelRunsResult.value.latest);
    } else {
      failures.push("model runs");
      setModelRuns([]);
      setLatestModelRun(null);
    }

    if (rosterResult.status === "fulfilled") {
      setRoster(rosterResult.value);
    } else {
      failures.push("roster");
      setRoster([]);
    }

    if (failures.length > 0) {
      setError(`Some dashboard data failed to load: ${failures.join(", ")}.`);
    }

    if (requestId === loadRequestIdRef.current) {
      setLoading(false);
    }
  }

  useEffect(() => {
    load();
  }, []);

  async function loadAuth() {
    setAuthLoading(true);
    try {
      const result = await fetchAuthState();
      setCsrfToken(result.csrf_token);
      setAuthState(result);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to load auth state");
      setCsrfToken("");
      setAuthState({ authenticated: false, user: null, csrf_token: null });
    } finally {
      setAuthLoading(false);
    }
  }

  useEffect(() => {
    loadAuth();
  }, []);

  const filtered = useMemo(() => {
    return props
      .filter((prop) => {
        const marketMatch = market === "all" || prop.market === market;
        const confidenceMatch = confidence === "all" || prop.confidence === confidence;
        return marketMatch && confidenceMatch;
      })
      .sort((a, b) =>
        modelProbabilityOrder === "asc"
          ? a.model_probability - b.model_probability
          : b.model_probability - a.model_probability
      );
  }, [props, market, confidence, modelProbabilityOrder]);
  const gems = useMemo(() => buildGems(props, discrepancies), [props, discrepancies]);

  async function handleRecalculate() {
    setRecalculating(true);
    setOperationStatus(null);
    try {
      await recalculate();
      await load();
      setOperationStatus("Projection board recalculated from the current prop lines and player history.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to recalculate projections");
    } finally {
      setRecalculating(false);
    }
  }

  async function handleSnapshotGems() {
    setSnapshottingGems(true);
    setOperationStatus("Capturing daily gem snapshot...");
    setError(null);
    try {
      const result = await createGemSnapshot(undefined, "balanced");
      await load();
      setOperationStatus(
        `Gem snapshot saved for ${result.snapshot_date}. Tracked ${result.tracked} gems (${result.settled} settled).`
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to snapshot gems");
    } finally {
      setSnapshottingGems(false);
    }
  }

  async function handleTrainModel() {
    setTraining(true);
    setError(null);
    try {
      await trainModel();
      const modelRunBoard = await fetchModelRuns();
      setModelRuns(modelRunBoard.runs);
      setLatestModelRun(modelRunBoard.latest);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to train model");
    } finally {
      setTraining(false);
    }
  }

  async function handleAdminLogin() {
    setAuthSubmitting(true);
    setError(null);
    try {
      const result = await loginAdmin(adminUsername, adminPassword);
      setCsrfToken(result.csrf_token);
      setAuthState(result);
      setAdminPassword("");
      setOperationStatus("Admin session active.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to sign in");
    } finally {
      setAuthSubmitting(false);
    }
  }

  async function handleAdminLogout() {
    setAuthSubmitting(true);
    setError(null);
    try {
      const result = await logoutAdmin();
      setCsrfToken("");
      setAuthState(result);
      setOperationStatus("Signed out of admin session.");
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to sign out");
    } finally {
      setAuthSubmitting(false);
    }
  }

  async function handleImportOdds(forceRefresh = false) {
    setImportingOdds(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await importOdds(forceRefresh);
      if (result.status === "missing_api_key") {
        setError(result.message ?? "Set ODDS_API_KEY to import sportsbook odds");
        return;
      }
      await load();
      setOperationStatus(`${forceRefresh ? "Fresh" : "Saved"} sportsbook odds loaded. Imported ${result.imported ?? 0} sportsbook rows.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to import sportsbook odds");
    } finally {
      setImportingOdds(false);
    }
  }

  async function handleImportCoversOdds(forceRefresh = false) {
    setImportingCoversOdds(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await importCoversOdds(forceRefresh);
      if (result.status === "failed") {
        setError(result.message ?? "Unable to import Covers odds");
        return;
      }
      setOperationStatus(result.message ?? `${forceRefresh ? "Fresh" : "Saved"} Covers odds loaded. Imported ${result.imported ?? 0} sportsbook rows from ${result.source ?? "covers"}.`);
      void load().catch((err) => {
        setError(
          `Covers import succeeded, but dashboard reload failed: ${
            err instanceof Error ? err.message : "unknown error"
          }`
        );
      });
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to import Covers odds");
    } finally {
      setImportingCoversOdds(false);
    }
  }

  async function handleRefreshResults(
    forceRefresh = false,
    includePlayerStats = true,
    missingOnly = false,
    includePreviousSeason = false,
    selectedDates?: string[]
  ) {
    if (forceRefresh) {
      const dateScope = selectedDates?.length ? ` for ${selectedDates.join(", ")}` : " for this season and the previous season";
      const confirmed = window.confirm(
        `Hard refresh will fetch fresh ESPN completed game and box score data${dateScope}. Continue?`
      );
      if (!confirmed) {
        return;
      }
    }
    setRefreshingResults(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await importEspnHistory(forceRefresh, includePlayerStats, missingOnly, includePreviousSeason, undefined, selectedDates);
      await load();
      const resultDates = result.selected_dates?.length ? result.selected_dates : result.selected_date ? [result.selected_date] : [];
      const scope = resultDates.length ? ` for ${resultDates.join(", ")}` : ` for ${result.seasons?.join(", ") ?? result.season}`;
      setOperationStatus(buildEspnOperationStatus({
        mode: missingOnly ? "Missing" : forceRefresh ? "Fresh" : "Saved",
        scope,
        syncedProps: result.synced_props ?? 0,
        predictions: result.predictions ?? 0,
        settledProps: result.settlements?.settled ?? 0
      }));
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to refresh completed results");
    } finally {
      setRefreshingResults(false);
    }
  }

  async function handleRefreshRoster(forceRefresh = true) {
    setRefreshingRoster(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await importRotowireInjuries(forceRefresh);
      const refreshedRoster = await fetchRoster();
      setRoster(refreshedRoster);
      void load();
      if (result.status === "db_locked") {
        setOperationStatus(result.message ?? "Roster refresh skipped because the database is busy. Try again in a few seconds.");
        return;
      }
      setOperationStatus(
        result.used_fallback_cache
          ? `Rotowire refresh fell back to saved roster data. Parsed ${result.parsed_rows ?? 0} rows${result.captured_at ? ` (${result.captured_at})` : ""}.`
          : `${forceRefresh ? "Fresh" : "Cached"} Rotowire lineup pull complete. Parsed ${result.parsed_rows ?? 0} rows${result.captured_at ? ` (${result.captured_at})` : ""}.`
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to refresh Rotowire lineup status");
    } finally {
      setRefreshingRoster(false);
    }
  }

  async function handleScanMissingScores() {
    setRefreshingMissingScores(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await fetchMissingEspnScores(30);
      setMissingEspnDates(result.dates ?? []);
      setMissingEspnGames(result.games ?? []);
      setOperationStatus(
        result.count
          ? `Found ${result.count} missing completed ESPN game score rows across ${result.dates.length} date(s).`
          : "No missing completed ESPN game scores found."
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to scan missing ESPN scores");
    } finally {
      setRefreshingMissingScores(false);
    }
  }

  async function handleImportMissingScores() {
    setRefreshingMissingScores(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await importMissingEspnScores(true, true, true, 30);
      const usedDates = result.missing_dates?.length ? result.missing_dates : result.selected_dates ?? [];
      await load();
      const latest = await fetchMissingEspnScores(30);
      setMissingEspnDates(latest.dates ?? []);
      setMissingEspnGames(latest.games ?? []);
      if (result.message) {
        setOperationStatus(result.message);
      } else {
        setOperationStatus(
          buildEspnOperationStatus({
            mode: "Imported missing ESPN scores",
            scope: ` for ${usedDates.length} date(s)`,
            syncedProps: result.synced_props ?? 0,
            predictions: result.predictions ?? 0,
            settledProps: result.settlements?.settled ?? 0
          })
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to import missing ESPN scores");
    } finally {
      setRefreshingMissingScores(false);
    }
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">WNBA pregame model</p>
          <h1>{tabTitle(activeTab)}</h1>
        </div>
        <div className="topbar-actions">
          <button className="icon-button text-button" onClick={load} disabled={loading} title="Reload dashboard data">
            <RefreshCw size={18} />
            {loading ? "Loading" : "Reload"}
          </button>
        </div>
      </header>

      <nav className="tabs" aria-label="Dashboard sections">
        <button className={activeTab === "props" ? "active" : ""} onClick={() => setActiveTab("props")}>
          <TrendingUp size={18} />
          Props
        </button>
        <button className={activeTab === "gems" ? "active" : ""} onClick={() => setActiveTab("gems")}>
          <TrendingUp size={18} />
          Gems
        </button>
        <button className={activeTab === "watchlist" ? "active" : ""} onClick={() => setActiveTab("watchlist")}>
          <ListChecks size={18} />
          Watchlist
        </button>
        <button className={activeTab === "matchups" ? "active" : ""} onClick={() => setActiveTab("matchups")}>
          <CalendarDays size={18} />
          Matchups
        </button>
        <button className={activeTab === "parlays" ? "active" : ""} onClick={() => setActiveTab("parlays")}>
          <ListChecks size={18} />
          Parlays
        </button>
        <button className={activeTab === "discrepancies" ? "active" : ""} onClick={() => setActiveTab("discrepancies")}>
          <SlidersHorizontal size={18} />
          Discrepancies
        </button>
        <button className={activeTab === "models" ? "active" : ""} onClick={() => setActiveTab("models")}>
          <BrainCircuit size={18} />
          Models
        </button>
        <button className={activeTab === "roster" ? "active" : ""} onClick={() => setActiveTab("roster")}>
          <ShieldCheck size={18} />
          Roster
        </button>
        <button className={activeTab === "data" ? "active" : ""} onClick={() => setActiveTab("data")}>
          <Database size={18} />
          Data
        </button>
      </nav>

      <section className="summary-grid">
        <Metric
          label={activeTab === "props" ? "Props ranked" : activeTab === "gems" ? "Gem candidates" : activeTab === "watchlist" ? "Watchlist legs" : activeTab === "matchups" ? "Games" : activeTab === "parlays" ? "Candidate legs" : activeTab === "discrepancies" ? "Line gaps" : activeTab === "roster" ? "Rostered players" : activeTab === "models" ? "Training rows" : "Missing score dates"}
          value={activeTab === "props" ? filtered.length.toString() : activeTab === "gems" ? gems.length.toString() : activeTab === "watchlist" ? watchlist.length.toString() : activeTab === "matchups" ? matchups.length.toString() : activeTab === "parlays" ? parlayCandidateCount(matchups).toString() : activeTab === "discrepancies" ? discrepancies.length.toString() : activeTab === "roster" ? roster.length.toString() : activeTab === "models" ? (latestModelRun?.training_rows ?? 0).toString() : missingEspnDates.length.toString()}
        />
        <Metric
          label={activeTab === "props" ? "Best EV" : activeTab === "gems" ? "Top gem score" : activeTab === "watchlist" ? "Top watch EV" : activeTab === "matchups" ? "Teams tracked" : activeTab === "parlays" ? "Games with legs" : activeTab === "discrepancies" ? "Books compared" : activeTab === "roster" ? "Unavailable players" : activeTab === "models" ? "Latest MAE" : "Upcoming games"}
          value={activeTab === "props" ? formatPercent(filtered[0]?.expected_value) : activeTab === "gems" ? formatNumber(gems[0]?.gem_score ?? null) : activeTab === "watchlist" ? formatPercent(watchlist[0]?.expected_value) : activeTab === "matchups" ? (matchups.length * 2).toString() : activeTab === "parlays" ? gamesWithParlayCandidates(matchups).toString() : activeTab === "discrepancies" ? countDiscrepancyBooks(discrepancies).toString() : activeTab === "roster" ? roster.length.toString() : activeTab === "models" ? formatLatestMae(latestModelRun) : matchups.length.toString()}
        />
        <Metric label="Settled props" value={(performance?.total_settled ?? performance?.settled ?? 0).toString()} />
        <Metric
          label="Win Rates"
          className="metric-compact"
          value={`Model ${performance?.win_rate == null ? "Pending" : formatPercent(performance.win_rate)} | Gems ${gemPerformance?.win_rate == null ? "Pending" : formatPercent(gemPerformance.win_rate)} | Watch ${watchlistPerformance?.win_rate == null ? "Pending" : formatPercent(watchlistPerformance.win_rate)}`}
        />
      </section>
      {performance?.message ? <div className="summary-message">{performance.message}</div> : null}

      {activeTab === "props" ? (
        <PropsView
          filtered={filtered}
          loading={loading}
          error={error}
          market={market}
          confidence={confidence}
          modelProbabilityOrder={modelProbabilityOrder}
          selected={selected}
          setMarket={setMarket}
          setConfidence={setConfidence}
          setModelProbabilityOrder={setModelProbabilityOrder}
          setSelected={setSelected}
        />
      ) : activeTab === "gems" ? (
        <GemsView gems={gems} matchups={matchups} loading={loading} error={error} />
      ) : activeTab === "watchlist" ? (
        <WatchlistView watchlist={watchlist} loading={loading} error={error} />
      ) : activeTab === "matchups" ? (
        <MatchupsView matchups={matchups} loading={loading} error={error} />
      ) : activeTab === "parlays" ? (
        <ParlayCandidatesView matchups={matchups} loading={loading} error={error} />
      ) : activeTab === "discrepancies" ? (
        <DiscrepanciesView discrepancies={discrepancies} loading={loading} error={error} />
      ) : activeTab === "data" ? (
        <DataView
          loading={loading}
          authLoading={authLoading}
          authSubmitting={authSubmitting}
          authState={authState}
          adminUsername={adminUsername}
          adminPassword={adminPassword}
          error={error}
          status={operationStatus}
          importingOdds={importingOdds}
          importingCoversOdds={importingCoversOdds}
          refreshingResults={refreshingResults}
          refreshingMissingScores={refreshingMissingScores}
          recalculating={recalculating}
          snapshottingGems={snapshottingGems}
          propsCount={props.length}
          matchupsCount={matchups.length}
          discrepanciesCount={discrepancies.length}
          missingEspnDates={missingEspnDates}
          missingEspnGames={missingEspnGames}
          onImportOdds={handleImportOdds}
          onImportCoversOdds={handleImportCoversOdds}
          onRefreshResults={handleRefreshResults}
          onScanMissingScores={handleScanMissingScores}
          onImportMissingScores={handleImportMissingScores}
          onAdminLogin={handleAdminLogin}
          onAdminLogout={handleAdminLogout}
          onAdminUsernameChange={setAdminUsername}
          onAdminPasswordChange={setAdminPassword}
          onRecalculate={handleRecalculate}
          onSnapshotGems={handleSnapshotGems}
          onReload={load}
        />
      ) : activeTab === "roster" ? (
        <RosterView
          roster={roster}
          loading={loading}
          error={error}
          status={operationStatus}
          refreshing={refreshingRoster}
          onRefresh={() => handleRefreshRoster(true)}
          canRefresh={Boolean(authState.authenticated && authState.user?.is_admin)}
        />
      ) : (
        <ModelsView runs={modelRuns} latest={latestModelRun} loading={loading || training} error={error} onTrain={handleTrainModel} />
      )}
    </main>
  );
}

function DataView({
  loading,
  authLoading,
  authSubmitting,
  authState,
  adminUsername,
  adminPassword,
  error,
  status,
  importingOdds,
  importingCoversOdds,
  refreshingResults,
  refreshingMissingScores,
  recalculating,
  snapshottingGems,
  propsCount,
  matchupsCount,
  discrepanciesCount,
  missingEspnDates,
  missingEspnGames,
  onImportOdds,
  onImportCoversOdds,
  onRefreshResults,
  onScanMissingScores,
  onImportMissingScores,
  onAdminLogin,
  onAdminLogout,
  onAdminUsernameChange,
  onAdminPasswordChange,
  onRecalculate,
  onSnapshotGems,
  onReload
}: {
  loading: boolean;
  authLoading: boolean;
  authSubmitting: boolean;
  authState: AuthState;
  adminUsername: string;
  adminPassword: string;
  error: string | null;
  status: string | null;
  importingOdds: boolean;
  importingCoversOdds: boolean;
  refreshingResults: boolean;
  refreshingMissingScores: boolean;
  recalculating: boolean;
  snapshottingGems: boolean;
  propsCount: number;
  matchupsCount: number;
  discrepanciesCount: number;
  missingEspnDates: string[];
  missingEspnGames: MissingEspnGame[];
  onImportOdds: (forceRefresh: boolean) => void;
  onImportCoversOdds: (forceRefresh: boolean) => void;
  onRefreshResults: (
    forceRefresh: boolean,
    includePlayerStats?: boolean,
    missingOnly?: boolean,
    includePreviousSeason?: boolean,
    selectedDates?: string[]
  ) => void;
  onScanMissingScores: () => void;
  onImportMissingScores: () => void;
  onAdminLogin: () => void;
  onAdminLogout: () => void;
  onAdminUsernameChange: (value: string) => void;
  onAdminPasswordChange: (value: string) => void;
  onRecalculate: () => void;
  onSnapshotGems: () => void;
  onReload: () => void;
}) {
  const [resultDate, setResultDate] = useState(todayInputValue());
  const [batchStartDate, setBatchStartDate] = useState(todayInputValue());
  const [batchEndDate, setBatchEndDate] = useState(todayInputValue());
  const parsedBatchDates = dateRangeValues(batchStartDate, batchEndDate);
  const busy = refreshingResults || refreshingMissingScores || importingOdds || importingCoversOdds || loading;
  const today = todayInputValue();
  const missingPriorDateGames = missingEspnGames.filter((game) => game.game_date < today).length;
  const missingTodayGames = missingEspnGames.length - missingPriorDateGames;
  const missingSummary = !missingEspnGames.length
    ? "No missing-score scan loaded yet."
    : `Missing: ${missingEspnGames.length} games (${missingPriorDateGames} prior-date, ${missingTodayGames} today) on ${missingEspnDates.length} date(s): ${missingEspnDates.join(", ")}`;
  const isAdmin = Boolean(authState.authenticated && authState.user?.is_admin);

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Data Operations</h2>
            <p>{loading ? "Loading data state" : "Refresh inputs, rebuild projections, and reload generated views"}</p>
          </div>
          <Database size={20} />
        </div>
        {error && <div className="error">{error}</div>}
        {status && <div className="success">{status}</div>}
        {!isAdmin ? (
          <article className="operation-card">
            <div>
              <p className="eyebrow">admin access</p>
              <h3>Sign In Required</h3>
              <p>Data operations are limited to authenticated admin users.</p>
            </div>
            {authLoading ? (
              <p className="reason">Checking current session...</p>
            ) : (
              <>
                <div className="operation-fields">
                  <label>
                    Username
                    <input type="text" value={adminUsername} onChange={(event) => onAdminUsernameChange(event.target.value)} autoComplete="username" />
                  </label>
                  <label>
                    Password
                    <input type="password" value={adminPassword} onChange={(event) => onAdminPasswordChange(event.target.value)} autoComplete="current-password" />
                  </label>
                </div>
                <div className="operation-actions">
                  <button className="icon-button text-button dark-button" onClick={onAdminLogin} disabled={authSubmitting || !adminUsername || !adminPassword}>
                    <ShieldCheck size={18} />
                    {authSubmitting ? "Signing In" : "Sign In"}
                  </button>
                </div>
              </>
            )}
          </article>
        ) : (
          <>
            <div className="operation-actions">
              <button className="secondary-button" onClick={onAdminLogout} disabled={authSubmitting}>
                {authSubmitting ? "Signing Out" : `Sign Out (${authState.user?.username})`}
              </button>
            </div>
        <div className="data-layout">
          <OperationCard
            title="The Odds API"
            description="Update prop prices, lines, line discrepancies, and current matchup markets from The Odds API."
            metrics={`${discrepanciesCount} line gaps | ${matchupsCount} upcoming games`}
            primaryLabel={importingOdds ? "Loading" : "Load Saved Odds"}
            secondaryLabel="Refresh Odds"
            disabled={importingOdds || importingCoversOdds || refreshingResults || loading}
            onPrimary={() => onImportOdds(false)}
            onSecondary={() => onImportOdds(true)}
          />
          <OperationCard
            title="Covers Odds"
            description="Import today's Covers matchup prop tables without using The Odds API credits."
            metrics={`${discrepanciesCount} line gaps | ${matchupsCount} upcoming games`}
            primaryLabel={importingCoversOdds ? "Loading" : "Load Saved Covers"}
            secondaryLabel="Refresh Covers"
            disabled={importingOdds || importingCoversOdds || refreshingResults || loading}
            onPrimary={() => onImportCoversOdds(false)}
            onSecondary={() => onImportCoversOdds(true)}
          />
          <OperationCard
            title="ESPN Completed Games"
            description="Update today's ESPN matchup results and player box scores as the season continues."
            metrics={`${matchupsCount} upcoming games`}
            primaryLabel={refreshingResults ? "Loading" : "Load Missing ESPN"}
            secondaryLabel="Refresh ESPN"
            disabled={busy}
            onPrimary={() => onRefreshResults(false, true, true, false)}
            onSecondary={() => onRefreshResults(true, true, false, true)}
          />
          <article className="operation-card">
            <div>
              <p className="eyebrow">fast ESPN results</p>
              <h3>Date Results</h3>
              <p>Fetch completed scores and box scores for selected game dates, settle outcomes, and rebuild predictions.</p>
            </div>
            <div className="operation-fields">
              <label>
                Game date
                <input type="date" value={resultDate} onChange={(event) => setResultDate(event.target.value)} />
              </label>
              <label>
                Batch start date
                <input type="date" value={batchStartDate} onChange={(event) => setBatchStartDate(event.target.value)} />
              </label>
              <label>
                Batch end date
                <input type="date" value={batchEndDate} min={batchStartDate || undefined} onChange={(event) => setBatchEndDate(event.target.value)} />
              </label>
            </div>
            <div className="operation-actions">
              <button
                className="icon-button text-button dark-button"
                onClick={() => onRefreshResults(true, true, false, false, [resultDate])}
                disabled={busy || !resultDate}
              >
                <RefreshCw size={18} />
                {refreshingResults ? "Loading" : "Refresh Date"}
              </button>
              <button
                className="secondary-button"
                onClick={() => onRefreshResults(true, true, false, false, parsedBatchDates)}
                disabled={busy || parsedBatchDates.length === 0}
              >
                Refresh Batch
              </button>
            </div>
          </article>
          <article className="operation-card">
            <div>
              <p className="eyebrow">targeted ESPN repair</p>
              <h3>Missing Scores</h3>
              <p>Find completed games with missing final score rows, then import only those dates instead of refreshing full seasons.</p>
              <p className="reason">{missingSummary}</p>
            </div>
            <div className="operation-actions">
              <button className="icon-button text-button dark-button" onClick={onScanMissingScores} disabled={busy}>
                <RefreshCw size={18} />
                {refreshingMissingScores ? "Loading" : "Scan Missing"}
              </button>
              <button className="secondary-button" onClick={onImportMissingScores} disabled={busy || missingEspnDates.length === 0}>
                Import Missing
              </button>
            </div>
          </article>
          <OperationCard
            title="Projection Board"
            description="Rebuild model projections from the current prop lines and player history."
            metrics={`${matchupsCount} upcoming games | ${missingEspnDates.length} missing score dates`}
            primaryLabel={recalculating ? "Recalculating" : "Recalculate"}
            secondaryLabel={snapshottingGems ? "Tracking Gems" : "Track Gems Daily"}
            disabled={refreshingResults || importingOdds || importingCoversOdds || loading || recalculating || snapshottingGems}
            onPrimary={onRecalculate}
            onSecondary={onSnapshotGems}
          />
          <OperationCard
            title="Reload Views"
            description="Reload all boards and metrics from current backend state."
            metrics={`${matchupsCount} games | ${missingEspnDates.length} missing score dates`}
            primaryLabel="Reload"
            secondaryLabel="Refresh Missing Scan"
            disabled={busy}
            onPrimary={onReload}
            onSecondary={onScanMissingScores}
          />
        </div>
          </>
        )}
      </div>
    </section>
  );
}

function RosterView({
  roster,
  loading,
  error,
  status,
  refreshing,
  onRefresh,
  canRefresh = true
}: {
  roster: RosterPlayer[];
  loading: boolean;
  error: string | null;
  status: string | null;
  refreshing: boolean;
  onRefresh: () => void;
  canRefresh?: boolean;
}) {
  const teams = useMemo(() => Array.from(new Set(roster.map((item) => item.team))).sort(), [roster]);
  const [selectedTeam, setSelectedTeam] = useState<string | null>(null);
  useEffect(() => {
    if (!teams.length) {
      setSelectedTeam(null);
      return;
    }
    if (!selectedTeam || !teams.includes(selectedTeam)) {
      setSelectedTeam(teams[0]);
    }
  }, [teams, selectedTeam]);
  const visibleRows = selectedTeam ? roster.filter((item) => item.team === selectedTeam) : roster;

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Team Roster Status</h2>
            <p>{loading ? "Loading lineup status" : "Rotowire lineup statuses grouped by team"}</p>
          </div>
          <ShieldCheck size={20} />
        </div>
        {canRefresh ? (
          <div className="operation-actions">
            <button className="icon-button text-button dark-button" onClick={onRefresh} disabled={loading || refreshing}>
              <RefreshCw size={18} />
              {refreshing ? "Refreshing" : "Refresh Roster"}
            </button>
          </div>
        ) : null}
        {error && <div className="error">{error}</div>}
        {status && <div className="success">{status}</div>}
        <div className="game-tabs" aria-label="Roster team tabs">
          {teams.map((team) => (
            <button key={team} className={selectedTeam === team ? "active" : ""} onClick={() => setSelectedTeam(team)}>
              <strong>{team}</strong>
              <em>{roster.filter((item) => item.team === team).length} players</em>
            </button>
          ))}
        </div>
        <div className="props-table-wrapper">
          <table className="props-table">
            <thead>
              <tr>
                <th>Team</th>
                <th>Player</th>
                <th>Status</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((item) => (
                <tr key={`${item.team}-${item.player_name}-${item.status}`}>
                  <td>{item.team}</td>
                  <td>{item.player_name}</td>
                  <td><span className={`status-pill ${statusClass(item.status)}`}>{item.status}</span></td>
                  <td>{item.captured_at ? formatDate(item.captured_at) : "N/A"}</td>
                </tr>
              ))}
              {!visibleRows.length && (
                <tr>
                  <td colSpan={4}>No Rotowire lineup rows available yet. Run injury import or reload matchups to refresh lineups.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function statusClass(status: string) {
  const value = status.trim().toUpperCase();
  if (value === "OUT") {
    return "out";
  }
  if (value === "GTD" || value === "QUESTIONABLE" || value === "DOUBTFUL") {
    return "gtd";
  }
  return "confirmed";
}

function buildEspnOperationStatus({
  mode,
  scope,
  syncedProps,
  predictions,
  settledProps
}: {
  mode: string;
  scope: string;
  syncedProps: number;
  predictions: number;
  settledProps: number;
}) {
  const prefix = mode === "Imported missing ESPN scores"
    ? `Imported missing ESPN scores${scope}.`
    : `${mode} ESPN completed games and box scores loaded${scope}.`;
  if (syncedProps === 0 && predictions === 0) {
    return `${prefix} Settled ${settledProps} props.`;
  }
  return `${prefix} Synced ${syncedProps} model prop lines, rebuilt ${predictions} predictions, and settled ${settledProps} props.`;
}

function OperationCard({
  title,
  description,
  metrics,
  primaryLabel,
  secondaryLabel,
  disabled,
  onPrimary,
  onSecondary
}: {
  title: string;
  description: string;
  metrics: string;
  primaryLabel: string;
  secondaryLabel: string;
  disabled: boolean;
  onPrimary: () => void;
  onSecondary: () => void;
}) {
  return (
    <article className="operation-card">
      <div>
        <p className="eyebrow">{metrics}</p>
        <h3>{title}</h3>
        <p>{description}</p>
      </div>
      <div className="operation-actions">
        <button className="icon-button text-button dark-button" onClick={onPrimary} disabled={disabled}>
          <RefreshCw size={18} />
          {primaryLabel}
        </button>
        <button className="secondary-button" onClick={onSecondary} disabled={disabled}>
          {secondaryLabel}
        </button>
      </div>
    </article>
  );
}

function ModelsView({
  runs,
  latest,
  loading,
  error,
  onTrain
}: {
  runs: ModelRun[];
  latest: ModelRun | null;
  loading: boolean;
  error: string | null;
  onTrain: () => void;
}) {
  const metrics = latest ? sortModelMetrics(latest.metrics) : [];
  const overallMetric = latest?.metrics.overall;
  const comparisonRuns = latestRunsByModel(runs);
  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Model Training</h2>
            <p>{loading ? "Training or loading model runs" : "Walk-forward evaluation using only prior games"}</p>
          </div>
          <button className="icon-button text-button dark-button" onClick={onTrain} disabled={loading}>
            <BrainCircuit size={18} />
            Train
          </button>
        </div>
        {error && <div className="error">{error}</div>}
        <div className="model-layout">
          <div className="model-card">
            <p className="eyebrow">Latest run</p>
            <h3>{latest?.model_version ?? "No run yet"}</h3>
            <p className="reason">
              {latest?.notes ?? "Run training to create a walk-forward benchmark. This does not use future games for each row."}
            </p>
            <div className="detail-grid">
              <Metric label="Status" value={latest?.status ?? "Pending"} />
              <Metric label="Holdout rows" value={(latest?.training_rows ?? 0).toString()} />
              <Metric label="Type" value={latest?.run_type ?? "N/A"} />
              <Metric label="Finished" value={latest?.finished_at ? formatDate(latest.finished_at) : "N/A"} />
            </div>
            <div className="detail-grid model-validation-grid">
              <Metric label="Settled rows" value={formatCount(overallMetric?.settled_rows)} />
              <Metric label="Side accuracy" value={formatMetricPercent(overallMetric?.side_accuracy)} />
              <Metric label="Calibration gap" value={formatMetricPercent(overallMetric?.calibration_gap)} />
              <Metric label="Realized ROI" value={formatMetricPercent(overallMetric?.realized_roi)} />
            </div>
          </div>
          <div className="model-card">
            <p className="eyebrow">Market metrics</p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Market</th>
                    <th>Rows</th>
                    <th>MAE</th>
                    <th>RMSE</th>
                    <th>Bias</th>
                    <th>Direction</th>
                    <th>Settled</th>
                    <th>Win rate</th>
                    <th>Cal gap</th>
                    <th>Brier</th>
                    <th>Avg edge</th>
                    <th>Avg EV</th>
                    <th>ROI</th>
                  </tr>
                </thead>
                <tbody>
                  {metrics.map(([market, metric]) => (
                    <tr key={market} className={market === "overall" ? "model-summary-row" : undefined}>
                      <td>{marketLabel(market)}</td>
                      <td>{metric.rows}</td>
                      <td>{formatNumber(metric.mae)}</td>
                      <td>{formatNumber(metric.rmse)}</td>
                      <td>{formatSigned(metric.bias)}</td>
                      <td>{formatPercent(metric.directional_accuracy ?? undefined)}</td>
                      <td>{formatCount(metric.settled_rows)}</td>
                      <td>{formatMetricPercent(metric.side_accuracy)}</td>
                      <td>{formatMetricPercent(metric.calibration_gap)}</td>
                      <td>{formatMetricNumber(metric.brier_score, 4)}</td>
                      <td>{formatMetricSigned(metric.avg_edge, 3)}</td>
                      <td>{formatMetricSigned(metric.avg_expected_value, 3)}</td>
                      <td>{formatMetricPercent(metric.realized_roi)}</td>
                    </tr>
                  ))}
                  {!metrics.length && (
                    <tr>
                      <td colSpan={13}>No model metrics yet.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
          <div className="model-card full">
            <p className="eyebrow">Model comparison</p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Model</th>
                    <th>Type</th>
                    <th>Rows</th>
                    <th>Avg MAE</th>
                    <th>PTS</th>
                    <th>REB</th>
                    <th>AST</th>
                    <th>PRA</th>
                    <th>Direction</th>
                    <th>Val accuracy</th>
                    <th>Val ROI</th>
                  </tr>
                </thead>
                <tbody>
                  {comparisonRuns.map((run) => (
                    <tr key={run.model_version}>
                      <td>{run.model_version}</td>
                      <td>{run.run_type}</td>
                      <td>{run.training_rows}</td>
                      <td>{formatLatestMae(run)}</td>
                      <td>{formatMetricMae(run, "points")}</td>
                      <td>{formatMetricMae(run, "rebounds")}</td>
                      <td>{formatMetricMae(run, "assists")}</td>
                      <td>{formatMetricMae(run, "points_rebounds_assists")}</td>
                      <td>{formatAverageDirection(run)}</td>
                      <td>{formatOverallMetricPercent(run, "side_accuracy")}</td>
                      <td>{formatOverallMetricPercent(run, "realized_roi")}</td>
                    </tr>
                  ))}
                  {!comparisonRuns.length && (
                    <tr>
                      <td colSpan={11}>No model runs saved yet.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
          <div className="model-card full">
            <p className="eyebrow">Run history</p>
            <div className="recent-list">
              {runs.map((run) => (
                <div key={run.id ?? run.started_at}>
                  <span>{formatDate(run.started_at)}</span>
                  <b>{run.model_version}</b>
                  <strong>{run.training_rows} rows</strong>
                  <em>{run.status}</em>
                </div>
              ))}
              {!runs.length && <p className="empty">No model runs saved yet.</p>}
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function PropsView({
  filtered,
  loading,
  error,
  market,
  confidence,
  modelProbabilityOrder,
  selected,
  setMarket,
  setConfidence,
  setModelProbabilityOrder,
  setSelected
}: {
  filtered: ValueProp[];
  loading: boolean;
  error: string | null;
  market: string;
  confidence: string;
  modelProbabilityOrder: SortDirection;
  selected: ValueProp | null;
  setMarket: (market: string) => void;
  setConfidence: (confidence: string) => void;
  setModelProbabilityOrder: (order: SortDirection) => void;
  setSelected: (prop: ValueProp) => void;
}) {
  return (
    <section className="workspace">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Pregame Props</h2>
            <p>{loading ? "Loading projections" : "Ranked by expected value and edge"}</p>
          </div>
          <SlidersHorizontal size={20} />
        </div>

        <div className="filters" aria-label="Prop filters">
          <div className="segmented">
            {markets.map((item) => (
              <button key={item.id} className={market === item.id ? "active" : ""} onClick={() => setMarket(item.id)}>
                {item.label}
              </button>
            ))}
          </div>
          <select value={confidence} onChange={(event) => setConfidence(event.target.value)} aria-label="Confidence">
            <option value="all">All confidence</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
          </select>
          <select
            value={modelProbabilityOrder}
            onChange={(event) => setModelProbabilityOrder(event.target.value as SortDirection)}
            aria-label="Model probability sort order"
          >
            <option value="desc">Model prob: Descending</option>
            <option value="asc">Model prob: Ascending</option>
          </select>
        </div>

        {error && <div className="error">{error}</div>}

        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Player</th>
                <th>Market</th>
                <th>Side</th>
                <th>Line</th>
                <th>Proj</th>
                <th>Edge</th>
                <th>EV</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((prop) => (
                <tr key={prop.id} className={selected?.id === prop.id ? "selected" : ""} onClick={() => setSelected(prop)}>
                  <td>
                    <div className="player-cell">
                      <TeamLogo src={prop.team_logo_url} alt={`${prop.team} logo`} />
                      <div>
                        <strong>{prop.player}</strong>
                        <span>{prop.team} | {prop.sportsbook}</span>
                        {renderRecentFormWithMinutes(prop, `${prop.id}-l5`)}
                      </div>
                    </div>
                  </td>
                  <td>{marketLabel(prop.market)}</td>
                  <td><span className={`side ${prop.recommended_side}`}>{prop.recommended_side}</span></td>
                  <td>{prop.line.toFixed(1)}</td>
                  <td>{prop.projection.toFixed(1)}</td>
                  <td>{formatPercent(prop.edge)}</td>
                  <td>{formatPercent(prop.expected_value)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      </div>

      <aside className="detail-panel">
        <div className="panel-header">
          <div>
            <h2>Prop Detail</h2>
            <p>Projection context and pricing</p>
          </div>
          <TrendingUp size={20} />
        </div>

        {selected ? (
          <div className="detail-content">
            <div>
              <div className="detail-title">
                <TeamLogo src={selected.team_logo_url} alt={`${selected.team} logo`} />
                <div>
                  <p className="eyebrow">{selected.team} | {selected.sportsbook}</p>
                  <h3>{selected.player}</h3>
                </div>
              </div>
              <p className="recommendation">
                {selected.recommended_side.toUpperCase()} {selected.line.toFixed(1)} {marketLabel(selected.market)}
              </p>
            </div>
              <div className="detail-grid">
                <Metric label="Projection" value={selected.projection.toFixed(1)} />
                <Metric label="Model probability" value={formatPercent(selected.model_probability)} />
                <Metric label="Book implied" value={formatPercent(selected.implied_probability)} />
                <Metric label="Rest" value={restLabel(selected.rest_days)} />
                <Metric label="Blowout" value={selected.blowout_risk} />
                <Metric label="Min impact" value={formatMinutes(selected.blowout_minutes_impact)} />
                <Metric label="Confidence" value={selected.confidence} />
              </div>
            <p className="reason">{selected.reason}</p>
            <div className="timestamps">
              <span>Prediction: {formatDate(selected.prediction_time)}</span>
              <span>Tipoff: {formatDate(selected.start_time)}</span>
            </div>
          </div>
        ) : (
          <p className="empty">No prop selected.</p>
        )}
      </aside>
    </section>
  );
}

function renderRecentFormWithMinutes(
  prop: Pick<ValueProp, "id" | "recent_values" | "recent_minutes" | "recommended_side" | "line">,
  keyPrefix: string
) {
  if (!Array.isArray(prop.recent_values) || prop.recent_values.length === 0) {
    return null;
  }
  return (
    <div className="prop-l5-strip" aria-label="Last 5 results and minutes">
      {prop.recent_values.slice(0, 5).map((value, idx) => {
        const hit = prop.recommended_side === "over" ? value > prop.line : value < prop.line;
        return (
          <span
            key={`${keyPrefix}-${idx}`}
            className={hit ? "hit" : "miss"}
            title={`${value.toFixed(1)} vs ${prop.recommended_side.toUpperCase()} ${prop.line.toFixed(1)}`}
          >
            {Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}
          </span>
        );
      })}
      {Array.isArray(prop.recent_minutes) && prop.recent_minutes.length > 0 && (
        <>
          <span className="l5-separator" title="Minutes distribution">|</span>
          {prop.recent_minutes.slice(0, 5).map((minutes, idx) => (
            <span
              key={`${keyPrefix}-min-${idx}`}
              className="min-chip"
              title={`Minutes played: ${minutes.toFixed(1)}`}
            >
              {minutes.toFixed(1)}
            </span>
          ))}
        </>
      )}
    </div>
  );
}

type GemPreset = "conservative" | "balanced" | "aggressive";
type GemProp = ValueProp & {
  gem_score: number;
  line_gap: number;
  price_gap: number;
  badges: string[];
};

function GemsView({ gems, matchups, loading, error }: { gems: GemProp[]; matchups: Matchup[]; loading: boolean; error: string | null }) {
  const [preset, setPreset] = useState<GemPreset>("balanced");
  const [marketFilter, setMarketFilter] = useState("all");
  const [confidenceFilter, setConfidenceFilter] = useState("all");
  const [sideFilter, setSideFilter] = useState("all");
  const [sortField, setSortField] = useState<"gem_score" | "expected_value" | "edge" | "line_gap" | "price_gap">("gem_score");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [groupedByMatchup, setGroupedByMatchup] = useState(true);
  const [matchupCap, setMatchupCap] = useState(3);
  const [selectedGroupKey, setSelectedGroupKey] = useState<string | null>(null);
  const shown = useMemo(() => {
    const cfg = {
      conservative: { minEv: 0.03, minEdge: 0.08, minScore: 0.55, allowLow: false, limit: 20 },
      balanced: { minEv: 0.02, minEdge: 0.05, minScore: 0.42, allowLow: true, limit: 24 },
      aggressive: { minEv: 0.01, minEdge: 0.035, minScore: 0.32, allowLow: true, limit: 30 }
    }[preset];
    return gems
      .filter((g) => g.expected_value >= cfg.minEv)
      .filter((g) => Math.abs(g.edge) >= cfg.minEdge)
      .filter((g) => cfg.allowLow || g.confidence !== "low")
      .filter((g) => g.gem_score >= cfg.minScore)
      .filter((g) => marketFilter === "all" || g.market === marketFilter)
      .filter((g) => confidenceFilter === "all" || g.confidence === confidenceFilter)
      .filter((g) => sideFilter === "all" || g.recommended_side === sideFilter)
      .sort((a, b) => {
        const aVal = sortField === "gem_score" ? a.gem_score
          : sortField === "expected_value" ? a.expected_value
          : sortField === "edge" ? a.edge
          : sortField === "line_gap" ? a.line_gap
          : a.price_gap;
        const bVal = sortField === "gem_score" ? b.gem_score
          : sortField === "expected_value" ? b.expected_value
          : sortField === "edge" ? b.edge
          : sortField === "line_gap" ? b.line_gap
          : b.price_gap;
        const delta = aVal - bVal;
        return sortDirection === "asc" ? delta : -delta;
      })
      .slice(0, cfg.limit);
  }, [gems, preset, marketFilter, confidenceFilter, sideFilter, sortField, sortDirection]);
  const grouped = useMemo(() => groupGemsByMatchup(shown, matchupCap, matchups), [shown, matchupCap, matchups]);
  const selectedGroup = grouped.find((group) => group.key === selectedGroupKey) ?? grouped[0] ?? null;

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Gems</h2>
            <p>{loading ? "Scoring opportunities" : "Edge + EV + discrepancy ranked targets"}</p>
          </div>
          <TrendingUp size={20} />
        </div>
        <div className="segmented">
          <button className={preset === "conservative" ? "active" : ""} onClick={() => setPreset("conservative")}>Conservative</button>
          <button className={preset === "balanced" ? "active" : ""} onClick={() => setPreset("balanced")}>Balanced</button>
          <button className={preset === "aggressive" ? "active" : ""} onClick={() => setPreset("aggressive")}>Aggressive</button>
        </div>
        <div className="filters" aria-label="Gem filters">
          <div className="segmented">
            {markets.map((item) => (
              <button key={`gem-${item.id}`} className={marketFilter === item.id ? "active" : ""} onClick={() => setMarketFilter(item.id)}>
                {item.label}
              </button>
            ))}
          </div>
          <select value={confidenceFilter} onChange={(event) => setConfidenceFilter(event.target.value)} aria-label="Gem confidence filter">
            <option value="all">All confidence</option>
            <option value="high">High</option>
            <option value="medium">Medium</option>
            <option value="low">Low</option>
          </select>
          <select value={sideFilter} onChange={(event) => setSideFilter(event.target.value)} aria-label="Gem side filter">
            <option value="all">All sides</option>
            <option value="over">Over</option>
            <option value="under">Under</option>
          </select>
          <select value={sortField} onChange={(event) => setSortField(event.target.value as typeof sortField)} aria-label="Gem sort field">
            <option value="gem_score">Sort: Gem score</option>
            <option value="expected_value">Sort: EV</option>
            <option value="edge">Sort: Edge</option>
            <option value="line_gap">Sort: Line gap</option>
            <option value="price_gap">Sort: Price gap</option>
          </select>
          <select value={sortDirection} onChange={(event) => setSortDirection(event.target.value as SortDirection)} aria-label="Gem sort direction">
            <option value="desc">Descending</option>
            <option value="asc">Ascending</option>
          </select>
          <select value={groupedByMatchup ? "grouped" : "flat"} onChange={(event) => setGroupedByMatchup(event.target.value === "grouped")} aria-label="Gem view mode">
            <option value="grouped">Grouped by Matchup</option>
            <option value="flat">Flat list</option>
          </select>
          {groupedByMatchup ? (
            <select value={String(matchupCap)} onChange={(event) => setMatchupCap(Number(event.target.value) || 3)} aria-label="Max gems per matchup">
              <option value="2">Cap 2 / matchup</option>
              <option value="3">Cap 3 / matchup</option>
              <option value="4">Cap 4 / matchup</option>
              <option value="5">Cap 5 / matchup</option>
            </select>
          ) : null}
        </div>
        {error && <div className="error">{error}</div>}
        {!groupedByMatchup ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Player</th>
                <th>Pick</th>
                <th>Edge</th>
                <th>EV</th>
                <th>Line Gap</th>
                <th>Price Gap</th>
                <th>Signals</th>
                <th>Conf</th>
                <th>Gem</th>
              </tr>
            </thead>
            <tbody>
              {shown.map((g) => (
                <tr key={`gem-${g.id}`}>
                  <td>
                    <strong>{g.player}</strong>
                    <span>{g.team} | {marketLabel(g.market)} | {g.sportsbook}</span>
                    {renderRecentFormWithMinutes(g, `${g.id}-gem-flat-l5`)}
                  </td>
                  <td><span className={`side ${g.recommended_side}`}>{g.recommended_side} {g.line.toFixed(1)}</span></td>
                  <td>{formatPercent(g.edge)}</td>
                  <td>{formatPercent(g.expected_value)}</td>
                  <td>{g.line_gap.toFixed(1)}</td>
                  <td>{g.price_gap}</td>
                  <td>
                    <div className="book-line-list">
                      {g.badges.map((badge) => (
                        <span key={`${g.id}-${badge}`}>{badge}</span>
                      ))}
                    </div>
                  </td>
                  <td>{g.confidence}</td>
                  <td><strong>{formatNumber(g.gem_score)}</strong></td>
                </tr>
              ))}
              {!shown.length && (
                <tr>
                  <td colSpan={9}>No gems pass the {preset} preset right now.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
        ) : (
          <>
            <div className="game-tabs" aria-label="Gem matchup groups">
              {grouped.map((group) => (
                <button
                  key={group.key}
                  className={selectedGroup?.key === group.key ? "active" : ""}
                  onClick={() => setSelectedGroupKey(group.key)}
                >
                  <span>{group.startTime ? formatDate(group.startTime) : "Scheduled"}</span>
                  <strong>{group.matchup}</strong>
                  <em>{group.items.length} gems | top {formatNumber(group.topScore)}</em>
                </button>
              ))}
            </div>
            {selectedGroup ? (
              <article key={selectedGroup.key} className="matchup-card">
                <div className="matchup-card-header">
                  <div>
                    <p className="eyebrow">{selectedGroup.startTime ? formatDate(selectedGroup.startTime) : "Scheduled"}</p>
                    <h3>{selectedGroup.matchup}</h3>
                  </div>
                  <div className="game-badges">
                    <span className="game-pill">{selectedGroup.items.length} gems</span>
                    <span className="rest-pill">Top {formatNumber(selectedGroup.topScore)}</span>
                    <span className="rest-pill">Avg {formatNumber(selectedGroup.avgScore)}</span>
                  </div>
                </div>
                {(() => {
                  const game = matchups.find((m) => String(m.id) === selectedGroup.key) ?? null;
                  return (
                    <div className="parlay-game-summary">
                      <MiniStat label="Spread" value={game?.spread_home != null ? `${game.home_team} ${formatSpread(game.spread_home)}` : "N/A"} />
                      <MiniStat label="Total" value={game?.game_total != null && game.game_total > 0 ? game.game_total.toFixed(1) : "N/A"} />
                      <MiniStat label={`${game?.away_team ?? "Away"} Rest`} value={restLabel(game?.away_rest_days ?? null)} />
                      <MiniStat label={`${game?.home_team ?? "Home"} Rest`} value={restLabel(game?.home_rest_days ?? null)} />
                      <MiniStat label="Gem Count" value={String(selectedGroup.items.length)} />
                      <MiniStat label="Top Score" value={formatNumber(selectedGroup.topScore)} />
                    </div>
                  );
                })()}
                <div className="table-wrap">
                  <table>
                    <thead>
                      <tr>
                        <th>Player</th>
                        <th>Pick</th>
                        <th>Edge</th>
                        <th>EV</th>
                        <th>Signals</th>
                        <th>Gem</th>
                      </tr>
                    </thead>
                    <tbody>
                      {selectedGroup.items.map((g) => (
                        <tr key={`group-gem-${selectedGroup.key}-${g.id}`}>
                          <td>
                            <strong>{g.player}</strong>
                            <span>{g.team} | {marketLabel(g.market)} | {g.sportsbook}</span>
                            {renderRecentFormWithMinutes(g, `${selectedGroup.key}-${g.id}-gem-group-l5`)}
                          </td>
                          <td><span className={`side ${g.recommended_side}`}>{g.recommended_side} {g.line.toFixed(1)}</span></td>
                          <td>{formatPercent(g.edge)}</td>
                          <td>{formatPercent(g.expected_value)}</td>
                          <td>
                            <div className="book-line-list">
                              {g.badges.map((badge) => (
                                <span key={`${selectedGroup.key}-${g.id}-${badge}`}>{badge}</span>
                              ))}
                            </div>
                          </td>
                          <td><strong>{formatNumber(g.gem_score)}</strong></td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              </article>
            ) : (
              <p className="empty">No gems pass the {preset} preset right now.</p>
            )}
          </>
        )}
      </div>
    </section>
  );
}

function DiscrepanciesView({
  discrepancies,
  loading,
  error
}: {
  discrepancies: LineDiscrepancy[];
  loading: boolean;
  error: string | null;
}) {
  const [market, setMarket] = useState("all");
  const [side, setSide] = useState("all");
  const grouped = groupDiscrepanciesByMatchup(discrepancies);
  const [selectedKey, setSelectedKey] = useState<string | null>(null);
  const selectedGroup = grouped.find((group) => group.key === selectedKey) ?? grouped[0] ?? null;
  const selectedDiscrepancies = selectedGroup?.items ?? [];
  const filtered = selectedDiscrepancies.filter((item) => {
    const marketMatch = market === "all" || item.market === market;
    const sideMatch = side === "all" || item.side === side;
    return marketMatch && sideMatch;
  });
  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Sportsbook Line Gaps</h2>
            <p>{loading ? "Loading sportsbook discrepancies" : "Best price, line range, and each book's offer"}</p>
          </div>
          <SlidersHorizontal size={20} />
        </div>
        <div className="game-tabs" aria-label="Discrepancy matchup tabs">
          {grouped.map((group) => (
            <button
              key={group.key}
              className={selectedGroup?.key === group.key ? "active" : ""}
              onClick={() => setSelectedKey(group.key)}
            >
              <span>{formatDate(group.commence_time)}</span>
              <strong>{group.matchup}</strong>
              <em>{group.items.length} gaps | {countDiscrepancyBooks(group.items)} books</em>
            </button>
          ))}
        </div>
        <div className="filters" aria-label="Discrepancy filters">
          <div className="segmented">
            {markets.map((item) => (
              <button key={item.id} className={market === item.id ? "active" : ""} onClick={() => setMarket(item.id)}>
                {item.label}
              </button>
            ))}
          </div>
          <select value={side} onChange={(event) => setSide(event.target.value)} aria-label="Side">
            <option value="all">All sides</option>
            <option value="over">Over</option>
            <option value="under">Under</option>
          </select>
        </div>
        {error && <div className="error">{error}</div>}
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Matchup</th>
                <th>Player</th>
                <th>Market</th>
                <th>Side</th>
                <th>Gap</th>
                <th>Best</th>
                <th>Books</th>
              </tr>
            </thead>
            <tbody>
              {filtered.map((item) => (
                <tr key={`${item.matchup}-${item.player_name}-${item.market}-${item.side}-${item.line_gap}-${item.price_gap}`}>
                  <td>
                    <strong>{item.matchup}</strong>
                    <span>{formatDate(item.commence_time)}</span>
                  </td>
                  <td>{item.player_name}</td>
                  <td>{marketLabel(item.market)}</td>
                  <td><span className={`side ${item.side}`}>{item.side}</span></td>
                  <td>
                    <strong>{item.line_gap.toFixed(1)}</strong>
                    <span>{item.price_gap} cents</span>
                  </td>
                  <td>
                    <strong>{item.best_price.sportsbook}</strong>
                    <span>{item.best_price.line.toFixed(1)} {formatAmerican(item.best_price.price)}</span>
                  </td>
                  <td>
                    <div className="book-line-list">
                      {item.book_lines.map((book) => (
                        <span key={`${item.player_name}-${item.market}-${item.side}-${book.sportsbook}-${book.line}-${book.price}`}>
                          {book.sportsbook}: {book.line.toFixed(1)} {formatAmerican(book.price)}
                        </span>
                      ))}
                    </div>
                  </td>
                </tr>
              ))}
              {!filtered.length && (
                <tr>
                  <td colSpan={7}>{selectedGroup ? "No line discrepancies match the selected filters." : "No line discrepancies found."}</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </section>
  );
}

function MatchupsView({ matchups, loading, error }: { matchups: Matchup[]; loading: boolean; error: string | null }) {
  const [selectedGameId, setSelectedGameId] = useState<number | null>(null);
  const selectedMatchup = matchups.find((matchup) => matchup.id === selectedGameId) ?? matchups[0] ?? null;

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Today's Games</h2>
            <p>{loading ? "Loading matchups" : "Last 10 form, home/away split, ATS, and totals"}</p>
          </div>
          <ShieldCheck size={20} />
        </div>
        {error && <div className="error">{error}</div>}
        <div className="game-tabs matchup-game-tabs" aria-label="Game tabs">
          {matchups.map((matchup) => {
            const winnerCode = normalizeTeamCode(matchup.winner_pick);
            const homeCode = normalizeTeamCode(matchup.home_team);
            const awayCode = normalizeTeamCode(matchup.away_team);
            const winnerClass =
              winnerCode === homeCode ? "winner-home" : winnerCode === awayCode ? "winner-away" : "winner-none";
            const activeClass = selectedMatchup?.id === matchup.id ? "active" : "";
            return (
            <button
              key={matchup.id}
              className={`${activeClass} ${winnerClass}`.trim()}
              onClick={() => setSelectedGameId(matchup.id)}
            >
              <span>{formatDate(matchup.start_time)}</span>
              <strong>{matchup.away_team} at {matchup.home_team}</strong>
              <em>{availableLabel(matchup)}</em>
            </button>
            );
          })}
        </div>
        <div className="matchup-grid single">
          {selectedMatchup ? (
            <article className="matchup-card" key={selectedMatchup.id}>
              <div className="matchup-card-header">
                <div className="matchup-header-top">
                  <div className="matchup-header-main">
                    <p className="eyebrow">{formatDate(selectedMatchup.start_time)}</p>
                    <div className="matchup-title-row">
                      <TeamLogo src={selectedMatchup.away_logo_url} alt={`${selectedMatchup.away_team} logo`} />
                      <h3>{selectedMatchup.away_team} at {selectedMatchup.home_team}</h3>
                      <TeamLogo src={selectedMatchup.home_logo_url} alt={`${selectedMatchup.home_team} logo`} />
                    </div>
                  </div>
                </div>
                <div className="matchup-market-badges">
                  <span className={`risk-pill ${riskClass(selectedMatchup.blowout_risk)}`}>
                    Blowout {selectedMatchup.blowout_risk}
                  </span>
                  <span className="market-pill">
                    {formatSpreadMarket(selectedMatchup)}
                  </span>
                  <span className="market-pill">
                    {formatTotalMarket(selectedMatchup)}
                  </span>
                  <span className="market-pill">
                    {formatMoneylineMarket(selectedMatchup)}
                  </span>
                </div>
              </div>
              <div className="team-comparison">
                <TeamSummary
                  label="Away"
                  team={selectedMatchup.away_team_name}
                  teamCode={selectedMatchup.away_team}
                  logoUrl={selectedMatchup.away_logo_url}
                  restDays={selectedMatchup.away_rest_days}
                  summary={selectedMatchup.away}
                  context="away"
                  coversTeamRow={selectedMatchup.covers_records?.team_table?.find((item) => normalizeTeamCode(item.team) === normalizeTeamCode(selectedMatchup.away_team))}
                  coversLast10Rows={selectedMatchup.covers_records?.away_last_10}
                />
                <TeamSummary
                  label="Home"
                  team={selectedMatchup.home_team_name}
                  teamCode={selectedMatchup.home_team}
                  logoUrl={selectedMatchup.home_logo_url}
                  restDays={selectedMatchup.home_rest_days}
                  summary={selectedMatchup.home}
                  context="home"
                  coversTeamRow={selectedMatchup.covers_records?.team_table?.find((item) => normalizeTeamCode(item.team) === normalizeTeamCode(selectedMatchup.home_team))}
                  coversLast10Rows={selectedMatchup.covers_records?.home_last_10}
                />
              </div>
              <div className="prediction-strip">
                <MiniStat label="Projected Score" value={formatProjectedScore(selectedMatchup)} />
                <MiniStat label="Winner" value={selectedMatchup.winner_pick} />
                <MiniStat label="ATS" value={selectedMatchup.ats_pick} />
                <MiniStat label="ATS Edge" value={formatNullableEdge(selectedMatchup.ats_edge)} />
                <MiniStat label="Model Total" value={formatProjectedTotal(selectedMatchup)} />
                <MiniStat label="O/U Edge" value={formatNullableEdge(selectedMatchup.total_edge)} />
                <MiniStat label="Confidence" value={selectedMatchup.game_confidence} />
              </div>
              <CoversRecordsPanel matchup={selectedMatchup} />
            </article>
          ) : (
            <p className="empty">No scheduled games found.</p>
          )}
        </div>
      </div>
    </section>
  );
}

function CoversRecordsPanel({ matchup }: { matchup: Matchup }) {
  const records = matchup.covers_records;
  const hasCovers = Boolean(records && (records.head_to_head.length || records.away_last_10.length || records.home_last_10.length));
  const h2hRows = records?.head_to_head.length
    ? records.head_to_head.slice(0, 10)
    : buildFallbackH2HRows(matchup);
  const singleH2HRow = h2hRows.length === 1 ? h2hRows[0] : null;
  const awayRows = hasCovers
    ? records?.away_last_10.slice(0, 10) ?? []
    : matchup.away.recent_games.slice(0, 10).map((game) => ({
        date: formatGameDateShort(game.game_date),
        opponent: game.opponent,
        location: (game.is_home ? "home" : "away") as "home" | "away",
        result: null,
        score: `${game.points} - ${game.opponent_points}`,
        ats: atsLabel(game.ats_result).replace("ATS ", ""),
        total: totalLabel(game.total_result)
      }));
  const homeRows = hasCovers
    ? records?.home_last_10.slice(0, 10) ?? []
    : matchup.home.recent_games.slice(0, 10).map((game) => ({
        date: formatGameDateShort(game.game_date),
        opponent: game.opponent,
        location: (game.is_home ? "home" : "away") as "home" | "away",
        result: null,
        score: `${game.points} - ${game.opponent_points}`,
        ats: atsLabel(game.ats_result).replace("ATS ", ""),
        total: totalLabel(game.total_result)
      }));
  const h2hOwner = h2hMatchupOwner(h2hRows, matchup);
  const h2hSummary = summarizeCoversTeamRows(h2hRows);
  if (!h2hRows.length && !awayRows.length && !homeRows.length) {
    return null;
  }

  return (
    <div className="covers-records-panel">
      <div className="covers-owner-strip">
        <span className="owner-label">Matchup Owner</span>
        <strong className="owner-team">{h2hOwner.owner}</strong>
        <span className="owner-record">{h2hOwner.record}</span>
        <span className="owner-record">H2H O/U {h2hSummary?.ou ?? "N/A"}</span>
      </div>
      <div className="covers-records-list">
        <h4>H2H Last 10</h4>
        {!h2hRows.length ? (
          <p className="empty">No prior head-to-head meetings.</p>
        ) : singleH2HRow ? (
          <div className="board-panel compact-panel">
            <p className="eyebrow">Only prior meeting</p>
            <div className="prediction-strip">
              <MiniStat label="Date" value={singleH2HRow.date} />
              <MiniStat label="Home" value={singleH2HRow.home ?? "N/A"} />
              <MiniStat label="Score" value={singleH2HRow.score} />
              <MiniStat label="ATS" value={singleH2HRow.ats} />
              <MiniStat label="O/U" value={singleH2HRow.total} />
            </div>
          </div>
        ) : (
          <div className="table-wrap covers-records-table-wrap">
            <table>
              <thead>
                <tr>
                  <th>Date</th>
                  <th>Home</th>
                  <th>Result</th>
                  <th>ATS</th>
                  <th>O/U</th>
                </tr>
              </thead>
              <tbody>
                {h2hRows.map((row) => (
                  <tr key={`h2h-${row.date}-${row.score}-${row.home ?? ""}`}>
                    <td>{row.date}</td>
                    <td>{coversRecordOpponent(row, "h2h", matchup)}</td>
                    <td>
                      <RecordResultCell row={row} matchup={matchup} winnerOnly />
                    </td>
                    <td>{row.ats}</td>
                    <td>{row.total}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>
      <div className="covers-records-list">
        <h4>{matchup.away_team} Last 10</h4>
        <div className="table-wrap covers-records-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>vs</th>
                <th>Result</th>
                <th>ATS</th>
                <th>O/U</th>
              </tr>
            </thead>
            <tbody>
              {awayRows.slice(0, 10).map((row) => (
                <tr key={`away-${row.date}-${row.score}-${row.opponent ?? ""}`}>
                  <td>{row.date}</td>
                  <td>{coversRecordOpponent(row, "team", matchup)}</td>
                  <td>
                    <RecordResultCell row={row} matchup={matchup} teamCode={matchup.away_team} />
                  </td>
                  <td>{row.ats}</td>
                  <td>{row.total}</td>
                </tr>
              ))}
              {!awayRows.length && (
                <tr>
                  <td colSpan={5}>No away recent games available.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
      <div className="covers-records-list">
        <h4>{matchup.home_team} Last 10</h4>
        <div className="table-wrap covers-records-table-wrap">
          <table>
            <thead>
              <tr>
                <th>Date</th>
                <th>vs</th>
                <th>Result</th>
                <th>ATS</th>
                <th>O/U</th>
              </tr>
            </thead>
            <tbody>
              {homeRows.slice(0, 10).map((row) => (
                <tr key={`home-${row.date}-${row.score}-${row.opponent ?? ""}`}>
                  <td>{row.date}</td>
                  <td>{coversRecordOpponent(row, "team", matchup)}</td>
                  <td>
                    <RecordResultCell row={row} matchup={matchup} teamCode={matchup.home_team} />
                  </td>
                  <td>{row.ats}</td>
                  <td>{row.total}</td>
                </tr>
              ))}
              {!homeRows.length && (
                <tr>
                  <td colSpan={5}>No home recent games available.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      </div>
    </div>
  );
}

function buildFallbackH2HRows(matchup: Matchup): CoversRecordRow[] {
  const awayCode = normalizeTeamCode(matchup.away_team);
  const homeCode = normalizeTeamCode(matchup.home_team);
  if (!awayCode || !homeCode) {
    return [];
  }

  return matchup.away.recent_games
    .filter((game) => normalizeTeamCode(game.opponent) === homeCode)
    .slice(0, 10)
    .map((game) => {
      const awayWasHome = Boolean(game.is_home);
      const homeTeam = awayWasHome ? awayCode : homeCode;
      const awayPoints = game.points;
      const homePoints = game.opponent_points;
      const winner =
        awayPoints === homePoints ? null : awayPoints > homePoints ? awayCode : homeCode;
      return {
        date: formatGameDateShort(game.game_date),
        home: homeTeam,
        winner,
        score: awayWasHome ? `${awayPoints} - ${homePoints}` : `${homePoints} - ${awayPoints}`,
        ats: atsLabel(game.ats_result).replace("ATS ", ""),
        total: totalLabel(game.total_result),
      };
    });
}

function h2hMatchupOwner(rows: CoversRecordRow[], matchup: Matchup): { owner: string; record: string } {
  if (!rows.length) {
    return { owner: "No edge", record: "No recent H2H games" };
  }
  const wins: Record<string, number> = {
    [matchup.home_team]: 0,
    [matchup.away_team]: 0
  };
  for (const row of rows) {
    const winner = normalizeTeamCode(row.winner);
    if (!winner) {
      continue;
    }
    if (winner === normalizeTeamCode(matchup.home_team)) {
      wins[matchup.home_team] += 1;
    } else if (winner === normalizeTeamCode(matchup.away_team)) {
      wins[matchup.away_team] += 1;
    }
  }
  const homeWins = wins[matchup.home_team];
  const awayWins = wins[matchup.away_team];
  if (homeWins === awayWins) {
    return { owner: "Even", record: `${matchup.home_team} ${homeWins} - ${awayWins} ${matchup.away_team}` };
  }
  if (homeWins > awayWins) {
    return { owner: matchup.home_team, record: `${homeWins}-${awayWins} in last ${rows.length}` };
  }
  return { owner: matchup.away_team, record: `${awayWins}-${homeWins} in last ${rows.length}` };
}

function coversRecordOpponent(row: CoversRecordRow, mode: "h2h" | "team", matchup?: Matchup) {
  if (mode === "h2h") {
    return (
      <RecordTeamCell
        prefix=""
        teamCode={row.home ?? "N/A"}
        logoUrl={logoForTeamCode(row.home, matchup)}
      />
    );
  }
  const prefix = row.location === "away" ? "at" : "vs";
  return (
    <RecordTeamCell
      prefix={prefix}
      teamCode={row.opponent ?? "N/A"}
      logoUrl={logoForTeamCode(row.opponent, matchup)}
    />
  );
}

function RecordTeamCell({
  prefix,
  teamCode,
  logoUrl,
  outcomeClass = "is-neutral",
}: {
  prefix: string;
  teamCode: string;
  logoUrl?: string | null;
  outcomeClass?: "is-win" | "is-loss" | "is-neutral";
}) {
  return (
    <span className={`record-team-cell ${outcomeClass}`}>
      {prefix ? <span>{prefix}</span> : null}
      <TeamLogo src={logoUrl} alt={`${teamCode} logo`} />
      <strong>{teamCode}</strong>
    </span>
  );
}

function RecordResultCell({
  row,
  matchup,
  teamCode,
  winnerOnly: _winnerOnly = false,
}: {
  row: CoversRecordRow;
  matchup: Matchup;
  teamCode?: string;
  winnerOnly?: boolean;
}) {
  const winnerLogo = logoForTeamCode(row.winner, matchup);
  const outcomeClass = teamCode ? recordOutcomeClass(row, teamCode) : "";
  return (
    <span className={`record-team-cell ${outcomeClass}`.trim()}>
      {winnerLogo ? <TeamLogo src={winnerLogo} alt={`${row.winner ?? "Winner"} logo`} /> : null}
      {!winnerLogo ? <strong>{row.result || row.winner || "Winner"}</strong> : null}
      <span className="record-score-text">{row.score}</span>
    </span>
  );
}

function recordOutcomeClass(row: CoversRecordRow, teamCode: string) {
  const normalizedTeam = normalizeTeamCode(teamCode);
  const normalizedWinner = normalizeTeamCode(row.winner);
  if (normalizedTeam && normalizedWinner) {
    return normalizedWinner === normalizedTeam ? "is-win" : "is-loss";
  }
  const token = (row.result ?? "").trim().charAt(0).toUpperCase();
  if (token === "W") {
    return "is-win";
  }
  if (token === "L") {
    return "is-loss";
  }
  return "";
}

function logoForTeamCode(value: string | null | undefined, matchup?: Matchup) {
  const normalized = normalizeTeamCode(value);
  if (!normalized) {
    return null;
  }
  if (matchup && normalized === normalizeTeamCode(matchup.home_team)) {
    return matchup.home_logo_url ?? WNBA_TEAM_LOGOS[normalized] ?? null;
  }
  if (matchup && normalized === normalizeTeamCode(matchup.away_team)) {
    return matchup.away_logo_url ?? WNBA_TEAM_LOGOS[normalized] ?? null;
  }
  return WNBA_TEAM_LOGOS[normalized] ?? null;
}

function normalizeTeamCode(value: string | null | undefined) {
  if (!value) {
    return null;
  }
  const cleaned = value.toUpperCase().replace(/[^A-Z]/g, "");
  const aliases: Record<string, string> = {
    ATLANTA: "ATL",
    ATLANTADREAM: "ATL",
    CHICAGO: "CHI",
    CHICAGOSKY: "CHI",
    CONNECTICUT: "CON",
    CONNECTICUTSUN: "CON",
    CONN: "CON",
    DALLAS: "DAL",
    DALLASWINGS: "DAL",
    GOLDENSTATE: "GS",
    GOLDENSTATEVALKYRIES: "GS",
    INDIANA: "IND",
    INDIANAFEVER: "IND",
    LASVEGAS: "LV",
    LASVEGASACES: "LV",
    LOSANGELES: "LA",
    LOSANGELESSPARKS: "LA",
    MINNESOTA: "MIN",
    MINNESOTALYNX: "MIN",
    NEWYORK: "NY",
    NEWYORKLIBERTY: "NY",
    PHOENIX: "PHX",
    PHOENIXMERCURY: "PHX",
    PORTLAND: "POR",
    PORTLANDFIRE: "POR",
    SEATTLE: "SEA",
    SEATTLESTORM: "SEA",
    TORONTO: "TOR",
    TORONTOTEMPO: "TOR",
    WAS: "WSH",
    WASHINGTON: "WSH",
    WASHINGTONMYSTICS: "WSH",
    PHO: "PHX",
    LAS: "LA",
    PDX: "POR"
  };
  return aliases[cleaned] ?? cleaned;
}

function summarizeCoversTeamRows(rows: CoversRecordRow[] | undefined) {
  if (!rows?.length) {
    return null;
  }
  let wins = 0;
  let losses = 0;
  let atsWins = 0;
  let atsLosses = 0;
  let atsPushes = 0;
  let overs = 0;
  let unders = 0;
  let ouPushes = 0;
  for (const row of rows) {
    const result = (row.result ?? "").toUpperCase();
    if (result === "W") wins += 1;
    else if (result === "L") losses += 1;

    const atsToken = (row.ats ?? "").trim().charAt(0).toUpperCase();
    if (atsToken === "W") atsWins += 1;
    else if (atsToken === "L") atsLosses += 1;
    else if (atsToken === "P") atsPushes += 1;

    const ouToken = (row.total ?? "").trim().charAt(0).toLowerCase();
    if (ouToken === "o") overs += 1;
    else if (ouToken === "u") unders += 1;
    else if (ouToken === "p") ouPushes += 1;
  }
  return {
    record: `${wins}-${losses}`,
    ats: `${atsWins}-${atsLosses}-${atsPushes}`,
    ou: `${overs}-${unders}-${ouPushes}`
  };
}

function summarizeCoversContextRecord(rows: CoversRecordRow[] | undefined, context: "home" | "away") {
  if (!rows?.length) {
    return null;
  }
  let wins = 0;
  let losses = 0;
  for (const row of rows) {
    if (row.location !== context) {
      continue;
    }
    const result = (row.result ?? "").toUpperCase();
    if (result === "W") wins += 1;
    else if (result === "L") losses += 1;
  }
  return `${wins}-${losses}`;
}

function summarizeCoversContextAts(rows: CoversRecordRow[] | undefined, context: "home" | "away") {
  if (!rows?.length) {
    return null;
  }
  let wins = 0;
  let losses = 0;
  let pushes = 0;
  for (const row of rows) {
    if (row.location !== context) {
      continue;
    }
    const atsToken = (row.ats ?? "").trim().charAt(0).toUpperCase();
    if (atsToken === "W") wins += 1;
    else if (atsToken === "L") losses += 1;
    else if (atsToken === "P") pushes += 1;
  }
  return `${wins}-${losses}-${pushes}`;
}

function summarizeCoversContextOu(rows: CoversRecordRow[] | undefined, context: "home" | "away") {
  if (!rows?.length) {
    return null;
  }
  let overs = 0;
  let unders = 0;
  let pushes = 0;
  for (const row of rows) {
    if (row.location !== context) {
      continue;
    }
    const ouToken = (row.total ?? "").trim().charAt(0).toLowerCase();
    if (ouToken === "o") overs += 1;
    else if (ouToken === "u") unders += 1;
    else if (ouToken === "p") pushes += 1;
  }
  return `${overs}-${unders}-${pushes}`;
}

function WatchlistView({ watchlist, loading, error }: { watchlist: WatchlistProp[]; loading: boolean; error: string | null }) {
  const [selectedGameId, setSelectedGameId] = useState<number | null>(null);
  const [marketFilter, setMarketFilter] = useState("all");
  const [sideFilter, setSideFilter] = useState("all");
  const [confidenceFilter, setConfidenceFilter] = useState("all");
  const [candidateSort, setCandidateSort] = useState<CandidateSortField>("expected_value");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");

  const gameGroups = useMemo(() => {
    const map = new Map<number, { gameId: number; startTime: string; away: string; home: string; count: number }>();
    watchlist.forEach((item) => {
      const current = map.get(item.game_id);
      if (current) {
        current.count += 1;
        return;
      }
      map.set(item.game_id, {
        gameId: item.game_id,
        startTime: item.start_time,
        away: item.away_team ?? "AWAY",
        home: item.home_team ?? "HOME",
        count: 1,
      });
    });
    return Array.from(map.values()).sort((a, b) => new Date(a.startTime).getTime() - new Date(b.startTime).getTime());
  }, [watchlist]);

  const selected = selectedGameId ?? gameGroups[0]?.gameId ?? null;
  const filtered = watchlist
    .filter((prop) => prop.game_id === selected)
    .filter((prop) => (marketFilter === "all" || prop.market === marketFilter))
    .filter((prop) => (sideFilter === "all" || prop.recommended_side === sideFilter))
    .filter((prop) => (confidenceFilter === "all" || prop.confidence === confidenceFilter))
    .sort((a, b) => compareCandidateProps(a, b, candidateSort, sortDirection));

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Watchlist</h2>
            <p>{loading ? "Loading watchlist" : "Low-confidence value plays that missed Props and Gems filters"}</p>
          </div>
          <ListChecks size={20} />
        </div>
        {error && <div className="error">{error}</div>}
        <div className="game-tabs" aria-label="Watchlist matchup tabs">
          {gameGroups.map((group) => (
            <button
              key={group.gameId}
              className={selected === group.gameId ? "active" : ""}
              onClick={() => setSelectedGameId(group.gameId)}
            >
              <span>{formatDate(group.startTime)}</span>
              <strong>{group.away} at {group.home}</strong>
              <em>{group.count} watch items</em>
            </button>
          ))}
        </div>
        <div className="parlay-tab-content">
          <div className="matchup-props">
            <div className="candidate-filters" aria-label="Watchlist filters">
              <div className="segmented candidate-tabs" aria-label="Market filter">
                {markets.map((item) => (
                  <button
                    key={`watch-market-${item.id}`}
                    className={marketFilter === item.id ? "active" : ""}
                    type="button"
                    onClick={() => setMarketFilter(item.id)}
                  >
                    {item.label}
                  </button>
                ))}
              </div>
              <div className="segmented candidate-tabs" aria-label="Side filter">
                {["all", "over", "under"].map((side) => (
                  <button
                    key={`watch-side-${side}`}
                    className={sideFilter === side ? "active" : ""}
                    type="button"
                    onClick={() => setSideFilter(side)}
                  >
                    {side === "all" ? "Both" : side.toUpperCase()}
                  </button>
                ))}
              </div>
              <select
                aria-label="Confidence filter"
                value={confidenceFilter}
                onChange={(event) => setConfidenceFilter(event.target.value)}
              >
                <option value="all">All confidence</option>
                <option value="high">High</option>
                <option value="medium">Medium</option>
                <option value="low">Low</option>
              </select>
              <select
                aria-label="Sort watchlist"
                value={candidateSort}
                onChange={(event) => setCandidateSort(event.target.value as CandidateSortField)}
              >
                <option value="expected_value">Sort by EV</option>
                <option value="edge">Sort by edge</option>
                <option value="projection">Sort by projection</option>
                <option value="line">Sort by line</option>
                <option value="projection_gap">Sort by proj gap</option>
                <option value="model_probability">Sort by model probability</option>
                <option value="confidence">Sort by confidence</option>
                <option value="player">Sort by player</option>
              </select>
              <div className="segmented candidate-tabs compact-tabs" aria-label="Sort direction">
                <button
                  className={sortDirection === "desc" ? "active" : ""}
                  type="button"
                  onClick={() => setSortDirection("desc")}
                >
                  Desc
                </button>
                <button
                  className={sortDirection === "asc" ? "active" : ""}
                  type="button"
                  onClick={() => setSortDirection("asc")}
                >
                  Asc
                </button>
              </div>
            </div>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Player</th>
                    <th>Market</th>
                    <th>Side</th>
                    <th>Line</th>
                    <th>Proj</th>
                    <th>Diff</th>
                    <th>Model Prob</th>
                    <th>Edge</th>
                    <th>EV</th>
                    <th>Confidence</th>
                  </tr>
                </thead>
                <tbody>
                  {filtered.map((prop) => (
                    <tr key={`watch-${prop.id}`}>
                      <td>
                        <div className="player-cell">
                          <TeamLogo src={prop.team_logo_url} alt={`${prop.team} logo`} />
                          <div>
                            <strong>{prop.player}</strong>
                            <span>{prop.team} | {prop.sportsbook}</span>
                            {renderRecentFormWithMinutes(prop, `${prop.id}-watch-l5`)}
                          </div>
                        </div>
                      </td>
                      <td>{marketLabel(prop.market)}</td>
                      <td><span className={`side ${prop.recommended_side}`}>{prop.recommended_side}</span></td>
                      <td>{prop.line.toFixed(1)}</td>
                      <td>{prop.projection.toFixed(1)}</td>
                      <td>{formatSigned(prop.projection - prop.line)}</td>
                      <td>{formatPercent(prop.model_probability)}</td>
                      <td>{formatPercent(prop.edge)}</td>
                      <td>{formatPercent(prop.expected_value)}</td>
                      <td>{prop.confidence}</td>
                    </tr>
                  ))}
                  {!filtered.length && (
                    <tr>
                      <td colSpan={10}>No watchlist props match current filters.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
        </div>
      </div>
    </section>
  );
}

function ParlayCandidatesView({ matchups, loading, error }: { matchups: Matchup[]; loading: boolean; error: string | null }) {
  const [selectedGameId, setSelectedGameId] = useState<number | null>(null);
  const selectedMatchup = matchups.find((matchup) => matchup.id === selectedGameId) ?? matchups[0] ?? null;

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Parlay Candidates</h2>
            <p>{loading ? "Loading candidate legs" : "Model-ranked legs and sportsbook gaps by matchup"}</p>
          </div>
          <ListChecks size={20} />
        </div>
        {error && <div className="error">{error}</div>}
        <div className="game-tabs" aria-label="Parlay candidate matchup tabs">
          {matchups.map((matchup) => (
            <button
              key={matchup.id}
              className={selectedMatchup?.id === matchup.id ? "active" : ""}
              onClick={() => setSelectedGameId(matchup.id)}
            >
              <span>{formatDate(matchup.start_time)}</span>
              <strong>{matchup.away_team} at {matchup.home_team}</strong>
              <em>{availableLabel(matchup)}</em>
            </button>
          ))}
        </div>
        <div className="parlay-tab-content">
          {selectedMatchup ? (
            <MatchupProps
              matchup={selectedMatchup}
              props={selectedMatchup.props ?? []}
              sportsbookProps={selectedMatchup.sportsbook_props ?? []}
              discrepancies={selectedMatchup.line_discrepancies ?? []}
            />
          ) : (
            <p className="empty">No scheduled games found.</p>
          )}
        </div>
      </div>
    </section>
  );
}

function MatchupProps({
  matchup,
  props,
  sportsbookProps,
  discrepancies
}: {
  matchup: Matchup;
  props: ValueProp[];
  sportsbookProps: Matchup["sportsbook_props"];
  discrepancies: LineDiscrepancy[];
}) {
  const [marketFilter, setMarketFilter] = useState("all");
  const [sideFilter, setSideFilter] = useState("all");
  const [confidenceFilter, setConfidenceFilter] = useState("all");
  const [candidateView, setCandidateView] = useState<"positive" | "all">("all");
  const [candidateSort, setCandidateSort] = useState<CandidateSortField>("expected_value");
  const [discrepancySort, setDiscrepancySort] = useState<DiscrepancySortField>("line_gap");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");

  const filteredProps = props
    .filter((prop) => {
      const marketMatch = marketFilter === "all" || prop.market === marketFilter;
      const sideMatch = sideFilter === "all" || prop.recommended_side === sideFilter;
      const confidenceMatch = confidenceFilter === "all" || prop.confidence === confidenceFilter;
      return marketMatch && sideMatch && confidenceMatch;
    })
    .sort((a, b) => compareCandidateProps(a, b, candidateSort, sortDirection));
  const positiveProps = filteredProps.filter((prop) => prop.expected_value > 0 && prop.edge > 0);
  const visibleProps = candidateView === "positive" ? positiveProps : filteredProps;
  const shortlist = positiveProps.slice(0, 5);
  const filteredDiscrepancies = discrepancies
    .filter((item) => {
      const marketMatch = marketFilter === "all" || item.market === marketFilter;
      const sideMatch = sideFilter === "all" || item.side === sideFilter;
      return marketMatch && sideMatch;
    })
    .slice()
    .sort((a, b) => compareDiscrepancies(a, b, discrepancySort, sortDirection));
  const discrepancyShortlist = filteredDiscrepancies.slice(0, 8);
  const filteredSportsbookProps = sportsbookProps
    .filter((item) => {
      const marketMatch = marketFilter === "all" || item.market === marketFilter;
      const sideMatch = sideFilter === "all" || item.side === sideFilter;
      return marketMatch && sideMatch;
    })
    .slice()
    .sort((a, b) => {
      const playerCompare = a.player_name.localeCompare(b.player_name);
      if (playerCompare !== 0) {
        return playerCompare;
      }
      const marketCompare = a.market.localeCompare(b.market);
      if (marketCompare !== 0) {
        return marketCompare;
      }
      return a.sportsbook.localeCompare(b.sportsbook);
    });
  const visibleSportsbookProps = filteredSportsbookProps.slice(0, 80);
  return (
    <div className="matchup-props">
      <div className="parlay-game-summary" aria-label="Game model prediction">
        <MiniStat label="Projected Score" value={formatProjectedScore(matchup)} />
        <MiniStat label="Winner" value={matchup.winner_pick} />
        <MiniStat label="ATS" value={matchup.ats_pick} />
        <MiniStat label="ATS Edge" value={formatNullableEdge(matchup.ats_edge)} />
        <MiniStat label="Model Total" value={formatProjectedTotal(matchup)} />
        <MiniStat label="O/U Edge" value={formatNullableEdge(matchup.total_edge)} />
        <MiniStat label="Confidence" value={matchup.game_confidence} />
      </div>
      <div className="panel-header compact">
        <div>
          <h3>Parlay Candidates</h3>
          <p>
            {props.length
              ? `${visibleProps.length} legs match the current filters, ranked by model EV and edge`
              : discrepancyShortlist.length
                ? `${discrepancyShortlist.length} sportsbook line gaps match the current filters while models are unavailable`
                : filteredSportsbookProps.length
                  ? `${filteredSportsbookProps.length} sportsbook prop lines match the current filters`
                  : "No sportsbook props attached to this game yet"}
          </p>
          {props.length > 0 && (
            <p>
              {`${props.length} total model props | ${filteredProps.length} after filters | ${positiveProps.length} with positive edge`}
            </p>
          )}
        </div>
      </div>
      <div className="candidate-filters" aria-label="Parlay candidate filters">
        <div className="segmented candidate-tabs" aria-label="Market filter">
          {markets.map((item) => (
            <button
              key={`candidate-market-${item.id}`}
              className={marketFilter === item.id ? "active" : ""}
              type="button"
              onClick={() => setMarketFilter(item.id)}
            >
              {item.label}
            </button>
          ))}
        </div>
        <div className="segmented candidate-tabs" aria-label="Side filter">
          {["all", "over", "under"].map((side) => (
            <button
              key={`candidate-side-${side}`}
              className={sideFilter === side ? "active" : ""}
              type="button"
              onClick={() => setSideFilter(side)}
            >
              {side === "all" ? "Both" : side.toUpperCase()}
            </button>
          ))}
        </div>
        {props.length > 0 && (
          <>
            <select
              aria-label="Confidence filter"
              value={confidenceFilter}
              onChange={(event) => setConfidenceFilter(event.target.value)}
            >
              <option value="all">All confidence</option>
              <option value="high">High</option>
              <option value="medium">Medium</option>
              <option value="low">Low</option>
            </select>
            <div className="segmented candidate-tabs" aria-label="Candidate view">
              <button
                className={candidateView === "positive" ? "active" : ""}
                type="button"
                onClick={() => setCandidateView("positive")}
              >
                Best edges
              </button>
              <button
                className={candidateView === "all" ? "active" : ""}
                type="button"
                onClick={() => setCandidateView("all")}
              >
                All props
              </button>
            </div>
            <select
              aria-label="Sort parlay candidates"
              value={candidateSort}
              onChange={(event) => setCandidateSort(event.target.value as CandidateSortField)}
            >
              <option value="expected_value">Sort by EV</option>
              <option value="edge">Sort by edge</option>
              <option value="projection">Sort by projection</option>
              <option value="line">Sort by line</option>
              <option value="projection_gap">Sort by proj gap</option>
              <option value="model_probability">Sort by model probability</option>
              <option value="confidence">Sort by confidence</option>
              <option value="player">Sort by player</option>
            </select>
          </>
        )}
        {!props.length && discrepancyShortlist.length > 0 && (
          <select
            aria-label="Sort sportsbook gaps"
            value={discrepancySort}
            onChange={(event) => setDiscrepancySort(event.target.value as DiscrepancySortField)}
          >
            <option value="line_gap">Sort by line gap</option>
            <option value="price_gap">Sort by price gap</option>
            <option value="books">Sort by books</option>
            <option value="player_name">Sort by player</option>
          </select>
        )}
        <div className="segmented candidate-tabs compact-tabs" aria-label="Sort direction">
          <button
            className={sortDirection === "desc" ? "active" : ""}
            type="button"
            onClick={() => setSortDirection("desc")}
          >
            Desc
          </button>
          <button
            className={sortDirection === "asc" ? "active" : ""}
            type="button"
            onClick={() => setSortDirection("asc")}
          >
            Asc
          </button>
        </div>
      </div>
      {shortlist.length > 0 && (
        <div className="candidate-strip">
          {shortlist.map((prop) => (
            <div key={`candidate-${prop.id}`} className="candidate-card">
              <span>{prop.team} | {marketLabel(prop.market)}</span>
              <strong>{prop.player}</strong>
              <em className={`candidate-side-${prop.recommended_side}`}>
                {prop.recommended_side.toUpperCase()} {prop.line.toFixed(1)} | EV {formatPercent(prop.expected_value)}
              </em>
            </div>
          ))}
        </div>
      )}
      {!props.length && discrepancyShortlist.length > 0 && (
        <div className="candidate-strip">
          {discrepancyShortlist.map((item) => (
            <div key={`gap-${item.player_name}-${item.market}-${item.side}-${item.line_gap}-${item.price_gap}`} className="candidate-card">
              <span>{marketLabel(item.market)} | {item.side.toUpperCase()}</span>
              <strong>{item.player_name}</strong>
              <em>
                {item.best_price.sportsbook} {item.best_price.line.toFixed(1)} {formatAmerican(item.best_price.price)}
                {" | "}Gap {item.line_gap.toFixed(1)}
              </em>
            </div>
          ))}
        </div>
      )}
      {props.length ? (
        <div className="table-wrap">
          <table>
            <thead>
              <tr>
                <th>Player</th>
                <th>Market</th>
                <th>Side</th>
                <th>Line</th>
                <th>Proj</th>
                <th>Diff</th>
                <th>Model Prob</th>
                <th>Edge</th>
                <th>EV</th>
                <th>Confidence</th>
              </tr>
            </thead>
            <tbody>
              {visibleProps.map((prop) => (
                <tr key={prop.id}>
                  <td>
                    <div className="player-cell">
                      <TeamLogo src={prop.team_logo_url} alt={`${prop.team} logo`} />
                      <div>
                        <strong>{prop.player}</strong>
                        <span>{prop.team} | {prop.sportsbook}</span>
                        {renderRecentFormWithMinutes(prop, `${prop.id}-matchup-l5`)}
                      </div>
                    </div>
                  </td>
                  <td>{marketLabel(prop.market)}</td>
                  <td><span className={`side ${prop.recommended_side}`}>{prop.recommended_side}</span></td>
                  <td>{prop.line.toFixed(1)}</td>
                  <td>{prop.projection.toFixed(1)}</td>
                  <td>{formatSigned(prop.projection - prop.line)}</td>
                  <td>{formatPercent(prop.model_probability)}</td>
                  <td>{formatPercent(prop.edge)}</td>
                  <td>{formatPercent(prop.expected_value)}</td>
                  <td>{prop.confidence}</td>
                </tr>
              ))}
              {!visibleProps.length && (
                <tr>
                  <td colSpan={10}>No modeled parlay candidates match these filters.</td>
                </tr>
              )}
            </tbody>
          </table>
        </div>
      ) : filteredSportsbookProps.length ? (
        <>
          <SportsbookLinesTable lines={visibleSportsbookProps} total={filteredSportsbookProps.length} />
          {discrepancyShortlist.length > 0 && (
            <LineGapsTable discrepancies={discrepancyShortlist} />
          )}
        </>
      ) : discrepancyShortlist.length ? (
        <LineGapsTable discrepancies={discrepancyShortlist} />
      ) : (
        <p className="empty">
          {sportsbookProps.length
            ? "No sportsbook prop rows match the current filters."
            : "Import sportsbook prop lines for this game, then refresh this matchup."}
        </p>
      )}
    </div>
  );
}

function LineGapsTable({ discrepancies }: { discrepancies: LineDiscrepancy[] }) {
  return (
    <div className="parlay-data-section">
      <h4>Sportsbook Line Gaps</h4>
      <div className="table-wrap">
        <table>
          <thead>
            <tr>
              <th>Player</th>
              <th>Market</th>
              <th>Side</th>
              <th>Best</th>
              <th>Low</th>
              <th>High</th>
              <th>Books</th>
            </tr>
          </thead>
          <tbody>
            {discrepancies.map((item) => (
              <tr key={`candidate-row-${item.player_name}-${item.market}-${item.side}-${item.line_gap}-${item.price_gap}`}>
                <td>{item.player_name}</td>
                <td>{marketLabel(item.market)}</td>
                <td><span className={`side ${item.side}`}>{item.side}</span></td>
                <td>
                  <strong>{item.best_price.sportsbook}</strong>
                  <span>{item.best_price.line.toFixed(1)} {formatAmerican(item.best_price.price)}</span>
                </td>
                <td>
                  <strong>{item.low_line.sportsbook}</strong>
                  <span>{item.low_line.line.toFixed(1)} {formatAmerican(item.low_line.price)}</span>
                </td>
                <td>
                  <strong>{item.high_line.sportsbook}</strong>
                  <span>{item.high_line.line.toFixed(1)} {formatAmerican(item.high_line.price)}</span>
                </td>
                <td>{item.books}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function SportsbookLinesTable({ lines, total }: { lines: Matchup["sportsbook_props"]; total: number }) {
  return (
    <div className="parlay-data-section">
      <h4>Sportsbook Lines</h4>
      <div className="table-wrap sportsbook-lines-table">
        <table>
          <thead>
            <tr>
              <th>Player</th>
              <th>Market</th>
              <th>Side</th>
              <th>Line</th>
              <th>Odds</th>
              <th>Book</th>
            </tr>
          </thead>
          <tbody>
            {lines.map((item) => (
              <tr key={`book-row-${item.id}-${item.player_name}-${item.market}-${item.side}`}>
                <td>{item.player_name}</td>
                <td>{marketLabel(item.market)}</td>
                <td><span className={`side ${item.side}`}>{item.side}</span></td>
                <td>{item.line.toFixed(1)}</td>
                <td>{formatAmerican(item.price)}</td>
                <td>{item.sportsbook}</td>
              </tr>
            ))}
            {total > lines.length && (
              <tr>
                <td colSpan={6}>Showing first {lines.length} of {total} sportsbook lines. Use filters to narrow the list.</td>
              </tr>
            )}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function TeamSummary({
  label,
  team,
  teamCode,
  logoUrl,
  restDays,
  summary,
  context,
  coversTeamRow,
  coversLast10Rows
}: {
  label: string;
  team: string;
  teamCode: string;
  logoUrl?: string | null;
  restDays: number | null;
  summary: TeamLast10;
  context: "home" | "away";
  coversTeamRow?: { team: string; record: string; ats: string; ou: string; away: string; home: string };
  coversLast10Rows?: CoversRecordRow[];
}) {
  const coversDerived = summarizeCoversTeamRows(coversLast10Rows);
  const winsLosses = coversTeamRow?.record ?? coversDerived?.record ?? `${summary.wins}-${summary.losses}`;
  const ats = coversTeamRow?.ats ?? coversDerived?.ats ?? `${summary.ats_wins}-${summary.ats_losses}-${summary.ats_pushes}`;
  const ou = coversTeamRow?.ou ?? coversDerived?.ou ?? `${summary.overs}-${summary.unders}-${summary.total_pushes}`;
  const contextDerived = summarizeCoversContextRecord(coversLast10Rows, context);
  const contextAtsDerived = summarizeCoversContextAts(coversLast10Rows, context);
  const contextOuDerived = summarizeCoversContextOu(coversLast10Rows, context);
  const contextRecord = coversTeamRow
    ? (context === "home" ? coversTeamRow.home : coversTeamRow.away)
    : contextDerived ?? (context === "home" ? `${summary.home_games}` : `${summary.away_games}`);
  return (
    <div className="team-summary">
      <div className="team-title">
        <TeamLogo src={logoUrl} alt={`${team} logo`} />
        <div>
          <span>{label}</span>
          <strong>{team}</strong>
          {coversTeamRow?.team && coversTeamRow.team !== teamCode && <em>{coversTeamRow.team}</em>}
        </div>
      </div>
      <div className="stat-strip">
        <MiniStat label="W-L" value={winsLosses} />
        <MiniStat label="Rest" value={restLabel(restDays)} />
        {context === "away" ? (
          <>
            <MiniStat label="ATS" value={ats} />
            <MiniStat label="Away Rec" value={contextRecord} />
          </>
        ) : (
          <>
            <MiniStat label="ATS" value={ats} />
            <MiniStat label="Home Rec" value={contextRecord} />
          </>
        )}
        <MiniStat label={context === "home" ? "Home O/U" : "Away O/U"} value={contextOuDerived ?? "N/A"} />
        <MiniStat label={context === "home" ? "Home ATS" : "Away ATS"} value={contextAtsDerived ?? "N/A"} />
        <MiniStat label="O/U" value={ou} />
      </div>
      <div className="points-row">
        <span>PF {summary.avg_points_for.toFixed(1)}</span>
        <span>PA {summary.avg_points_against.toFixed(1)}</span>
      </div>
    </div>
  );
}

function TeamLogo({ src, alt }: { src?: string | null; alt: string }) {
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    setFailed(false);
  }, [src]);

  if (!src || failed) {
    return <div className="team-logo fallback" aria-hidden="true" />;
  }
  return <img className="team-logo" src={src} alt={alt} loading="lazy" onError={() => setFailed(true)} />;
}

function MiniStat({ label, value }: { label: string; value: string }) {
  return (
    <div className="mini-stat">
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function Metric({ label, value, className = "" }: { label: string; value: string; className?: string }) {
  return (
    <div className={`metric ${className}`.trim()}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function formatPercent(value?: number) {
  if (value == null || Number.isNaN(value)) {
    return "Pending";
  }
  return `${(value * 100).toFixed(1)}%`;
}

function formatNumber(value: number | null) {
  if (value == null || Number.isNaN(value)) {
    return "N/A";
  }
  return value.toFixed(2);
}

function formatSigned(value: number | null) {
  if (value == null || Number.isNaN(value)) {
    return "N/A";
  }
  return value > 0 ? `+${value.toFixed(2)}` : value.toFixed(2);
}

function formatMetricNumber(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) {
    return "N/A";
  }
  return value.toFixed(digits);
}

function formatMetricSigned(value?: number | null, digits = 2) {
  if (value == null || Number.isNaN(value)) {
    return "N/A";
  }
  return value > 0 ? `+${value.toFixed(digits)}` : value.toFixed(digits);
}

function formatMetricPercent(value?: number | null) {
  if (value == null || Number.isNaN(value)) {
    return "N/A";
  }
  return `${(value * 100).toFixed(1)}%`;
}

function formatCount(value?: number | null) {
  if (value == null || Number.isNaN(value)) {
    return "N/A";
  }
  return value.toString();
}

function formatLatestMae(run: ModelRun | null) {
  if (!run) {
    return "Pending";
  }
  const values = Object.values(run.metrics)
    .map((metric) => metric.mae)
    .filter((value): value is number => value != null);
  if (!values.length) {
    return "N/A";
  }
  return (values.reduce((sum, value) => sum + value, 0) / values.length).toFixed(2);
}

function latestRunsByModel(runs: ModelRun[]) {
  const latestByModel = new Map<string, ModelRun>();
  runs.forEach((run) => {
    const existing = latestByModel.get(run.model_version);
    if (!existing || new Date(run.started_at).getTime() > new Date(existing.started_at).getTime()) {
      latestByModel.set(run.model_version, run);
    }
  });
  return Array.from(latestByModel.values()).sort(
    (a, b) => new Date(b.started_at).getTime() - new Date(a.started_at).getTime()
  );
}

function formatMetricMae(run: ModelRun, market: string) {
  return formatNumber(run.metrics[market]?.mae ?? null);
}

function formatAverageDirection(run: ModelRun) {
  const values = Object.values(run.metrics)
    .map((metric) => metric.directional_accuracy)
    .filter((value): value is number => value != null);
  if (!values.length) {
    return "N/A";
  }
  return formatPercent(values.reduce((sum, value) => sum + value, 0) / values.length);
}

function formatOverallMetricPercent(run: ModelRun, key: "side_accuracy" | "realized_roi") {
  return formatMetricPercent(run.metrics.overall?.[key]);
}

function sortModelMetrics(metrics: ModelRun["metrics"]) {
  return Object.entries(metrics).sort(([left], [right]) => {
    if (left === "overall") {
      return -1;
    }
    if (right === "overall") {
      return 1;
    }
    return marketLabel(left).localeCompare(marketLabel(right));
  });
}

function tabTitle(tab: DashboardTab) {
  const titles = {
    props: "Prop Value Board",
    gems: "Gem Finder",
    watchlist: "Prop Watchlist",
    matchups: "Pregame Matchups",
    parlays: "Parlay Candidates",
    discrepancies: "Line Discrepancies",
    roster: "Roster Status",
    models: "Model Lab",
    data: "Data Operations"
  };
  return titles[tab];
}

function buildGems(props: ValueProp[], discrepancies: LineDiscrepancy[]): GemProp[] {
  const byKey = new Map<string, { line_gap: number; price_gap: number }>();
  for (const d of discrepancies) {
    const key = `${d.game_id}|${normalizePropName(d.player_name)}|${d.market}|${d.side}`;
    const prev = byKey.get(key);
    if (!prev || d.line_gap > prev.line_gap || d.price_gap > prev.price_gap) {
      byKey.set(key, { line_gap: d.line_gap, price_gap: d.price_gap });
    }
  }
  const edgeMax = Math.max(...props.map((p) => Math.abs(p.edge)), 0.001);
  const evMax = Math.max(...props.map((p) => Math.max(p.expected_value, 0)), 0.001);
  return props
    .map((p) => {
      const key = `${p.game_id}|${normalizePropName(p.player)}|${p.market}|${p.recommended_side}`;
      const d = byKey.get(key) ?? { line_gap: 0, price_gap: 0 };
      const edgeNorm = Math.min(Math.abs(p.edge) / edgeMax, 1);
      const evNorm = Math.min(Math.max(p.expected_value, 0) / evMax, 1);
      const discNorm = Math.min((d.line_gap * 0.7) + ((d.price_gap / 100) * 0.3), 1);
      const confidenceFactor = p.confidence === "high" ? 1 : p.confidence === "medium" ? 0.85 : 0.65;
      const gemScore = ((0.45 * edgeNorm) + (0.35 * evNorm) + (0.20 * discNorm)) * confidenceFactor;
      const badges = gemBadges(p, d.line_gap, d.price_gap);
      return { ...p, line_gap: d.line_gap, price_gap: d.price_gap, gem_score: Number(gemScore.toFixed(3)), badges };
    })
    .sort((a, b) => b.gem_score - a.gem_score || b.expected_value - a.expected_value || b.edge - a.edge);
}

function normalizePropName(value: string): string {
  return value.toLowerCase().replace(/[^a-z0-9]/g, "");
}

function gemBadges(prop: ValueProp, lineGap: number, priceGap: number): string[] {
  const badges: string[] = [];
  if (lineGap >= 1.0) {
    badges.push("Best Line");
  }
  if (priceGap >= 30) {
    badges.push("Steam Lag");
  }
  if (Math.abs(prop.edge) >= 0.08 && prop.expected_value >= 0.03) {
    badges.push("Model+Market Agree");
  }
  if (!badges.length) {
    badges.push("Core Edge");
  }
  return badges;
}

function groupGemsByMatchup(gems: GemProp[], capPerMatchup: number, matchups: Matchup[]): Array<{
  key: string;
  matchup: string;
  startTime: string;
  items: GemProp[];
  topScore: number;
  avgScore: number;
}> {
  const matchupById = new Map<number, Matchup>(matchups.map((m) => [m.id, m]));
  const groups = new Map<string, GemProp[]>();
  for (const g of gems) {
    const key = `${g.game_id}`;
    const current = groups.get(key) ?? [];
    current.push(g);
    groups.set(key, current);
  }
  return Array.from(groups.entries())
    .map(([key, items]) => {
      const sorted = [...items].sort((a, b) => b.gem_score - a.gem_score || b.expected_value - a.expected_value).slice(0, capPerMatchup);
      const topScore = sorted[0]?.gem_score ?? 0;
      const avgScore = sorted.length ? (sorted.reduce((sum, item) => sum + item.gem_score, 0) / sorted.length) : 0;
      const first = sorted[0];
      const gameId = Number(key);
      const matchup = matchupById.get(gameId);
      return {
        key,
        matchup: matchup ? `${matchup.away_team} @ ${matchup.home_team}` : `Game ${key}`,
        startTime: matchup?.start_time ?? first?.start_time ?? "",
        items: sorted,
        topScore,
        avgScore
      };
    })
    .sort((a, b) => b.topScore - a.topScore || b.avgScore - a.avgScore);
}

function marketLabel(market: string) {
  const labels: Record<string, string> = {
    overall: "Overall",
    points: "PTS",
    rebounds: "REB",
    assists: "AST",
    points_rebounds: "P+R",
    points_assists: "P+A",
    rebounds_assists: "R+A",
    threes: "3PM",
    steals: "STL",
    blocks: "BLK",
    blocks_steals: "STL+BLK",
    points_rebounds_assists: "PRA"
  };
  return labels[market] ?? market;
}

function formatDate(value: string) {
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit"
  }).format(new Date(value));
}

function formatGameDateShort(value: string) {
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) {
    return value;
  }
  return new Intl.DateTimeFormat("en-US", {
    month: "short",
    day: "numeric",
    year: "2-digit"
  }).format(new Date(time));
}

function todayInputValue() {
  const date = new Date();
  const month = String(date.getMonth() + 1).padStart(2, "0");
  const day = String(date.getDate()).padStart(2, "0");
  return `${date.getFullYear()}-${month}-${day}`;
}

function dateRangeValues(startDate: string, endDate: string) {
  if (!startDate || !endDate) {
    return [];
  }
  const start = new Date(`${startDate}T00:00:00Z`);
  const end = new Date(`${endDate}T00:00:00Z`);
  if (Number.isNaN(start.getTime()) || Number.isNaN(end.getTime()) || end.getTime() < start.getTime()) {
    return [];
  }
  const dates: string[] = [];
  const cursor = new Date(start);
  while (cursor.getTime() <= end.getTime()) {
    dates.push(cursor.toISOString().slice(0, 10));
    cursor.setUTCDate(cursor.getUTCDate() + 1);
  }
  return dates;
}

function availableLabel(matchup: Matchup) {
  const parlayCount = (matchup.props ?? []).filter((prop) => prop.expected_value > 0 && prop.edge > 0).length;
  const discrepancyCount = matchup.line_discrepancies?.length ?? 0;
  const sportsbookCount = matchup.sportsbook_props?.length ?? 0;
  return `${parlayCount} parlay | ${discrepancyCount} gaps | ${sportsbookCount} book`;
}

function compareCandidateProps(a: ValueProp, b: ValueProp, field: CandidateSortField, direction: SortDirection) {
  const multiplier = direction === "asc" ? 1 : -1;
  const primary = compareSortable(candidateSortValue(a, field), candidateSortValue(b, field)) * multiplier;
  if (primary !== 0) {
    return primary;
  }
  return (b.expected_value - a.expected_value) || (b.edge - a.edge) || a.player.localeCompare(b.player);
}

function candidateSortValue(prop: ValueProp, field: CandidateSortField) {
  if (field === "confidence") {
    return confidenceRank(prop.confidence);
  }
  if (field === "player") {
    return prop.player;
  }
  if (field === "projection_gap") {
    return Math.abs(prop.projection - prop.line);
  }
  return prop[field];
}

function compareDiscrepancies(a: LineDiscrepancy, b: LineDiscrepancy, field: DiscrepancySortField, direction: SortDirection) {
  const multiplier = direction === "asc" ? 1 : -1;
  const primary = compareSortable(discrepancySortValue(a, field), discrepancySortValue(b, field)) * multiplier;
  if (primary !== 0) {
    return primary;
  }
  return (b.line_gap - a.line_gap) || (b.price_gap - a.price_gap) || a.player_name.localeCompare(b.player_name);
}

function discrepancySortValue(item: LineDiscrepancy, field: DiscrepancySortField) {
  return item[field];
}

function compareSortable(a: string | number, b: string | number) {
  if (typeof a === "string" || typeof b === "string") {
    return String(a).localeCompare(String(b));
  }
  return a - b;
}

function confidenceRank(confidence: ValueProp["confidence"]) {
  const ranks = { low: 1, medium: 2, high: 3 };
  return ranks[confidence];
}

function parlayCandidateCount(matchups: Matchup[]) {
  return matchups.reduce(
    (count, matchup) => count + (matchup.props ?? []).filter((prop) => prop.expected_value > 0 && prop.edge > 0).length,
    0
  );
}

function gamesWithParlayCandidates(matchups: Matchup[]) {
  return matchups.filter((matchup) => (matchup.props ?? []).some((prop) => prop.expected_value > 0 && prop.edge > 0)).length;
}

function countDiscrepancyBooks(discrepancies: LineDiscrepancy[]) {
  return new Set(discrepancies.flatMap((item) => item.book_lines.map((book) => book.sportsbook))).size;
}

function groupDiscrepanciesByMatchup(discrepancies: LineDiscrepancy[]) {
  const groups = new Map<string, { key: string; matchup: string; commence_time: string; items: LineDiscrepancy[] }>();
  for (const item of discrepancies) {
    const normalizedTime = normalizedDateKey(item.commence_time);
    const key = `${normalizedTime}-${item.matchup}`;
    const group = groups.get(key) ?? {
      key,
      matchup: item.matchup,
      commence_time: normalizedTime,
      items: []
    };
    group.items.push(item);
    groups.set(key, group);
  }
  return Array.from(groups.values()).sort((a, b) => new Date(a.commence_time).getTime() - new Date(b.commence_time).getTime());
}

function normalizedDateKey(value: string) {
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) {
    return value;
  }
  return new Date(time).toISOString();
}

function atsLabel(value: string) {
  if (value === "cover") {
    return "ATS W";
  }
  if (value === "no_cover") {
    return "ATS L";
  }
  if (value === "unknown") {
    return "ATS N/A";
  }
  return "ATS P";
}

function totalLabel(value: string) {
  if (value === "unknown") {
    return "N/A";
  }
  return value.toUpperCase();
}

function restLabel(days: number | null) {
  if (days == null) {
    return "N/A";
  }
  if (days <= 0) {
    return "B2B";
  }
  if (days === 1) {
    return "1 day";
  }
  return `${days} days`;
}

function formatMinutes(value: number) {
  if (value === 0) {
    return "0.0";
  }
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}`;
}

function formatSpread(value: number | null) {
  if (value == null) {
    return "N/A";
  }
  return value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1);
}

function formatMoneyline(value: number | null) {
  if (value == null) {
    return "N/A";
  }
  return value > 0 ? `+${Math.round(value)}` : Math.round(value).toString();
}

function formatGameTotal(value: number | null) {
  if (value == null || value <= 0) {
    return "N/A";
  }
  return value.toFixed(1);
}

function formatSpreadMarket(matchup: Matchup) {
  const market = matchup.spread_market;
  if (!market) {
    return `${matchup.home_team} ${formatSpread(matchup.spread_home)}`;
  }
  return `${matchup.away_team} ${formatSpread(market.away_line)} (${formatMoneyline(market.away_price)}) / ${matchup.home_team} ${formatSpread(market.home_line)} (${formatMoneyline(market.home_price)})`;
}

function formatTotalMarket(matchup: Matchup) {
  const market = matchup.total_market;
  if (!market) {
    return `${formatGameTotal(matchup.game_total)}`;
  }
  return `O${formatGameTotal(market.over_line)} (${formatMoneyline(market.over_price)}) / U${formatGameTotal(market.under_line)} (${formatMoneyline(market.under_price)})`;
}

function formatMoneylineMarket(matchup: Matchup) {
  const market = matchup.moneyline_market;
  const awayPrice = market?.away_price ?? matchup.away_moneyline;
  const homePrice = market?.home_price ?? matchup.home_moneyline;
  return `${matchup.away_team} ${formatMoneyline(awayPrice)} / ${matchup.home_team} ${formatMoneyline(homePrice)}`;
}

function formatNullableEdge(value: number | null) {
  if (value == null) {
    return "N/A";
  }
  return value > 0 ? `+${value.toFixed(1)}` : value.toFixed(1);
}

function formatProjectedScore(matchup: Matchup) {
  if (matchup.away_projected_points == null || matchup.home_projected_points == null) {
    return "N/A";
  }
  return `${matchup.away_team} ${matchup.away_projected_points.toFixed(1)} - ${matchup.home_team} ${matchup.home_projected_points.toFixed(1)}`;
}

function formatProjectedTotal(matchup: Matchup) {
  if (matchup.projected_total == null) {
    return "N/A";
  }
  return `${matchup.total_pick} ${matchup.projected_total.toFixed(1)}`;
}

function formatAmerican(value: number) {
  return value > 0 ? `+${value}` : value.toString();
}

function riskClass(value: string) {
  return value.replace(" ", "-");
}
