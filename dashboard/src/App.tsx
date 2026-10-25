import { useCallback, useEffect, useRef, useState } from "react";
import {
  api,
  getToken,
  setToken,
  clearToken,
  subscribeEvents,
  type Telemetry,
  type Graph,
  type JobStats,
  type Usage,
  type Activity,
  type JobEvent,
  type Truncation,
  ApiError,
} from "./api";
import NeuralGraph from "./components/NeuralGraph";
import Sidebar from "./components/Sidebar";
import { TaskQueue, ModelRouter, EventStream, KnowledgePanel } from "./components/Panels";
import PromptBar from "./components/PromptBar";

/**
 * The dashboard shell.
 *
 * Data flow is deliberately simple and honest:
 * - the **graph** is refetched on a short interval and shortly after any job
 *   event arrives, so what is drawn tracks what is true;
 * - **telemetry** is polled separately (it is a host measurement, not DB state)
 *   and degrades to a visible "unavailable" state rather than freezing;
 * - **events** arrive over the same SSE stream the CLI uses.
 *
 * Every count rendered below comes from the snapshot the core just returned.
 * When the core omits a key, the UI prints "n/a" — never `0`, which would be a
 * measurement the backend did not make.
 *
 * Focus mode: clicking the graph background (or pressing the expand control)
 * hides the side panels; Escape restores them. The graph component is *not*
 * remounted, so the force layout survives the transition.
 */

const POLL_GRAPH_MS = 2500;
const POLL_TELEMETRY_MS = 2000;
/** A burst of events should cause one refresh, not one per event. */
const EVENT_REFRESH_DEBOUNCE_MS = 400;

