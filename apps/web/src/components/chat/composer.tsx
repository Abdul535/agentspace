"use client";

import { ArrowUp, Square } from "lucide-react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";
import type { QueuedMessage, QueueKind } from "@/lib/api/use-agent-run";

/**
 * Prompt input. Idle: Enter sends, Shift+Enter adds a line. While a run is going,
 * Enter steers it (read after the current turn) and Alt+Enter queues a follow-up
 * (read once it would stop); the button stops the run.
 */
export function Composer({
  value,
  onChange,
  running,
  queued,
  onSend,
  onQueue,
  onStop,
}: {
  value: string;
  onChange: (value: string) => void;
  running: boolean;
  queued: QueuedMessage[];
  onSend: (content: string) => void;
  onQueue: (kind: QueueKind, content: string) => void;
  onStop: () => void;
}) {
  const canSend = !running && value.trim().length > 0;

  // Sends or queues the typed text, depending on whether a run is going.
  function submit(kind: QueueKind) {
    const content = value.trim();
    if (!content) return;
    if (running) onQueue(kind, content);
    else onSend(content);
    onChange("");
  }

  return (
    <div className="flex flex-col gap-2">
      {queued.length > 0 && (
        <ul className="flex flex-col gap-1" aria-label="Queued messages">
          {queued.map((q, i) => (
            <li key={i} className="flex min-w-0 items-center gap-2 text-sm text-muted-foreground">
              <Badge variant="outline">{q.kind === "steer" ? "After this turn" : "When done"}</Badge>
              <span className="truncate">{q.content}</span>
            </li>
          ))}
        </ul>
      )}
      <form
        className="relative"
        onSubmit={(event) => {
          event.preventDefault();
          submit("steer");
        }}
      >
        <Textarea
          value={value}
          onChange={(event) => onChange(event.target.value)}
          onKeyDown={(event) => {
            if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
              event.preventDefault();
              submit(event.altKey ? "follow_up" : "steer");
            }
          }}
          placeholder={
            running
              ? "Steer the agent: Enter adds it after this turn, Alt+Enter when it's done…"
              : "Ask the agent to read or write files…"
          }
          aria-label="Message"
          rows={3}
          className="max-h-60 resize-none pr-14"
          autoFocus
        />
        {running ? (
          <Button
            type="button"
            size="icon"
            variant="secondary"
            className="absolute right-2 bottom-2"
            onClick={onStop}
            aria-label="Stop"
          >
            <Square className="fill-current" />
          </Button>
        ) : (
          <Button type="submit" size="icon" className="absolute right-2 bottom-2" disabled={!canSend} aria-label="Send">
            <ArrowUp />
          </Button>
        )}
      </form>
    </div>
  );
}
