/**
 * Typed client for the SYNAPSE ONE core API.
 *
 * This is deliberately the *same* API the CLI uses — the dashboard is a thin
 * client of one persistent core, not a second backend. Every type here mirrors
 * a Pydantic contract in `src/synapse/api/dashboard_contracts.py`; if the
 * backend changes shape, this file must change with it.
 *
 * Nullability is preserved on purpose. A `null` success rate means "nothing has
 * finished yet"; a `0` would mean "everything failed". Collapsing them in the
 * type would let the UI lie.
 */

const TOKEN_KEY = "synapse.token";

export function getToken(): string {
  return localStorage.getItem(TOKEN_KEY) ?? "";
}

export function setToken(token: string): void {
  localStorage.setItem(TOKEN_KEY, token);
}

export function clearToken(): void {
  localStorage.removeItem(TOKEN_KEY);
}

// --- wire types (mirror dashboard_contracts.py) ------------------------------

export interface Memory {
  total_bytes: number;
  available_bytes: number;
  used_bytes: number;
  percent: number;
}

export interface Disk {
  total_bytes: number;
  used_bytes: number;
  free_bytes: number;
  percent: number;
  mount: string;
}

export interface Network {
  bytes_sent_per_sec: number;
  bytes_recv_per_sec: number;
  sample_age_seconds: number;
  total_bytes_sent: number;
  total_bytes_recv: number;
}

/** Either a real GPU reading, or an explicit statement that none exists. */
export interface Gpu {
  available: boolean;
  name?: string | null;
  index?: number | null;
  utilization_percent?: number | null;
  memory_used_mb?: number | null;
  memory_total_mb?: number | null;
  memory_percent?: number | null;
  temperature_c?: number | null;
  reason?: string | null;
}

export interface Telemetry {
  timestamp: string;
  cpu_percent: number;
  cpu_count_logical: number;
  cpu_count_physical: number | null;
  load_average_1m: number | null;
  load_average_unavailable_reason: string | null;
  memory: Memory;
  swap_percent: number | null;
  swap_unavailable_reason: string | null;
  disk: Disk;
  network: Network | null;
  gpu: Gpu;
  process_uptime_seconds: number;
  process_rss_bytes: number;
  process_threads: number;
}

export type NodeKind = "job" | "worker" | "document" | "model" | "chunk";
export type EdgeKind = "claimed" | "used" | "produced" | "contains";

export interface GraphNode {
  id: string;
  kind: NodeKind;
  label: string;
  status: string | null;
  group: string | null;
  weight: number;
  created_at: string | null;
  details: Record<string, unknown>;
}

export interface GraphEdge {
  source: string;
  target: string;
  kind: EdgeKind;
}

export interface Truncation {
  shown: number;
  total: number | null;
  cap: number;
}

export interface Graph {
  generated_at: string;
  nodes: GraphNode[];
  edges: GraphEdge[];
  counts: Record<string, number>;
  truncated: Record<string, Truncation>;
}

export interface JobStats {
  total: number;
  by_status: Record<string, number>;
  active: number;
  queued: number;
  succeeded_24h: number;
  failed_24h: number;
  /** null = no job has finished yet (NOT "0% succeeded"). */
  success_rate_24h: number | null;
  throughput_per_hour_24h: number;
}

export interface ModelRollup {
  provider: string;
  model_id: string;
  calls: number;
  input_tokens: number;
  output_tokens: number;
  /** null = no priced call for this model (NOT "$0.00"). */
  total_cost_usd: number | null;
  avg_latency_ms: number;
  success_rate: number;
  last_used_at: string | null;
}

export interface Usage {
  total_calls: number;
  succeeded: number;
  failed: number;
  total_input_tokens: number;
  total_output_tokens: number;
  total_cost_usd: number | null;
  priced_calls: number;
  unknown_price_calls: number;
  avg_latency_ms: number | null;
  models: ModelRollup[];
}

export interface Activity {
  active_hours_7d: number;
  active_days_7d: number;
  total_runtime_seconds_7d: number | null;
  first_event_at: string | null;
  last_event_at: string | null;
  domains: Record<string, number>;
  source_types: Record<string, number>;
}

export interface JobEvent {
  event_id: number;
  job_id: string;
  event_type: string;
  timestamp: string;
  component_id: string;
  status: string | null;
  correlation_id: string | null;
  metadata: Record<string, unknown>;
}

// --- search --------------------------------------------------------------------

