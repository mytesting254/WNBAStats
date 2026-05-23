import { BrainCircuit, CalendarDays, Database, ListChecks, RefreshCw, ShieldCheck, SlidersHorizontal, TrendingUp } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import {
  fetchLineDiscrepancies,
  fetchMatchups,
  fetchModelRuns,
  fetchPerformance,
  fetchRoster,
  fetchValueBoard,
  importCoversOdds,
  importEspnHistory,
  importOdds,
  importRotowireInjuries,
  recalculate,
  trainModel,
  type CoversRecordRow,
  type LineDiscrepancy,
  type Matchup,
  type ModelPerformance,
  type ModelRun,
  type RosterPlayer,
  type TeamLast10,
  type ValueProp
} from "./api";

const markets = [
  { id: "all", label: "All" },
  { id: "points", label: "PTS" },
  { id: "rebounds", label: "REB" },
  { id: "assists", label: "AST" },
  { id: "points_rebounds_assists", label: "PRA" },
  { id: "threes", label: "3PM" },
  { id: "steals", label: "STL" },
  { id: "blocks", label: "BLK" },
  { id: "blocks_steals", label: "STL+BLK" }
];

type DashboardTab = "props" | "matchups" | "parlays" | "discrepancies" | "roster" | "models" | "data";
type CandidateSortField = "expected_value" | "edge" | "projection" | "line" | "projection_gap" | "confidence" | "player";
type DiscrepancySortField = "line_gap" | "price_gap" | "books" | "player_name";
type SortDirection = "desc" | "asc";

export function App() {
  const [props, setProps] = useState<ValueProp[]>([]);
  const [matchups, setMatchups] = useState<Matchup[]>([]);
  const [discrepancies, setDiscrepancies] = useState<LineDiscrepancy[]>([]);
  const [roster, setRoster] = useState<RosterPlayer[]>([]);
  const [performance, setPerformance] = useState<ModelPerformance | null>(null);
  const [modelRuns, setModelRuns] = useState<ModelRun[]>([]);
  const [latestModelRun, setLatestModelRun] = useState<ModelRun | null>(null);
  const [training, setTraining] = useState(false);
  const [importingOdds, setImportingOdds] = useState(false);
  const [importingCoversOdds, setImportingCoversOdds] = useState(false);
  const [refreshingResults, setRefreshingResults] = useState(false);
  const [refreshingRoster, setRefreshingRoster] = useState(false);
  const [recalculating, setRecalculating] = useState(false);
  const [activeTab, setActiveTab] = useState<DashboardTab>("matchups");
  const [market, setMarket] = useState("all");
  const [confidence, setConfidence] = useState("all");
  const [selected, setSelected] = useState<ValueProp | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [operationStatus, setOperationStatus] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);

  async function load() {
    setLoading(true);
    setError(null);
    const [
      boardResult,
      performanceResult,
      matchupsResult,
      discrepanciesResult,
      modelRunsResult,
      rosterResult
    ] = await Promise.allSettled([
      fetchValueBoard(),
      fetchPerformance(),
      fetchMatchups(),
      fetchLineDiscrepancies(),
      fetchModelRuns(),
      fetchRoster()
    ]);

    const failures: string[] = [];

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

    setLoading(false);
  }

  useEffect(() => {
    load();
  }, []);

  const filtered = useMemo(() => {
    return props.filter((prop) => {
      const marketMatch = market === "all" || prop.market === market;
      const confidenceMatch = confidence === "all" || prop.confidence === confidence;
      return marketMatch && confidenceMatch;
    });
  }, [props, market, confidence]);

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
      await load();
      setOperationStatus(result.message ?? `${forceRefresh ? "Fresh" : "Saved"} Covers odds loaded. Imported ${result.imported ?? 0} sportsbook rows from ${result.source ?? "covers"}.`);
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
      setOperationStatus(
        `${missingOnly ? "Missing" : forceRefresh ? "Fresh" : "Saved"} ESPN completed games and box scores loaded${scope}. Synced ${result.synced_props ?? 0} model prop lines, rebuilt ${result.predictions ?? 0} predictions, and settled ${result.settlements?.settled ?? 0} props.`
      );
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
      await load();
      setOperationStatus(
        `${forceRefresh ? "Fresh" : "Cached"} Rotowire lineup pull complete. Parsed ${result.parsed_rows ?? 0} rows${result.captured_at ? ` (${result.captured_at})` : ""}.`
      );
    } catch (err) {
      setError(err instanceof Error ? err.message : "Unable to refresh Rotowire lineup status");
    } finally {
      setRefreshingRoster(false);
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
          label={activeTab === "props" ? "Props ranked" : activeTab === "matchups" ? "Games" : activeTab === "parlays" ? "Candidate legs" : activeTab === "discrepancies" ? "Line gaps" : activeTab === "roster" ? "Rostered players" : activeTab === "models" ? "Training rows" : "Model props"}
          value={activeTab === "props" ? filtered.length.toString() : activeTab === "matchups" ? matchups.length.toString() : activeTab === "parlays" ? parlayCandidateCount(matchups).toString() : activeTab === "discrepancies" ? discrepancies.length.toString() : activeTab === "roster" ? roster.length.toString() : activeTab === "models" ? (latestModelRun?.training_rows ?? 0).toString() : props.length.toString()}
        />
        <Metric
          label={activeTab === "props" ? "Best EV" : activeTab === "matchups" ? "Teams tracked" : activeTab === "parlays" ? "Games with legs" : activeTab === "discrepancies" ? "Books compared" : activeTab === "roster" ? "Unavailable players" : activeTab === "models" ? "Latest MAE" : "Upcoming games"}
          value={activeTab === "props" ? formatPercent(filtered[0]?.expected_value) : activeTab === "matchups" ? (matchups.length * 2).toString() : activeTab === "parlays" ? gamesWithParlayCandidates(matchups).toString() : activeTab === "discrepancies" ? countDiscrepancyBooks(discrepancies).toString() : activeTab === "roster" ? roster.length.toString() : activeTab === "models" ? formatLatestMae(latestModelRun) : matchups.length.toString()}
        />
        <Metric label="Settled props" value={performance?.settled.toString() ?? "0"} />
        <Metric label="Win rate" value={performance?.win_rate == null ? "Pending" : formatPercent(performance.win_rate)} />
      </section>
      {performance?.message ? <div className="summary-message">{performance.message}</div> : null}

      {activeTab === "props" ? (
        <PropsView
          filtered={filtered}
          loading={loading}
          error={error}
          market={market}
          confidence={confidence}
          selected={selected}
          setMarket={setMarket}
          setConfidence={setConfidence}
          setSelected={setSelected}
        />
      ) : activeTab === "matchups" ? (
        <MatchupsView matchups={matchups} loading={loading} error={error} />
      ) : activeTab === "parlays" ? (
        <ParlayCandidatesView matchups={matchups} loading={loading} error={error} />
      ) : activeTab === "discrepancies" ? (
        <DiscrepanciesView discrepancies={discrepancies} loading={loading} error={error} />
      ) : activeTab === "data" ? (
        <DataView
          loading={loading}
          error={error}
          status={operationStatus}
          importingOdds={importingOdds}
          importingCoversOdds={importingCoversOdds}
          refreshingResults={refreshingResults}
          recalculating={recalculating}
          propsCount={props.length}
          matchupsCount={matchups.length}
          discrepanciesCount={discrepancies.length}
          onImportOdds={handleImportOdds}
          onImportCoversOdds={handleImportCoversOdds}
          onRefreshResults={handleRefreshResults}
          onRecalculate={handleRecalculate}
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
        />
      ) : (
        <ModelsView runs={modelRuns} latest={latestModelRun} loading={loading || training} error={error} onTrain={handleTrainModel} />
      )}
    </main>
  );
}

