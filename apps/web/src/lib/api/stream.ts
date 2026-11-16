import { type AgentEvent, errorMessage } from "./client";

/**
 * Send a prompt and yield the agent's events as they stream in.
 *
 * `POST /sessions/{id}/messages` answers with server-sent events. The browser's
 * EventSource only does GET, so this reads the fetch body and splits it into
 * events itself. Aborting `signal` closes the connection, which stops the run.
 */
export async function* streamPrompt(
  sessionId: string,
  content: string,
  signal: AbortSignal,
): AsyncGenerator<AgentEvent> {
  const response = await fetch(`/api/sessions/${encodeURIComponent(sessionId)}/messages`, {
    method: "POST",
    headers: { "content-type": "application/json", accept: "text/event-stream" },
    body: JSON.stringify({ content }),
    signal,
  });
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

function parseEvent(block: string): AgentEvent | null {
  const data = block
    .split("\n")
    .filter((line) => line.startsWith("data:"))
    .map((line) => line.slice(5).replace(/^ /, ""))
    .join("\n");
  return data ? (JSON.parse(data) as AgentEvent) : null;
}
