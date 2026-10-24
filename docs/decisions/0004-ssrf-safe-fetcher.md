# ADR 0004: SSRF-safe, robots-aware outbound HTTP fetcher

## Status

Accepted — 2026-10-24

## Context

The knowledge layer ingests arbitrary URLs the user asks about (`ingest_url`
task). Fetching a user-supplied URL from a background service is the classic
SSRF entry point: a page that redirects to `169.254.169.254` (cloud metadata),
`127.0.0.1` (our own API), or a private LAN host turns a benign-looking
"read this page" into an internal-network probe. Just as serious for
ingestion: unbounded response bodies (memory exhaustion), non-text payloads,
and crawling that ignores `robots.txt` (which would make this tool a bad
neighbour on the open web).

## Decision

- **All outbound HTTP goes through `synapse.net`** (`SafeFetcher`). Task code
  never opens a raw socket.
- **Two gates before any byte is read** (OWASP SSRF Prevention Cheat Sheet):
  1. static URL validation — scheme must be `http`/`https`, port only `80`/`443`,
     userinfo in the URL is refused;
  2. DNS resolution with **per-IP public-routability checks** — if *any*
     resolved answer is private, loopback, link-local, reserved, or otherwise
     non-global, the host is refused outright (defeats round-robin rebinding).
- **Redirects are never followed automatically.** `follow_redirects=False`;
  each hop is re-validated by the same gate, so a public site cannot bounce us
  into an internal address. Capped at 5 hops.
- **Fail closed.** A DNS resolution error is a hard block, never a silent pass.
- **Bodies are capped while streaming** (5 MiB default) so a hostile page
  cannot force full buffering; a lying `Content-Length` over the cap is
  rejected before reading.
- **Content-type allowlist:** HTML, plain text, XML, RSS/Atom, JSON only.
- **`robots.txt` is enforced** for our honest User-Agent
  (`SynapseOne/0.1 ...; respects robots.txt`) before the real fetch, fetched
  through the same SSRF gate. Missing robots → allow (standard practice);
  `401`/`403` on robots → disallow (the polite reading); `404`/`5xx` → allow.
- **Per-host politeness delay** (1 s) between requests to the same host.
- **Extracted text is inert.** `html_text.py` uses stdlib `html.parser` — not
  regex stripping, not a rendering engine. Nothing executes scripts, renders
  HTML, or treats remote content as instructions (spec 15.3).

## Consequences

- The metadata-endpoint, loopback-probe, and LAN-scan SSRF classes are blocked
  at every hop, and there are tests proving each refusal (`tests/test_net.py`).
- Only public `http`/`https` on ports 80/443 can ever be fetched; internal
  tooling URLs are therefore unsupported by design for `ingest_url`.
- **Documented residual risk:** between validation and the TLS connect, a
  deliberately hostile authoritative DNS server could re-resolve the name to a
  private address (classic DNS-rebinding TOCTOU). For a loopback-bound,
  single-user local tool this is accepted; the layered mitigations above make
  exploitation require the user to knowingly fetch a hostile target.
- Politeness throttling adds ~1 s per host, which is the right trade for a
  tool that may crawl on a user's behalf.
- Ingestion is bounded (5 MiB fetched, 200k chars extracted), so a hostile page
  cannot bloat the database or a future prompt.
