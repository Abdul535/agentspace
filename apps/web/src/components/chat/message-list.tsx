"use client";

import { AlertCircle } from "lucide-react";

import { ToolCallView } from "@/components/chat/tool-call";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import type { AssistantMessage, Message, ToolResultMessage } from "@/lib/api/client";

/** The transcript: user prompts, assistant replies with their tool calls inline. Tool results render inside their call. */
export function MessageList({
  messages,
  draft,
  pendingTools,
}: {
  messages: Message[];
  draft: string | null;
  pendingTools: Set<string>;
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
          return <AssistantReply key={index} message={message} results={results} pendingTools={pendingTools} />;
        }
        return null;
      })}
      {draft !== null && (
        <p className="text-pretty whitespace-pre-wrap">
          {draft}
          <span className="ml-0.5 inline-block h-4 w-1.5 animate-pulse bg-foreground/60 align-text-bottom" />
        </p>
      )}
    </div>
  );
}

function AssistantReply({
  message,
  results,
  pendingTools,
}: {
  message: AssistantMessage;
  results: Map<string, ToolResultMessage>;
  pendingTools: Set<string>;
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
          <p key={index} className="text-pretty whitespace-pre-wrap">
            {block.text}
          </p>
        ) : (
          <ToolCallView
            key={block.id}
            call={block}
            result={results.get(block.id)}
            pending={pendingTools.has(block.id)}
          />
        ),
      )}
    </div>
  );
}
