import type { Gpu, Telemetry, Activity, JobStats } from "../api";

/**
 * Left column: system status and real activity aggregates.
 *
 * Every metric here comes from a measurement or a database rollup. When the
 * backend reports a value as unavailable (no GPU, no load average on Windows),
 * this panel says so in words rather than drawing a bar at 0% — an empty bar
 * and an absent capability must not look the same.
 *
 * Navigation is deliberately not a fake router: the dashboard is one screen.
 * The two entries that do something move the camera; the rest are disabled
 * with a title explaining why, rather than highlighting a page that does not
 * exist.
 */

const NAV: { label: string; glyph: string; view?: "overview" | "graph" }[] = [
  { label: "Overview", glyph: "▦", view: "overview" },
  { label: "Neural Network", glyph: "✦", view: "graph" },
  { label: "Knowledge Graph", glyph: "◇" },
  { label: "Source Monitor", glyph: "▤" },
  { label: "Tasks & Agents", glyph: "◫" },
  { label: "Models", glyph: "◎" },
  { label: "Connectors", glyph: "⇄" },
  { label: "Projects", glyph: "▣" },
  { label: "Terminal", glyph: "▸" },
];

function formatBytes(bytes: number): string {
  const units = ["B", "KB", "MB", "GB", "TB"];
  let value = bytes;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  return `${value.toFixed(value >= 100 || unit === 0 ? 0 : 1)} ${units[unit]}`;
}

/** The API reports VRAM in megabytes; format it without implying precision. */
function formatMegabytes(megabytes: number): string {
  return formatBytes(megabytes * 1024 * 1024);
}

function formatRate(bytesPerSecond: number): string {
  return `${formatBytes(bytesPerSecond)}/s`;
}

function formatDuration(seconds: number | null): string {
  if (seconds === null) return "not measured";
  const h = Math.floor(seconds / 3600);
  const m = Math.floor((seconds % 3600) / 60);
  if (h > 0) return `${h}h ${m}m`;
  if (m > 0) return `${m}m ${Math.floor(seconds % 60)}s`;
  return `${Math.floor(seconds)}s`;
}

function Metric({
  label,
  percent,
  detail,
}: {
  label: string;
  percent: number | null;
  detail?: string;
}) {
  // null means "not measured" — render an explicit state, never a zero bar.
  if (percent === null) {
    return (
      <div className="metric">
        <div className="metric-head">
          <span>{label}</span>
          <b className="na" title="The backend did not measure this value">
            n/a
          </b>
        </div>
        <div className="bar empty" aria-hidden="true" />
        {detail && <div className="metric-detail">{detail}</div>}
      </div>
    );
  }
  const tone = percent > 85 ? "bad" : percent > 65 ? "warn" : "";
  return (
    <div className="metric">
      <div className="metric-head">
        <span>{label}</span>
        <b>{percent.toFixed(0)}%</b>
      </div>
      <div className={`bar ${tone}`} aria-hidden="true">
        <span style={{ width: `${Math.min(100, percent)}%` }} />
      </div>
      {detail && <div className="metric-detail">{detail}</div>}
    </div>
  );
}

/** Comma-joined parts of a GPU reading, skipping fields the backend omitted. */
function gpuDetail(gpu: Gpu): string {
  const parts: string[] = [];
  if (gpu.name) parts.push(gpu.name);

  const used = gpu.memory_used_mb;
  const total = gpu.memory_total_mb;
  if (used != null && total != null) parts.push(`${formatMegabytes(used)} / ${formatMegabytes(total)} VRAM`);
  else if (total != null) parts.push(`${formatMegabytes(total)} VRAM total`);

  if (gpu.temperature_c != null) parts.push(`${gpu.temperature_c}°C`);
  if (parts.length === 0) return gpu.reason ?? "no readings reported";
  return parts.join(" · ");
}