function DataView({
  loading,
  error,
  status,
  importingOdds,
  importingCoversOdds,
  refreshingResults,
  recalculating,
  propsCount,
  matchupsCount,
  discrepanciesCount,
  onImportOdds,
  onImportCoversOdds,
  onRefreshResults,
  onRecalculate,
  onReload
}: {
  loading: boolean;
  error: string | null;
  status: string | null;
  importingOdds: boolean;
  importingCoversOdds: boolean;
  refreshingResults: boolean;
  recalculating: boolean;
  propsCount: number;
  matchupsCount: number;
  discrepanciesCount: number;
  onImportOdds: (forceRefresh: boolean) => void;
  onImportCoversOdds: (forceRefresh: boolean) => void;
  onRefreshResults: (
    forceRefresh: boolean,
    includePlayerStats?: boolean,
    missingOnly?: boolean,
    includePreviousSeason?: boolean,
    selectedDates?: string[]
  ) => void;
  onRecalculate: () => void;
  onReload: () => void;
}) {
  const [resultDate, setResultDate] = useState(todayInputValue());
  const [batchStartDate, setBatchStartDate] = useState(todayInputValue());
  const [batchEndDate, setBatchEndDate] = useState(todayInputValue());
  const parsedBatchDates = dateRangeValues(batchStartDate, batchEndDate);
  const busy = refreshingResults || importingOdds || importingCoversOdds || loading;

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
        <div className="data-layout">
          <OperationCard
            title="The Odds API"
            description="Update prop prices, lines, line discrepancies, and current matchup markets from The Odds API."
            metrics={`${propsCount} model props | ${discrepanciesCount} line gaps`}
            primaryLabel={importingOdds ? "Loading" : "Load Saved Odds"}
            secondaryLabel="Refresh Odds"
            disabled={importingOdds || importingCoversOdds || refreshingResults || loading}
            onPrimary={() => onImportOdds(false)}
            onSecondary={() => onImportOdds(true)}
          />
          <OperationCard
            title="Covers Odds"
            description="Import today's Covers matchup prop tables without using The Odds API credits."
            metrics={`${propsCount} model props | ${discrepanciesCount} line gaps`}
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
          <OperationCard
            title="Projection Board"
            description="Rebuild model projections from the current prop lines and player history."
            metrics={`${propsCount} current predictions`}
            primaryLabel={recalculating ? "Recalculating" : "Recalculate"}
            secondaryLabel="Reload Views"
            disabled={refreshingResults || importingOdds || importingCoversOdds || loading || recalculating}
            onPrimary={onRecalculate}
            onSecondary={onReload}
          />
        </div>
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
  onRefresh
}: {
  roster: RosterPlayer[];
  loading: boolean;
  error: string | null;
  status: string | null;
  refreshing: boolean;
  onRefresh: () => void;
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
        <div className="operation-actions">
          <button className="icon-button text-button dark-button" onClick={onRefresh} disabled={loading || refreshing}>
            <RefreshCw size={18} />
            {refreshing ? "Refreshing" : "Refresh Roster"}
          </button>
        </div>
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
  const metrics = latest ? Object.entries(latest.metrics) : [];
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
              <Metric label="Rows" value={(latest?.training_rows ?? 0).toString()} />
              <Metric label="Type" value={latest?.run_type ?? "N/A"} />
              <Metric label="Finished" value={latest?.finished_at ? formatDate(latest.finished_at) : "N/A"} />
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
                  </tr>
                </thead>
                <tbody>
                  {metrics.map(([market, metric]) => (
                    <tr key={market}>
                      <td>{marketLabel(market)}</td>
                      <td>{metric.rows}</td>
                      <td>{formatNumber(metric.mae)}</td>
                      <td>{formatNumber(metric.rmse)}</td>
                      <td>{formatSigned(metric.bias)}</td>
                      <td>{formatPercent(metric.directional_accuracy ?? undefined)}</td>
                    </tr>
                  ))}
                  {!metrics.length && (
                    <tr>
                      <td colSpan={6}>No model metrics yet.</td>
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
                    </tr>
                  ))}
                  {!comparisonRuns.length && (
                    <tr>
                      <td colSpan={9}>No model runs saved yet.</td>
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
  selected,
  setMarket,
  setConfidence,
  setSelected
}: {
  filtered: ValueProp[];
  loading: boolean;
  error: string | null;
  market: string;
  confidence: string;
  selected: ValueProp | null;
  setMarket: (market: string) => void;
  setConfidence: (confidence: string) => void;
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
        <div className="game-tabs" aria-label="Game tabs">
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
        <div className="matchup-grid single">
          {selectedMatchup ? (
            <article className="matchup-card" key={selectedMatchup.id}>
              <div className="matchup-card-header">
                <div>
                  <p className="eyebrow">{formatDate(selectedMatchup.start_time)}</p>
                  <div className="matchup-title-row">
                    <TeamLogo src={selectedMatchup.away_logo_url} alt={`${selectedMatchup.away_team} logo`} />
                    <h3>{selectedMatchup.away_team} at {selectedMatchup.home_team}</h3>
                    <TeamLogo src={selectedMatchup.home_logo_url} alt={`${selectedMatchup.home_team} logo`} />
                  </div>
                </div>
                <div className="game-badges">
                  <span className="game-pill">Pregame</span>
                  <span className={`risk-pill ${riskClass(selectedMatchup.blowout_risk)}`}>
                    Blowout {selectedMatchup.blowout_risk}
                  </span>
                  <span className="rest-pill">Line {selectedMatchup.home_team} {formatSpread(selectedMatchup.spread_home)}</span>
                  <span className="rest-pill">Total {selectedMatchup.game_total?.toFixed(1) ?? "N/A"}</span>
                  <span className="rest-pill">{selectedMatchup.away_team} {restLabel(selectedMatchup.away_rest_days)}</span>
                  <span className="rest-pill">{selectedMatchup.home_team} {restLabel(selectedMatchup.home_rest_days)}</span>
                </div>
              </div>
              <div className="team-comparison">
                <TeamSummary label="Away" team={selectedMatchup.away_team_name} logoUrl={selectedMatchup.away_logo_url} restDays={selectedMatchup.away_rest_days} summary={selectedMatchup.away} />
                <TeamSummary label="Home" team={selectedMatchup.home_team_name} logoUrl={selectedMatchup.home_logo_url} restDays={selectedMatchup.home_rest_days} summary={selectedMatchup.home} />
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
              <p className="reason matchup-reason">{selectedMatchup.game_reason}</p>
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
  if (!records || (!records.head_to_head.length && !records.away_last_10.length && !records.home_last_10.length)) {
    return null;
  }

  return (
    <div className="covers-records-panel">
      <CoversRecordList title="H2H Last 10" rows={records.head_to_head.slice(0, 5)} mode="h2h" />
      <CoversRecordList title={`${matchup.away_team} Last 10`} rows={records.away_last_10.slice(0, 5)} mode="team" />
      <CoversRecordList title={`${matchup.home_team} Last 10`} rows={records.home_last_10.slice(0, 5)} mode="team" />
    </div>
  );
}

function CoversRecordList({
  title,
  rows,
  mode
}: {
  title: string;
  rows: CoversRecordRow[];
  mode: "h2h" | "team";
}) {
  if (!rows.length) {
    return null;
  }

  return (
    <div className="covers-records-list">
      <h4>{title}</h4>
      {rows.map((row) => (
        <div key={`${title}-${row.date}-${row.score}-${row.opponent ?? row.home ?? ""}`}>
          <span>{row.date}</span>
          <strong>{coversRecordOpponent(row, mode)}</strong>
          <b>{row.result ? `${row.result} ` : ""}{row.score}</b>
          <em>{row.ats} | {row.total}</em>
        </div>
      ))}
    </div>
  );
}

function coversRecordOpponent(row: CoversRecordRow, mode: "h2h" | "team") {
  if (mode === "h2h") {
    return `Home ${row.home ?? "N/A"}`;
  }
  const prefix = row.location === "away" ? "at" : "vs";
  return `${prefix} ${row.opponent ?? "N/A"}`;
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
              <em>{prop.recommended_side.toUpperCase()} {prop.line.toFixed(1)} | EV {formatPercent(prop.expected_value)}</em>
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
                      </div>
                    </div>
                  </td>
                  <td>{marketLabel(prop.market)}</td>
                  <td><span className={`side ${prop.recommended_side}`}>{prop.recommended_side}</span></td>
                  <td>{prop.line.toFixed(1)}</td>
                  <td>{prop.projection.toFixed(1)}</td>
                  <td>{formatSigned(prop.projection - prop.line)}</td>
                  <td>{formatPercent(prop.edge)}</td>
                  <td>{formatPercent(prop.expected_value)}</td>
                  <td>{prop.confidence}</td>
                </tr>
              ))}
              {!visibleProps.length && (
                <tr>
                  <td colSpan={9}>No modeled parlay candidates match these filters.</td>
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
  logoUrl,
  restDays,
  summary
}: {
  label: string;
  team: string;
  logoUrl?: string | null;
  restDays: number | null;
  summary: TeamLast10;
}) {
  return (
    <div className="team-summary">
      <div className="team-title">
        <TeamLogo src={logoUrl} alt={`${team} logo`} />
        <div>
          <span>{label}</span>
          <strong>{team}</strong>
        </div>
      </div>
      <div className="stat-strip">
        <MiniStat label="W-L" value={`${summary.wins}-${summary.losses}`} />
        <MiniStat label="Rest" value={restLabel(restDays)} />
        <MiniStat label="Home/Away" value={`${summary.home_games}/${summary.away_games}`} />
        <MiniStat label="ATS" value={`${summary.ats_wins}-${summary.ats_losses}-${summary.ats_pushes}`} />
        <MiniStat label="O/U" value={`${summary.overs}-${summary.unders}-${summary.total_pushes}`} />
      </div>
      <div className="points-row">
        <span>PF {summary.avg_points_for.toFixed(1)}</span>
        <span>PA {summary.avg_points_against.toFixed(1)}</span>
      </div>
      <div className="recent-list">
        {summary.recent_games.slice(0, 5).map((game) => (
          <div key={`${team}-${game.game_date}-${game.opponent}`}>
            <span>{game.is_home ? "vs" : "at"} {game.opponent}</span>
            <strong>{game.points}-{game.opponent_points}</strong>
            <em>{atsLabel(game.ats_result)} | {totalLabel(game.total_result)}</em>
          </div>
        ))}
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

function Metric({ label, value }: { label: string; value: string }) {
  return (
    <div className="metric">
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

function tabTitle(tab: DashboardTab) {
  const titles = {
    props: "Prop Value Board",
    matchups: "Pregame Matchups",
    parlays: "Parlay Candidates",
    discrepancies: "Line Discrepancies",
    roster: "Roster Status",
    models: "Model Lab",
    data: "Data Operations"
  };
  return titles[tab];
}

function marketLabel(market: string) {
  const labels: Record<string, string> = {
    points: "PTS",
    rebounds: "REB",
    assists: "AST",
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
