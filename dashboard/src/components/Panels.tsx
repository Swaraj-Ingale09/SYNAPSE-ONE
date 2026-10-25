import type { ReactNode } from "react";
import type { Usage, Graph, JobEvent, Activity } from "../api";

/**
 * Right column: task queue, model router, knowledge sources and the live event
 * stream.
 *
 * The task queue is derived from the real graph nodes (jobs), and the model
 * router from the real `model_usage` ledger — so when no model has been called,
 * the panel says so instead of rendering invented percentages.
 *
 * Three states are kept distinct on purpose, because collapsing them is how a
 * dashboard starts lying: **loading** (request in flight), **failed** (the core
 * did not answer) and **empty** (the core answered with zero rows). Only the
 * last one is allowed to say "nothing yet".
 */

const MODEL_COLORS = ["#3882f6", "#22d3ee", "#a78bfa", "#f472b6", "#34d399", "#fbbf24", "#fb7185", "#94a3b8"];

function timeAgo(iso: string | null | undefined): string {
  if (!iso) return "time unknown";
  const ms = Date.now() - new Date(iso).getTime();
  if (Number.isNaN(ms)) return "time unknown";
  if (ms < 0) return "just now";
  const s = Math.floor(ms / 1000);
  if (s < 60) return `${s}s ago`;
  const m = Math.floor(s / 60);
  if (m < 60) return `${m}m ago`;
  const h = Math.floor(m / 60);
  if (h < 24) return `${h}h ago`;
  return `${Math.floor(h / 24)}d ago`;
}

function readString(value: unknown): string | null {
  return typeof value === "string" && value.length > 0 ? value : null;
}

function readNumber(value: unknown): number | null {
  return typeof value === "number" && Number.isFinite(value) ? value : null;
}

/** One consistent shell for loading / empty / failure copy. */
function Message({
  tone,
  title,
  children,
}: {
  tone: "empty" | "error" | "loading";
  title?: string;
  children: ReactNode;
}) {
  return (
    <div className={`empty ${tone}`}>
      {tone === "loading" && <span className="spinner" aria-hidden="true" />}
      {title && <div className="empty-title">{title}</div>}
      <div className="empty-body">{children}</div>
    </div>
  );
}

export function TaskQueue({ graph, error }: { graph: Graph | null; error: string | null }) {
  const jobs = (graph?.nodes ?? [])
    .filter((n) => n.kind === "job")
    .sort((a, b) => (a.created_at ?? "").localeCompare(b.created_at ?? ""))
    .reverse();

  const counts = jobs.reduce<Record<string, number>>((acc, job) => {
    const status = job.status ?? "unknown";
    acc[status] = (acc[status] ?? 0) + 1;
    return acc;
  }, {});

  const filterChips: [string, number][] = [["All", jobs.length], ...Object.entries(counts).slice(0, 5)];
  const shown = jobs.slice(0, 9);
  const totalJobs = graph?.truncated.jobs?.total ?? null;

  return (
    <section className="card panel">
      <h3>
        Task Queue <span className="spacer" />
        <span className="muted">{graph ? `${jobs.length} job nodes` : "—"}</span>
      </h3>

      {error ? (
        <Message tone="error" title="Queue unavailable">
          {error}
        </Message>
      ) : !graph ? (
        <Message tone="loading">Loading the queue from the core…</Message>
      ) : jobs.length === 0 ? (
        <Message tone="empty">
          No jobs yet. Use the prompt bar below or the <code>synapse</code> CLI to create one.
        </Message>
      ) : (
        <>
          <div className="chip-row">
            {filterChips.map(([label, count]) => (
              <span
                key={label}
                className={`badge ${label === "All" ? "all" : statusClass(label)}`}
                title={`${count} ${label === "All" ? "job nodes" : `${label} job nodes`} in this snapshot`}
              >
                {label} ({count})
              </span>
            ))}
          </div>

          {shown.map((job) => {
            const taskType = readString(job.details.task_type) ?? job.label;
            const worker = readString(job.details.worker_id);
            const durationMs = readNumber(job.details.duration_ms);
            const status = job.status ?? "unknown";
            return (
              <div className="task" key={job.id}>
                <div className="body">
                  <div className="title" title={taskType}>
                    {taskType}
                  </div>
                  <div className="meta">
                    {worker ?? "unclaimed"}
                    {durationMs != null ? ` · ${Math.round(durationMs / 1000)}s` : ""}
                    {` · ${timeAgo(job.created_at)}`}
                  </div>
                </div>
                <span className={`badge ${statusClass(status)}`}>{status}</span>
              </div>
            );
          })}

          {(jobs.length > shown.length || (totalJobs != null && totalJobs > jobs.length)) && (
            <div className="list-note">
              showing {shown.length} of {jobs.length} job nodes
              {totalJobs != null && totalJobs > jobs.length
                ? ` · ${totalJobs.toLocaleString()} jobs exist in total`
                : ""}
            </div>
          )}
        </>
      )}
    </section>
  );
}

