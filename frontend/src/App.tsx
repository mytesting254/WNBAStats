import { BrainCircuit, CalendarDays, Database, ListChecks, RefreshCw, ShieldCheck, SlidersHorizontal, TrendingUp } from "lucide-react";
import { Component, useEffect, useMemo, useRef, useState, type ErrorInfo, type ReactNode } from "react";
import {
  fetchAuthState,
  fetchUnsettledPropAudit,
  voidDnpProps,
  fetchCacheStatus,
  fetchMissingEspnScores,
  fetchOpsHealth,
  fetchLineDiscrepancies,
  fetchGemPerformance,
  fetchMatchups,
  fetchModelRuns,
  fetchPerformance,
  fetchRoster,
  fetchSpecialStocks,
  fetchSpecialStocksPerformance,
  auditStalePayloads,
  auditDbLock,
  deleteStalePayloads,
  fetchValueBoard,
  generateSpecialStocks,
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
  repairCurrentSlateProps,
  recoverDbLock,
  settleProps,
  setCsrfToken,
  trainModel,
  waitForModelTrainingCompletion,
  type AuthState,
  type CacheStatus,
  type CacheViewStatus,
  type CoversRecordRow,
  type DbLockAudit,
  type GemPerformance,
  type LineDiscrepancy,
  type Matchup,
  type MissingEspnGame,
  type ModelPerformance,
  type ModelRun,
  type OpsHealth,
  type PropSyncHealth,
  type RosterPlayer,
  type SpecialStocksSnapshot,
  type SpecialStocksPerformance,
  type StalePayloadAudit,
  type TeamLast10,
  type TeamRatings,
  type ValueProp,
  type WatchlistPerformance,
  type WatchlistProp,
  type UnsettledPropAuditItem,
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

type DashboardTab = "props" | "gems" | "watchlist" | "matchups" | "parlays" | "special" | "discrepancies" | "roster" | "models" | "data";
type CandidateSortField = "expected_value" | "edge" | "projection" | "line" | "projection_gap" | "model_probability" | "confidence" | "player";
type DiscrepancySortField = "line_gap" | "price_gap" | "books" | "player_name";
type SpecialStocksSortField =
  | "player_name"
  | "tier"
  | "projected_steals"
  | "projected_blocks"
  | "projected_stocks"
  | "steal_prob_1_plus"
  | "steal_prob_2_plus"
  | "block_prob_1_plus"
  | "block_prob_2_plus"
  | "stocks_prob_2_plus"
  | "stocks_prob_3_plus"
  | "actual_stocks"
  | "captured_at";
type SortDirection = "desc" | "asc";
type TabLoadingState = Record<DashboardTab, boolean>;
type CacheUpdateEvent = { id: number; created_at: string; views: string[] };
type RosterStatusChange = {
  team: string;
  player_name: string;
  from_status: string;
  to_status: string;
};
type RosterPullSummary = {
  captured_at: string | null;
  changes: RosterStatusChange[];
  remaining_gtd: RosterPlayer[];
};

const INITIAL_LOAD_TIMEOUT_MS = 45000;
const AUTH_LOAD_TIMEOUT_MS = 15000;
const INITIAL_TAB_LOADING: TabLoadingState = {
  props: false,
  gems: false,
  watchlist: false,
  matchups: false,
  parlays: false,
  special: false,
  discrepancies: false,
  roster: false,
  models: false,
  data: false
};
const CACHE_NAMES_BY_TAB: Record<DashboardTab, string[]> = {
  props: ["current_value_board.json"],
  gems: ["current_value_board.json", "current_matchups.json", "line_discrepancies.json", "gem_performance.json"],
  watchlist: ["current_watchlist.json", "watchlist_performance.json"],
  matchups: ["current_matchups.json"],
  parlays: ["current_value_board.json", "current_matchups.json"],
  special: [],
  discrepancies: ["line_discrepancies.json"],
  roster: ["roster.json", "current_matchups.json"],
  models: ["model_runs.json", "model_performance.json"],
  data: []
};

function withTimeout<T>(promise: Promise<T>, timeoutMs: number, label: string): Promise<T> {
  return new Promise<T>((resolve, reject) => {
    const timer = window.setTimeout(() => {
      reject(new Error(`${label} timed out after ${Math.floor(timeoutMs / 1000)}s`));
    }, timeoutMs);

    promise.then(
      (value) => {
        window.clearTimeout(timer);
        resolve(value);
      },
      (error) => {
        window.clearTimeout(timer);
        reject(error);
      }
    );
  });
}

function rosterPlayerKey(player: Pick<RosterPlayer, "team" | "player_name">) {
  return `${player.team}::${player.player_name}`.toUpperCase();
}

function buildRosterPullSummary(previous: RosterPlayer[], next: RosterPlayer[], capturedAt: string | null): RosterPullSummary {
  const previousMap = new Map(previous.map((player) => [rosterPlayerKey(player), player]));
  const nextMap = new Map(next.map((player) => [rosterPlayerKey(player), player]));
  const keys = Array.from(new Set([...previousMap.keys(), ...nextMap.keys()])).sort();
  const changes: RosterStatusChange[] = [];

  for (const key of keys) {
    const before = previousMap.get(key);
    const after = nextMap.get(key);
    if (before && after && before.status === after.status) {
      continue;
    }
    if (!before && !after) {
      continue;
    }
    const player = after ?? before;
    if (!player) {
      continue;
    }
    changes.push({
      team: player.team,
      player_name: player.player_name,
      from_status: before?.status ?? "AVAILABLE",
      to_status: after?.status ?? "AVAILABLE",
    });
  }

  const remainingGtd = [...next]
    .filter((player) => player.status.trim().toUpperCase() === "GTD")
    .sort((left, right) => {
      const teamDelta = left.team.localeCompare(right.team);
      if (teamDelta !== 0) {
        return teamDelta;
      }
      return left.player_name.localeCompare(right.player_name);
    });

  return {
    captured_at: capturedAt,
    changes,
    remaining_gtd: remainingGtd,
  };
}

type NormalizedCoversRecords = {
  head_to_head: CoversRecordRow[];
  away_last_10: CoversRecordRow[];
  home_last_10: CoversRecordRow[];
  team_table: Array<{
    team: string;
    record: string;
    ats: string;
    ou: string;
    away: string;
    home: string;
  }>;
};

class DashboardErrorBoundary extends Component<
  { children: ReactNode; onReset?: () => void },
  { hasError: boolean }
> {
  state = { hasError: false };

  static getDerivedStateFromError() {
    return { hasError: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo) {
    console.error("Dashboard render error", error, info);
  }

  componentDidUpdate(prevProps: Readonly<{ children: ReactNode; onReset?: () => void }>) {
    if (this.state.hasError && prevProps.children !== this.props.children) {
      this.setState({ hasError: false });
    }
  }

  render() {
    if (!this.state.hasError) {
      return this.props.children;
    }
    return (
      <section className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Dashboard Error</h2>
            <p>A view failed to render. Existing API data is still available.</p>
          </div>
          <ShieldCheck size={20} />
        </div>
        <div className="error">A dashboard panel crashed while rendering refreshed data.</div>
        {this.props.onReset ? (
          <button className="icon-button text-button dark-button" onClick={this.props.onReset}>
            <RefreshCw size={18} />
            Reload
          </button>
        ) : null}
      </section>
    );
  }
}

export function App() {
  const [props, setProps] = useState<ValueProp[]>([]);
  const [watchlist, setWatchlist] = useState<WatchlistProp[]>([]);
  const [matchups, setMatchups] = useState<Matchup[]>([]);
  const [specialStocks, setSpecialStocks] = useState<SpecialStocksSnapshot[]>([]);
  const [specialPerformance, setSpecialPerformance] = useState<SpecialStocksPerformance | null>(null);
  const [discrepancies, setDiscrepancies] = useState<LineDiscrepancy[]>([]);
  const [roster, setRoster] = useState<RosterPlayer[]>([]);
  const [rosterPullSummary, setRosterPullSummary] = useState<RosterPullSummary | null>(null);
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
  const [settlingProps, setSettlingProps] = useState(false);
  const [snapshottingGems, setSnapshottingGems] = useState(false);
  const [generatingSpecial, setGeneratingSpecial] = useState(false);
  const [auditingStalePayloads, setAuditingStalePayloads] = useState(false);
  const [deletingStalePayloads, setDeletingStalePayloads] = useState(false);
  const [auditingDbLock, setAuditingDbLock] = useState(false);
  const [recoveringDbLock, setRecoveringDbLock] = useState(false);
  const [activeTab, setActiveTab] = useState<DashboardTab>("props");
  const [market, setMarket] = useState("all");
  const [sideFilter, setSideFilter] = useState("all");
  const [confidence, setConfidence] = useState("all");
  const [sportsbookFilter, setSportsbookFilter] = useState("all");
  const [modelProbabilityOrder, setModelProbabilityOrder] = useState<SortDirection>("desc");
  const [selected, setSelected] = useState<ValueProp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [operationStatus, setOperationStatus] = useState<string | null>(null);
  const [stalePayloadAudit, setStalePayloadAudit] = useState<StalePayloadAudit | null>(null);
  const [dbLockAudit, setDbLockAudit] = useState<DbLockAudit | null>(null);
  const [unsettledPropAudit, setUnsettledPropAudit] = useState<{ count: number; items: UnsettledPropAuditItem[] } | null>(null);
  const [auditingUnsettledProps, setAuditingUnsettledProps] = useState(false);
  const [stalePayloadAck, setStalePayloadAck] = useState("");
  const [availabilityToast, setAvailabilityToast] = useState<string | null>(null);
  const [missingEspnDates, setMissingEspnDates] = useState<string[]>([]);
  const [missingEspnGames, setMissingEspnGames] = useState<MissingEspnGame[]>([]);
  const [loading, setLoading] = useState(true);
  const [tabLoading, setTabLoading] = useState<TabLoadingState>(INITIAL_TAB_LOADING);
  const [loadedTabs, setLoadedTabs] = useState<Record<DashboardTab, boolean>>({
    props: false,
    gems: false,
    watchlist: false,
    matchups: false,
    parlays: false,
    special: false,
    discrepancies: false,
    roster: false,
    models: false,
    data: false
  });
  const [authState, setAuthState] = useState<AuthState>({ authenticated: false, user: null, csrf_token: null });
  const [authLoading, setAuthLoading] = useState(true);
  const [authSubmitting, setAuthSubmitting] = useState(false);
  const [opsHealth, setOpsHealth] = useState<OpsHealth | null>(null);
  const [cacheStatus, setCacheStatus] = useState<CacheStatus | null>(null);
  const [activePipeline, setActivePipeline] = useState<{
    scope: string;
    label: string;
    stage: string;
    detail: string;
    startedAt: string;
    waitingForBackground: boolean;
    backendJobId?: number | null;
  } | null>(null);
  const [displayPropSync, setDisplayPropSync] = useState<PropSyncHealth | null>(null);
  const loadRequestIdRef = useRef(0);
  const operationsBusyRef = useRef(false);
  const toastTimerRef = useRef<number | null>(null);
  const lastCompletedPropSyncRef = useRef<string | null>(null);
  const idlePrefetchScheduledRef = useRef(false);
  const activeTabRef = useRef<DashboardTab>(activeTab);
  const [adminUsername, setAdminUsername] = useState("");
  const [adminPassword, setAdminPassword] = useState("");
  const isAdmin = Boolean(authState.authenticated && authState.user?.is_admin);
  const topSpecialStocks =
    specialStocks.length > 0
      ? Math.max(...specialStocks.map((snapshot) => snapshot.projected_stocks))
      : null;
  const operationsBusy =
    loading ||
    training ||
    importingOdds ||
    importingCoversOdds ||
    refreshingResults ||
    refreshingMissingScores ||
    refreshingRoster ||
    recalculating ||
    settlingProps ||
    snapshottingGems ||
    generatingSpecial ||
    auditingStalePayloads ||
    deletingStalePayloads ||
    auditingDbLock ||
    recoveringDbLock;

  useEffect(() => {
    if (!isAdmin && activeTab === "models") {
      setActiveTab("props");
    }
  }, [activeTab, isAdmin]);

  useEffect(() => {
    activeTabRef.current = activeTab;
  }, [activeTab]);

  useEffect(() => {
    if (loading) {
      return;
    }
    const timer = window.setTimeout(() => document.body.classList.add("arena-background"), 0);
    return () => window.clearTimeout(timer);
  }, [loading]);

  async function load(options?: { silent?: boolean }) {
    const requestId = ++loadRequestIdRef.current;
    if (!options?.silent) {
      setLoading(true);
    }
    setError(null);
    try {
      const board = await withTimeout(fetchValueBoard(), INITIAL_LOAD_TIMEOUT_MS, "value board");

      if (requestId !== loadRequestIdRef.current) {
        return;
      }

      setProps(board);
      setSelected((current) => {
        if (board.length === 0) {
          return null;
        }
        if (current) {
          const matchingProp = board.find((item) => item.id === current.id);
          if (matchingProp) {
            return matchingProp;
          }
        }
        return board[0] ?? null;
      });
      setLoadedTabs((current) => ({ ...current, props: true }));
    } catch (err) {
      if (requestId === loadRequestIdRef.current) {
        setError(err instanceof Error ? err.message : "Unable to load value board");
      }
    } finally {
      if (!options?.silent && requestId === loadRequestIdRef.current) {
        setLoading(false);
      }
    }
  }

  async function loadTab(tab: Exclude<DashboardTab, "props">, options?: { force?: boolean }) {
    if (!options?.force && loadedTabs[tab]) {
      return;
    }
    setTabLoading((current) => ({ ...current, [tab]: true }));
    setError(null);
    try {
      if (tab === "gems") {
        const [nextMatchups, nextDiscrepancies, nextPerformance] = await Promise.all([
          withTimeout(fetchMatchups(), INITIAL_LOAD_TIMEOUT_MS, "matchups"),
          withTimeout(fetchLineDiscrepancies(), INITIAL_LOAD_TIMEOUT_MS, "line discrepancies"),
          withTimeout(fetchGemPerformance(), INITIAL_LOAD_TIMEOUT_MS, "gem performance")
        ]);
        setMatchups(nextMatchups);
        setDiscrepancies(nextDiscrepancies);
        setGemPerformance(nextPerformance);
      } else if (tab === "watchlist") {
        const [nextWatchlist, nextPerformance] = await Promise.all([
          withTimeout(fetchWatchlist(), INITIAL_LOAD_TIMEOUT_MS, "watchlist"),
          withTimeout(fetchWatchlistPerformance(), INITIAL_LOAD_TIMEOUT_MS, "watchlist performance")
        ]);
        setWatchlist(nextWatchlist);
        setWatchlistPerformance(nextPerformance);
      } else if (tab === "matchups" || tab === "parlays") {
        setMatchups(await withTimeout(fetchMatchups(), INITIAL_LOAD_TIMEOUT_MS, "matchups"));
      } else if (tab === "special") {
        const [nextSpecialStocks, nextSpecialPerformance] = await Promise.all([
          withTimeout(fetchSpecialStocks(), INITIAL_LOAD_TIMEOUT_MS, "special props"),
          withTimeout(fetchSpecialStocksPerformance(), INITIAL_LOAD_TIMEOUT_MS, "special stats")
        ]);
        setSpecialStocks(nextSpecialStocks);
        setSpecialPerformance(nextSpecialPerformance);
      } else if (tab === "discrepancies") {
        setDiscrepancies(await withTimeout(fetchLineDiscrepancies(), INITIAL_LOAD_TIMEOUT_MS, "line discrepancies"));
      } else if (tab === "roster") {
        const [nextRoster, nextMatchups] = await Promise.all([
          withTimeout(fetchRoster(), INITIAL_LOAD_TIMEOUT_MS, "roster"),
          withTimeout(fetchMatchups(), INITIAL_LOAD_TIMEOUT_MS, "matchups")
        ]);
        setRoster(nextRoster);
        setMatchups(nextMatchups);
      } else if (tab === "models") {
        const [nextRuns, nextPerformance] = await Promise.all([
          withTimeout(fetchModelRuns(), INITIAL_LOAD_TIMEOUT_MS, "model runs"),
          withTimeout(fetchPerformance(), INITIAL_LOAD_TIMEOUT_MS, "model performance")
        ]);
        setModelRuns(nextRuns.runs);
        setLatestModelRun(nextRuns.latest);
        setPerformance(nextPerformance);
      } else if (tab === "data") {
        const [nextHealth, nextCacheStatus] = await Promise.all([
          withTimeout(fetchOpsHealth(), INITIAL_LOAD_TIMEOUT_MS, "operations health"),
          withTimeout(fetchCacheStatus(), INITIAL_LOAD_TIMEOUT_MS, "cache status")
        ]);
        setOpsHealth(nextHealth);
        setCacheStatus(nextCacheStatus);
      }
      setLoadedTabs((current) => ({ ...current, [tab]: true }));
    } catch (err) {
      setError(err instanceof Error ? err.message : `Unable to load ${tab}`);
    } finally {
      setTabLoading((current) => ({ ...current, [tab]: false }));
    }
  }

  useEffect(() => {
    load();
  }, []);

  useEffect(() => {
    if (activeTab !== "props") {
      void loadTab(activeTab);
    }
  }, [activeTab]);

  useEffect(() => {
    if (loading || !loadedTabs.props || idlePrefetchScheduledRef.current) {
      return;
    }
    idlePrefetchScheduledRef.current = true;
    let didPrefetch = false;
    const prefetch = () => {
      didPrefetch = true;
      if (document.hidden) {
        return;
      }
      void loadTab("matchups");
      void loadTab("roster");
    };
    const idleWindow = window as Window & {
      requestIdleCallback?: (callback: () => void, options?: { timeout: number }) => number;
      cancelIdleCallback?: (handle: number) => void;
    };
    if (idleWindow.requestIdleCallback) {
      const handle = idleWindow.requestIdleCallback(prefetch, { timeout: 3000 });
      return () => {
        idleWindow.cancelIdleCallback?.(handle);
        if (!didPrefetch) idlePrefetchScheduledRef.current = false;
      };
    }
    const handle = window.setTimeout(prefetch, 1500);
    return () => {
      window.clearTimeout(handle);
      if (!didPrefetch) idlePrefetchScheduledRef.current = false;
    };
  }, [loading, loadedTabs.props]);

  useEffect(() => {
    if (!("EventSource" in window)) {
      return;
    }
    const events = new EventSource("/api/cache/events");
    const refreshActiveView = (message: MessageEvent<string>) => {
      try {
        const event = JSON.parse(message.data) as CacheUpdateEvent;
        const tab = activeTabRef.current;
        if (!event.views.some((name) => CACHE_NAMES_BY_TAB[tab].includes(name))) {
          return;
        }
        if (tab === "props") {
          void load({ silent: true });
        } else if (tab !== "data") {
          void loadTab(tab, { force: true });
        }
      } catch {
        // Ignore malformed server-sent events and keep the current view usable.
      }
    };
    events.addEventListener("cache-update", refreshActiveView as EventListener);
    return () => {
      events.removeEventListener("cache-update", refreshActiveView as EventListener);
      events.close();
    };
  }, []);

  async function loadAuth() {
    setAuthLoading(true);
    try {
      const result = await withTimeout(fetchAuthState(), AUTH_LOAD_TIMEOUT_MS, "auth state");
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

  useEffect(() => {
    if (operationsBusy) {
      operationsBusyRef.current = true;
      return;
    }
    if (!operationsBusyRef.current) return;

    operationsBusyRef.current = false;
    const message = error
      ? "Previous job finished with an error. Buttons are available again."
      : operationStatus
        ? `Queue clear. ${operationStatus}`
        : "Queue clear. Buttons are available again.";

    setAvailabilityToast(message);
    if (toastTimerRef.current != null) window.clearTimeout(toastTimerRef.current);
    toastTimerRef.current = window.setTimeout(() => {
      setAvailabilityToast(null);
      toastTimerRef.current = null;
    }, 5000);

    if (document.hidden && "Notification" in window && Notification.permission === "granted") {
      new Notification("WNBA Stats ready", { body: message });
    }
  }, [operationsBusy, error, operationStatus]);

  useEffect(() => {
    return () => {
      if (toastTimerRef.current != null) window.clearTimeout(toastTimerRef.current);
    };
  }, []);

  useEffect(() => {
    const intervalMs = opsHealth?.prop_sync.running || activePipeline ? 2000 : 15000;
    const interval = window.setInterval(() => {
      fetchOpsHealth().then(setOpsHealth).catch(() => {});
      fetchCacheStatus().then(setCacheStatus).catch(() => {});
    }, intervalMs);
    return () => window.clearInterval(interval);
  }, [activePipeline, opsHealth?.prop_sync.running]);

  useEffect(() => {
    const next = opsHealth?.prop_sync ?? null;
    setDisplayPropSync((previous) => mergePropSyncProgress(previous, next));
  }, [opsHealth?.prop_sync]);

  useEffect(() => {
    const propSync = displayPropSync;
    if (!propSync || propSync.running || !propSync.finished_at) {
      return;
    }
    if (lastCompletedPropSyncRef.current === propSync.finished_at) {
      return;
    }
    lastCompletedPropSyncRef.current = propSync.finished_at;
    void load({ silent: true });
  }, [displayPropSync?.running, displayPropSync?.finished_at]);

  useEffect(() => {
    if (!activePipeline?.waitingForBackground) {
      return;
    }
    const propSync = displayPropSync;
    if (!propSync || propSync.running || !propSync.finished_at) {
      return;
    }
    const pipelineStartedAt = Date.parse(activePipeline.startedAt);
    const syncFinishedAt = Date.parse(propSync.finished_at);
    if (Number.isNaN(pipelineStartedAt) || Number.isNaN(syncFinishedAt) || syncFinishedAt < pipelineStartedAt) {
      return;
    }
    setActivePipeline(null);
  }, [activePipeline, displayPropSync]);

  useEffect(() => {
    if (!activePipeline?.waitingForBackground) {
      return;
    }
    const propSync = displayPropSync;
    if (!propSync?.running || propSync.job_id == null) {
      return;
    }
    const pipelineStartedAt = Date.parse(activePipeline.startedAt);
    const syncStartedAt = Date.parse(propSync.started_at ?? "");
    if (Number.isNaN(pipelineStartedAt) || Number.isNaN(syncStartedAt) || syncStartedAt < pipelineStartedAt) {
      return;
    }
    if (activePipeline.scope && propSync.scope && activePipeline.scope !== propSync.scope) {
      return;
    }
    setActivePipeline((current) => {
      if (current == null || !current.waitingForBackground) {
        return current;
      }
      if (current.backendJobId === propSync.job_id) {
        return current;
      }
      return {
        ...current,
        backendJobId: propSync.job_id,
      };
    });
  }, [activePipeline, displayPropSync]);

  function handleReload() {
    if (activeTab === "props") {
      void load();
      return;
    }
    void loadTab(activeTab, { force: true });
  }

  const filtered = useMemo(() => {
    return props
      .filter((prop) => {
        const marketMatch = market === "all" || prop.market === market;
        const sideMatch = sideFilter === "all" || prop.recommended_side === sideFilter;
        const confidenceMatch = confidence === "all" || prop.confidence === confidence;
        const sportsbookMatch = sportsbookFilter === "all" || displaySportsbookName(prop) === sportsbookFilter;
        return marketMatch && sideMatch && confidenceMatch && sportsbookMatch;
      })
      .sort((a, b) =>
        modelProbabilityOrder === "asc"
          ? a.model_probability - b.model_probability
          : b.model_probability - a.model_probability
      );
  }, [props, market, sideFilter, confidence, sportsbookFilter, modelProbabilityOrder]);
  const gems = useMemo(() => buildGems(props, discrepancies), [props, discrepancies]);
  const activeTabLoading = activeTab === "props" ? loading : tabLoading[activeTab];

  async function handleRecalculate() {
    setRecalculating(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await repairCurrentSlateProps();
      if (result.status === "busy") {
        setOperationStatus("Current-slate repair is already running.");
        return;
      }
      await load();
      const scopeLabel = result.scope === "scheduled" ? "scheduled slate" : "current slate";
      const scanned = result.scanned_props ?? result.synced_props ?? 0;
      const changed = result.synced_props ?? 0;
      if (!result.target_game_ids?.length) {
        setOperationStatus("No scheduled games found to recalculate.");
      } else {
        setOperationStatus(
          `${scopeLabel[0].toUpperCase()}${scopeLabel.slice(1)} refreshed: ${changed} prop lines changed from ${scanned} scanned rows, ${result.rebuilt_predictions ?? 0} projections rebuilt.`
        );
      }
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to recalculate projections");
    } finally {
      setRecalculating(false);
    }
  }

  async function handleSettleProps(selectedDates?: string[]) {
    setSettlingProps(true);
    setOperationStatus(null);
    setError(null);
    try {
      const result = await settleProps(undefined, selectedDates);
      await load();
      const usedDates = result.selected_dates?.length
        ? result.selected_dates
        : result.selected_date
          ? [result.selected_date]
          : [];
      const scope = usedDates.length ? ` for ${usedDates.join(", ")}` : "";
      setOperationStatus(
        `Settled ${result.props?.settled ?? 0} props, ${result.special?.settled ?? 0} special props, and ${result.games?.settled ?? 0} game predictions${scope}.`
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to settle props");
    } finally {
      setSettlingProps(false);
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

  async function handleGenerateSpecialStocks() {
    if (!isAdmin) {
      setError("Unauthorized. Sign in on the Data tab and try Generate again.");
      return;
    }
    setGeneratingSpecial(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await generateSpecialStocks();
      const [snapshots, performance] = await Promise.all([
        fetchSpecialStocks(),
        fetchSpecialStocksPerformance()
      ]);
      setSpecialStocks(snapshots);
      setSpecialPerformance(performance);
      setLoadedTabs((current) => ({ ...current, special: true }));
      setOperationStatus(`Generated ${result.generated} model-only special props.`);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to generate special props");
    } finally {
      setGeneratingSpecial(false);
    }
  }

  async function handleTrainModel() {
    if (!isAdmin) {
      setError("Unauthorized. Sign in on the Data tab and try Train again.");
      return;
    }
    setTraining(true);
    setError(null);
    try {
      const queued = await trainModel();
      setOperationStatus(queued.message ?? "Model training queued.");
      const result = await waitForModelTrainingCompletion(queued.started_at ?? null);
      const modelRunBoard = await fetchModelRuns();
      setModelRuns(modelRunBoard.runs);
      setLatestModelRun(modelRunBoard.latest);
      const evalRows = typeof result?.training_rows === "number" ? result.training_rows : modelRunBoard.latest?.training_rows;
      setOperationStatus(
        typeof evalRows === "number"
          ? `Model training finished. Evaluated ${evalRows} rows.`
          : "Model training finished."
      );
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
    setActivePipeline({
      scope: "odds_import",
      label: forceRefresh ? "Refresh Odds" : "Load Saved Odds",
      stage: forceRefresh ? "requesting_provider" : "loading_saved_cache",
      detail: forceRefresh
        ? "Requesting today's Odds API payloads. Successful responses are written to the raw cache before sync."
        : "Reading the saved Odds API cache and syncing sportsbook rows.",
      startedAt: new Date().toISOString(),
      waitingForBackground: false,
    });
    try {
      const result = await importOdds(forceRefresh);
      if (result.status === "busy") {
        setActivePipeline(null);
        setOperationStatus(result.message ?? "Another data pipeline is already running.");
        return;
      }
      if (result.status === "queued") {
        setActivePipeline((current) =>
          current == null
            ? null
            : {
                ...current,
                stage: "queued",
                detail: result.message ?? "Odds import queued. Waiting for backend progress.",
                waitingForBackground: true,
              }
        );
        setOperationStatus(result.message ?? "Odds import queued.");
        return;
      }
      if (result.status === "missing_api_key" || result.status === "provider_error") {
        setActivePipeline(null);
        setError(result.message ?? "Set ODDS_API_KEY to import sportsbook odds");
        return;
      }
      setActivePipeline((current) =>
        current == null
          ? null
          : {
              ...current,
              stage: result.sync_started ? "queued" : "publishing_payloads",
              detail: result.message
                ?? (result.sync_started
                  ? "Odds import finished. Background prop sync is queued."
                  : "Odds import finished. Reloading dashboard payloads."),
              waitingForBackground: Boolean(result.sync_started),
            }
      );
      await load();
      setOperationStatus(
        result.message
          ?? `${forceRefresh ? "Fresh" : "Saved"} sportsbook odds loaded. Imported ${result.imported ?? 0} sportsbook rows.`
      );
    } catch (err) {
      setActivePipeline(null);
      setError(err instanceof Error ? err.message : "Unable to import sportsbook odds");
    } finally {
      setImportingOdds(false);
      setActivePipeline((current) => (current?.waitingForBackground ? current : null));
    }
  }

  async function handleImportCoversOdds(forceRefresh = false) {
    setImportingCoversOdds(true);
    setError(null);
    setOperationStatus(null);
    setActivePipeline({
      scope: "covers_import",
      label: forceRefresh ? "Refresh Covers" : "Load Saved Covers",
      stage: forceRefresh ? "requesting_provider" : "loading_saved_cache",
      detail: forceRefresh
        ? "Fetching today's Covers matchup pages and extracting prop tables."
        : "Reading the saved Covers cache and preparing prop sync.",
      startedAt: new Date().toISOString(),
      waitingForBackground: false,
    });
    try {
      const result = await importCoversOdds(forceRefresh);
      if (result.status === "failed") {
        setActivePipeline(null);
        setError(result.message ?? "Unable to import Covers odds");
        return;
      }
      setActivePipeline((current) =>
        current == null
          ? null
          : {
              ...current,
              stage: result.sync_started ? "queued" : "publishing_payloads",
              detail: result.message
                ?? (result.sync_started
                  ? "Covers import finished. Background prop sync is queued."
                  : "Covers import finished. Reloading dashboard payloads."),
              waitingForBackground: Boolean(result.sync_started),
            }
      );
      await load();
      setOperationStatus(result.message ?? `${forceRefresh ? "Fresh" : "Saved"} Covers odds loaded. Imported ${result.imported ?? 0} sportsbook rows from ${result.source ?? "covers"}.`);
    } catch (err) {
      setActivePipeline(null);
      setError(err instanceof Error ? err.message : "Unable to import Covers odds");
    } finally {
      setImportingCoversOdds(false);
      setActivePipeline((current) => (current?.waitingForBackground ? current : null));
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
    setActivePipeline({
      scope: "espn_history",
      label: forceRefresh ? "Refresh ESPN" : missingOnly ? "Load Missing ESPN" : "Load Saved ESPN",
      stage: "requesting_provider",
      detail: includePlayerStats
        ? "Fetching ESPN scoreboards and player box scores, then rebuilding settlements and payloads."
        : "Fetching ESPN scoreboards and refreshing completed game results.",
      startedAt: new Date().toISOString(),
      waitingForBackground: false,
    });
    try {
      const result = await importEspnHistory(forceRefresh, includePlayerStats, missingOnly, includePreviousSeason, undefined, selectedDates);
      setActivePipeline((current) =>
        current == null
          ? null
          : {
              ...current,
              stage: "publishing_payloads",
              detail: "ESPN import finished. Reloading dashboard payloads.",
            }
      );
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
      setActivePipeline(null);
      setError(err instanceof Error ? err.message : "Unable to refresh completed results");
    } finally {
      setRefreshingResults(false);
      setActivePipeline(null);
    }
  }

  async function handleRefreshRoster(forceRefresh = true) {
    setRefreshingRoster(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await importRotowireInjuries(forceRefresh);
      if (result.status === "db_locked") {
        setOperationStatus(result.message ?? "Roster refresh skipped because the database is busy. Try again in a few seconds.");
        return;
      }
      try {
        const refreshedRoster = await fetchRoster();
        setRosterPullSummary(buildRosterPullSummary(roster, refreshedRoster, result.captured_at ?? null));
        setRoster(refreshedRoster);
        if (result.roster_cache_refresh?.status === "queued") {
          window.setTimeout(() => {
            void loadTab("roster", { force: true });
          }, 2500);
        }
      } catch (err) {
        const detail = err instanceof Error ? err.message : "Unable to reload roster after Rotowire refresh";
        setOperationStatus(
          `${forceRefresh ? "Fresh" : "Cached"} Rotowire lineup pull completed, but roster reload failed. ${detail}`
        );
        return;
      }
      setOperationStatus(
        result.used_fallback_cache
          ? `Rotowire refresh fell back to saved roster data. Parsed ${result.parsed_rows ?? 0} rows${result.captured_at ? ` (${result.captured_at})` : ""}.`
          : `${forceRefresh ? "Fresh" : "Cached"} Rotowire lineup pull complete. Parsed ${result.parsed_rows ?? 0} rows${result.captured_at ? ` (${result.captured_at})` : ""}${result.roster_cache_refresh?.status === "queued" ? ". Detailed roster metrics are updating in the background." : ""}.`
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
    setActivePipeline({
      scope: "espn_missing",
      label: "Import Missing ESPN Scores",
      stage: "requesting_provider",
      detail: "Fetching missing ESPN final scores and box scores, then rebuilding affected payloads.",
      startedAt: new Date().toISOString(),
      waitingForBackground: false,
    });
    try {
      const result = await importMissingEspnScores(true, true, true, 30);
      const usedDates = result.missing_dates?.length ? result.missing_dates : result.selected_dates ?? [];
      setActivePipeline((current) =>
        current == null
          ? null
          : {
              ...current,
              stage: "publishing_payloads",
              detail: "Missing-score import finished. Reloading dashboard payloads.",
            }
      );
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
      setActivePipeline(null);
      setError(err instanceof Error ? err.message : "Unable to import missing ESPN scores");
    } finally {
      setRefreshingMissingScores(false);
      setActivePipeline(null);
    }
  }

  async function handleAuditStalePayloads() {
    setAuditingStalePayloads(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await auditStalePayloads();
      setStalePayloadAudit(result);
      setStalePayloadAck("");
      setOperationStatus(
        result.stale
          ? `Audit complete. Found ${result.stale} stale payload${result.stale === 1 ? "" : "s"} out of ${result.checked} cache file${result.checked === 1 ? "" : "s"}.`
          : result.message
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to audit stale payloads");
    } finally {
      setAuditingStalePayloads(false);
    }
  }

  async function handleDeleteStalePayloads() {
    setDeletingStalePayloads(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await deleteStalePayloads(stalePayloadAck);
      setStalePayloadAudit({
        stale: 0,
        checked: result.checked,
        files: [],
        message: result.message,
      });
      setStalePayloadAck("");
      await load();
      setOperationStatus(
        result.deleted
          ? `Deleted ${result.deleted} stale payload${result.deleted === 1 ? "" : "s"}.`
          : result.message
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to delete stale payloads");
    } finally {
      setDeletingStalePayloads(false);
    }
  }

  async function handleAuditDbLock() {
    setAuditingDbLock(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await auditDbLock();
      setDbLockAudit(result);
      setOperationStatus(result.message);
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to audit database lock state");
    } finally {
      setAuditingDbLock(false);
    }
  }

  async function handleRecoverDbLock() {
    const confirmed = window.confirm(
      "Run SQLite recovery now? This performs a write probe and WAL checkpoint only. It will not force-break an active writer lock."
    );
    if (!confirmed) {
      return;
    }
    setRecoveringDbLock(true);
    setError(null);
    setOperationStatus(null);
    try {
      const result = await recoverDbLock();
      setDbLockAudit(result);
      setOperationStatus(result.message);
      await load();
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to run database recovery");
    } finally {
      setRecoveringDbLock(false);
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
          <button className="icon-button text-button" onClick={handleReload} disabled={activeTabLoading} title="Reload dashboard data">
            <RefreshCw size={18} />
            {activeTabLoading ? "Loading" : "Reload"}
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
        <button className={activeTab === "special" ? "active" : ""} onClick={() => setActiveTab("special")}>
          <ShieldCheck size={18} />
          Special
        </button>
        <button className={activeTab === "discrepancies" ? "active" : ""} onClick={() => setActiveTab("discrepancies")}>
          <SlidersHorizontal size={18} />
          Discrepancies
        </button>
        {isAdmin ? (
          <button className={activeTab === "models" ? "active" : ""} onClick={() => setActiveTab("models")}>
            <BrainCircuit size={18} />
            Models
          </button>
        ) : null}
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
          label={activeTab === "props" ? "Props ranked" : activeTab === "gems" ? "Gem candidates" : activeTab === "watchlist" ? "Watchlist legs" : activeTab === "matchups" ? "Games" : activeTab === "parlays" ? "Candidate legs" : activeTab === "special" ? "Special props" : activeTab === "discrepancies" ? "Line gaps" : activeTab === "roster" ? "Rostered players" : activeTab === "models" ? "Training rows" : "Missing score dates"}
          value={activeTab === "props" ? filtered.length.toString() : activeTab === "gems" ? gems.length.toString() : activeTab === "watchlist" ? watchlist.length.toString() : activeTab === "matchups" ? matchups.length.toString() : activeTab === "parlays" ? parlayCandidateCount(matchups, props).toString() : activeTab === "special" ? specialStocks.length.toString() : activeTab === "discrepancies" ? discrepancies.length.toString() : activeTab === "roster" ? roster.length.toString() : activeTab === "models" ? (latestModelRun?.training_rows ?? 0).toString() : missingEspnDates.length.toString()}
        />
        <Metric
          label={activeTab === "props" ? "Best EV" : activeTab === "gems" ? "Top gem score" : activeTab === "watchlist" ? "Top watch EV" : activeTab === "matchups" ? "Teams tracked" : activeTab === "parlays" ? "Games with legs" : activeTab === "special" ? "Top stocks" : activeTab === "discrepancies" ? "Books compared" : activeTab === "roster" ? "Unavailable players" : activeTab === "models" ? "Latest MAE" : "Upcoming games"}
          value={activeTab === "props" ? formatPercent(filtered[0]?.expected_value) : activeTab === "gems" ? formatNumber(gems[0]?.gem_score ?? null) : activeTab === "watchlist" ? formatPercent(watchlist[0]?.expected_value) : activeTab === "matchups" ? (matchups.length * 2).toString() : activeTab === "parlays" ? gamesWithParlayCandidates(matchups, props).toString() : activeTab === "special" ? formatNumber(topSpecialStocks) : activeTab === "discrepancies" ? countDiscrepancyBooks(discrepancies).toString() : activeTab === "roster" ? roster.length.toString() : activeTab === "models" ? formatLatestMae(latestModelRun) : matchups.length.toString()}
        />
        <Metric label="Settled props" value={(performance?.total_settled ?? performance?.settled ?? 0).toString()} />
        <Metric
          label="Win Rates"
          className="metric-compact"
          value={`Model ${performance?.win_rate == null ? "Pending" : formatPercent(performance.win_rate)} | Gems ${gemPerformance?.win_rate == null ? "Pending" : formatPercent(gemPerformance.win_rate)} | Watch ${watchlistPerformance?.win_rate == null ? "Pending" : formatPercent(watchlistPerformance.win_rate)}`}
        />
      </section>
      {availabilityToast && (
        <div className="availability-toast" role="status" aria-live="polite">
          {availabilityToast}
        </div>
      )}
      {performance?.message ? <div className="summary-message">{performance.message}</div> : null}

      <DashboardErrorBoundary key={activeTab} onReset={handleReload}>
        {activeTab === "props" ? (
          <PropsView
            items={props}
            filtered={filtered}
            loading={loading}
            error={error}
            cacheStatus={cacheStatus?.views.props ?? null}
            market={market}
            sideFilter={sideFilter}
            confidence={confidence}
            sportsbookFilter={sportsbookFilter}
            modelProbabilityOrder={modelProbabilityOrder}
            selected={selected}
            setMarket={setMarket}
            setSideFilter={setSideFilter}
            setConfidence={setConfidence}
            setSportsbookFilter={setSportsbookFilter}
            setModelProbabilityOrder={setModelProbabilityOrder}
            setSelected={setSelected}
          />
        ) : activeTab === "gems" ? (
          <GemsView gems={gems} matchups={matchups} loading={tabLoading.gems} error={error} />
        ) : activeTab === "watchlist" ? (
          <WatchlistView watchlist={watchlist} loading={tabLoading.watchlist} error={error} cacheStatus={cacheStatus?.views.watchlist ?? null} />
        ) : activeTab === "matchups" ? (
          <MatchupsView matchups={matchups} loading={tabLoading.matchups} error={error} cacheStatus={cacheStatus?.views.matchups ?? null} />
        ) : activeTab === "parlays" ? (
          <ParlayCandidatesView
            matchups={matchups}
            props={props}
            loading={tabLoading.parlays}
            error={error}
            propsCacheStatus={cacheStatus?.views.parlays.props ?? null}
            matchupsCacheStatus={cacheStatus?.views.parlays.matchups ?? null}
          />
        ) : activeTab === "special" ? (
          <SpecialStocksView
            snapshots={specialStocks}
            performance={specialPerformance}
            loading={tabLoading.special}
            error={error}
            canGenerate={isAdmin}
            generating={generatingSpecial}
            onGenerate={handleGenerateSpecialStocks}
          />
        ) : activeTab === "discrepancies" ? (
          <DiscrepanciesView discrepancies={discrepancies} loading={tabLoading.discrepancies} error={error} />
        ) : activeTab === "data" ? (
          <DataView
            loading={tabLoading.data}
            authLoading={authLoading}
            authSubmitting={authSubmitting}
            authState={authState}
            opsHealth={opsHealth}
            displayPropSync={displayPropSync}
            activePipeline={activePipeline}
            adminUsername={adminUsername}
            adminPassword={adminPassword}
            error={error}
            status={operationStatus}
            importingOdds={importingOdds}
            importingCoversOdds={importingCoversOdds}
            refreshingResults={refreshingResults}
            refreshingMissingScores={refreshingMissingScores}
            recalculating={recalculating}
            settlingProps={settlingProps}
            snapshottingGems={snapshottingGems}
            auditingStalePayloads={auditingStalePayloads}
            deletingStalePayloads={deletingStalePayloads}
            auditingDbLock={auditingDbLock}
            recoveringDbLock={recoveringDbLock}
            propsCount={props.length}
            matchupsCount={matchups.length}
            discrepanciesCount={discrepancies.length}
            missingEspnDates={missingEspnDates}
            missingEspnGames={missingEspnGames}
            stalePayloadAudit={stalePayloadAudit}
            dbLockAudit={dbLockAudit}
            unsettledPropAudit={unsettledPropAudit}
            auditingUnsettledProps={auditingUnsettledProps}
            stalePayloadAck={stalePayloadAck}
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
            onSettleProps={handleSettleProps}
            onSnapshotGems={handleSnapshotGems}
            onAuditStalePayloads={handleAuditStalePayloads}
            onDeleteStalePayloads={handleDeleteStalePayloads}
            onAuditDbLock={handleAuditDbLock}
            onRecoverDbLock={handleRecoverDbLock}
            onAuditUnsettledProps={async () => {
              setAuditingUnsettledProps(true);
              setError(null);
              try { setUnsettledPropAudit(await fetchUnsettledPropAudit()); } catch (err) { setError(err instanceof Error ? err.message : "Unable to audit unsettled props"); } finally { setAuditingUnsettledProps(false); }
            }}
            onVoidDnp={async (gameId, playerId) => {
              setAuditingUnsettledProps(true);
              try { const result = await voidDnpProps(gameId, playerId); setUnsettledPropAudit(await fetchUnsettledPropAudit()); setOperationStatus(`Voided ${result.voided_prop_lines} DNP prop lines.`); } catch (err) { setError(err instanceof Error ? err.message : "Unable to void DNP props"); } finally { setAuditingUnsettledProps(false); }
            }}
            onStalePayloadAckChange={setStalePayloadAck}
            onReload={handleReload}
          />
        ) : activeTab === "roster" ? (
          <RosterView
            roster={roster}
            pullSummary={rosterPullSummary}
            matchups={matchups}
            loading={tabLoading.roster}
            error={error}
            status={operationStatus}
            refreshing={refreshingRoster}
            onRefresh={() => handleRefreshRoster(true)}
            canRefresh={isAdmin}
          />
        ) : activeTab === "models" ? (
          <ModelsView
            runs={modelRuns}
            latest={latestModelRun}
            loading={tabLoading.models || training}
            error={error}
            onTrain={handleTrainModel}
            canTrain={isAdmin}
          />
        ) : null}
      </DashboardErrorBoundary>
    </main>
  );
}

function DataView({
  loading,
  authLoading,
  authSubmitting,
  authState,
  opsHealth,
  displayPropSync,
  activePipeline,
  adminUsername,
  adminPassword,
  error,
  status,
  importingOdds,
  importingCoversOdds,
  refreshingResults,
  refreshingMissingScores,
  recalculating,
  settlingProps,
  snapshottingGems,
  auditingStalePayloads,
  deletingStalePayloads,
  auditingDbLock,
  recoveringDbLock,
  propsCount,
  matchupsCount,
  discrepanciesCount,
  missingEspnDates,
  missingEspnGames,
  stalePayloadAudit,
  dbLockAudit,
  unsettledPropAudit,
  auditingUnsettledProps,
  stalePayloadAck,
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
  onSettleProps,
  onSnapshotGems,
  onAuditStalePayloads,
  onDeleteStalePayloads,
  onAuditDbLock,
  onRecoverDbLock,
  onAuditUnsettledProps,
  onVoidDnp,
  onStalePayloadAckChange,
  onReload
}: {
  loading: boolean;
  authLoading: boolean;
  authSubmitting: boolean;
  authState: AuthState;
  opsHealth: OpsHealth | null;
  displayPropSync: PropSyncHealth | null;
  activePipeline: {
    scope: string;
    label: string;
    stage: string;
    detail: string;
    startedAt: string;
    waitingForBackground: boolean;
    backendJobId?: number | null;
  } | null;
  adminUsername: string;
  adminPassword: string;
  error: string | null;
  status: string | null;
  importingOdds: boolean;
  importingCoversOdds: boolean;
  refreshingResults: boolean;
  refreshingMissingScores: boolean;
  recalculating: boolean;
  settlingProps: boolean;
  snapshottingGems: boolean;
  auditingStalePayloads: boolean;
  deletingStalePayloads: boolean;
  auditingDbLock: boolean;
  recoveringDbLock: boolean;
  propsCount: number;
  matchupsCount: number;
  discrepanciesCount: number;
  missingEspnDates: string[];
  missingEspnGames: MissingEspnGame[];
  stalePayloadAudit: StalePayloadAudit | null;
  dbLockAudit: DbLockAudit | null;
  unsettledPropAudit: { count: number; items: UnsettledPropAuditItem[] } | null;
  auditingUnsettledProps: boolean;
  stalePayloadAck: string;
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
  onSettleProps: (selectedDates?: string[]) => void;
  onSnapshotGems: () => void;
  onAuditStalePayloads: () => void;
  onDeleteStalePayloads: () => void;
  onAuditDbLock: () => void;
  onRecoverDbLock: () => void;
  onAuditUnsettledProps: () => void;
  onVoidDnp: (gameId: number, playerId: number) => void;
  onStalePayloadAckChange: (value: string) => void;
  onReload: () => void;
}) {
  const [resultDate, setResultDate] = useState(todayInputValue());
  const [batchStartDate, setBatchStartDate] = useState(todayInputValue());
  const [batchEndDate, setBatchEndDate] = useState(todayInputValue());
  const parsedBatchDates = dateRangeValues(batchStartDate, batchEndDate);
  const busy =
    refreshingResults ||
    refreshingMissingScores ||
    importingOdds ||
    importingCoversOdds ||
    loading ||
    settlingProps ||
    auditingStalePayloads ||
    deletingStalePayloads ||
    auditingDbLock ||
    recoveringDbLock;
  const today = todayInputValue();
  const missingPriorDateGames = missingEspnGames.filter((game) => game.game_date < today).length;
  const missingTodayGames = missingEspnGames.length - missingPriorDateGames;
  const missingSummary = !missingEspnGames.length
    ? "No missing-score scan loaded yet."
    : `Missing: ${missingEspnGames.length} games (${missingPriorDateGames} prior-date, ${missingTodayGames} today) on ${missingEspnDates.length} date(s): ${missingEspnDates.join(", ")}`;
  const isAdmin = Boolean(authState.authenticated && authState.user?.is_admin);
  const propSync = displayPropSync;
  const backendPipelineSync = activePipeline?.waitingForBackground
    ? matchActivePipelineSync(activePipeline, propSync)
    : null;
  const pipelineRunning = activePipeline != null;
  const pipelineStageLabel = activePipeline
    ? activePipeline.waitingForBackground
      ? formatPropSyncStage(backendPipelineSync?.stage)
      : formatPipelineStage(activePipeline.stage)
    : formatPropSyncStage(propSync?.stage);
  const pipelinePercent = pipelineRunning
    ? activePipeline?.waitingForBackground
      ? Math.max(0, Math.min(100, Math.round((backendPipelineSync?.percent ?? 0) * 100)))
      : 15
    : Math.max(0, Math.min(100, Math.round((propSync?.percent ?? 0) * 100)));
  const pipelineLabel = pipelineRunning
    ? activePipeline!.label
    : propSync == null
      ? "Unknown"
      : propSync.running
        ? "Background Prop Sync"
        : "Clear";
  const pipelineDetail = pipelineRunning
    ? activePipeline!.waitingForBackground
      ? backendPipelineSync?.message || activePipeline!.detail
      : activePipeline!.detail
    : propSync == null
      ? "Operations health unavailable."
      : propSync.running
        ? propSync.message || `Started ${formatDateTime(propSync.started_at)}`
        : `Last finished ${formatDateTime(propSync.finished_at)}`;
  const progressPercent = Math.max(0, Math.min(100, Math.round((propSync?.percent ?? 0) * 100)));
  const progressStageLabel = formatPropSyncStage(propSync?.stage);
  const queueLabel = propSync == null ? "Unknown" : propSync.running ? "Running" : "Clear";
  const queueDetail = propSync == null
    ? "Operations health unavailable."
    : propSync.running
      ? propSync.message || `Started ${formatDateTime(propSync.started_at)}`
      : `Last finished ${formatDateTime(propSync.finished_at)}`;
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
        <div className="status-card-row">
          <article className="operation-card operation-card-compact">
            <div>
              <p className="eyebrow">live pipeline</p>
              <h3>{pipelineLabel}</h3>
              <p>{pipelineDetail}</p>
              <div className="progress-block" aria-live="polite">
                <div className="progress-meta">
                  <strong>{pipelineStageLabel}</strong>
                  <span>{pipelinePercent}%</span>
                </div>
                <div className="progress-track" role="progressbar" aria-valuenow={pipelinePercent} aria-valuemin={0} aria-valuemax={100}>
                  <div className="progress-fill" style={{ width: `${pipelinePercent}%` }} />
                </div>
                <p className="progress-caption">
                  {pipelineRunning
                    ? activePipeline?.waitingForBackground
                      ? formatPropSyncProgressCaption(backendPipelineSync ?? undefined)
                      : "Request is running on the backend."
                    : propSync?.running
                      ? formatPropSyncProgressCaption(propSync)
                      : "No active ingest request."}
                </p>
              </div>
            </div>
            <div className="detail-grid detail-grid-compact">
              <Metric label="Request" value={pipelineRunning ? "Active" : "Idle"} />
              <Metric label="Phase" value={pipelineStageLabel} />
              <Metric label="Started" value={pipelineRunning ? formatDateTime(activePipeline?.startedAt) : formatDateTime(propSync?.started_at)} />
              <Metric
                label="Background"
                value={pipelineRunning ? (activePipeline?.waitingForBackground ? "Queued" : "None") : (propSync?.running ? "Running" : "Idle")}
              />
            </div>
          </article>
          <article className="operation-card operation-card-compact">
            <div>
              <p className="eyebrow">prop sync queue</p>
              <h3>{queueLabel}</h3>
              <p>{queueDetail}</p>
              {propSync ? (
                <div className="progress-block" aria-live="polite">
                  <div className="progress-meta">
                    <strong>{progressStageLabel}</strong>
                    <span>{progressPercent}%</span>
                  </div>
                  <div className="progress-track" role="progressbar" aria-valuenow={progressPercent} aria-valuemin={0} aria-valuemax={100}>
                    <div className="progress-fill" style={{ width: `${progressPercent}%` }} />
                  </div>
                  <p className="progress-caption">
                    {formatPropSyncProgressCaption(propSync)}
                  </p>
                </div>
              ) : null}
              {propSync?.last_error ? <p className="reason">Last error: {propSync.last_error}</p> : null}
            </div>
            <div className="detail-grid detail-grid-compact">
              <Metric label="Running" value={propSync?.running ? "Yes" : "No"} />
              <Metric label="Phase" value={progressStageLabel} />
              <Metric label="Started" value={formatDateTime(propSync?.started_at)} />
              <Metric label="Finished" value={formatDateTime(propSync?.finished_at)} />
              <Metric
                label="Last result"
                value={propSync?.last_result && Object.keys(propSync.last_result).length ? "Ready" : "None"}
              />
            </div>
          </article>
        </div>
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
            disabled={importingOdds || importingCoversOdds || refreshingResults || settlingProps || loading}
            onPrimary={() => onImportOdds(false)}
            onSecondary={() => onImportOdds(true)}
          />
          <OperationCard
            title="Covers Odds"
            description="Import today's Covers matchup prop tables without using The Odds API credits."
            metrics={`${discrepanciesCount} line gaps | ${matchupsCount} upcoming games`}
            primaryLabel={importingCoversOdds ? "Loading" : "Load Saved Covers"}
            secondaryLabel="Refresh Covers"
            disabled={importingOdds || importingCoversOdds || refreshingResults || settlingProps || loading}
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
              <button
                className="secondary-button"
                onClick={() => onSettleProps([resultDate])}
                disabled={busy || !resultDate}
              >
                {settlingProps ? "Settling" : "Settle Date"}
              </button>
              <button
                className="secondary-button"
                onClick={() => onSettleProps(parsedBatchDates)}
                disabled={busy || parsedBatchDates.length === 0}
              >
                Settle Batch
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
          <article className="operation-card">
            <div>
              <p className="eyebrow">settlement review</p>
              <h3>Unsettled Props</h3>
              <p>Review final-game props that could not settle. Missing box scores require DNP confirmation before a prop can be voided.</p>
              {unsettledPropAudit ? (
                unsettledPropAudit.items.length ? (
                  <div className="reason">
                    {unsettledPropAudit.items.map((item) => (
                      <div key={`${item.game_id}-${item.player_id}`}>
                        {item.game_date} · {item.player_name} · {item.prop_count} props · {item.review_status.replace(/_/g, " ")}
                        {item.availability_reason ? ` (${item.availability_reason})` : ""}
                        {item.review_status === "missing_boxscore" ? (
                          <button className="secondary-button" onClick={() => { if (window.confirm(`Confirm ${item.player_name} was a DNP and void ${item.prop_count} props?`)) onVoidDnp(item.game_id, item.player_id); }} disabled={busy || auditingUnsettledProps}>
                            Void confirmed DNP
                          </button>
                        ) : null}
                      </div>
                    ))}
                  </div>
                ) : <p className="reason">No unsettled final props.</p>
              ) : null}
            </div>
            <div className="operation-actions">
              <button className="icon-button text-button dark-button" onClick={onAuditUnsettledProps} disabled={busy || auditingUnsettledProps}>
                <ListChecks size={18} />
                {auditingUnsettledProps ? "Auditing" : "Audit Unsettled Props"}
              </button>
            </div>
          </article>
          <OperationCard
            title="Projection Board"
            description="Rebuild model projections from the current prop lines and player history."
            metrics={`${matchupsCount} upcoming games | ${missingEspnDates.length} missing score dates`}
            primaryLabel={recalculating ? "Recalculating" : "Recalculate"}
            secondaryLabel={snapshottingGems ? "Tracking Gems" : "Track Gems Daily"}
            disabled={refreshingResults || importingOdds || importingCoversOdds || loading || recalculating || settlingProps || snapshottingGems}
            onPrimary={onRecalculate}
            onSecondary={onSnapshotGems}
          />
          <article className="operation-card">
            <div>
              <p className="eyebrow">{stalePayloadAudit ? `${stalePayloadAudit.stale} stale of ${stalePayloadAudit.checked} checked` : "cache housekeeping"}</p>
              <h3>Stale Payload Audit</h3>
              <p>Audit cached payload files first, then choose whether to delete the stale ones. Type the acknowledgement exactly before delete is enabled.</p>
              <p className="reason">Acknowledgement: <strong>{`DELETE STALE PAYLOAD`}</strong></p>
              {stalePayloadAudit ? (
                <p className="reason">
                  {stalePayloadAudit.files.length
                    ? `Pending delete: ${stalePayloadAudit.files.join(", ")}`
                    : stalePayloadAudit.message}
                </p>
              ) : null}
            </div>
            <div className="operation-fields">
              <label>
                Acknowledgement
                <input
                  type="text"
                  value={stalePayloadAck}
                  onChange={(event) => onStalePayloadAckChange(event.target.value)}
                  placeholder="Type DELETE STALE PAYLOAD"
                />
              </label>
            </div>
            <div className="operation-actions">
              <button className="icon-button text-button dark-button" onClick={onAuditStalePayloads} disabled={busy}>
                <RefreshCw size={18} />
                {auditingStalePayloads ? "Auditing" : "Audit Stale Payloads"}
              </button>
              <button
                className="secondary-button"
                onClick={onDeleteStalePayloads}
                disabled={
                  busy ||
                  !stalePayloadAudit ||
                  stalePayloadAudit.stale === 0 ||
                  stalePayloadAck.trim() !== "DELETE STALE PAYLOAD"
                }
              >
                {deletingStalePayloads ? "Deleting" : "Delete Audited Payloads"}
              </button>
            </div>
          </article>
          <article className="operation-card">
            <div>
              <p className="eyebrow">{dbLockAudit ? (dbLockAudit.locked ? "sqlite locked" : dbLockAudit.status) : "sqlite recovery"}</p>
              <h3>DB Lock Audit</h3>
              <p>Check whether local SQLite is writable, inspect WAL sidecar files, and run a safe recovery attempt that checkpoints WAL without force-breaking an active writer.</p>
              <p className="reason">
                {dbLockAudit
                  ? `${dbLockAudit.message}${dbLockAudit.wal ? ` WAL ${dbLockAudit.wal.exists ? `${dbLockAudit.wal.size_bytes} bytes` : "missing"}.` : ""}${dbLockAudit.shm ? ` SHM ${dbLockAudit.shm.exists ? `${dbLockAudit.shm.size_bytes} bytes` : "missing"}.` : ""}`
                  : "Run audit first. Recovery only succeeds when no other writer is actively holding the database."}
              </p>
            </div>
            <div className="operation-actions">
              <button className="icon-button text-button dark-button" onClick={onAuditDbLock} disabled={busy}>
                <RefreshCw size={18} />
                {auditingDbLock ? "Auditing" : "Audit DB Lock"}
              </button>
              <button className="secondary-button" onClick={onRecoverDbLock} disabled={busy}>
                {recoveringDbLock ? "Recovering" : "Attempt Recovery"}
              </button>
            </div>
          </article>
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
  pullSummary,
  matchups,
  loading,
  error,
  status,
  refreshing,
  onRefresh,
  canRefresh = true
}: {
  roster: RosterPlayer[];
  pullSummary: RosterPullSummary | null;
  matchups: Matchup[];
  loading: boolean;
  error: string | null;
  status: string | null;
  refreshing: boolean;
  onRefresh: () => void;
  canRefresh?: boolean;
}) {
  const teams = useMemo(() => Array.from(new Set(roster.map((item) => item.team))).sort(), [roster]);
  const todayIso = localIsoDate();
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
  const visibleRows = useMemo(() => {
    const filtered = selectedTeam ? roster.filter((item) => item.team === selectedTeam) : roster;
    return [...filtered].sort((left, right) => {
      const leftSeverity = rosterStatusSeverity(left.status);
      const rightSeverity = rosterStatusSeverity(right.status);
      if (rightSeverity !== leftSeverity) {
        return rightSeverity - leftSeverity;
      }
      const impactDelta = (right.player_impact_score ?? -1) - (left.player_impact_score ?? -1);
      if (impactDelta !== 0) {
        return impactDelta;
      }
      return left.player_name.localeCompare(right.player_name);
    });
  }, [roster, selectedTeam]);
  const latestCapturedAt = useMemo(() => {
    const timestamps = roster
      .map((item) => item.captured_at)
      .filter((value): value is string => Boolean(value))
      .map((value) => new Date(value).getTime())
      .filter((value) => !Number.isNaN(value));
    if (!timestamps.length) {
      return null;
    }
    return new Date(Math.max(...timestamps)).toISOString();
  }, [roster]);
  const rosterCapturedDate = latestCapturedAt ? latestCapturedAt.slice(0, 10) : null;
  const todaysMatchupTeams = useMemo(() => {
    const currentTeams = new Set<string>();
    for (const matchup of matchups) {
      if (matchup.game_date !== todayIso) {
        continue;
      }
      currentTeams.add(matchup.home_team);
      currentTeams.add(matchup.away_team);
    }
    return Array.from(currentTeams).sort();
  }, [matchups, todayIso]);
  const missingTodayTeams = useMemo(
    () => todaysMatchupTeams.filter((team) => !teams.includes(team)),
    [todaysMatchupTeams, teams]
  );
  const rosterFreshnessWarning = useMemo(() => {
    if (!latestCapturedAt && missingTodayTeams.length) {
      return `Rotowire roster feed has no current timestamp. Today's slate includes ${missingTodayTeams.join(", ")}, but those teams are missing from the roster feed.`;
    }
    const warningParts: string[] = [];
    if (latestCapturedAt && rosterCapturedDate && rosterCapturedDate < todayIso) {
      warningParts.push(`Rotowire roster feed last updated ${formatDateTime(latestCapturedAt)}.`);
    }
    if (missingTodayTeams.length) {
      warningParts.push(`Today's slate includes ${missingTodayTeams.join(", ")}, but those teams are missing from the roster feed.`);
    }
    return warningParts.length ? warningParts.join(" ") : null;
  }, [latestCapturedAt, missingTodayTeams, rosterCapturedDate, todayIso]);
  const teamSummary = visibleRows[0] ?? null;
  const teamImpactPercent = teamSummary?.team_injury_factor != null
    ? (1 - teamSummary.team_injury_factor) * 100
    : null;

  return (
    <section className="matchup-list">
      <div className="board-panel roster-panel">
        <div className="panel-header">
          <div>
            <h2>Team Roster Status</h2>
            <p>{loading ? "Loading lineup status" : "Rotowire lineup statuses grouped by team with impact context"}</p>
          </div>
          <ShieldCheck size={20} />
        </div>
        {canRefresh ? (
          <div className="operation-actions roster-actions">
            <button className="icon-button text-button dark-button" onClick={onRefresh} disabled={loading || refreshing}>
              <RefreshCw size={18} />
              {refreshing ? "Refreshing" : "Refresh Roster"}
            </button>
          </div>
        ) : null}
        {error && <div className="error">{error}</div>}
        {status && <div className="success">{status}</div>}
        {rosterFreshnessWarning && <div className="warning">{rosterFreshnessWarning}</div>}
        <section className="roster-change-card" aria-live="polite">
          <div className="roster-change-card-header">
            <div>
              <h3>Latest Pull Changes</h3>
              <p>
                {pullSummary?.captured_at
                  ? `Compared against the prior roster snapshot at ${formatDateTime(pullSummary.captured_at)}.`
                  : "Run Refresh Roster to compare the latest pull against the prior snapshot."}
              </p>
            </div>
            {pullSummary ? (
              <span className="roster-change-count">
                {pullSummary.changes.length} change{pullSummary.changes.length === 1 ? "" : "s"}
              </span>
            ) : null}
          </div>
          {pullSummary ? (
            pullSummary.changes.length ? (
              <div className="roster-change-list">
                {pullSummary.changes.map((change) => (
                  <article
                    key={`${change.team}-${change.player_name}-${change.from_status}-${change.to_status}`}
                    className="roster-change-item"
                  >
                    <div>
                      <strong>{change.player_name}</strong>
                      <span>{change.team}</span>
                    </div>
                    <p>{change.from_status} to {change.to_status}</p>
                  </article>
                ))}
              </div>
            ) : (
              <p className="roster-change-empty">
                No changes yet.
                {pullSummary.remaining_gtd.length
                  ? ` ${pullSummary.remaining_gtd.length} player${pullSummary.remaining_gtd.length === 1 ? "" : "s"} still GTD: ${pullSummary.remaining_gtd.map((player) => `${player.player_name} (${player.team})`).join(", ")}.`
                  : " No players are still GTD."}
              </p>
            )
          ) : (
            <p className="roster-change-empty">No roster pull has been compared yet.</p>
          )}
        </section>
        <div className="game-tabs roster-tabs" aria-label="Roster team tabs">
          {teams.map((team) => (
            <button key={team} className={selectedTeam === team ? "active" : ""} onClick={() => setSelectedTeam(team)}>
              <span>Team</span>
              <strong>{team}</strong>
              <em>{roster.filter((item) => item.team === team).length} players</em>
            </button>
          ))}
        </div>
        {teamSummary ? (
          <section className="summary-grid roster-summary-grid">
            <Metric label="Key absences" value={formatCount(teamSummary.team_missing_key_players)} className="metric-compact" />
            <Metric
              label="Team impact"
              value={teamImpactPercent == null ? "N/A" : `-${teamImpactPercent.toFixed(1)}%`}
              className={`metric-compact ${teamImpactMetricClass(teamImpactPercent)}`.trim()}
            />
            <Metric label="Penalty score" value={formatMetricNumber(teamSummary.team_penalty_points)} className="metric-compact" />
            <Metric label="Tracked players" value={formatCount(visibleRows.length)} className="metric-compact" />
          </section>
        ) : null}
        <div className="props-table-wrapper roster-table-wrapper">
          <table className="props-table roster-table">
            <thead>
              <tr>
                <th>Player</th>
                <th>Status</th>
                <th>Role</th>
                <th>MPG</th>
                <th>Contrib</th>
                <th>Impact</th>
                <th>Out Since</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {visibleRows.map((item) => (
                <tr key={`${item.team}-${item.player_name}-${item.status}`} className={`roster-row roster-row-${statusClass(item.status)}`}>
                  <td>
                    <div className="roster-player-cell">
                      <PlayerLabel name={item.player_name} position={item.position} />
                      <span className="roster-player-meta">{formatRosterPosition(item.position)} | {item.team}</span>
                    </div>
                  </td>
                  <td>
                    <div className="roster-status-cell">
                      <span className={`status-pill ${statusClass(item.status)}`}>{item.status}</span>
                    </div>
                  </td>
                  <td>{formatRosterRole(item.rotation_role)}</td>
                  <td>{formatMetricNumber(item.recent_minutes_avg, 1)}</td>
                  <td>{formatMetricNumber(item.recent_contribution_avg, 1)}</td>
                  <td>{formatMetricNumber(item.player_impact_score, 1)}</td>
                  <td title={item.out_since ? formatDate(item.out_since) : undefined}>
                    {item.out_since ? formatOutSince(item.out_since) : "—"}
                  </td>
                  <td>{item.captured_at ? formatDate(item.captured_at) : "N/A"}</td>
                </tr>
              ))}
              {!visibleRows.length && (
                <tr>
                  <td colSpan={8}>No Rotowire lineup rows available yet. Run injury import or reload matchups to refresh lineups.</td>
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

function formatRosterRole(role?: string | null) {
  if (!role) {
    return "N/A";
  }
  return role
    .split("_")
    .map((part) => part.charAt(0).toUpperCase() + part.slice(1))
    .join(" ");
}

function formatRosterPosition(position?: string | null) {
  const value = String(position || "").trim().toUpperCase();
  return value || "POS N/A";
}

function rosterStatusSeverity(status: string) {
  const value = status.trim().toUpperCase();
  if (value === "OUT" || value === "INACTIVE" || value === "SUSPENDED" || value === "UNAVAILABLE") {
    return 3;
  }
  if (value === "GTD" || value === "QUESTIONABLE" || value === "DOUBTFUL") {
    return 2;
  }
  if (value === "PROBABLE") {
    return 1;
  }
  return 0;
}

function teamImpactMetricClass(value: number | null) {
  if (value == null || Number.isNaN(value)) {
    return "";
  }
  if (value >= 7) {
    return "metric-impact-severe";
  }
  if (value >= 4) {
    return "metric-impact-moderate";
  }
  if (value >= 1.5) {
    return "metric-impact-mild";
  }
  return "metric-impact-low";
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
  onTrain,
  canTrain
}: {
  runs: ModelRun[];
  latest: ModelRun | null;
  loading: boolean;
  error: string | null;
  onTrain: () => void;
  canTrain: boolean;
}) {
  const metrics = latest ? sortModelMetrics(latest.metrics) : [];
  const playerMetrics = metrics.filter(([market]) => !market.startsWith("game_"));
  const gameMetrics = metrics.filter(([market]) => market.startsWith("game_"));
  const overallMetric = latest?.metrics.overall;
  const overallGameMetric = latest?.metrics.game_overall;
  const comparisonRuns = latestRunsByModel(runs);
  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Model Training</h2>
            <p>{loading ? "Training or loading model runs" : "Walk-forward evaluation using only prior games"}</p>
          </div>
          {canTrain && (
            <button className="icon-button text-button dark-button" onClick={onTrain} disabled={loading}>
              <BrainCircuit size={18} />
              Train
            </button>
          )}
        </div>
        {error && <div className="error">{error}</div>}
        <div className="model-layout">
          <div className="model-card">
            <p className="eyebrow">Latest run</p>
            <h3>{latest?.model_version ?? "No run yet"}</h3>
            <p className="reason">
              {latest?.notes ?? "Run training to create a segmented walk-forward benchmark. This does not use future games for each evaluation window."}
            </p>
            <div className="detail-grid">
              <Metric label="Status" value={latest?.status ?? "Pending"} />
              <Metric label="Eval rows" value={(latest?.training_rows ?? 0).toString()} />
              <Metric label="Type" value={latest?.run_type ?? "N/A"} />
              <Metric label="Finished" value={latest?.finished_at ? formatDate(latest.finished_at) : "N/A"} />
            </div>
            <div className="detail-grid model-validation-grid">
              <Metric label="Base MAE" value={formatMetricNumber(overallMetric?.baseline_mae)} />
              <Metric label="MAE delta" value={formatMetricSigned(overallMetric?.mae_improvement, 3)} />
              <Metric label="Base RMSE" value={formatMetricNumber(overallMetric?.baseline_rmse)} />
              <Metric label="RMSE delta" value={formatMetricSigned(overallMetric?.rmse_improvement, 3)} />
            </div>
            <div className="detail-grid model-validation-grid">
              <Metric label="Settled rows" value={formatCount(overallMetric?.settled_rows)} />
              <Metric label="Side accuracy" value={formatMetricPercent(overallMetric?.side_accuracy)} />
              <Metric label="Calibration gap" value={formatMetricPercent(overallMetric?.calibration_gap)} />
              <Metric label="Realized ROI" value={formatMetricPercent(overallMetric?.realized_roi)} />
            </div>
            <div className="detail-grid model-validation-grid">
              <Metric label="Eval segments" value={formatCount(overallMetric?.segment_count)} />
              <Metric label="Skipped segs" value={formatCount(overallMetric?.skipped_segments)} />
              <Metric label="Game eval rows" value={formatCount(overallGameMetric?.rows)} />
              <Metric label="Game MAE delta" value={formatMetricSigned(overallGameMetric?.mae_improvement, 3)} />
            </div>
            <div className="detail-grid model-validation-grid">
              <Metric label="Game RMSE delta" value={formatMetricSigned(overallGameMetric?.rmse_improvement, 3)} />
              <Metric label="Game dir delta" value={formatMetricPercent(overallGameMetric?.directional_accuracy_improvement)} />
              <Metric label="Bias" value={formatMetricSigned(overallMetric?.bias, 3)} />
              <Metric label="Base bias" value={formatMetricSigned(overallMetric?.baseline_bias, 3)} />
            </div>
            <div className="detail-grid model-validation-grid">
              <Metric
                label="Train kept"
                value={formatTrainingKept(
                  overallMetric?.training_sample_diagnostics?.included_rows,
                  overallMetric?.training_sample_diagnostics?.candidate_rows
                )}
              />
              <Metric
                label="Ctx skips"
                value={formatCount(overallMetric?.training_sample_diagnostics?.skipped_incomplete_context)}
              />
              <Metric
                label="Residual kept"
                value={formatTrainingKept(
                  overallMetric?.residual_training_sample_diagnostics?.included_rows,
                  overallMetric?.residual_training_sample_diagnostics?.candidate_rows
                )}
              />
              <Metric
                label="Residual ctx skips"
                value={formatCount(overallMetric?.residual_training_sample_diagnostics?.skipped_incomplete_context)}
              />
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
                    <th>Base MAE</th>
                    <th>MAE delta</th>
                    <th>RMSE</th>
                    <th>Base RMSE</th>
                    <th>RMSE delta</th>
                    <th>Bias</th>
                    <th>Base bias</th>
                    <th>Direction</th>
                    <th>Segments</th>
                    <th>Skipped</th>
                    <th>Train kept</th>
                    <th>Ctx skips</th>
                    <th>Residual kept</th>
                    <th>Residual ctx skips</th>
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
                  {playerMetrics.map(([market, metric]) => (
                    <tr key={market} className={market === "overall" ? "model-summary-row" : undefined}>
                      <td>{marketLabel(market)}</td>
                      <td>{metric.rows}</td>
                      <td>{formatNumber(metric.mae)}</td>
                      <td>{formatMetricNumber(metric.baseline_mae)}</td>
                      <td>{formatMetricSigned(metric.mae_improvement, 3)}</td>
                      <td>{formatNumber(metric.rmse)}</td>
                      <td>{formatMetricNumber(metric.baseline_rmse)}</td>
                      <td>{formatMetricSigned(metric.rmse_improvement, 3)}</td>
                      <td>{formatSigned(metric.bias)}</td>
                      <td>{formatMetricSigned(metric.baseline_bias, 3)}</td>
                      <td>{formatPercent(metric.directional_accuracy ?? undefined)}</td>
                      <td>{formatCount(metric.segment_count)}</td>
                      <td>{formatCount(metric.skipped_segments)}</td>
                      <td>{formatTrainingKept(metric.training_sample_diagnostics?.included_rows, metric.training_sample_diagnostics?.candidate_rows)}</td>
                      <td>{formatCount(metric.training_sample_diagnostics?.skipped_incomplete_context)}</td>
                      <td>{formatTrainingKept(metric.residual_training_sample_diagnostics?.included_rows, metric.residual_training_sample_diagnostics?.candidate_rows)}</td>
                      <td>{formatCount(metric.residual_training_sample_diagnostics?.skipped_incomplete_context)}</td>
                      <td>{formatCount(metric.settled_rows)}</td>
                      <td>{formatMetricPercent(metric.side_accuracy)}</td>
                      <td>{formatMetricPercent(metric.calibration_gap)}</td>
                      <td>{formatMetricNumber(metric.brier_score, 4)}</td>
                      <td>{formatMetricSigned(metric.avg_edge, 3)}</td>
                      <td>{formatMetricSigned(metric.avg_expected_value, 3)}</td>
                      <td>{formatMetricPercent(metric.realized_roi)}</td>
                    </tr>
                  ))}
                  {!playerMetrics.length && (
                    <tr>
                      <td colSpan={24}>No model metrics yet.</td>
                    </tr>
                  )}
                </tbody>
              </table>
            </div>
          </div>
          <div className="model-card">
            <p className="eyebrow">Game residual metrics</p>
            <div className="table-wrap">
              <table>
                <thead>
                  <tr>
                    <th>Market</th>
                    <th>Rows</th>
                    <th>Blend MAE</th>
                    <th>Base MAE</th>
                    <th>MAE delta</th>
                    <th>Blend RMSE</th>
                    <th>Base RMSE</th>
                    <th>RMSE delta</th>
                    <th>Blend Dir</th>
                    <th>Base Dir</th>
                    <th>Dir delta</th>
                  </tr>
                </thead>
                <tbody>
                  {gameMetrics.map(([market, metric]) => (
                    <tr key={market} className={market === "game_overall" ? "model-summary-row" : undefined}>
                      <td>{marketLabel(market)}</td>
                      <td>{metric.rows}</td>
                      <td>{formatNumber(metric.mae)}</td>
                      <td>{formatNumber(metric.baseline_mae ?? null)}</td>
                      <td>{formatMetricSigned(metric.mae_improvement, 3)}</td>
                      <td>{formatNumber(metric.rmse)}</td>
                      <td>{formatNumber(metric.baseline_rmse ?? null)}</td>
                      <td>{formatMetricSigned(metric.rmse_improvement, 3)}</td>
                      <td>{formatPercent(metric.directional_accuracy ?? undefined)}</td>
                      <td>{formatPercent(metric.baseline_directional_accuracy ?? undefined)}</td>
                      <td>{formatMetricPercent(metric.directional_accuracy_improvement)}</td>
                    </tr>
                  ))}
                  {!gameMetrics.length && (
                    <tr>
                      <td colSpan={11}>No game residual metrics yet.</td>
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
                    <th>ATS MAE delta</th>
                    <th>ATS Dir delta</th>
                    <th>Total MAE delta</th>
                    <th>Total Dir delta</th>
                    <th>Game MAE delta</th>
                    <th>Game Dir delta</th>
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
                      <td>{formatMetricSigned(run.metrics.game_ats?.mae_improvement, 3)}</td>
                      <td>{formatMetricPercent(run.metrics.game_ats?.directional_accuracy_improvement)}</td>
                      <td>{formatMetricSigned(run.metrics.game_total?.mae_improvement, 3)}</td>
                      <td>{formatMetricPercent(run.metrics.game_total?.directional_accuracy_improvement)}</td>
                      <td>{formatMetricSigned(run.metrics.game_overall?.mae_improvement, 3)}</td>
                      <td>{formatMetricPercent(run.metrics.game_overall?.directional_accuracy_improvement)}</td>
                    </tr>
                  ))}
                  {!comparisonRuns.length && (
                    <tr>
                      <td colSpan={17}>No model runs saved yet.</td>
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
  items,
  filtered,
  loading,
  error,
  cacheStatus,
  market,
  sideFilter,
  confidence,
  sportsbookFilter,
  modelProbabilityOrder,
  selected,
  setMarket,
  setSideFilter,
  setConfidence,
  setSportsbookFilter,
  setModelProbabilityOrder,
  setSelected
}: {
  items: ValueProp[];
  filtered: ValueProp[];
  loading: boolean;
  error: string | null;
  cacheStatus: CacheViewStatus | null;
  market: string;
  sideFilter: string;
  confidence: string;
  sportsbookFilter: string;
  modelProbabilityOrder: SortDirection;
  selected: ValueProp | null;
  setMarket: (market: string) => void;
  setSideFilter: (side: string) => void;
  setConfidence: (confidence: string) => void;
  setSportsbookFilter: (sportsbook: string) => void;
  setModelProbabilityOrder: (order: SortDirection) => void;
  setSelected: (prop: ValueProp) => void;
}) {
  const sportsbookOptions = useMemo(
    () =>
      sportsbookFilterOptions(
        items.filter((prop) => {
          const marketMatch = market === "all" || prop.market === market;
          const sideMatch = sideFilter === "all" || prop.recommended_side === sideFilter;
          const confidenceMatch = confidence === "all" || prop.confidence === confidence;
          return marketMatch && sideMatch && confidenceMatch;
        })
      ),
    [items, market, sideFilter, confidence]
  );
  return (
    <section className="workspace">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Pregame Props</h2>
            <p>{loading ? "Loading projections" : "Ranked by expected value and edge"}</p>
            <div className="cache-badge-row">
              <CacheFreshnessBadge label="Props cache" status={cacheStatus} />
            </div>
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
          <div className="segmented candidate-tabs" aria-label="Prop side filter">
            {["all", "over", "under"].map((side) => (
              <button
                key={`props-side-${side}`}
                className={sideFilter === side ? "active" : ""}
                type="button"
                onClick={() => setSideFilter(side)}
              >
                {side === "all" ? "Both" : side.toUpperCase()}
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
            value={sportsbookFilter}
            onChange={(event) => setSportsbookFilter(event.target.value)}
            aria-label="Props best sportsbook filter"
          >
            {sportsbookOptions.map((sportsbook) => (
              <option key={`props-book-${sportsbook}`} value={sportsbook}>
                {sportsbook === "all" ? "All best books" : sportsbook}
              </option>
            ))}
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
                        <PlayerLabel name={prop.player} position={prop.position} increasedRole={prop.increased_role} />
                        <span>{prop.team} | {displaySportsbookName(prop)}</span>
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
                  <p className="eyebrow">{selected.team} | {displaySportsbookName(selected)}</p>
                  <h3><PlayerLabel name={selected.player} position={selected.position} increasedRole={selected.increased_role} /></h3>
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

function renderRecentOutcomesWithMinutes(
  snapshot: Pick<SpecialStocksSnapshot, "recent_values" | "recent_minutes">,
  keyPrefix: string
) {
  if (!Array.isArray(snapshot.recent_values) || snapshot.recent_values.length === 0) {
    return <span className="empty-inline">No history</span>;
  }
  return (
    <div className="prop-l5-strip" aria-label="Last 5 outcomes and minutes">
      {snapshot.recent_values.slice(0, 5).map((value, idx) => (
        <span
          key={`${keyPrefix}-${idx}`}
          className="neutral"
          title={`Stocks result: ${value.toFixed(1)}`}
        >
          {Number.isInteger(value) ? value.toFixed(0) : value.toFixed(1)}
        </span>
      ))}
      {Array.isArray(snapshot.recent_minutes) && snapshot.recent_minutes.length > 0 && (
        <>
          <span className="l5-separator" title="Minutes distribution">|</span>
          {snapshot.recent_minutes.slice(0, 5).map((minutes, idx) => (
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
  const [sportsbookFilter, setSportsbookFilter] = useState("all");
  const [sortField, setSortField] = useState<"gem_score" | "expected_value" | "edge" | "line_gap" | "price_gap">("gem_score");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const [groupedByMatchup, setGroupedByMatchup] = useState(true);
  const [matchupCap, setMatchupCap] = useState(3);
  const [selectedGroupKey, setSelectedGroupKey] = useState<string | null>(null);
  const sportsbookOptions = useMemo(() => sportsbookFilterOptions(gems), [gems]);
  const shown = useMemo(() => {
    const cfg = {
      conservative: { minScore: 0.55, limit: 20 },
      balanced: { minScore: 0.42, limit: 24 },
      aggressive: { minScore: 0.32, limit: 30 }
    }[preset];
    return gems
      .filter((g) => qualifiesForGem(g, preset))
      .filter((g) => g.gem_score >= cfg.minScore)
      .filter((g) => marketFilter === "all" || g.market === marketFilter)
      .filter((g) => confidenceFilter === "all" || g.confidence === confidenceFilter)
      .filter((g) => sideFilter === "all" || g.recommended_side === sideFilter)
      .filter((g) => sportsbookFilter === "all" || displaySportsbookName(g) === sportsbookFilter)
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
  }, [gems, preset, marketFilter, confidenceFilter, sideFilter, sportsbookFilter, sortField, sortDirection]);
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
          <select value={sportsbookFilter} onChange={(event) => setSportsbookFilter(event.target.value)} aria-label="Gem best sportsbook filter">
            {sportsbookOptions.map((sportsbook) => (
              <option key={`gem-book-${sportsbook}`} value={sportsbook}>
                {sportsbook === "all" ? "All best books" : sportsbook}
              </option>
            ))}
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
                <th>Best</th>
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
                    <span>{g.team} | {marketLabel(g.market)}</span>
                    {renderRecentFormWithMinutes(g, `${g.id}-gem-flat-l5`)}
                  </td>
                  <td><span className={`side ${g.recommended_side}`}>{g.recommended_side} {g.line.toFixed(1)}</span></td>
                  <td>{displaySportsbookName(g)}</td>
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
                  <td colSpan={10}>No gems pass the {preset} preset right now.</td>
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
                        <th>Best</th>
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
                            <span>{g.team} | {marketLabel(g.market)}</span>
                            {renderRecentFormWithMinutes(g, `${selectedGroup.key}-${g.id}-gem-group-l5`)}
                          </td>
                          <td><span className={`side ${g.recommended_side}`}>{g.recommended_side} {g.line.toFixed(1)}</span></td>
                          <td>{displaySportsbookName(g)}</td>
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

function MatchupsView({
  matchups,
  loading,
  error,
  cacheStatus,
}: {
  matchups: Matchup[];
  loading: boolean;
  error: string | null;
  cacheStatus: CacheViewStatus | null;
}) {
  const [selectedGameId, setSelectedGameId] = useState<number | null>(null);
  const selectedMatchup = matchups.find((matchup) => matchup.id === selectedGameId) ?? matchups[0] ?? null;
  const selectedCoversRecords = normalizeCoversRecords(selectedMatchup?.covers_records);

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Today's Games</h2>
            <p>{loading ? "Loading matchups" : "Last 10 form, home/away split, ATS, and totals"}</p>
            <div className="cache-badge-row">
              <CacheFreshnessBadge label="Matchups cache" status={cacheStatus} />
            </div>
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
                  ratings={selectedMatchup.away_team_ratings}
                  context="away"
                  coversTeamRow={selectedCoversRecords.team_table.find((item) => normalizeTeamCode(item.team) === normalizeTeamCode(selectedMatchup.away_team))}
                  coversLast10Rows={selectedCoversRecords.away_last_10}
                />
                <TeamSummary
                  label="Home"
                  team={selectedMatchup.home_team_name}
                  teamCode={selectedMatchup.home_team}
                  logoUrl={selectedMatchup.home_logo_url}
                  restDays={selectedMatchup.home_rest_days}
                  summary={selectedMatchup.home}
                  ratings={selectedMatchup.home_team_ratings}
                  context="home"
                  coversTeamRow={selectedCoversRecords.team_table.find((item) => normalizeTeamCode(item.team) === normalizeTeamCode(selectedMatchup.home_team))}
                  coversLast10Rows={selectedCoversRecords.home_last_10}
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
                <MiniStat
                  label="Net Diff"
                  value={formatSignedNumber(selectedMatchup.rating_differentials?.season_net_diff)}
                  className={signedValueTone(selectedMatchup.rating_differentials?.season_net_diff)}
                />
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
  const records = normalizeCoversRecords(matchup.covers_records);
  const hasCovers = Boolean(records.head_to_head.length || records.away_last_10.length || records.home_last_10.length);
  const h2hRows = records.head_to_head.length
    ? records.head_to_head.slice(0, 10)
    : buildFallbackH2HRows(matchup);
  const singleH2HRow = h2hRows.length === 1 ? h2hRows[0] : null;
  const awayRows = hasCovers
    ? records.away_last_10.slice(0, 10)
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
    ? records.home_last_10.slice(0, 10)
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

  const h2hGames = [
    ...matchup.away.recent_games
      .filter((game) => normalizeTeamCode(game.opponent) === homeCode)
      .map((game) => ({ game, perspective: awayCode as string })),
    ...matchup.home.recent_games
      .filter((game) => normalizeTeamCode(game.opponent) === awayCode)
      .map((game) => ({ game, perspective: homeCode as string })),
  ];
  const deduped = new Map<string, CoversRecordRow>();
  for (const { game, perspective } of h2hGames) {
    const perspectiveIsAway = perspective === awayCode;
    const teamWasHome = Boolean(game.is_home);
    const homeTeam = teamWasHome ? perspective : perspectiveIsAway ? homeCode : awayCode;
    const awayPoints = perspectiveIsAway ? game.points : game.opponent_points;
    const homePoints = perspectiveIsAway ? game.opponent_points : game.points;
    const winner = awayPoints === homePoints ? null : awayPoints > homePoints ? awayCode : homeCode;
    const score = teamWasHome
      ? `${game.points} - ${game.opponent_points}`
      : `${game.opponent_points} - ${game.points}`;
    const row: CoversRecordRow = {
      date: formatGameDateShort(game.game_date),
      home: homeTeam,
      winner,
      score,
      ats: atsLabel(game.ats_result).replace("ATS ", ""),
      total: totalLabel(game.total_result),
    };
    const key = `${game.game_date}-${[awayCode, homeCode].sort().join("-")}-${score}`;
    if (!deduped.has(key)) {
      deduped.set(key, row);
    }
  }
  return Array.from(deduped.values()).slice(0, 10);
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

function normalizeCoversRecords(records: Matchup["covers_records"] | null | undefined): NormalizedCoversRecords {
  const source = records && typeof records === "object" ? records : null;
  return {
    head_to_head: Array.isArray(source?.head_to_head) ? source.head_to_head.map(normalizeCoversRecordRow).filter((row): row is CoversRecordRow => row != null) : [],
    away_last_10: Array.isArray(source?.away_last_10) ? source.away_last_10.map(normalizeCoversRecordRow).filter((row): row is CoversRecordRow => row != null) : [],
    home_last_10: Array.isArray(source?.home_last_10) ? source.home_last_10.map(normalizeCoversRecordRow).filter((row): row is CoversRecordRow => row != null) : [],
    team_table: Array.isArray(source?.team_table) ? source.team_table : [],
  };
}

function normalizeCoversRecordRow(row: unknown): CoversRecordRow | null {
  if (!row || typeof row !== "object") {
    return null;
  }
  const source = row as Partial<CoversRecordRow>;
  const date = typeof source.date === "string" ? source.date : "";
  const score = typeof source.score === "string" ? source.score : "";
  if (!date || !score) {
    return null;
  }
  return {
    date,
    score,
    ats: typeof source.ats === "string" ? source.ats : "",
    total: typeof source.total === "string" ? source.total : "",
    home: typeof source.home === "string" ? source.home : undefined,
    winner: typeof source.winner === "string" ? source.winner : source.winner ?? undefined,
    opponent: typeof source.opponent === "string" ? source.opponent : undefined,
    location: source.location === "home" || source.location === "away" ? source.location : undefined,
    result: typeof source.result === "string" ? source.result : source.result ?? undefined,
  };
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

function WatchlistView({
  watchlist,
  loading,
  error,
  cacheStatus,
}: {
  watchlist: WatchlistProp[];
  loading: boolean;
  error: string | null;
  cacheStatus: CacheViewStatus | null;
}) {
  const [selectedGameId, setSelectedGameId] = useState<number | null>(null);
  const [marketFilter, setMarketFilter] = useState("all");
  const [sideFilter, setSideFilter] = useState("all");
  const [confidenceFilter, setConfidenceFilter] = useState("all");
  const [sportsbookFilter, setSportsbookFilter] = useState("all");
  const [candidateSort, setCandidateSort] = useState<CandidateSortField>("expected_value");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const sportsbookOptions = useMemo(() => sportsbookFilterOptions(watchlist), [watchlist]);

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

  const selectedGroup = gameGroups.find((group) => group.gameId === selectedGameId) ?? gameGroups[0] ?? null;
  const selected = selectedGroup?.gameId ?? null;
  const filtered = watchlist
    .filter((prop) => prop.game_id === selected)
    .filter((prop) => (marketFilter === "all" || prop.market === marketFilter))
    .filter((prop) => (sideFilter === "all" || prop.recommended_side === sideFilter))
    .filter((prop) => (confidenceFilter === "all" || prop.confidence === confidenceFilter))
    .filter((prop) => (sportsbookFilter === "all" || displaySportsbookName(prop) === sportsbookFilter))
    .sort((a, b) => compareCandidateProps(a, b, candidateSort, sortDirection));

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Watchlist</h2>
            <p>{loading ? "Loading watchlist" : "Low-confidence value plays that missed Props and Gems filters"}</p>
            <div className="cache-badge-row">
              <CacheFreshnessBadge label="Watchlist cache" status={cacheStatus} />
            </div>
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
                aria-label="Watchlist best sportsbook filter"
                value={sportsbookFilter}
                onChange={(event) => setSportsbookFilter(event.target.value)}
              >
                {sportsbookOptions.map((sportsbook) => (
                  <option key={`watch-book-${sportsbook}`} value={sportsbook}>
                    {sportsbook === "all" ? "All best books" : sportsbook}
                  </option>
                ))}
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
                    <th>Best</th>
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
                        <PlayerLabel name={prop.player} position={prop.position} increasedRole={prop.increased_role} />
                        <span>{prop.team}</span>
                        {renderRecentFormWithMinutes(prop, `${prop.id}-watch-l5`)}
                          </div>
                        </div>
                      </td>
                      <td>{marketLabel(prop.market)}</td>
                      <td><span className={`side ${prop.recommended_side}`}>{prop.recommended_side}</span></td>
                      <td>{displaySportsbookName(prop)}</td>
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
                      <td colSpan={11}>No watchlist props match current filters.</td>
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

function ParlayCandidatesView({
  matchups,
  props,
  loading,
  error,
  propsCacheStatus,
  matchupsCacheStatus,
}: {
  matchups: Matchup[];
  props: ValueProp[];
  loading: boolean;
  error: string | null;
  propsCacheStatus: CacheViewStatus | null;
  matchupsCacheStatus: CacheViewStatus | null;
}) {
  const [selectedGameId, setSelectedGameId] = useState<number | null>(null);
  const matchupCards = useMemo(
    () =>
      matchups.map((matchup) => ({
        matchup,
        props: propsForMatchup(matchup, props),
      })),
    [matchups, props]
  );
  const selectedCard = matchupCards.find(({ matchup }) => matchup.id === selectedGameId) ?? matchupCards[0] ?? null;
  const selectedMatchup = selectedCard?.matchup ?? null;

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Parlay Candidates</h2>
            <p>{loading ? "Loading candidate legs" : "Model-ranked legs and sportsbook gaps by matchup"}</p>
            <div className="cache-badge-row">
              <CacheFreshnessBadge label="Props cache" status={propsCacheStatus} />
              <CacheFreshnessBadge label="Matchups cache" status={matchupsCacheStatus} />
            </div>
          </div>
          <ListChecks size={20} />
        </div>
        {error && <div className="error">{error}</div>}
        <div className="game-tabs" aria-label="Parlay candidate matchup tabs">
          {matchupCards.map(({ matchup, props: matchupProps }) => (
            <button
              key={matchup.id}
              className={selectedMatchup?.id === matchup.id ? "active" : ""}
              onClick={() => setSelectedGameId(matchup.id)}
            >
              <span>{formatDate(matchup.start_time)}</span>
              <strong>{matchup.away_team} at {matchup.home_team}</strong>
              <em>{parlayAvailabilityLabel(matchup, matchupProps)}</em>
            </button>
          ))}
        </div>
        <div className="parlay-tab-content">
          {selectedMatchup ? (
            <MatchupProps
              matchup={selectedMatchup}
              props={selectedCard?.props ?? []}
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

function SpecialStocksView({
  snapshots,
  performance,
  loading,
  error,
  canGenerate,
  generating,
  onGenerate,
}: {
  snapshots: SpecialStocksSnapshot[];
  performance: SpecialStocksPerformance | null;
  loading: boolean;
  error: string | null;
  canGenerate: boolean;
  generating: boolean;
  onGenerate: () => void;
}) {
  const [selectedGameId, setSelectedGameId] = useState<number | null>(null);
  const [sortField, setSortField] = useState<SpecialStocksSortField>("stocks_prob_2_plus");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const highConfidenceThreshold = performance?.threshold_recommendations?.high_confidence_threshold ?? 0.5;
  const watchThreshold = performance?.threshold_recommendations?.watch_threshold ?? 0.4;
  const cards = useMemo(() => {
    const grouped = new Map<number, {
      gameId: number;
      gameDate: string;
      startTime: string | null;
      homeTeam: string | null;
      awayTeam: string | null;
      candidateCount50Plus: number;
      avgProb2Plus: number;
      playerCount: number;
      players: SpecialStocksSnapshot[];
    }>();
    for (const snapshot of snapshots) {
      const current = grouped.get(snapshot.game_id);
      if (current) {
        current.players.push(snapshot);
        if (snapshot.board_candidate_count_threshold == null && (snapshot.stocks_prob_2_plus ?? 0) >= highConfidenceThreshold) {
          current.candidateCount50Plus += 1;
        }
      } else {
        grouped.set(snapshot.game_id, {
          gameId: snapshot.game_id,
          gameDate: snapshot.game_date,
          startTime: snapshot.start_time ?? null,
          homeTeam: snapshot.home_team ?? null,
          awayTeam: snapshot.away_team ?? null,
          candidateCount50Plus:
            snapshot.board_candidate_count_threshold
            ?? ((snapshot.stocks_prob_2_plus ?? 0) >= highConfidenceThreshold ? 1 : 0),
          avgProb2Plus: 0,
          playerCount: snapshot.board_player_count ?? 0,
          players: [snapshot],
        });
      }
    }
    return Array.from(grouped.values()).map((card) => ({
      ...card,
      candidateCount50Plus: card.players[0]?.board_candidate_count_threshold
        ?? card.players.filter((player) => (player.stocks_prob_2_plus ?? 0) >= highConfidenceThreshold).length,
      avgProb2Plus: card.players[0]?.board_avg_prob_2_plus
        ?? (card.players.length > 0
          ? card.players.reduce((sum, player) => sum + (player.stocks_prob_2_plus ?? 0), 0) / card.players.length
          : 0),
      playerCount: card.players[0]?.board_player_count ?? (card.playerCount > 0 ? card.playerCount : card.players.length),
      players: card.players.slice().sort((left, right) => {
        const tierValue = (snapshot: SpecialStocksSnapshot) => (
          (snapshot.stocks_prob_2_plus ?? 0) >= highConfidenceThreshold
            ? 2
            : (snapshot.stocks_prob_2_plus ?? 0) >= watchThreshold
              ? 1
              : 0
        );
        const valueForSort = (snapshot: SpecialStocksSnapshot, field: SpecialStocksSortField) => {
          switch (field) {
            case "player_name":
              return snapshot.player_name ?? "";
            case "tier":
              return tierValue(snapshot);
            case "projected_steals":
              return snapshot.projected_steals ?? 0;
            case "projected_blocks":
              return snapshot.projected_blocks ?? 0;
            case "projected_stocks":
              return snapshot.projected_stocks ?? 0;
            case "steal_prob_1_plus":
              return snapshot.steal_prob_1_plus ?? 0;
            case "steal_prob_2_plus":
              return snapshot.steal_prob_2_plus ?? 0;
            case "block_prob_1_plus":
              return snapshot.block_prob_1_plus ?? 0;
            case "block_prob_2_plus":
              return snapshot.block_prob_2_plus ?? 0;
            case "stocks_prob_2_plus":
              return snapshot.stocks_prob_2_plus ?? 0;
            case "stocks_prob_3_plus":
              return snapshot.stocks_prob_3_plus ?? 0;
            case "actual_stocks":
              return snapshot.actual_stocks ?? -1;
            case "captured_at":
              return Date.parse(snapshot.captured_at ?? "") || 0;
            default:
              return 0;
          }
        };
        const leftValue = valueForSort(left, sortField);
        const rightValue = valueForSort(right, sortField);
        let comparison = 0;
        if (typeof leftValue === "string" && typeof rightValue === "string") {
          comparison = leftValue.localeCompare(rightValue);
        } else {
          comparison = Number(leftValue) - Number(rightValue);
        }
        if (comparison === 0) {
          const probabilityDelta = (right.stocks_prob_2_plus ?? 0) - (left.stocks_prob_2_plus ?? 0);
          if (Math.abs(probabilityDelta) > 0.0001) {
            return probabilityDelta;
          }
          return right.projected_stocks - left.projected_stocks;
        }
        return sortDirection === "asc" ? comparison : -comparison;
      }),
    })).sort((left, right) => {
      const leftTime = Date.parse(left.startTime || left.gameDate);
      const rightTime = Date.parse(right.startTime || right.gameDate);
      if (Number.isNaN(leftTime) && Number.isNaN(rightTime)) {
        return left.gameId - right.gameId;
      }
      if (Number.isNaN(leftTime)) {
        return 1;
      }
      if (Number.isNaN(rightTime)) {
        return -1;
      }
      return leftTime - rightTime;
    });
  }, [highConfidenceThreshold, snapshots, sortDirection, sortField, watchThreshold]);
  const selectedCard = cards.find((card) => card.gameId === selectedGameId) ?? cards[0] ?? null;
  const toggleSort = (field: SpecialStocksSortField) => {
    if (sortField === field) {
      setSortDirection((current) => (current === "desc" ? "asc" : "desc"));
      return;
    }
    setSortField(field);
    setSortDirection(field === "player_name" || field === "captured_at" ? "asc" : "desc");
  };
  const sortIndicator = (field: SpecialStocksSortField) => {
    if (sortField !== field) {
      return "↕";
    }
    return sortDirection === "asc" ? "↑" : "↓";
  };

  return (
    <section className="matchup-list">
      <div className="board-panel">
        <div className="panel-header">
          <div>
            <h2>Special Props</h2>
            <p>{loading ? "Loading model-only stocks props" : "In-house steals and blocks projections for players already on the slate"}</p>
          </div>
          {canGenerate ? (
            <button className="icon-button text-button dark-button" onClick={onGenerate} disabled={generating}>
              <RefreshCw size={18} />
              {generating ? "Generating" : "Generate"}
            </button>
          ) : (
            <ShieldCheck size={20} />
          )}
        </div>
        {error && <div className="error">{error}</div>}
        <div className="game-tabs" aria-label="Special props matchup tabs">
          {cards.map((card) => (
            <button
              key={card.gameId}
              className={selectedCard?.gameId === card.gameId ? "active" : ""}
              onClick={() => setSelectedGameId(card.gameId)}
            >
              <span>{formatDate(card.startTime || card.gameDate)}</span>
              <strong>{card.awayTeam && card.homeTeam ? `${card.awayTeam} at ${card.homeTeam}` : `Game ${card.gameId}`}</strong>
              <em>{`${card.candidateCount50Plus} at ${formatPercent(highConfidenceThreshold)}+ | ${card.playerCount} props`}</em>
            </button>
          ))}
        </div>
        <div className="parlay-tab-content">
          {selectedCard ? (
            <div className="matchup-props">
              <div className="panel-header compact">
                <div>
                  <h3>Model-only Stocks Board</h3>
                  <p>{`${selectedCard.playerCount} slate players with regular lines ranked by 2+ stocks probability`}</p>
                  <p>{`${selectedCard.candidateCount50Plus} players are at ${formatPercent(highConfidenceThreshold)}+ for 2+ stocks. Board average: ${formatPercent(selectedCard.avgProb2Plus)}`}</p>
                  <p>{`Tier cutoffs: high ${formatPercent(highConfidenceThreshold)} | watch ${formatPercent(watchThreshold)}`}</p>
                  <p>No sportsbook line is attached. These rows are for internal tracking and UI review.</p>
                </div>
              </div>
              <div className="stat-strip">
                <MiniStat label="Settled" value={String(performance?.settled_count ?? 0)} />
                <MiniStat label="Pending" value={String(performance?.pending_count ?? 0)} />
                <MiniStat label="2+ Stocks Hit" value={formatPercent(performance?.hit_rate_2_plus ?? undefined)} />
                <MiniStat label="3+ Stocks Hit" value={formatPercent(performance?.hit_rate_3_plus ?? undefined)} />
                <MiniStat
                  label={`${formatPercent(highConfidenceThreshold)}+ Hit`}
                  value={formatPercent(performance?.recommended_candidate_hit_rate_2_plus ?? undefined)}
                />
                <MiniStat label="Avg 2+ Prob" value={formatPercent(performance?.avg_prob_2_plus ?? undefined)} />
                <MiniStat label="Avg 3+ Prob" value={formatPercent(performance?.avg_prob_3_plus ?? undefined)} />
              </div>
              <div className="table-wrap special-calibration-table-wrap">
                <table className="special-calibration-table">
                  <thead>
                    <tr>
                      <th>2+ Stocks Prob</th>
                      <th>Settled</th>
                      <th>Hits</th>
                      <th>Avg Prob</th>
                      <th>Actual Hit</th>
                    </tr>
                  </thead>
                  <tbody>
                    {(performance?.calibration_buckets ?? []).map((bucket) => (
                      <tr key={bucket.label}>
                        <td>{bucket.label}</td>
                        <td>{bucket.count}</td>
                        <td>{bucket.hits}</td>
                        <td>{formatPercent(bucket.avg_prob ?? undefined)}</td>
                        <td>{formatPercent(bucket.hit_rate ?? undefined)}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
              <div className="table-wrap special-props-table-wrap">
                <table className="props-table special-props-table">
                  <colgroup>
                    <col className="special-col-player" />
                    <col className="special-col-tier" />
                    <col className="special-col-metric" />
                    <col className="special-col-metric" />
                    <col className="special-col-metric" />
                    <col className="special-col-probability" />
                    <col className="special-col-probability" />
                    <col className="special-col-probability" />
                    <col className="special-col-probability" />
                    <col className="special-col-probability" />
                    <col className="special-col-probability" />
                    <col className="special-col-captured" />
                  </colgroup>
                  <thead>
                    <tr>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("player_name")}>Player {sortIndicator("player_name")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("tier")}>Tier {sortIndicator("tier")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("projected_steals")}>STL {sortIndicator("projected_steals")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("projected_blocks")}>BLK {sortIndicator("projected_blocks")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("projected_stocks")}>STL+BLK {sortIndicator("projected_stocks")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("steal_prob_1_plus")}>1+ STL {sortIndicator("steal_prob_1_plus")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("steal_prob_2_plus")}>2+ STL {sortIndicator("steal_prob_2_plus")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("block_prob_1_plus")}>1+ BLK {sortIndicator("block_prob_1_plus")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("block_prob_2_plus")}>2+ BLK {sortIndicator("block_prob_2_plus")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("stocks_prob_2_plus")}>2+ Stocks {sortIndicator("stocks_prob_2_plus")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("stocks_prob_3_plus")}>3+ Stocks {sortIndicator("stocks_prob_3_plus")}</button></th>
                      <th><button type="button" className="table-sort-button" onClick={() => toggleSort("captured_at")}>Captured {sortIndicator("captured_at")}</button></th>
                    </tr>
                  </thead>
                  <tbody>
                    {selectedCard.players.map((snapshot) => (
                      <tr key={snapshot.id}>
                        <td>
                          <div className="player-cell special-player-cell">
                            <TeamLogo
                              src={
                                snapshot.team_logo_url
                                ?? (() => {
                                  const normalized = snapshot.team ? normalizeTeamCode(snapshot.team) : null;
                                  return normalized ? (WNBA_TEAM_LOGOS[normalized] ?? null) : null;
                                })()
                              }
                              alt={`${snapshot.team ?? "Team"} logo`}
                            />
                            <div>
                              <PlayerLabel name={snapshot.player_name} position={snapshot.position} />
                              <span>{`${snapshot.team ?? "—"} | ${snapshot.data_quality === "model_only" ? "Model only" : snapshot.data_quality}`}</span>
                              {renderRecentOutcomesWithMinutes(snapshot, `special-${snapshot.id}`)}
                            </div>
                          </div>
                        </td>
                        <td>
                          {(snapshot.stocks_prob_2_plus ?? 0) >= highConfidenceThreshold
                            ? "High"
                            : (snapshot.stocks_prob_2_plus ?? 0) >= watchThreshold
                              ? "Watch"
                              : "Below"}
                        </td>
                        <td>{formatNumber(snapshot.projected_steals)}</td>
                        <td>{formatNumber(snapshot.projected_blocks)}</td>
                        <td>{formatNumber(snapshot.projected_stocks)}</td>
                        <td>{formatPercent(snapshot.steal_prob_1_plus)}</td>
                        <td>{formatPercent(snapshot.steal_prob_2_plus)}</td>
                        <td>{formatPercent(snapshot.block_prob_1_plus)}</td>
                        <td>{formatPercent(snapshot.block_prob_2_plus)}</td>
                        <td>{formatPercent(snapshot.stocks_prob_2_plus)}</td>
                        <td>{formatPercent(snapshot.stocks_prob_3_plus)}</td>
                        <td>{formatDateTime(snapshot.captured_at)}</td>
                      </tr>
                    ))}
                    {!selectedCard.players.length && (
                      <tr>
                        <td colSpan={12}>No special props generated for this game yet.</td>
                      </tr>
                    )}
                  </tbody>
                </table>
              </div>
            </div>
          ) : (
            <p className="empty">No special props generated yet.</p>
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
  const [sportsbookFilter, setSportsbookFilter] = useState("all");
  const [candidateView, setCandidateView] = useState<"positive" | "all">("all");
  const [candidateSort, setCandidateSort] = useState<CandidateSortField>("expected_value");
  const [discrepancySort, setDiscrepancySort] = useState<DiscrepancySortField>("line_gap");
  const [sortDirection, setSortDirection] = useState<SortDirection>("desc");
  const sportsbookOptions = useMemo(() => sportsbookFilterOptions(props), [props]);

  const filteredProps = props
    .filter((prop) => {
      const marketMatch = marketFilter === "all" || prop.market === marketFilter;
      const sideMatch = sideFilter === "all" || prop.recommended_side === sideFilter;
      const confidenceMatch = confidenceFilter === "all" || prop.confidence === confidenceFilter;
      const sportsbookMatch = sportsbookFilter === "all" || displaySportsbookName(prop) === sportsbookFilter;
      return marketMatch && sideMatch && confidenceMatch && sportsbookMatch;
    })
    .sort((a, b) => compareCandidateProps(a, b, candidateSort, sortDirection));
  const positiveProps = filteredProps.filter((prop) => qualifiesForParlayCandidate(prop));
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
              : matchup.model_props_status === "pending"
                ? "Model projections are still rebuilding for this matchup. Sportsbook rows are shown below in degraded mode."
                : matchup.model_props_status === "sportsbook_only"
                  ? "Sportsbook prop lines are available, but no model-ready props have been built for this matchup yet."
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
            <select
              aria-label="Parlay best sportsbook filter"
              value={sportsbookFilter}
              onChange={(event) => setSportsbookFilter(event.target.value)}
            >
              {sportsbookOptions.map((sportsbook) => (
                <option key={`parlay-book-${sportsbook}`} value={sportsbook}>
                  {sportsbook === "all" ? "All best books" : sportsbook}
                </option>
              ))}
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
              <PlayerLabel name={prop.player} position={prop.position} increasedRole={prop.increased_role} />
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
                <th>Best</th>
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
                        <PlayerLabel name={prop.player} position={prop.position} increasedRole={prop.increased_role} />
                        <span>{prop.team}</span>
                        {renderRecentFormWithMinutes(prop, `${prop.id}-matchup-l5`)}
                      </div>
                    </div>
                  </td>
                  <td>{marketLabel(prop.market)}</td>
                  <td><span className={`side ${prop.recommended_side}`}>{prop.recommended_side}</span></td>
                  <td>{displaySportsbookName(prop)}</td>
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
                  <td colSpan={11}>No modeled parlay candidates match these filters.</td>
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
  ratings,
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
  ratings?: TeamRatings | null;
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
  const venueRatings = context === "home" ? ratings?.home : ratings?.away;
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
        <MiniStat label={`${teamCode} Rest`} value={restLabel(restDays)} />
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
      <div className="stat-strip ratings-strip">
        <MiniStat
          label="Season Net"
          value={formatRatingWithRank(ratings?.season?.net_rating, ratings?.net_rank)}
          className={rankTone(ratings?.net_rank)}
        />
        <MiniStat
          label="Season Off"
          value={formatRatingWithRank(ratings?.season?.off_rating, ratings?.off_rank)}
          className={rankTone(ratings?.off_rank)}
        />
        <MiniStat
          label="Season Def"
          value={formatRatingWithRank(ratings?.season?.def_rating, ratings?.def_rank, true)}
          className={rankTone(ratings?.def_rank, true)}
        />
        <MiniStat
          label="L10 Net"
          value={formatSignedNumber(ratings?.last_10?.net_rating)}
          className={signedValueTone(ratings?.last_10?.net_rating)}
        />
        <MiniStat
          label={context === "home" ? "Home Net" : "Away Net"}
          value={formatSignedNumber(venueRatings?.net_rating)}
          className={signedValueTone(venueRatings?.net_rating)}
        />
        <MiniStat
          label="Pace"
          value={<PaceSignal value={ratings?.season?.pace} rank={ratings?.pace_rank} />}
          className={paceTone(ratings?.pace_rank)}
        />
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

function PlayerLabel({
  name,
  position,
  increasedRole = false
}: {
  name: string;
  position?: string | null;
  increasedRole?: boolean;
}) {
  const safeName = String(name ?? "").trim() || "Unknown";
  const pos = String(position || "").trim().toUpperCase();
  return (
    <strong className="player-label">
      {safeName}
      {pos ? <sup className="player-label-pos">{pos}</sup> : null}
      {increasedRole ? <span className="role-boost-indicator" title="Increased role due to teammate absence">↑</span> : null}
    </strong>
  );
}

function MiniStat({ label, value, className = "" }: { label: string; value: ReactNode; className?: string }) {
  return (
    <div className={`mini-stat ${className}`.trim()}>
      <span>{label}</span>
      <strong>{value}</strong>
    </div>
  );
}

function formatRatingValue(value: number | null | undefined) {
  return typeof value === "number" && Number.isFinite(value) ? value.toFixed(1) : "N/A";
}

function formatSignedNumber(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "N/A";
  }
  return `${value > 0 ? "+" : ""}${value.toFixed(1)}`;
}

function formatRatingWithRank(
  value: number | null | undefined,
  rank: number | null | undefined,
  reverseGood = false,
) {
  const ratingText = formatRatingValue(value);
  if (ratingText === "N/A") {
    return ratingText;
  }
  if (typeof rank !== "number" || !Number.isFinite(rank)) {
    return ratingText;
  }
  return `${ratingText} (#${rank}${reverseGood ? " D" : ""})`;
}

function rankTone(rank: number | null | undefined, reverseGood = false) {
  if (typeof rank !== "number" || !Number.isFinite(rank)) {
    return "";
  }
  const isPositive = reverseGood ? rank >= 10 : rank <= 4;
  const isNegative = reverseGood ? rank <= 4 : rank >= 10;
  if (isPositive) {
    return "mini-stat-positive";
  }
  if (isNegative) {
    return "mini-stat-negative";
  }
  return "mini-stat-neutral";
}

function paceTone(rank: number | null | undefined) {
  if (typeof rank !== "number" || !Number.isFinite(rank)) {
    return "";
  }
  if (rank <= 4) {
    return "mini-stat-positive";
  }
  if (rank >= 10) {
    return "mini-stat-negative";
  }
  return "mini-stat-neutral";
}

function signedValueTone(value: number | null | undefined) {
  if (typeof value !== "number" || !Number.isFinite(value)) {
    return "";
  }
  if (value >= 3) {
    return "mini-stat-positive";
  }
  if (value <= -3) {
    return "mini-stat-negative";
  }
  return "mini-stat-neutral";
}

function PaceSignal({ value, rank }: { value: number | null | undefined; rank: number | null | undefined }) {
  const text = formatRatingValue(value);
  if (text === "N/A") {
    return text;
  }
  if (typeof rank !== "number" || !Number.isFinite(rank)) {
    return text;
  }
  if (rank <= 4) {
    return (
      <span className="pace-signal pace-signal-fast">
        <span className="pace-value">{text}</span>
        <span className="pace-arrow" aria-hidden="true">↑</span>
      </span>
    );
  }
  if (rank >= 10) {
    return (
      <span className="pace-signal pace-signal-slow">
        <span className="pace-value">{text}</span>
        <span className="pace-arrow" aria-hidden="true">↓</span>
      </span>
    );
  }
  return (
    <span className="pace-signal pace-signal-neutral">
      <span className="pace-value">{text}</span>
      <span className="pace-neutral-arrows" aria-hidden="true">
        <span className="pace-arrow-left">←</span>
        <span className="pace-arrow-right">→</span>
      </span>
    </span>
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

function CacheFreshnessBadge({ label, status }: { label: string; status: CacheViewStatus | null }) {
  const badgeClass = !status?.exists ? "missing" : status.is_fresh ? "fresh" : "stale";
  const detail = !status?.exists
    ? "No server cache yet"
    : status.cached_at
      ? `Updated ${formatRelativeAge(status.cached_at)}`
      : "Cache timestamp unavailable";
  return (
    <div className={`cache-badge ${badgeClass}`.trim()} title={status?.cached_at ? formatDateTime(status.cached_at) : detail}>
      <strong>{label}</strong>
      <span>{detail}</span>
    </div>
  );
}

function formatPropSyncStage(value?: string | null) {
  const labels: Record<string, string> = {
    queued: "Queued",
    loading_saved_cache: "Loading saved cache",
    requesting_provider: "Requesting provider data",
    syncing_props: "Syncing props",
    refreshing_covers_context: "Refreshing Covers context",
    rebuilding_predictions: "Rebuilding projections",
    rebuilding_games: "Rebuilding game predictions",
    settling_props: "Settling props",
    settling_games: "Settling games",
    publishing_payloads: "Publishing payloads"
  };
  if (!value) {
    return "Idle";
  }
  return labels[value] ?? value.split("_").join(" ");
}

function formatPipelineStage(value?: string | null) {
  const labels: Record<string, string> = {
    loading_saved_cache: "Loading saved cache",
    requesting_provider: "Requesting provider data",
    queued: "Queued for background sync",
    publishing_payloads: "Refreshing dashboard payloads",
  };
  if (!value) {
    return "Idle";
  }
  return labels[value] ?? formatPropSyncStage(value);
}

function matchActivePipelineSync(
  activePipeline: {
    scope: string;
    startedAt: string;
    waitingForBackground: boolean;
    backendJobId?: number | null;
  } | null,
  sync: PropSyncHealth | null,
) {
  if (!activePipeline?.waitingForBackground || !sync) {
    return null;
  }
  if (activePipeline.backendJobId != null && sync.job_id != null && activePipeline.backendJobId !== sync.job_id) {
    return null;
  }
  if (activePipeline.scope && sync.scope && activePipeline.scope !== sync.scope) {
    return null;
  }
  const pipelineStartedAt = Date.parse(activePipeline.startedAt);
  const syncStartedAt = Date.parse(sync.started_at ?? "");
  if (!Number.isNaN(pipelineStartedAt) && !Number.isNaN(syncStartedAt) && syncStartedAt < pipelineStartedAt) {
    return null;
  }
  return sync;
}

function mergePropSyncProgress(previous: PropSyncHealth | null, next: PropSyncHealth | null): PropSyncHealth | null {
  if (!next) {
    return previous;
  }
  if (!previous) {
    return next;
  }

  const previousJobId = previous.job_id ?? null;
  const nextJobId = next.job_id ?? null;
  const previousUpdatedAt = Date.parse(previous.updated_at ?? "");
  const nextUpdatedAt = Date.parse(next.updated_at ?? "");
  const nextFinishedAt = Date.parse(next.finished_at ?? "");

  if (previousJobId != null && nextJobId != null && previousJobId !== nextJobId) {
    const previousStartedAt = Date.parse(previous.started_at ?? "");
    const nextStartedAt = Date.parse(next.started_at ?? "");
    if (!Number.isNaN(previousStartedAt) && !Number.isNaN(nextStartedAt) && nextStartedAt < previousStartedAt) {
      return previous;
    }
    return next;
  }

  if (
    previous.running
    && !next.running
    && (
      previousJobId === nextJobId
      || (
        !Number.isNaN(nextUpdatedAt)
        && (Number.isNaN(previousUpdatedAt) || nextUpdatedAt >= previousUpdatedAt)
      )
      || (
        !Number.isNaN(nextFinishedAt)
        && (Number.isNaN(previousUpdatedAt) || nextFinishedAt >= previousUpdatedAt)
      )
    )
  ) {
    return next;
  }

  const previousStageIndex = Math.max(0, Number(previous.stage_index ?? 0));
  const nextStageIndex = Math.max(0, Number(next.stage_index ?? 0));
  if (nextStageIndex < previousStageIndex) {
    return previous;
  }

  const previousStage = previous.stage ?? null;
  const nextStage = next.stage ?? null;
  const previousCurrent = Math.max(0, Number(previous.current ?? 0));
  const nextCurrent = Math.max(0, Number(next.current ?? 0));
  if (nextStageIndex === previousStageIndex && previousStage === nextStage && nextCurrent < previousCurrent) {
    return previous;
  }

  if (!Number.isNaN(previousUpdatedAt) && !Number.isNaN(nextUpdatedAt) && nextUpdatedAt < previousUpdatedAt) {
    return previous;
  }

  return next;
}

function formatPropSyncProgressCaption(sync?: {
  running: boolean;
  status?: string | null;
  stage?: string | null;
  stage_index?: number;
  stage_total?: number;
  current?: number;
  total?: number;
  message?: string | null;
}) {
  if (!sync) {
    return "No active background job.";
  }
  if (!sync.running) {
    return sync.message || "No active background job.";
  }
  const stageIndex = Math.max(0, Number(sync.stage_index ?? 0));
  const stageTotal = Math.max(1, Number(sync.stage_total ?? 1));
  const current = Math.max(0, Number(sync.current ?? 0));
  const total = Math.max(0, Number(sync.total ?? 0));
  const stageSummary = stageIndex > 0 ? `Stage ${stageIndex} of ${stageTotal}` : "Starting background job";
  if (total > 0) {
    return `${stageSummary}. ${current} of ${total} in this stage.`;
  }
  return sync.message || `${stageSummary}.`;
}

function formatRelativeAge(value?: string | null) {
  if (!value) {
    return "unknown";
  }
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) {
    return "unknown";
  }
  const diffSeconds = Math.max(0, Math.round((Date.now() - time) / 1000));
  if (diffSeconds < 60) {
    return `${diffSeconds}s ago`;
  }
  if (diffSeconds < 3600) {
    return `${Math.round(diffSeconds / 60)}m ago`;
  }
  return `${Math.round(diffSeconds / 3600)}h ago`;
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
  const values = Object.entries(run.metrics)
    .filter(([market]) => market !== "overall" && !market.startsWith("game_"))
    .map(([, metric]) => metric.mae)
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

function formatTrainingKept(included?: number | null, candidate?: number | null) {
  if (included == null && candidate == null) {
    return "N/A";
  }
  const includedLabel = included == null ? "N/A" : formatCount(included);
  const candidateLabel = candidate == null ? "N/A" : formatCount(candidate);
  return `${includedLabel}/${candidateLabel}`;
}

function formatAverageDirection(run: ModelRun) {
  const values = Object.entries(run.metrics)
    .filter(([market]) => market !== "overall" && !market.startsWith("game_"))
    .map(([, metric]) => metric.directional_accuracy)
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
    const leftMetric = metrics[left];
    const rightMetric = metrics[right];
    const leftSettled = leftMetric?.settled_rows ?? 0;
    const rightSettled = rightMetric?.settled_rows ?? 0;
    if (leftSettled !== rightSettled) {
      return rightSettled - leftSettled;
    }
    const leftRows = leftMetric?.rows ?? 0;
    const rightRows = rightMetric?.rows ?? 0;
    if (leftRows !== rightRows) {
      return rightRows - leftRows;
    }
    const leftAccuracy = leftMetric?.side_accuracy ?? -1;
    const rightAccuracy = rightMetric?.side_accuracy ?? -1;
    if (leftAccuracy !== rightAccuracy) {
      return rightAccuracy - leftAccuracy;
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
    special: "Special Props",
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

function qualifiesForGem(prop: ValueProp, preset: GemPreset) {
  const edge = Math.abs(prop.edge);
  const ev = prop.expected_value;
  const confidence = prop.confidence;
  const market = prop.market;
  const side = prop.recommended_side;
  if (ev <= 0 || edge <= 0) {
    return false;
  }

  const isCombo = ["points_rebounds", "points_assists", "rebounds_assists", "points_rebounds_assists"].includes(market);
  const isFragileOver = side === "over" && ["points", "threes", "points_rebounds", "points_assists"].includes(market);
  const presetFloor = preset === "conservative"
    ? { edge: 0.08, ev: 0.03 }
    : preset === "balanced"
      ? { edge: 0.06, ev: 0.02 }
      : { edge: 0.05, ev: 0.015 };

  if (confidence === "high") {
    if (isFragileOver) {
      return edge >= Math.max(presetFloor.edge, 0.09) && ev >= Math.max(presetFloor.ev, 0.025);
    }
    if (isCombo) {
      return edge >= Math.max(presetFloor.edge, 0.08) && ev >= Math.max(presetFloor.ev, 0.025);
    }
    return edge >= presetFloor.edge && ev >= presetFloor.ev;
  }

  if (confidence === "medium") {
    if (isFragileOver) {
      return edge >= Math.max(presetFloor.edge, 0.11) && ev >= Math.max(presetFloor.ev, 0.035);
    }
    if (isCombo || market === "assists") {
      return edge >= Math.max(presetFloor.edge, 0.08) && ev >= Math.max(presetFloor.ev, 0.025);
    }
    return edge >= Math.max(presetFloor.edge, 0.07) && ev >= Math.max(presetFloor.ev, 0.02);
  }

  if (side === "over") {
    return false;
  }
  if (!["rebounds", "points_rebounds", "rebounds_assists"].includes(market)) {
    return false;
  }
  return edge >= Math.max(presetFloor.edge, 0.08) && ev >= Math.max(presetFloor.ev, 0.025);
}

function qualifiesForParlayCandidate(prop: ValueProp) {
  const edge = Math.abs(prop.edge);
  const ev = prop.expected_value;
  const market = prop.market;
  const side = prop.recommended_side;
  if (ev <= 0 || edge <= 0) {
    return false;
  }
  if (prop.confidence === "high") {
    if (side === "over" && ["points", "threes"].includes(market)) {
      return edge >= 0.10 && ev >= 0.03;
    }
    return edge >= 0.07 && ev >= 0.02;
  }
  if (prop.confidence === "medium") {
    if (side === "over" && ["points", "threes", "points_rebounds", "points_assists"].includes(market)) {
      return edge >= 0.12 && ev >= 0.04;
    }
    if (["points_rebounds_assists", "rebounds_assists", "assists"].includes(market)) {
      return edge >= 0.09 && ev >= 0.03;
    }
    return edge >= 0.08 && ev >= 0.025;
  }
  if (side === "over") {
    return false;
  }
  if (!["rebounds", "points_rebounds", "rebounds_assists"].includes(market)) {
    return false;
  }
  return edge >= 0.10 && ev >= 0.03;
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
    game_ats: "Game ATS",
    game_total: "Game Total",
    game_overall: "Game Overall",
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

const APP_TIME_ZONE = "America/New_York";
const APP_TIME_ZONE_LABEL = "ET";

function formatDate(value: string) {
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) {
    return "Scheduled";
  }
  return new Intl.DateTimeFormat("en-US", {
    timeZone: APP_TIME_ZONE,
    month: "short",
    day: "numeric",
    hour: "numeric",
    minute: "2-digit"
  }).format(new Date(time)) + ` ${APP_TIME_ZONE_LABEL}`;
}

function formatDateTime(value?: string | null) {
  if (!value) {
    return "N/A";
  }
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) {
    return "N/A";
  }
  return formatDate(value);
}

function formatOutSince(value?: string | null) {
  if (!value) {
    return "—";
  }
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) {
    return "—";
  }
  const elapsedMs = Date.now() - time;
  if (elapsedMs < 0) {
    return "today";
  }
  const elapsedDays = Math.floor(elapsedMs / (1000 * 60 * 60 * 24));
  if (elapsedDays <= 0) {
    return "today";
  }
  return `${elapsedDays}d`;
}

function localIsoDate(value = new Date()) {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: APP_TIME_ZONE,
    year: "numeric",
    month: "2-digit",
    day: "2-digit"
  }).formatToParts(value);
  const year = parts.find((part) => part.type === "year")?.value ?? "0000";
  const month = parts.find((part) => part.type === "month")?.value ?? "01";
  const day = parts.find((part) => part.type === "day")?.value ?? "01";
  return `${year}-${month}-${day}`;
}

function formatGameDateShort(value: string) {
  const time = new Date(value).getTime();
  if (Number.isNaN(time)) {
    return value;
  }
  return new Intl.DateTimeFormat("en-US", {
    timeZone: APP_TIME_ZONE,
    month: "short",
    day: "numeric",
    year: "2-digit"
  }).format(new Date(time));
}

function todayInputValue() {
  return localIsoDate();
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
  const parlayCount = (matchup.props ?? []).filter((prop) => qualifiesForParlayCandidate(prop)).length;
  const discrepancyCount = matchup.line_discrepancies?.length ?? 0;
  const sportsbookCount = matchup.sportsbook_props?.length ?? 0;
  if ((matchup.model_props_status ?? "empty") === "pending") {
    return `model rebuild pending | ${discrepancyCount} gaps | ${sportsbookCount} book`;
  }
  if ((matchup.model_props_status ?? "empty") === "sportsbook_only") {
    return `sportsbook only | ${discrepancyCount} gaps | ${sportsbookCount} book`;
  }
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

function displaySportsbookName(item: { sportsbook: string; display_sportsbook?: string | null }) {
  return item.display_sportsbook?.trim() || item.sportsbook?.trim() || "Unknown";
}

function sportsbookFilterOptions(items: Array<{ sportsbook: string; display_sportsbook?: string | null }>) {
  const options = Array.from(
    new Set(
      items
        .map((item) => displaySportsbookName(item))
        .filter((sportsbook): sportsbook is string => Boolean(sportsbook))
    )
  ).sort((a, b) => a.localeCompare(b));
  return ["all", ...options];
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

function propsForMatchup(matchup: Matchup, props: ValueProp[]) {
  const matchupProps = Array.isArray(matchup.props) ? matchup.props : [];
  const fallbackProps = props.filter(
    (prop) =>
      prop.start_time === matchup.start_time &&
      (normalizeTeamCode(prop.team) === normalizeTeamCode(matchup.home_team) ||
        normalizeTeamCode(prop.team) === normalizeTeamCode(matchup.away_team))
  );
  if (!fallbackProps.length) {
    return matchupProps;
  }
  const merged = new Map<number, ValueProp>();
  for (const item of [...matchupProps, ...fallbackProps]) {
    merged.set(item.id, item);
  }
  return Array.from(merged.values()).sort((left, right) => {
    const evDiff = right.expected_value - left.expected_value;
    if (evDiff !== 0) {
      return evDiff;
    }
    return right.edge - left.edge;
  });
}

function parlayAvailabilityLabel(matchup: Matchup, props: ValueProp[]) {
  const parlayCount = props.filter((prop) => qualifiesForParlayCandidate(prop)).length;
  if (parlayCount > 0) {
    return `${parlayCount} candidate${parlayCount === 1 ? "" : "s"}`;
  }
  if (props.length > 0) {
    return `${props.length} model prop${props.length === 1 ? "" : "s"}`;
  }
  if ((matchup.model_props_status ?? "empty") === "pending") {
    return "model rebuild pending";
  }
  if ((matchup.model_props_status ?? "empty") === "sportsbook_only") {
    return "sportsbook only";
  }
  return availableLabel(matchup);
}

function parlayCandidateCount(matchups: Matchup[], props: ValueProp[]) {
  return matchups.reduce(
    (count, matchup) => count + propsForMatchup(matchup, props).filter((prop) => qualifiesForParlayCandidate(prop)).length,
    0
  );
}

function gamesWithParlayCandidates(matchups: Matchup[], props: ValueProp[]) {
  return matchups.filter((matchup) => propsForMatchup(matchup, props).some((prop) => qualifiesForParlayCandidate(prop))).length;
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
  const awayLine = market?.away_line ?? (matchup.spread_home != null ? -matchup.spread_home : null);
  const homeLine = market?.home_line ?? matchup.spread_home;
  const awayPrice = market?.away_price ?? matchup.away_spread_price ?? null;
  const homePrice = market?.home_price ?? matchup.home_spread_price ?? null;
  if (awayLine == null || homeLine == null) {
    return `${matchup.home_team} ${formatSpread(matchup.spread_home)}`;
  }
  if (awayPrice == null || homePrice == null) {
    return `${matchup.away_team} ${formatSpread(awayLine)} / ${matchup.home_team} ${formatSpread(homeLine)}`;
  }
  return `${matchup.away_team} ${formatSpread(awayLine)} (${formatMoneyline(awayPrice)}) / ${matchup.home_team} ${formatSpread(homeLine)} (${formatMoneyline(homePrice)})`;
}

function formatTotalMarket(matchup: Matchup) {
  const market = matchup.total_market;
  const overLine = market?.over_line ?? matchup.game_total;
  const underLine = market?.under_line ?? matchup.game_total;
  const overPrice = market?.over_price ?? matchup.over_price ?? null;
  const underPrice = market?.under_price ?? matchup.under_price ?? null;
  if (overLine == null || underLine == null) {
    return `${formatGameTotal(matchup.game_total)}`;
  }
  if (overPrice == null || underPrice == null) {
    return `O${formatGameTotal(overLine)} / U${formatGameTotal(underLine)}`;
  }
  return `O${formatGameTotal(overLine)} (${formatMoneyline(overPrice)}) / U${formatGameTotal(underLine)} (${formatMoneyline(underPrice)})`;
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