export default function App() {
  const [token, setTokenState] = useState(getToken());
  const [authError, setAuthError] = useState<string | null>(null);

  const [telemetry, setTelemetry] = useState<Telemetry | null>(null);
  const [telemetryError, setTelemetryError] = useState<string | null>(null);
  const [graph, setGraph] = useState<Graph | null>(null);
  const [graphError, setGraphError] = useState<string | null>(null);
  const [stats, setStats] = useState<JobStats | null>(null);
  const [usage, setUsage] = useState<Usage | null>(null);
  const [activity, setActivity] = useState<Activity | null>(null);
  /** Aggregate (stats/usage/activity) failure — distinct from "nothing there". */
  const [dataError, setDataError] = useState<string | null>(null);
  const [events, setEvents] = useState<JobEvent[]>([]);
  const [connected, setConnected] = useState(false);
  const [focused, setFocused] = useState(false);
  const [tick, setTick] = useState(0);

  const bump = useCallback(() => setTick((t) => t + 1), []);
  const eventIdRef = useRef(0);
  const graphSeqRef = useRef(0);
  const refreshTimerRef = useRef<number | null>(null);

  // --- data loading ----------------------------------------------------------
  const loadGraph = useCallback(async () => {
    // Polls, event bursts and manual reloads can overlap. Tag each request and
    // ignore stale responses so an older snapshot can never overwrite a newer.
    const seq = ++graphSeqRef.current;
    try {
      const next = await api.graph();
      if (seq !== graphSeqRef.current) return;
      setGraph(next);
      setGraphError(null);
    } catch (error) {
      if (seq !== graphSeqRef.current) return;
      setGraphError(describeError(error));
    }
  }, []);

  const loadAll = useCallback(async () => {
    await loadGraph();
    try {
      const [s, u, a] = await Promise.all([api.stats(), api.usage(), api.activity()]);
      setStats(s);
      setUsage(u);
      setActivity(a);
      setDataError(null);
      setAuthError(null);
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        setAuthError("Token rejected by the core. Update it in Settings.");
      }
      // Keep the previous values visible, but tell the truth about why they
      // may be stale instead of silently presenting them as current.
      setDataError(describeError(error));
    }
  }, [loadGraph]);

  /** Coalesce the "something happened, refresh" signal from the event stream. */
  const scheduleGraphRefresh = useCallback(() => {
    if (refreshTimerRef.current !== null) return;
    refreshTimerRef.current = window.setTimeout(() => {
      refreshTimerRef.current = null;
      void loadGraph();
    }, EVENT_REFRESH_DEBOUNCE_MS);
  }, [loadGraph]);

  useEffect(() => {
    if (!token) return;
    void loadAll();
  }, [token, loadAll, tick]);

  // Telemetry polls on its own clock: it measures the host, not the database.
  useEffect(() => {
    if (!token) return;
    let cancelled = false;
    const load = async () => {
      // A hidden tab does not need to measure the host 30 times a minute.
      if (document.visibilityState === "hidden") return;
      try {
        const next = await api.telemetry();
        if (!cancelled) {
          setTelemetry(next);
          setTelemetryError(null);
        }
      } catch (error) {
        if (!cancelled) setTelemetryError(describeError(error));
      }
    };
    void load();
    const id = window.setInterval(() => void load(), POLL_TELEMETRY_MS);
    return () => {
      cancelled = true;
      window.clearInterval(id);
    };
  }, [token, tick]);

  // Graph refresh on a timer.
  useEffect(() => {
    if (!token) return;
    const id = window.setInterval(() => {
      if (document.visibilityState === "hidden") return;
      void loadGraph();
    }, POLL_GRAPH_MS);
    return () => window.clearInterval(id);
  }, [token, loadGraph]);

  // Coming back to the tab: catch up immediately rather than waiting a poll.
  useEffect(() => {
    if (!token) return;
    const onVisible = () => {
      if (document.visibilityState === "visible") void loadAll();
    };
    document.addEventListener("visibilitychange", onVisible);
    return () => document.removeEventListener("visibilitychange", onVisible);
  }, [token, loadAll]);

  useEffect(
    () => () => {
      if (refreshTimerRef.current !== null) window.clearTimeout(refreshTimerRef.current);
    },
    [],
  );

  // --- live events ------------------------------------------------------------
  useEffect(() => {
    if (!token) return;
    const unsubscribe = subscribeEvents(
      (event) => {
        eventIdRef.current = Math.max(eventIdRef.current, event.event_id);
        setEvents((prev) => [event, ...prev].slice(0, 200));
        // Any real event means the graph likely changed: refresh promptly.
        scheduleGraphRefresh();
      },
      (up) => {
        setConnected(up);
        if (up) void loadAll();
      },
    );
    return unsubscribe;
  }, [token, scheduleGraphRefresh, loadAll]);

  // --- escape restores the layout ---------------------------------------------
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if (e.key === "Escape" && focused) setFocused(false);
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [focused]);

  if (!token) {
    return (
      <AuthGate
        onToken={(t) => {
          setToken(t);
          setTokenState(t);
        }}
      />
    );
  }

  const degraded = graphError !== null || telemetryError !== null || dataError !== null;

  return (
    <div className={`app ${focused ? "focused" : ""}`}>
      <header className="topbar panel">
        <div className="brand">
          <span className="glyph" aria-hidden="true">
            ◉
          </span>
          <span>
            SYNAPSE ONE
            <small>your personal intelligence infrastructure</small>
          </span>
        </div>

        <span className="status-pill" role="status">
          <span className={`dot ${connected ? "online" : "offline"}`} aria-hidden="true" />
          {connected ? "CORE ONLINE" : "RECONNECTING"}
        </span>

        <span className="status-pill">
          <span className={`dot ${degraded ? "offline" : "online"}`} aria-hidden="true" />
          {degraded ? "DATA DEGRADED" : "LIVE MONITORING"}
        </span>

        <span className="spacer" />

        {authError && (
          <span className="chip chip-bad" title={authError}>
            auth failed
          </span>
        )}
        {telemetryError && (
          <span className="chip chip-bad" title={telemetryError}>
            telemetry unavailable
          </span>
        )}
        <span className="chip" title="Process CPU load for the core API host, across all logical cores">
          CPU <b>{telemetry ? `${telemetry.cpu_percent.toFixed(0)}%` : "—"}</b>
        </span>
        <span
          className="chip"
          title={
            telemetry?.gpu.available
              ? `GPU utilisation · ${telemetry.gpu.name ?? "unknown device"}`
              : (telemetry?.gpu.reason ?? "No GPU telemetry available")
          }
        >
          GPU{" "}
          <b>
            {telemetry ? (telemetry.gpu.available ? gpuPercent(telemetry) : "n/a") : "—"}
          </b>
        </span>
        <span className="chip" title="Host memory in use, from the telemetry sampler">
          RAM <b>{telemetry ? `${telemetry.memory.percent.toFixed(0)}%` : "—"}</b>
        </span>
        <button
          className="chip"
          onClick={() => {
            clearToken();
            setTokenState("");
          }}
          title="Forget the stored token and return to the connect screen"
        >
          Owner ⏷
        </button>
      </header>

      <Sidebar
        telemetry={telemetry}
        telemetryError={telemetryError}
        activity={activity}
        stats={stats}
        focused={focused}
        onToggleFocus={() => setFocused((f) => !f)}
      />

      <main className="center panel">
        <div className="graph-head">
          <span className="graph-glyph" aria-hidden="true">
            ✦
          </span>
          <div className="graph-title">
            <h2>Neural Intelligence Network</h2>
            <p>Live topology of real jobs, workers, documents and models</p>
          </div>
          <span
            className={`live-badge ${connected ? "" : "offline"}`}
            title={
              connected
                ? "Receiving the core's event stream"
                : "Event stream disconnected — polling continues"
            }
          >
            <span className={`dot ${connected ? "online" : "offline"}`} aria-hidden="true" />{" "}
            {connected ? "LIVE" : "OFFLINE"}
          </span>
          <span className="spacer" />

          <div className="counts">
            <Count label="active" {...countText(graph, "active")} />
            <Count label="jobs" {...countText(graph, "jobs")} />
            <Count label="links" {...countText(graph, "edges")} />
            <Count label="docs" {...countText(graph, "documents")} />
          </div>

          <button
            className="chip"
            onClick={() => setFocused((f) => !f)}
            aria-pressed={focused}
            title={focused ? "Restore layout (Esc)" : "Expand graph"}
          >
            {focused ? "⤡ restore" : "⤢ expand"}
          </button>
        </div>

        <div className="graph-meta" aria-hidden={graph === null}>
          {graph ? (
            <>
              snapshot{" "}
              <time dateTime={graph.generated_at} title={new Date(graph.generated_at).toLocaleString()}>
                {new Date(graph.generated_at).toLocaleTimeString()}
              </time>
              {graph.counts.chunks !== undefined && <> · {graph.counts.chunks} chunks shown</>}
            </>
          ) : (
            "snapshot not loaded"
          )}
        </div>

        {graphError ? (
          <div className="empty error">
            <div className="empty-title">Could not load the graph</div>
            {graphError}
            <br />
            The core may not be running. This dashboard reads it over HTTP at 127.0.0.1:8765.
            <div className="empty-actions">
              <button className="chip" onClick={() => void loadGraph()}>
                retry now
              </button>
            </div>
          </div>
        ) : !graph ? (
          <div className="empty">
            <span className="spinner" aria-hidden="true" /> Loading the graph from the core…
          </div>
        ) : graph.nodes.length === 0 ? (
          <div className="empty">
            <div className="empty-title">The system is idle.</div>
            No nodes exist because no jobs or documents exist yet.
            <br />
            Type in the bar below, or run <code>synapse ask "…"</code> in any terminal, and a real
            node appears here.
          </div>
        ) : (
          <NeuralGraph graph={graph} focused={focused} onToggleFocus={() => setFocused((f) => !f)} />
        )}
      </main>

      <aside className="right scroll-thin">
        <TaskQueue graph={graph} error={graphError} />
        <ModelRouter usage={usage} error={dataError} />
        <KnowledgePanel activity={activity} error={dataError} />
        <EventStream events={events} connected={connected} />
      </aside>

      <PromptBar onSubmitted={bump} />
    </div>
  );
}

