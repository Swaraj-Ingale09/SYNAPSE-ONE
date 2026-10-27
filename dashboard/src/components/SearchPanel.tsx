import { useEffect, useRef, useState } from "react";
import { api, ApiError, type ConnectorInfo, type SearchResult } from "../api";

/**
 * Live connector search — the dashboard half of the same `/v1/search` endpoint
 * the CLI uses. It never fabricates data: the connector dropdown is exactly the
 * catalog the core returned (a failed catalog load disables the form and shows
 * the real error, never a fake list), and every value rendered below the form
 * comes straight from the search response.
 *
 * Honesty rules applied here:
 * - a `null` total renders "count not reported by provider", never `0`;
 * - a `null` rate renders nothing (there is nothing to report);
 * - a missing URL renders the title as plain text with an explicit "n/a" link
 *   marker; a missing date renders "n/a"; a missing snippet is simply omitted,
 *   because an absent body is not an error;
 * - a fact like ["snippet", "600 of 4821 chars"] is a truncation disclosure
 *   from the provider and is shown as a chip, verbatim;
 * - failures render the backend's structured reason (e.g. `rate_limited`)
 *   plus its human message, falling back to the transport error message when
 *   the body cannot be parsed.
 *
 * Concurrency: each search bumps a sequence counter; a response whose sequence
 * is no longer current (superseded by a newer search, or the panel unmounted)
 * is ignored, so a slow old response can never overwrite a fresh one.
 */

interface SearchFailure {
  reason: string | null;
  message: string;
}

/** Pull {detail: {reason, message}} out of an ApiError body, if it is there. */
function describeFailure(error: unknown): SearchFailure {
  if (error instanceof ApiError) {
    const detail = (error.body as { detail?: { reason?: unknown; message?: unknown } } | null)
      ?.detail;
    const reason = typeof detail?.reason === "string" && detail.reason ? detail.reason : null;
    const message = typeof detail?.message === "string" && detail.message ? detail.message : null;
    return { reason, message: message ?? error.message };
  }
  return { reason: null, message: error instanceof Error ? error.message : String(error) };
}

