"use client";

import { useQueryClient } from "@tanstack/react-query";
import { useRef, useState } from "react";

import type { Message } from "./client";
import { queryKeys } from "./queries";
import { streamPrompt } from "./stream";

type RunState = {
  running: boolean;
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
  messages: [],
  draft: null,
  pendingTools: new Set(),
  toolOutput: new Map(),
  error: null,
};

/** Run prompts against one session, exposing the live state of the current run. */
export function useAgentRun(sessionId: string) {
  const queryClient = useQueryClient();
  const [state, setState] = useState<RunState>(idle);
  const controller = useRef<AbortController | null>(null);

  async function send(content: string) {
    if (controller.current) return;
    const abort = new AbortController();
    controller.current = abort;
    setState({ ...idle, running: true });

    let error: string | null = null;
    try {
      for await (const event of streamPrompt(sessionId, content, abort.signal)) {
        setState((s) => {
          switch (event.type) {
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
        });
      }
    } catch (e) {
      if (!abort.signal.aborted) error = e instanceof Error ? e.message : String(e);
    } finally {
      controller.current = null;
      // The server saved every finished message; show its transcript, then drop the live copy.
      await Promise.all([
        queryClient.refetchQueries({ queryKey: queryKeys.session(sessionId) }),
        queryClient.invalidateQueries({ queryKey: queryKeys.sessions, exact: true }),
      ]);
      setState({ ...idle, error });
    }
  }

  function stop() {
    controller.current?.abort();
  }

  return { ...state, send, stop };
}