function statusClass(status: string): string {
  if (status === "running") return "running";
  if (status === "queued" || status === "created") return "queued";
  if (status === "completed") return "completed";
  if (status === "failed" || status === "cancelled" || status === "expired") return "failed";
  if (status.startsWith("waiting") || status === "retrying") return "retrying";
  // Unknown statuses get neutral styling — never the "completed" colour, which
  // would imply an outcome the backend did not report.
  return "neutral";
}

export function ModelRouter({ usage, error }: { usage: Usage | null; error: string | null }) {
  const shell = (body: ReactNode) => (
    <section className="card panel">
      <h3>
        Model Router <span className="spacer" />
        <span className="muted">{usage ? `${usage.total_calls} calls` : "—"}</span>
      </h3>
      {body}
    </section>
  );

  if (error && !usage) {
    return shell(
      <Message tone="error" title="Usage ledger unavailable">
        {error}
      </Message>,
    );
  }
  if (!usage) return shell(<Message tone="loading">Loading the usage ledger…</Message>);
  if (usage.total_calls === 0) {
    return shell(
      <Message tone="empty">
        No model calls recorded yet. The ledger fills in as the gateway serves requests —
        percentages are computed from real calls, never estimated.
      </Message>,
    );
  }

  const total = usage.models.reduce((sum, m) => sum + m.calls, 0) || 1;
  const latency = usage.avg_latency_ms;

  return shell(
    <>
      <div className="router-summary">
        <Donut models={usage.models} total={total} />
        <div className="router-figures">
          <div className="figure">
            {latency != null ? `${Math.round(latency)}ms` : <span className="na">n/a</span>}
          </div>
          <div className="figure-label">
            avg latency{latency == null ? " · not measured" : ""}
          </div>
          <div className="figure small">
            {usage.total_cost_usd != null ? `$${usage.total_cost_usd.toFixed(4)}` : "n/a"}
          </div>
          <div className="figure-label">
            total cost
            {usage.unknown_price_calls > 0 ? ` · ${usage.unknown_price_calls} unpriced` : ""}
          </div>
        </div>
      </div>

      {usage.models.map((model, i) => (
        <div
          className="model-row"
          key={`${model.provider}/${model.model_id}`}
          title={[
            `${model.provider} / ${model.model_id}`,
            `${model.calls} calls`,
            `${model.input_tokens.toLocaleString()} in / ${model.output_tokens.toLocaleString()} out tokens`,
            `avg ${Math.round(model.avg_latency_ms)}ms`,
            `${(model.success_rate * 100).toFixed(1)}% succeeded`,
            model.total_cost_usd != null ? `$${model.total_cost_usd.toFixed(4)}` : "cost not priced",
          ].join(" · ")}
        >
          <span className="swatch" style={{ background: MODEL_COLORS[i % MODEL_COLORS.length] }} />
          <span className="name">{model.model_id}</span>
          <span className="val">
            {((model.calls / total) * 100).toFixed(1)}% · {model.calls}
          </span>
        </div>
      ))}

      {error && (
        <div className="list-note warn" title={error}>
          showing the last successful reading — refresh failed
        </div>
      )}
    </>,
  );
}

