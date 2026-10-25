# ADR 0006: Serving the dashboard from the core, and how it authenticates

## Status

Accepted — 2026-10-25

## Context

The dashboard is a browser client of the core API, which already requires a
bearer token on every `/v1` route. That creates two questions a static build
of `dashboard/dist` cannot answer by itself:

1. How do the static assets reach the browser without a second server process?
2. How does the browser obtain the token, given that `EventSource` (the only
   browser-native SSE client) cannot send an `Authorization` header?

## Decision

- **The core serves the built assets itself.** `create_app` mounts
  `dashboard/dist/assets` at `/assets` and serves `index.html` at `/` plus a
  catch-all so SPA client routing works. This kills the CORS problem by
  construction (same origin) and means `synapse dashboard` is a single
  process: no node, no reverse proxy, no vite dev server in the packaged flow.
- **Catch-all hygiene.** The fallback explicitly refuses paths under
  `v1/`, `health`, `ready`, `docs`, `openapi.json`, `redoc`, verifies the
  candidate file is inside `dashboard/dist` before serving it (path-traversal
  guard), and only returns `index.html` otherwise. FastAPI registers routes in
  definition order, so API routes win; the catch-all is registered last.
- **Token entry is explicit, not automatic.** The first page load shows a
  gate asking for the same bearer token the CLI prints (`synapse token`).
  It is stored in `localStorage` for that browser profile.
- **SSE carries the token as a query parameter.** Since `EventSource` cannot
  set headers, `/v1/events/stream` accepts `token` in the query string as an
  alternative to the header. This is a deliberate, accepted trade:
  - *Justification:* the core binds to loopback only, single-user, and query
    strings are not written to access logs in the default uvicorn config that
    ships with this project (access logging records the path, not params).
  - *Residual risk:* any other local process could read `/proc`-style command
    lines or a browser extension could leak the URL. For a loopback-bound
    single-user workstation this is the same threat surface the token file on
    disk already has, so the residual is accepted rather than silently taken.
  - *Improvement path:* migrate the dashboard to `fetch`-based SSE streaming
    (headers supported), replacing `EventSource`. Not done yet; recorded here
    so it happens on purpose, not by accident.

## Consequences

- `synapse dashboard` starts the core (if needed), rebuilds nothing, and
  simply opens a browser on the core's index — one command to a live UI.
- If `dashboard/dist` has not been built, the API still works; the app logs a
  clear warning and `/` returns 404 instead of a broken page.
- The dashboard cannot show a number that was not measured: it renders
  whatever `null`-honest contracts the backend returns, and every route is
  still 401 without the token, verified by tests (`tests/test_dashboard.py`).