/** One entry in the connector catalog served by the core. */
export interface ConnectorInfo {
  name: string;
  domain: string;
  description: string;
}

/** One search result, exactly as the connector returned it. */
export interface SearchItem {
  title: string;
  url: string | null;
  date: string | null;
  snippet: string | null;
  /** Provider extras, e.g. ["stars", "120"]; may disclose truncation — render as-is. */
  facts: [string, string][];
}

/** Provider rate-limit reading — a null limit/remaining means "not reported". */
export interface SearchRate {
  limit: number | null;
  remaining: number | null;
  source: string;
}

export interface SearchResult {
  connector: string;
  domain: string;
  query: string;
  requested: number;
  page_size: number;
  items: SearchItem[];
  /** null = the provider reported no total (NOT "0 results"). */
  total: number | null;
  fetched_at: string;
  rate: SearchRate | null;
}

// --- transport ----------------------------------------------------------------

export class ApiError extends Error {
  constructor(
    readonly status: number,
    message: string,
    /** Parsed response body when the server sent one (e.g. {detail: {reason, message}}). */
    readonly body?: unknown,
  ) {
    super(message);
    this.name = "ApiError";
  }
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const token = getToken();
  const response = await fetch(path, {
    ...init,
    headers: {
      ...(init?.headers ?? {}),
      ...(token ? { Authorization: `Bearer ${token}` } : {}),
    },
  });
  if (!response.ok) {
    const raw = await response.text().catch(() => "");
    // Error bodies carry structured truth ({detail: {reason, message}}).
    // Parse them when we can so the UI can show the real reason; parsing must
    // never throw, so a non-JSON body degrades to null, not to a second error.
    let body: unknown = null;
    try {
      body = raw ? (JSON.parse(raw) as unknown) : null;
    } catch {
      body = null;
    }
    throw new ApiError(response.status, `${response.status} ${path}: ${raw.slice(0, 200)}`, body);
  }
  return (await response.json()) as T;
}

export const api = {
  telemetry: () => request<Telemetry>("/v1/dashboard/telemetry"),
  graph: () => request<Graph>("/v1/dashboard/graph"),
  stats: () => request<JobStats>("/v1/dashboard/stats"),
  usage: () => request<Usage>("/v1/dashboard/usage"),
  activity: () => request<Activity>("/v1/dashboard/activity"),
  events: (sinceId: number) => request<JobEvent[]>(`/v1/events?since_id=${sinceId}&limit=100`),
  connectors: () => request<{ connectors: ConnectorInfo[] }>("/v1/search/connectors"),
  search: (connector: string, query: string, limit = 10) =>
    request<SearchResult>("/v1/search", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ connector, query, limit }),
    }),
};

/**
 * Subscribe to the real event stream over SSE.
 *
 * The stream is the same one the CLI consumes. `EventSource` cannot send an
 * Authorization header, so the token rides in the query string; that is
 * acceptable here because the core binds to loopback only, and it is recorded
 * in ADR 0006 rather than silently assumed.
 */
export function subscribeEvents(
  onEvent: (event: JobEvent) => void,
  onStatus: (connected: boolean) => void,
): () => void {
  const token = getToken();
  const source = new EventSource(`/v1/events/stream?token=${encodeURIComponent(token)}`);

  source.onopen = () => onStatus(true);
  source.onerror = () => onStatus(false);

  const handler = (event: MessageEvent) => {
    try {
      onEvent(JSON.parse(event.data) as JobEvent);
    } catch {
      // A malformed frame must not kill the subscription.
    }
  };

  // The backend names events by their type; listen generically by also using
  // the default "message" channel, which sse-starlette emits for unnamed events.
  source.addEventListener("message", handler as EventListener);
  for (const type of [
    "job_created",
    "job_started",
    "job_completed",
    "job_failed",
    "job_cancelled",
    "job_retrying",
    "job_requeued",
    "job_lease_expired",
  ]) {
    source.addEventListener(type, handler as EventListener);
  }

  return () => source.close();
}

/** Ask the core to run a prompt, exactly as the CLI does. */
export async function ask(prompt: string): Promise<{ id: string; status: string }> {
  const created = await request<{ id: string; status: string }>("/v1/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_type: "ask", payload: { prompt } }),
  });
  return created;
}

/** Ingest a URL into the knowledge layer, exactly as the CLI does. */
export async function ingestUrl(url: string): Promise<{ id: string; status: string }> {
  return request<{ id: string; status: string }>("/v1/jobs", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ task_type: "ingest_url", payload: { url } }),
  });
}
