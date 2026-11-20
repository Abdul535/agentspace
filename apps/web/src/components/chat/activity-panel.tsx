"use client";

import { AlertCircle, Bot, Check, ChevronRight, LoaderCircle, User, X } from "lucide-react";

import { ToolIcon } from "@/components/chat/tool-call";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { Message, ToolCall, ToolResultMessage } from "@/lib/api/client";
import { imagesOf, textOf } from "@/lib/api/content";
import { cn } from "@/lib/utils";

// null: no result and not running, e.g. the run was stopped before the tool ran.
type ToolStatus = "running" | "done" | "failed" | null;

// One row in the timeline: a message exchanged or a tool call made.
type Entry =
  | { kind: "user" | "assistant"; text: string }
  | { kind: "tool"; call: ToolCall; status: ToolStatus }
  | { kind: "error"; text: string };

/** Right-hand timeline of every message exchanged and tool call made in the session. */
export function ActivityPanel({
  messages,
  draft,
  pendingTools,
}: {
  messages: Message[];
  draft: string | null;
  pendingTools: Set<string>;
}) {
  const entries = buildEntries(messages, draft, pendingTools);
  const toolCount = entries.filter((entry) => entry.kind === "tool").length;
  const messageCount = entries.length - toolCount;

  return (
    <aside id="activity-panel" className="hidden w-80 shrink-0 flex-col border-l bg-muted/30 lg:flex">
      <header className="flex h-12 shrink-0 items-center gap-2 border-b px-4">
        <h2 className="text-sm font-medium">Activity</h2>
        <span className="ml-auto text-xs text-muted-foreground tabular-nums">
          {messageCount} {messageCount === 1 ? "message" : "messages"} · {toolCount}{" "}
          {toolCount === 1 ? "tool call" : "tool calls"}
        </span>
      </header>
      {entries.length === 0 ? (
        <p className="p-4 text-sm text-pretty text-muted-foreground">Messages and tool calls will show up here.</p>
      ) : (
        <ol className="flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto p-2">
          {entries.map((entry, index) => (
            <EntryRow key={index} entry={entry} />
          ))}
        </ol>
      )}
    </aside>
  );
}

function EntryRow({ entry }: { entry: Entry }) {
  if (entry.kind === "tool") {
    const summary = toolSummary(entry.call);
    const args = Object.entries(entry.call.arguments);
    return (
      <li>
        <Collapsible>
          <CollapsibleTrigger
            disabled={args.length === 0}
            className="group flex w-full items-start gap-2 rounded-md px-2 py-1.5 text-left text-sm transition-colors hover:bg-muted disabled:cursor-default disabled:hover:bg-transparent"
          >
            <ToolIcon name={entry.call.name} className="mt-0.5 size-4 shrink-0 text-muted-foreground" />
            <div className="min-w-0 flex-1">
              <p className="font-medium">{entry.call.name}</p>
              {summary && <p className="truncate font-mono text-xs text-muted-foreground">{summary}</p>}
            </div>
            <StatusIcon status={entry.status} />
            {args.length > 0 && (
              <ChevronRight className="mt-0.5 size-4 shrink-0 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
            )}
          </CollapsibleTrigger>
          <CollapsibleContent>
            <dl className="mx-2 mb-1.5 flex flex-col gap-2 rounded-md border bg-card p-2">
              {args.map(([key, value]) => (
                <div key={key} className="flex flex-col gap-0.5">
                  <dt className="text-xs text-muted-foreground">{key}</dt>
                  <dd className="max-h-40 overflow-auto font-mono text-xs break-all whitespace-pre-wrap">
                    {formatArg(value)}
                  </dd>
                </div>
              ))}
            </dl>
          </CollapsibleContent>
        </Collapsible>
      </li>
    );
  }

  const Icon = entry.kind === "user" ? User : entry.kind === "assistant" ? Bot : AlertCircle;
  const label = entry.kind === "user" ? "You" : entry.kind === "assistant" ? "Assistant" : "Error";
  return (
    <li className="flex items-start gap-2 rounded-md px-2 py-1.5 text-sm">
      <Icon
        className={cn("mt-0.5 size-4 shrink-0", entry.kind === "error" ? "text-destructive" : "text-muted-foreground")}
      />
      <div className="min-w-0 flex-1">
        <p className="font-medium">{label}</p>
        <p className="line-clamp-2 text-xs text-pretty break-words text-muted-foreground">{entry.text}</p>
      </div>
    </li>
  );
}

function StatusIcon({ status }: { status: ToolStatus }) {
  if (status === null) return null;
  if (status === "running") {
    return <LoaderCircle className="mt-0.5 size-4 shrink-0 animate-spin text-muted-foreground" aria-label="Running" />;
  }
  if (status === "failed") return <X className="mt-0.5 size-4 shrink-0 text-destructive" aria-label="Failed" />;
  return <Check className="mt-0.5 size-4 shrink-0 text-muted-foreground" aria-label="Done" />;
}

// Flattens the transcript into timeline rows; tool results become the status of their call, not rows of their own.
function buildEntries(messages: Message[], draft: string | null, pendingTools: Set<string>): Entry[] {
  const results = new Map<string, ToolResultMessage>();
  for (const message of messages) {
    if (message.role === "tool_result") results.set(message.tool_call_id, message);
  }

  const entries: Entry[] = [];
  for (const message of messages) {
    if (message.role === "user") {
      const images = imagesOf(message.content).length;
      // An image-only prompt still gets a line, so the panel shows that the user said something.
      const text = textOf(message.content) || (images === 1 ? "An image" : images ? `${images} images` : "");
      entries.push({ kind: "user", text });
    } else if (message.role === "assistant") {
      if (message.stop_reason === "error" || message.stop_reason === "aborted") {
        entries.push({
          kind: "error",
          text: message.error_message ?? (message.stop_reason === "aborted" ? "Stopped" : "The model request failed"),
        });
        continue;
      }
      for (const block of message.content) {
        // The panel tracks what the agent did; its reasoning stays in the chat.
        if (block.type === "thinking") continue;
        if (block.type === "text") {
          if (block.text.trim()) entries.push({ kind: "assistant", text: block.text });
          continue;
        }
        const result = results.get(block.id);
        const status = result ? (result.is_error ? "failed" : "done") : pendingTools.has(block.id) ? "running" : null;
        entries.push({ kind: "tool", call: block, status });
      }
    }
  }
  if (draft) entries.push({ kind: "assistant", text: draft });
  return entries;
}

// The one argument that best says what a call did: the shell command, the search pattern, or the path.
function toolSummary(call: ToolCall): string | null {
  for (const key of ["command", "pattern", "path"]) {
    const value = call.arguments[key];
    if (typeof value === "string") return value;
  }
  return null;
}

// Strings as-is so commands and file contents keep their line breaks; anything else as indented JSON.
function formatArg(value: unknown): string {
  return typeof value === "string" ? value : JSON.stringify(value, null, 2);
}