function Donut({ models, total }: { models: Usage["models"]; total: number }) {
  const size = 96;
  const radius = 40;
  const circumference = 2 * Math.PI * radius;
  let offset = 0;

  const description = models
    .map((model) => `${model.model_id}: ${model.calls} of ${total} calls`)
    .join(", ");

  return (
    <svg
      width={size}
      height={size}
      viewBox={`0 0 ${size} ${size}`}
      className="donut"
      role="img"
      aria-label={`Model call share — ${description}`}
    >
      <circle cx={size / 2} cy={size / 2} r={radius} fill="none" stroke="rgba(255,255,255,0.05)" strokeWidth="11" />
      {models.map((model, i) => {
        const fraction = model.calls / total;
        const dash = fraction * circumference;
        const element = (
          <circle
            key={`${model.provider}/${model.model_id}`}
            cx={size / 2}
            cy={size / 2}
            r={radius}
            fill="none"
            stroke={MODEL_COLORS[i % MODEL_COLORS.length]}
            strokeWidth="11"
            strokeDasharray={`${dash} ${circumference - dash}`}
            strokeDashoffset={-offset}
            transform={`rotate(-90 ${size / 2} ${size / 2})`}
            strokeLinecap="butt"
          />
        );
        offset += dash;
        return element;
      })}
    </svg>
  );
}

export function EventStream({ events, connected }: { events: JobEvent[]; connected: boolean }) {
  return (
    <section className="card panel">
      <h3>
        Live Event Stream
        <span className="spacer" />
        <span className={`dot ${connected ? "online" : "offline"}`} aria-hidden="true" />
        <span className="muted">{connected ? "streaming" : "reconnecting"}</span>
      </h3>

      {events.length === 0 ? (
        <Message tone="empty">
          {connected
            ? "Waiting for the first event. Create a job to see it appear here in real time."
            : "Not connected to the event stream. The panels still poll the core."}
        </Message>
      ) : (
        <div className="stream scroll-thin">
          {events.slice(0, 40).map((event) => (
            <div
              className="stream-row"
              key={event.event_id}
              title={`${event.event_type} · job ${event.job_id} · ${new Date(event.timestamp).toLocaleString()}`}
            >
              <span className="t">{new Date(event.timestamp).toLocaleTimeString()}</span>
              <span className="e">{event.event_type.replace(/^job_/, "")}</span>
              <span className="j">{event.job_id.slice(0, 8)}</span>
            </div>
          ))}
        </div>
      )}
    </section>
  );
}

export function KnowledgePanel({ activity, error }: { activity: Activity | null; error: string | null }) {
  const sources = activity?.source_types ?? {};
  const entries = Object.entries(sources);

  return (
    <section className="card panel">
      <h3>
        Knowledge Sources <span className="spacer" />
        {activity && <span className="muted">{entries.length} types</span>}
      </h3>
      {error && !activity ? (
        <Message tone="error" title="Knowledge activity unavailable">
          {error}
        </Message>
      ) : !activity ? (
        <Message tone="loading">Loading knowledge activity…</Message>
      ) : entries.length === 0 ? (
        <Message tone="empty">
          Nothing ingested yet. Use <code>synapse ingest &lt;url&gt;</code> to add a document, then it
          appears in the graph as a real node with real provenance.
        </Message>
      ) : (
        entries.map(([type, count]) => (
          <div className="model-row" key={type} title={`${count} ${type} documents found in the 7-day activity window`}>
            <span className="swatch" style={{ background: "var(--document)" }} />
            <span className="name capitalize">{type}</span>
            <span className="val">{count} docs</span>
          </div>
        ))
      )}
      {error && activity && (
        <div className="list-note warn" title={error}>
          showing the last successful reading — refresh failed
        </div>
      )}
    </section>
  );
}