export default function SearchPanel() {
  const [connectors, setConnectors] = useState<ConnectorInfo[]>([]);
  const [catalogError, setCatalogError] = useState<SearchFailure | null>(null);
  const [catalogLoaded, setCatalogLoaded] = useState(false);
  const [connector, setConnector] = useState("");
  const [query, setQuery] = useState("");
  const [busy, setBusy] = useState(false);
  const [result, setResult] = useState<SearchResult | null>(null);
  const [searchError, setSearchError] = useState<SearchFailure | null>(null);

  const seqRef = useRef(0);
  const mountedRef = useRef(true);

  // Load the connector catalog once. The panel only renders inside the
  // authenticated shell, so the bearer token is already present.
  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const data = await api.connectors();
        if (cancelled) return;
        setConnectors(data.connectors);
        setConnector((current) => current || data.connectors[0]?.name || "");
        setCatalogError(null);
      } catch (error) {
        if (!cancelled) setCatalogError(describeFailure(error));
      } finally {
        if (!cancelled) setCatalogLoaded(true);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  // Invalidate any in-flight search when the panel unmounts.
  useEffect(() => {
    mountedRef.current = true;
    return () => {
      mountedRef.current = false;
      seqRef.current += 1;
    };
  }, []);

  const trimmed = query.trim();
  const formDisabled = !catalogLoaded || catalogError !== null;

  async function submit() {
    if (!trimmed || busy || formDisabled || !connector) return;
    const seq = ++seqRef.current;
    setBusy(true);
    setSearchError(null);
    setResult(null);
    try {
      const next = await api.search(connector, trimmed);
      if (!mountedRef.current || seq !== seqRef.current) return; // superseded
      setResult(next);
    } catch (error) {
      if (!mountedRef.current || seq !== seqRef.current) return; // superseded
      setSearchError(describeFailure(error));
    } finally {
      if (mountedRef.current && seq === seqRef.current) setBusy(false);
    }
  }

  return (
    <section className="card panel">
      <h3>
        Search <span className="spacer" />
        {catalogLoaded && !catalogError && (
          <span className="muted">{connectors.length} connectors</span>
        )}
      </h3>

      <form
        className="search-form"
        onSubmit={(e) => {
          e.preventDefault();
          void submit();
        }}
      >
        <select
          className="search-connector"
          aria-label="Search connector"
          value={connector}
          disabled={formDisabled}
          onChange={(e) => setConnector(e.target.value)}
        >
          {catalogLoading(catalogLoaded, catalogError) && <option value="">loading…</option>}
          {catalogError && <option value="">connectors unavailable</option>}
          {connectors.map((c) => (
            <option key={c.name} value={c.name}>
              {c.name} — {c.domain}
            </option>
          ))}
        </select>
        <input
          className="search-query"
          value={query}
          aria-label="Search query"
          placeholder="search a live connector…"
          autoComplete="off"
          spellCheck={false}
          disabled={formDisabled}
          onChange={(e) => setQuery(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Enter") void submit();
          }}
        />
        <button
          className="search-send"
          type="submit"
          disabled={formDisabled || !trimmed || busy}
          title="Run the query against the selected connector"
        >
          {busy ? "Searching…" : "Search"}
        </button>
      </form>

      {catalogError ? (
        <div className="empty error">
          <div className="empty-title">Connector catalog unavailable</div>
          {catalogError.reason && <span className="chip chip-bad">{catalogError.reason}</span>}{" "}
          {catalogError.message}
          <br />
          Search is disabled until the catalog loads — no connectors are invented.
        </div>
      ) : !catalogLoaded ? (
        <div className="empty">
          <span className="spinner" aria-hidden="true" /> loading connectors…
        </div>
      ) : busy ? (
        <div className="empty">
          <span className="spinner" aria-hidden="true" /> searching {connector}…
        </div>
      ) : searchError ? (
        <div className="empty error">
          <div className="empty-title">Search failed</div>
          {searchError.reason && <span className="chip chip-bad">{searchError.reason}</span>}{" "}
          {searchError.message}
        </div>
      ) : result === null ? (
        <div className="empty">
          Pick a connector and type a query. Results come live from the provider through the core —
          nothing here is cached or invented.
        </div>
      ) : (
        <>
          <div className="search-head">
            <span className="mono">
              {result.connector} · {result.domain} · "{result.query}"
            </span>{" "}
            · fetched{" "}
            <time
              className="mono"
              dateTime={result.fetched_at}
              title={new Date(result.fetched_at).toLocaleString()}
            >
              {new Date(result.fetched_at).toLocaleTimeString()}
            </time>
          </div>

          {result.page_size < result.requested && (
            <div className="list-note warn">
              provider page cap: showing {result.page_size} of {result.requested} requested
            </div>
          )}

          <div className="search-meta">
            <span>
              {result.total !== null
                ? `${result.total.toLocaleString()} total results`
                : "count not reported by provider"}
            </span>
            {result.rate && (
              <span className="mono">
                rate limit {result.rate.remaining ?? "n/a"}/{result.rate.limit ?? "n/a"} (
                {result.rate.source})
              </span>
            )}
          </div>

          {result.items.length === 0 ? (
            <div className="empty">The provider returned no results for this query.</div>
          ) : (
            result.items.map((item, i) => (
              <div className="search-item" key={i}>
                <div className="search-title">
                  {item.url !== null ? (
                    <a href={item.url} target="_blank" rel="noreferrer noopener">
                      {item.title}
                    </a>
                  ) : (
                    <>
                      {item.title} <span className="na">· link n/a</span>
                    </>
                  )}
                </div>
                <div className="search-item-meta">
                  <span className={item.date === null ? "na" : undefined}>
                    {item.date ?? "n/a"}
                  </span>
                </div>
                {item.snippet !== null && <div className="search-snippet">{item.snippet}</div>}
                {item.facts.length > 0 && (
                  <div className="chip-row">
                    {item.facts.map(([key, value], fi) => (
                      <span className="badge neutral" key={`${key}-${fi}`}>
                        {key}: {value}
                      </span>
                    ))}
                  </div>
                )}
              </div>
            ))
          )}
        </>
      )}
    </section>
  );
}

function catalogLoading(loaded: boolean, error: SearchFailure | null): boolean {
  return !loaded && error === null;
}
