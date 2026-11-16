"use client";

import { ArrowUp, Square } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Textarea } from "@/components/ui/textarea";

/** Prompt input. Enter sends, Shift+Enter adds a line; while a run is going the button stops it. */
export function Composer({
  running,
  onSend,
  onStop,
}: {
  running: boolean;
  onSend: (content: string) => void;
  onStop: () => void;
}) {
  const [value, setValue] = useState("");
  const canSend = !running && value.trim().length > 0;

  function submit() {
    if (!canSend) return;
    onSend(value.trim());
    setValue("");
  }

  return (
    <form
      className="relative"
      onSubmit={(event) => {
        event.preventDefault();
        submit();
      }}
    >
      <Textarea
        value={value}
        onChange={(event) => setValue(event.target.value)}
        onKeyDown={(event) => {
          if (event.key === "Enter" && !event.shiftKey && !event.nativeEvent.isComposing) {
            event.preventDefault();
            submit();
          }
        }}
        placeholder="Ask the agent to read or write files…"
        aria-label="Message"
        rows={3}
        className="max-h-60 resize-none pr-14"
        autoFocus
      />
      {running ? (
        <Button type="button" size="icon" variant="secondary" className="absolute right-2 bottom-2" onClick={onStop} aria-label="Stop">
          <Square className="fill-current" />
        </Button>
      ) : (
        <Button type="submit" size="icon" className="absolute right-2 bottom-2" disabled={!canSend} aria-label="Send">
          <ArrowUp />
        </Button>
      )}
    </form>
  );
}