function describeError(error: unknown): string {
  if (error instanceof Error) return error.message;
  return String(error);
}

/** GPU utilisation, or "n/a" when the device exists but reported no reading. */
function gpuPercent(telemetry: Telemetry): string {
  const value = telemetry.gpu.utilization_percent;
  return value === null || value === undefined ? "n/a" : `${value.toFixed(0)}%`;
}

/**
 * One header counter. `countText` decides whether the backend actually sent a
 * number; an absent key renders "n/a" instead of a fabricated zero.
 */
function Count({ label, value, hint }: { label: string; value: string; hint: string }) {
  return (
    <span className="count" title={hint}>
      <b>{value}</b>
      <span>{label}</span>
    </span>
  );
}

function countText(graph: Graph | null, key: string): { value: string; hint: string } {
  if (!graph) return { value: "n/a", hint: "Graph snapshot not loaded yet." };

  const shown: number | undefined = graph.counts[key];
  if (shown === undefined) {
    return { value: "n/a", hint: `The core did not report a "${key}" count for this snapshot.` };
  }

  const truncation: Truncation | undefined = graph.truncated[key];
  if (truncation?.total != null && truncation.total > shown) {
    return {
      value: `${shown.toLocaleString()}/${truncation.total.toLocaleString()}`,
      hint: `Showing ${shown} of ${truncation.total} ${key}; the snapshot is capped at ${truncation.cap}.`,
    };
  }
  return { value: shown.toLocaleString(), hint: `${shown} ${key} in this snapshot.` };
}

function AuthGate({ onToken }: { onToken: (token: string) => void }) {
  const [value, setValue] = useState("");
  const trimmed = value.trim();

  return (
    <div className="auth-gate">
      <form
        className="card panel"
        onSubmit={(e) => {
          e.preventDefault();
          if (trimmed) onToken(trimmed);
        }}
      >
        <h3>Connect to SYNAPSE ONE</h3>
        <p>
          The dashboard is a thin client of the local core API, which requires the same bearer token
          the CLI uses.
          <br />
          <br />
          Run <code>synapse token</code> to print it.
        </p>
        <label className="field">
          <span>Access token</span>
          <input
            autoFocus
            value={value}
            placeholder="paste token"
            autoComplete="off"
            spellCheck={false}
            onChange={(e) => setValue(e.target.value)}
          />
        </label>
        <button className="send" type="submit" disabled={!trimmed}>
          Connect
        </button>
      </form>
    </div>
  );
}
