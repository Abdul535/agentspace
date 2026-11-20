"use client";

import { code } from "@streamdown/code";
import { AlertCircle, ChevronRight } from "lucide-react";
import { Streamdown } from "streamdown";

import { ToolCallView } from "@/components/chat/tool-call";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { AssistantMessage, CompactionSummaryMessage, Message, ToolResultMessage } from "@/lib/api/client";

// Shiki highlighting for fenced code; defined once so Streamdown's memo isn't broken by a new object each render.
const plugins = { code };

/** The transcript: user prompts, assistant replies with their tool calls inline, and notices as dividers. Tool results render inside their call. */
export function MessageList({
  messages,
  draft,
  pendingTools,
  toolOutput,
}: {
  messages: Message[];
  draft: string | null;
  pendingTools: Set<string>;
  toolOutput: Map<string, string>;
}) {
  const results = new Map<string, ToolResultMessage>();
  for (const message of messages) {
    if (message.role === "tool_result") results.set(message.tool_call_id, message);
  }

  return (
    <div className="flex flex-col gap-6">
      {messages.map((message, index) => {
        if (message.role === "user") {
          return (
            <div
              key={index}
              className="max-w-[85%] self-end rounded-2xl bg-primary px-4 py-2.5 text-pretty whitespace-pre-wrap text-primary-foreground"
            >
              {message.content}
            </div>
          );
        }
        if (message.role === "assistant") {
          return (
            <AssistantReply
              key={index}
              message={message}
              results={results}
              pendingTools={pendingTools}
              toolOutput={toolOutput}
            />
          );
        }
        if (message.role === "notice") {
          return <Notice key={index} text={message.text} />;
        }
        if (message.role === "compaction_summary") {
          return <CompactionDivider key={index} message={message} />;
        }
        return null;
      })}
      {draft !== null && (
        <Streamdown plugins={plugins} caret="block" isAnimating>
          {draft}
        </Streamdown>
      )}
    </div>
  );
}

// A line for the reader, such as a model switch; the model never sees it.
function Notice({ text }: { text: string }) {
  return (
    <div role="note" className="flex items-center gap-3 text-xs text-muted-foreground">
      <div className="h-px flex-1 bg-border" />
      <span className="text-balance text-center">{text}</span>
      <div className="h-px flex-1 bg-border" />
    </div>
  );
}

// Where older messages were summarized (ADR-009). They stay visible above it; the model now reads the summary.
function CompactionDivider({ message }: { message: CompactionSummaryMessage }) {
  return (
    <Collapsible className="flex flex-col gap-2">
      <CollapsibleTrigger className="group flex items-center gap-3 text-xs text-muted-foreground hover:text-foreground">
        <div className="h-px flex-1 bg-border" />
        <span className="flex items-center gap-1">
          <ChevronRight className="size-3.5 transition-transform group-data-[state=open]:rotate-90" />
          {/* `tokens_before` is the whole context at the time (system prompt and tools too), not just what was summarized. */}
          Conversation compacted · context was about {message.tokens_before.toLocaleString()} tokens
        </span>
        <div className="h-px flex-1 bg-border" />
      </CollapsibleTrigger>
      <CollapsibleContent className="rounded-lg border bg-card px-4 py-3 text-sm">
        <Streamdown plugins={plugins}>{message.summary}</Streamdown>
      </CollapsibleContent>
    </Collapsible>
  );
}

function AssistantReply({
  message,
  results,
  pendingTools,
  toolOutput,
}: {
  message: AssistantMessage;
  results: Map<string, ToolResultMessage>;
  pendingTools: Set<string>;
  toolOutput: Map<string, string>;
}) {
  if (message.stop_reason === "error" || message.stop_reason === "aborted") {
    return (
      <Alert variant="destructive">
        <AlertCircle />
        <AlertTitle>{message.stop_reason === "aborted" ? "Stopped" : "The model request failed"}</AlertTitle>
        {message.error_message && <AlertDescription>{message.error_message}</AlertDescription>}
      </Alert>
    );
  }

  return (
    <div className="flex flex-col gap-2">
      {message.content.map((block, index) =>
        block.type === "text" ? (
          <Streamdown key={index} plugins={plugins} mode="static">
            {block.text}
          </Streamdown>
        ) : (
          <ToolCallView
            key={block.id}
            call={block}
            result={results.get(block.id)}
            pending={pendingTools.has(block.id)}
            liveOutput={toolOutput.get(block.id)}
          />
        ),
      )}
    </div>
  );
}
