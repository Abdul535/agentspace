"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, type Message } from "./client";
import { queryKeys } from "./queries";
import { type RunStreamEvent, streamPrompt, watchRun } from "./stream";

type RunState = {
  running: boolean;
  /** The transcript a `run_snapshot` gave when attaching; shown instead of the fetched one until the run ends. */
  base: Message[] | null;
  /** Messages finished during this run, shown until the refetched transcript has them. */
  messages: Message[];
  /** Text of the assistant message being streamed; null when none is streaming. */
  draft: string | null;
  /** Tool calls currently executing, by call id. */
  pendingTools: Set<string>;
  /** Latest live output of running tool calls (e.g. bash so far), by call id. Each update replaces the last. */
  toolOutput: Map<string, string>;
  error: string | null;
};

const idle: RunState = {
  running: false,
  base: null,
  messages: [],
  draft: null,
  pendingTools: new Set(),
  toolOutput: new Map(),
  error: null,
};

// Applies one streamed event to the live run state.
function reduce(s: RunState, event: RunStreamEvent): RunState {
  switch (event.type) {
    case "run_snapshot": {
      const draft = event.streaming
        ? event.streaming.content.flatMap((block) => (block.type === "text" ? [block.text] : [])).join("")
        : null;
      return { ...s, base: event.messages, messages: [], draft, pendingTools: new Set(event.pending_tools) };
    }
    case "message_start":
      return event.message.role === "assistant" ? { ...s, draft: "" } : s;
    case "message_update":
      return event.event.type === "text_delta" ? { ...s, draft: (s.draft ?? "") + event.event.delta } : s;
    case "message_end":
      return { ...s, messages: [...s.messages, event.message], draft: null };
    case "tool_execution_start":
      return { ...s, pendingTools: new Set(s.pendingTools).add(event.tool_call_id) };
    case "tool_execution_update": {
      const text = event.partial.content.map((block) => block.text).join("");
      return { ...s, toolOutput: new Map(s.toolOutput).set(event.tool_call_id, text) };
    }
    case "tool_execution_end": {
      const pendingTools = new Set(s.pendingTools);
      pendingTools.delete(event.result.tool_call_id);
      // The result message replaces the live output.
      const toolOutput = new Map(s.toolOutput);
      toolOutput.delete(event.result.tool_call_id);
      return { ...s, pendingTools, toolOutput };
    }
    default:
      return s;
  }
}

/**
 * Run prompts against one session and show its live run.
 *
 * The server owns runs (ADR-007): leaving the page only stops listening, and
 * `serverRunning` (from the fetched session) reattaches to a run already going.
 */
export function useAgentRun(sessionId: string, serverRunning: boolean) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<RunState>(idle);
  // The connection this hook is listening on; null when not listening.
  const controller = useRef<AbortController | null>(null);

  // Reads one run stream into state; when it ends, swaps the live copy for the saved transcript.
  async function listen(open: (signal: AbortSignal) => AsyncGenerator<RunStreamEvent>) {
    if (controller.current) return;
    const abort = new AbortController();
    controller.current = abort;
    setState({ ...idle, running: true });

    let error: string | null = null;
    try {
      for await (const event of open(abort.signal)) {
        setState((s) => reduce(s, event));
      }
    } catch (e) {
      if (!abort.signal.aborted) error = e instanceof Error ? e.message : String(e);
    } finally {
      // Stopped listening on purpose (unmounted): another listener may already own the state.
      if (controller.current === abort) {
        controller.current = null;
        await Promise.all([
          queryClient.refetchQueries({ queryKey: queryKeys.session(sessionId) }),
          queryClient.invalidateQueries({ queryKey: queryKeys.sessions, exact: true }),
        ]);
        setState({ ...idle, error });
      }
    }
  }

  // Attach to a run the server already has, e.g. after a reload or from a second tab.
  // An effect, not a query: it subscribes to a stream rather than fetching data.
  useEffect(() => {
    if (!serverRunning || controller.current) return;
    void listen((signal) => watchRun(sessionId, signal));
    // Set synchronously by `listen`; the cast undoes TS narrowing it to null from the check above.
    const mine = controller.current as AbortController | null;
    return () => {
      if (controller.current !== mine) return;
      mine?.abort();
      controller.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- `listen` is new each render; attach only when this flips.
  }, [serverRunning, sessionId]);

  // Leaving the page stops listening, never the run.
  useEffect(() => () => controller.current?.abort(), []);

  function send(content: string) {
    void listen((signal) => streamPrompt(sessionId, content, signal));
  }

  // Stop the run on the server; the stream then ends on its own with `agent_end` (reason "aborted").
  async function stop() {
    // A 409 means the run already ended, and its stream is closing anyway.
    await api.POST("/sessions/{session_id}/abort", { params: { path: { session_id: sessionId } } });
  }

  return { ...state, send, stop };
}
