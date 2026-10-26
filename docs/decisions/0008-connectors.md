# ADR 0008: Free public-API connectors, verified before hardcoding

## Status

Accepted — 2026-10-26

## Context

The workstation could fetch and ingest * URLs the user handed it (ADR 0004's
SSRF-safe fetcher), but it could not search anything on its own: every domain
of knowledge — coding, scholarship, medicine, earth science, weather, finance —
was empty until the user pasted a URL. The obvious fix (wire up search APIs)
collides with three things this project refuses to do:

1. **No provider may be hardcoded on faith.** Endpoint, response shape, page
   caps, and rate limits must be read from the live server, not from blog
   posts or memory; a rate limit guessed wrong renders as a lie in the UI.
2. **robots.txt on the API host decides.** This workstation's fetcher already
   refuses to fetch what a provider's robots.txt disallows to our User-Agent
   (ADR 0004); a connector whose host disallows us is dead on arrival and must
   be replaced, not special-cased.
3. **Failures must say what happened.** "0 results" for a 429 or a blocked
   host is exactly the empty-looking success the spec forbids.

## Decision

**1. Eight keyless connectors, one registry, every fact verified live first.**

| connector   | domain     | API host                            | robots.txt verdict (live 2026-10-26)                     |
|-------------|------------|-------------------------------------|----------------------------------------------------------|
| `github`    | coding     | `api.github.com`                    | 404 → no robots file → permitted                        |
| `stackoverflow` | coding | `api.stackexchange.com`             | 400 → no usable robots file → permitted                 |
| `openalex`  | knowledge  | `api.openalex.org`                  | 200, `User-agent: *` with no Disallow → permitted       |
| `crossref`  | knowledge  | `api.crossref.org`                  | 404 → no robots file → permitted                        |
| `europepmc` | medical    | `www.ebi.ac.uk`                     | 200, `can_fetch(our UA) = True` → permitted             |
| `usgs`      | earth      | `earthquake.usgs.gov`               | 404 → no robots file → permitted                        |
| `weather`   | weather    | `api.met.no` (forecast) + `geocoding-api.open-meteo.com` (geocoding) | 200, `User-agent: *` with empty `Disallow:` → permitted; geocoding host 404 → permitted |
| `frankfurter` | finance  | `api.frankfurter.dev`               | 200, `User-agent: *` with no Disallow → permitted       |

Verified facts behind the table (each read from the server, never assumed):

- **GitHub search limit is 10 requests/minute** (`x-ratelimit-limit: 10` on the
  search endpoint) — not the 60/hour core-API figure commonly quoted.
- **Stack Exchange reports quota in the body** (`quota_max`/`quota_remaining`);
  it reports no result `total`, so `total` stays `None`.
- **USGS event metadata reports no result count** → `total` stays `None`.
- **Page caps:** every paginating connector caps requests at 100 (verified as
  the live maximum); `weather` returns one location/one forecast (`max 1`).
- **Crossref and GitHub return JSON with no `Accept` header**; Europe PMC
  query errors arrive as **HTTP 200 with `errCode`/`errMsg`**, so they are
  classified `invalid_query`, not `http_error`.
- **OpenAlex abstracts** are served as inverted indexes and are reconstructed
  into readable text; absent indexes yield `None`, never a fabricated summary.

**2. Excluded providers, and their replacements.** Three otherwise-attractive
providers disallow our User-Agent on their API host and are therefore out:

| excluded host              | robots.txt (live)                | replacement                                        |
|----------------------------|----------------------------------|----------------------------------------------------|
| `eutils.ncbi.nlm.nih.gov`  | `User-agent: *` / `Disallow: /`  | Europe PMC (medical)                               |
| `export.arxiv.org`         | `User-agent: *` / `Disallow: /`  | OpenAlex + Crossref (scholarship)                  |
| `api.open-meteo.com` (forecast) | `User-agent: *` / `Disallow: /` | MET Norway forecast; open-meteo's *geocoding* host (no disallow) still used for place lookup |

No special case, no exception: if robots forbids us, the connector does not
exist.

**3. Rate-limit policy: 429-only retries, `Retry-After` only, bounded waits.**
- Retry **on HTTP 429 only**, at most **2 retries** per request. Other errors
  fail immediately with their real status.
- The only wait instruction honored is **`Retry-After`** (delay-seconds or
  HTTP-date, RFC 7231); absent → 1 s default; a wait over **10 s** fails as
  `rate_limited` instead of sleeping.
- **`x-ratelimit-reset` is never interpreted** — its semantics differ per
  provider (GitHub: epoch seconds; OpenAlex: relative seconds — both verified
  live), so any interpretation would be a guess. It is displayed nowhere.
- Stack Exchange's **`backoff` envelope** is honored up to **60 s**; a larger
  request fails as `rate_limited` rather than sleeping unbounded.
- Beyond server instructions, every connector keeps a local politeness floor
  (`min_interval_seconds = 1.0`), enforced inside the shared `SafeFetcher`.

**4. One contract for all connectors.**
- Failures are `ConnectorError(reason, detail)` with stable reasons —
  `rate_limited`, `blocked`, `http_error`, `fetch_failed`, `invalid_response`,
  `invalid_query`, `unknown_connector` — and a human-true `detail`. An unknown
  connector name fails listing the real names; it is never answered with an
  empty result set.
- Results are `SearchResponse(requested, page_size, items, total, fetched_at,
  rate)`; **`total: None` means the provider reported no count**, distinct
  from a measured `0`. `rate` records who reported the quota (`headers` or
  `body`) — we never infer it.
- All connector HTTP goes through `SafeFetcher` (SSRF gate, robots check,
  content-type allowlist, 1 MiB response cap). Long snippets are trimmed with
  a disclosed `snippet: "N of M chars"` fact, never silently.

**5. Connectors search; they do not reason.** Synthesis over search results
stays in the model gateway (ADR 0003). Connectors are retrieval-only, so a
missing model key can never masquerade as a failed search, and vice versa.

## Consequences

- `synapse search <connector> <query>` works today against eight providers
  with zero API keys; the registry is the single place to add a ninth.
- A blocked host, a rate limit, or a malformed response reaches the user as a
  typed reason with the server's real detail — never as an empty table.
- Every wait is bounded (≤ 10 s per 429, ≤ 60 s per backoff envelope), so no
  search can hang the CLI or a dashboard request indefinitely.
- The robots table must be re-verified when a provider is added; the exclusion
  list is a standing reminder that "free API" does not mean "allowed API".
- Tests (`tests/test_connectors.py`) pin the parsers, the honesty rules
  (`total=None` vs `0`, trim disclosure), the 429/backoff pacing with an
  injected `sleep`, and the typed-failure paths — all without touching the
  network.
