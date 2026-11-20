import { type AgentEvent, errorMessage, type RunSnapshot } from "./client";

/** What the run streams carry: agent events, plus a snapshot first when attaching. */
export type RunStreamEvent = AgentEvent | RunSnapshot;

/**
 * Send a prompt and yield the run's events as they stream in.
 *
 * The server owns the run (ADR-007): aborting `signal` only stops listening.
 * Stop the run itself with `POST /sessions/{id}/abort`.
 */
export async function* streamPrompt(
  sessionId: string,
  content: string,
  signal: AbortSignal,
): AsyncGenerator<RunStreamEvent> {
  yield* readEvents(
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
      method: "POST",
      headers: { "content-type": "application/json", accept: "text/event-stream" },
      body: JSON.stringify({ content }),
      signal,
    }),
  );
}

/** Continue the transcript as it is, with no new message (a retry); streams like `streamPrompt`. */
export async function* streamContinue(sessionId: string, signal: AbortSignal): AsyncGenerator<RunStreamEvent> {
  yield* readEvents(
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/continue`, {
      method: "POST",
      headers: { accept: "text/event-stream" },
      signal,
    }),
  );
}

/** Summarize the older part of the transcript now (ADR-009); streams like `streamPrompt`. */
export async function* streamCompact(sessionId: string, signal: AbortSignal): AsyncGenerator<RunStreamEvent> {
  yield* readEvents(
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/compact`, {
      method: "POST",
      headers: { accept: "text/event-stream" },
      signal,
    }),
  );
}

/** Attach to a session's run: a `run_snapshot` first, then live events until the run ends. */
export async function* watchRun(sessionId: string, signal: AbortSignal): AsyncGenerator<RunStreamEvent> {
  yield* readEvents(
    await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/events`, {
      headers: { accept: "text/event-stream" },
      signal,
    }),
  );
}

// Splits a fetch body into SSE events: EventSource can't POST, and fetch also lets us read error bodies.
async function* readEvents(response: Response): AsyncGenerator<RunStreamEvent> {
  if (!response.ok || !response.body) {
    const body = await response.json().catch(() => null);
    throw new Error(body ? errorMessage(body) : `HTTP ${response.status}`);
  }

  const reader = response.body.pipeThrough(new TextDecoderStream()).getReader();
  let buffer = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buffer += value.replaceAll("\r\n", "\n");
    let end: number;
    while ((end = buffer.indexOf("\n\n")) !== -1) {
      const event = parseEvent(buffer.slice(0, end));
      buffer = buffer.slice(end + 2);
      if (event) yield event;
    }
  }
}

// One SSE block's `data` lines, joined and parsed.
function parseEvent(block: string): RunStreamEvent | null {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  return data ? (JSON.parse(data) as RunStreamEvent) : null;
}
