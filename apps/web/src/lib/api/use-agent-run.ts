"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef, useState } from "react";

import { api, errorMessage, type Message } from "./client";
import { textOf } from "./content";
import { queryKeys } from "./queries";
import { type RunStreamEvent, streamCompact, streamContinue, streamPrompt, watchRun } from "./stream";

/** Where a message typed during a run goes: after the current turn, or once the run would stop. */
export type QueueKind = "steer" | "follow_up";

/** A message queued on the running agent that it hasn't picked up yet. */
export type QueuedMessage = { kind: QueueKind; content: string };

type RunState = {
  running: boolean;
  /** The transcript a `run_snapshot` gave when attaching; shown instead of the fetched one until the run ends. */
  base: Message[] | null;
  /** Messages finished during this run, shown until the refetched transcript has them. */
  messages: Message[];
  /** Text of the assistant message being streamed; null when none is streaming. */
  draft: string | null;
  /** Reasoning of the assistant message being streamed, which arrives before its text (ADR-012); null when none. */
  draftThinking: string | null;
  /** Tool calls currently executing, by call id. */
  pendingTools: Set<string>;
  /** Latest live output of running tool calls (e.g. bash so far), by call id. Each update replaces the last. */
  toolOutput: Map<string, string>;
  /** The run is summarizing older messages (ADR-009). */
  compacting: boolean;
  error: string | null;
};

const idle: RunState = {
  running: false,
  base: null,
  messages: [],
  draft: null,
  draftThinking: null,
  pendingTools: new Set(),
  toolOutput: new Map(),
  compacting: false,
  error: null,
};

// Applies one streamed event to the live run state.
function reduce(s: RunState, event: RunStreamEvent): RunState {
  switch (event.type) {
    case "run_snapshot": {
      const content = event.streaming?.content ?? [];
      const draft = event.streaming
        ? content.flatMap((block) => (block.type === "text" ? [block.text] : [])).join("")
        : null;
      const thinking = content.flatMap((block) => (block.type === "thinking" ? [block.thinking] : [])).join("");
      return {
        ...s,
        base: event.messages,
        messages: [],
        draft,
        draftThinking: thinking || null,
        pendingTools: new Set(event.pending_tools),
      };
    }
    case "message_start":
      return event.message.role === "assistant" ? { ...s, draft: "", draftThinking: null } : s;
    case "message_update":
      if (event.event.type === "text_delta") return { ...s, draft: (s.draft ?? "") + event.event.delta };
      if (event.event.type === "thinking_delta") {
        return { ...s, draftThinking: (s.draftThinking ?? "") + event.event.delta };
      }
      return s;
    case "message_end":
      return { ...s, messages: [...s.messages, event.message], draft: null, draftThinking: null };
    case "tool_execution_start":
      return { ...s, pendingTools: new Set(s.pendingTools).add(event.tool_call_id) };
    case "tool_execution_update": {
      const text = textOf(event.partial.content);
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
    case "compaction_start":
      return { ...s, compacting: true };
    case "compaction_end":
      return { ...s, compacting: false };
    default:
      return s;
  }
}

/**
 * Run prompts against one session and show its live run.
 *
 * The server owns runs (ADR-007): leaving the page only stops listening, and
 * `serverRunning` (from the fetched session) reattaches to a run already going.
 * Messages queued during the run that it never picked up go back through `onUnsent`.
 */
export function useAgentRun(sessionId: string, serverRunning: boolean, onUnsent: (texts: string[]) => void) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<RunState>(idle);
  // A ref as well as state: `listen` reads the latest list when the run ends, after many renders.
  const queuedRef = useRef<QueuedMessage[]>([]);
  const [queued, setQueued] = useState<QueuedMessage[]>([]);
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
        // A queued message the agent picked up comes back as a user `message_end`: it's no longer queued.
        if (event.type === "message_end" && event.message.role === "user") {
          const content = event.message.content;
          const index = queuedRef.current.findIndex((q) => q.content === content);
          if (index !== -1) updateQueued(queuedRef.current.toSpliced(index, 1));
        }
        // A failed compaction leaves no message behind, so its reason is kept to show once the run ends.
        if (event.type === "compaction_end" && event.error) error = event.error;
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
        // The run ended (max turns, error, stop) before reaching these: hand them back instead of losing them.
        if (queuedRef.current.length > 0) onUnsent(queuedRef.current.map((q) => q.content));
        updateQueued([]);
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

  // `images` are data URLs from the composer (ADR-015).
  function send(content: string, images: string[] = []) {
    void listen((signal) => streamPrompt(sessionId, content, images, signal));
  }

  // Have the agent answer the transcript as it stands, e.g. after an error, a stop or a server restart.
  function resume() {
    void listen((signal) => streamContinue(sessionId, signal));
  }

  // Summarize the older part of the transcript now, instead of waiting for it to near the window.
  function compact() {
    void listen((signal) => streamCompact(sessionId, signal));
  }

  function updateQueued(next: QueuedMessage[]) {
    queuedRef.current = next;
    setQueued(next);
  }

  // Queue a message on the running agent; a 409 means the run is ending and won't pick it up.
  async function queue(kind: QueueKind, content: string) {
    // Listed before the request: the agent may pick it up before the response arrives.
    const item = { kind, content };
    updateQueued([...queuedRef.current, item]);
    const path = kind === "steer" ? "/sessions/{session_id}/steer" : "/sessions/{session_id}/follow-up";
    const { error } = await api.POST(path, { params: { path: { session_id: sessionId } }, body: { content } });
    if (error) {
      updateQueued(queuedRef.current.filter((q) => q !== item));
      onUnsent([content]);
      setState((s) => ({ ...s, error: errorMessage(error) }));
    }
  }

  // Stop the run on the server; the stream then ends on its own with `agent_end` (reason "aborted").
  async function stop() {
    // A 409 means the run already ended, and its stream is closing anyway.
    await api.POST("/sessions/{session_id}/abort", { params: { path: { session_id: sessionId } } });
  }

  return { ...state, queued, send, queue, stop, resume, compact };
}
