import { useState, useRef, useEffect } from "react";
import { ask, ingestUrl, ApiError } from "../api";

/**
 * The bottom command bar — the dashboard half of the "one client, two surfaces"
 * idea. It does not have its own backend: it POSTs the same `/v1/jobs` request
 * the `synapse` CLI sends, so a prompt typed here and a prompt typed in any
 * terminal land in the same queue and are executed by the same worker.
 *
 * Two modes, matching the two tasks that exist:
 * - **Ask** — queue an `ask` job (routes through the model gateway).
 * - **Ingest** — queue an `ingest_url` job (SSRF-safe fetcher + knowledge layer).
 *
 * Nothing here reports a result. It reports the job id and status the core
 * returned, because that is all that is known at submit time.
 */

type Mode = "ask" | "ingest";

const MODE_COPY: Record<Mode, { label: string; glyph: string; placeholder: string; title: string }> = {
  ask: {
    label: "Ask",
    glyph: "✦",
    placeholder: "Ask anything… it queues a real job through the model gateway  ( / to focus )",
    title: "Queue an ask job",
  },
  ingest: {
    label: "Ingest",
    glyph: "⬇",
    placeholder: "Paste a URL to ingest into the knowledge layer (SSRF-safe fetcher)…",
    title: "Queue an ingest_url job",
  },
};

export default function PromptBar({ onSubmitted }: { onSubmitted: () => void }) {
  const [value, setValue] = useState("");
  const [mode, setMode] = useState<Mode>("ask");
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState<{ ok: boolean; text: string } | null>(null);
  const inputRef = useRef<HTMLInputElement>(null);

  // "/" focuses the bar from anywhere, mirroring the CLI's ergonomics. It must
  // not fire while the user is already typing in a field or a rich editor.
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      const typing =
        target?.tagName === "INPUT" ||
        target?.tagName === "TEXTAREA" ||
        target?.tagName === "SELECT" ||
        target?.isContentEditable === true;
      if (e.key === "/" && !typing) {
        e.preventDefault();
        inputRef.current?.focus();
      }
      if (e.key === "Escape" && target === inputRef.current) {
        inputRef.current?.blur();
      }
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  async function submit() {
    const trimmed = value.trim();
    if (!trimmed || busy) return;
    setBusy(true);
    setNotice(null);
    try {
      const copy = MODE_COPY[mode];
      const job = mode === "ask" ? await ask(trimmed) : await ingestUrl(trimmed);
      setNotice({ ok: true, text: `Queued ${copy.label.toLowerCase()} job ${job.id.slice(0, 8)} — ${job.status}` });
      setValue("");
      onSubmitted();
    } catch (error) {
      const message =
        error instanceof ApiError
          ? error.status === 401
            ? "Not authorised — check the token in Settings."
            : error.message
          : String(error);
      setNotice({ ok: false, text: message });
    } finally {
      setBusy(false);
    }
  }

  const copy = MODE_COPY[mode];

  return (
    <footer className="promptbar panel">
      {(Object.keys(MODE_COPY) as Mode[]).map((key) => {
        const item = MODE_COPY[key];
        return (
          <button
            key={key}
            className={`mode ${mode === key ? "active" : ""}`}
            onClick={() => setMode(key)}
            aria-pressed={mode === key}
            title={item.title}
          >
            <span aria-hidden="true">{item.glyph}</span> {item.label}
          </button>
        );
      })}

      <input
        ref={inputRef}
        value={value}
        aria-label={mode === "ask" ? "Ask the core a question" : "URL to ingest"}
        aria-keyshortcuts="/"
        placeholder={copy.placeholder}
        autoComplete="off"
        spellCheck={false}
        onChange={(e) => setValue(e.target.value)}
        onKeyDown={(e) => {
          if (e.key === "Enter") void submit();
        }}
      />

      {notice && (
        <span
          className="prompt-hint"
          role="status"
          aria-live="polite"
          style={{ color: notice.ok ? "var(--good)" : "var(--bad)" }}
          title={notice.text}
        >
          {notice.text}
        </span>
      )}

      <button
        className="send"
        onClick={() => void submit()}
        disabled={!value.trim() || busy}
        title={mode === "ask" ? "Queue this prompt as a real job" : "Queue this URL as an ingest job"}
      >
        {busy ? "Queueing…" : "Send"}
      </button>
    </footer>
  );
}