export default function Sidebar({
  telemetry,
  telemetryError,
  activity,
  stats,
  focused,
  onToggleFocus,
}: {
  telemetry: Telemetry | null;
  telemetryError: string | null;
  activity: Activity | null;
  stats: JobStats | null;
  focused: boolean;
  onToggleFocus: () => void;
}) {
  const gpu = telemetry?.gpu;
  const activeView = focused ? "graph" : "overview";

  return (
    <aside className="sidebar panel scroll-thin">
      <nav className="nav" aria-label="Dashboard sections">
        {NAV.map((item) => {
          const disabled = item.view === undefined;
          return (
            <button
              key={item.label}
              className={item.view === activeView ? "active" : ""}
              disabled={disabled}
              aria-current={item.view === activeView ? "page" : undefined}
              title={
                disabled
                  ? `No separate "${item.label}" page — this dashboard is a single live screen.`
                  : item.view === "graph"
                    ? "Expand the graph (Esc restores the layout)"
                    : "Restore the full layout"
              }
              onClick={() => {
                if (item.view === "graph") onToggleFocus();
                if (item.view === "overview" && focused) onToggleFocus();
              }}
            >
              <span className="nav-glyph" aria-hidden="true">
                {item.glyph}
              </span>
              {item.label}
            </button>
          );
        })}
      </nav>

      <div>
        <div className="section-title">
          System Status
          <span className="spacer" />
          <span
            className={`section-note ${telemetryError ? "bad" : telemetry ? "ok" : "warn"}`}
            title={telemetryError ?? undefined}
          >
            {telemetryError ? "last reading kept" : telemetry ? "measured now" : "unavailable"}
          </span>
        </div>

        {telemetryError && (
          <div className="section-note bad" title={telemetryError}>
            telemetry request failed
          </div>
        )}

        <Metric
          label="CPU"
          percent={telemetry?.cpu_percent ?? null}
          detail={
            telemetry
              ? `${telemetry.cpu_count_logical} logical cores${
                  telemetry.cpu_count_physical != null
                    ? ` · ${telemetry.cpu_count_physical} physical`
                    : ""
                }`
              : undefined
          }
        />

        {gpu ? (
          gpu.available ? (
            <Metric label="GPU" percent={gpu.utilization_percent ?? null} detail={gpuDetail(gpu)} />
          ) : (
            <Metric label="GPU" percent={null} detail={gpu.reason ?? "no GPU detected"} />
          )
        ) : (
          <Metric label="GPU" percent={null} detail={telemetry ? "not reported" : undefined} />
        )}

        <Metric
          label="Memory"
          percent={telemetry?.memory.percent ?? null}
          detail={
            telemetry
              ? `${formatBytes(telemetry.memory.used_bytes)} of ${formatBytes(
                  telemetry.memory.total_bytes,
                )}`
              : undefined
          }
        />

        <Metric
          label="Storage"
          percent={telemetry?.disk.percent ?? null}
          detail={telemetry ? `${formatBytes(telemetry.disk.free_bytes)} free` : undefined}
        />

        {telemetry?.network ? (
          <div className="rate-row" title={`Sampled ${telemetry.network.sample_age_seconds.toFixed(1)}s ago`}>
            <span>Network</span>
            <span className="mono">
              ↓ {formatRate(telemetry.network.bytes_recv_per_sec)} · ↑{" "}
              {formatRate(telemetry.network.bytes_sent_per_sec)}
            </span>
          </div>
        ) : (
          telemetry && (
            <div className="section-note">
              Network rate not measurable yet — the sampler needs two samples.
            </div>
          )
        )}

        {telemetry?.load_average_1m === null && telemetry.load_average_unavailable_reason && (
          <div className="section-note">{telemetry.load_average_unavailable_reason}</div>
        )}
      </div>

      <div>
        <div className="section-title">Activity (real, 7 days)</div>
        <dl className="kv kv-2">
          <dt>Active hours</dt>
          <dd>{activity ? `${activity.active_hours_7d}h` : "—"}</dd>
          <dt>Active days</dt>
          <dd>{activity ? activity.active_days_7d : "—"}</dd>
          <dt>Total runtime</dt>
          <dd>{activity ? formatDuration(activity.total_runtime_seconds_7d) : "—"}</dd>
          <dt>First event</dt>
          <dd>
            {activity?.first_event_at
              ? new Date(activity.first_event_at).toLocaleDateString()
              : "—"}
          </dd>
          <dt>Jobs total</dt>
          <dd>{stats ? stats.total : "—"}</dd>
          <dt>Active now</dt>
          <dd>{stats ? stats.active : "—"}</dd>
        </dl>
        <div className="section-note">
          "Active hours" counts distinct hours with at least one job event — system activity, not a
          person's attendance.
        </div>
      </div>

      <div>
        <div className="section-title">
          Jobs (24h)
          <span className="spacer" />
          <span className="section-note">from job stats</span>
        </div>
        <dl className="kv kv-2">
          <dt>Succeeded</dt>
          <dd>{stats ? stats.succeeded_24h : "—"}</dd>
          <dt>Failed</dt>
          <dd>{stats ? stats.failed_24h : "—"}</dd>
          <dt>Queued now</dt>
          <dd>{stats ? stats.queued : "—"}</dd>
          <dt>Throughput</dt>
          <dd>{stats ? `${stats.throughput_per_hour_24h.toFixed(1)}/h` : "—"}</dd>
          <dt>Success rate</dt>
          <dd>
            {!stats ? (
              "—"
            ) : stats.success_rate_24h === null ? (
              <span className="na" title="No job finished in the window, so no rate exists">
                n/a
              </span>
            ) : (
              `${(stats.success_rate_24h * 100).toFixed(1)}%`
            )}
          </dd>
        </dl>
        {stats?.success_rate_24h === null && (
          <div className="section-note">No job finished in the window, so there is no rate to report.</div>
        )}
      </div>

      {activity && Object.keys(activity.domains).length > 0 && (
        <div>
          <div className="section-title">
            Domains Worked
            <span className="spacer" />
            <span className="section-note">share of job events</span>
          </div>
          {Object.entries(activity.domains).map(([domain, count]) => {
            const total = Object.values(activity.domains).reduce((a, b) => a + b, 0) || 1;
            return (
              <div className="metric" key={domain}>
                <div className="metric-head">
                  <span>{domain}</span>
                  <b>{count}</b>
                </div>
                <div className="bar" aria-hidden="true">
                  <span style={{ width: `${(count / total) * 100}%` }} />
                </div>
              </div>
            );
          })}
        </div>
      )}

      {activity && Object.keys(activity.source_types).length > 0 && (
        <div>
          <div className="section-title">Knowledge Sources</div>
          {Object.entries(activity.source_types).map(([type, count]) => (
            <div className="rate-row" key={type}>
              <span className="capitalize">{type}</span>
              <b className="mono">{count} docs</b>
            </div>
          ))}
        </div>
      )}
    </aside>
  );
}
