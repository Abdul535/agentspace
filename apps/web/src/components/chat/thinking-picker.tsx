"use client";

import { Brain, LoaderCircle } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import { Command, CommandItem, CommandList } from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { ThinkingLevel } from "@/lib/api/client";
import { useModels, useUpdateSession } from "@/lib/api/queries";

// Every level the API takes, lightest first, with what each one asks of the model.
const LEVELS: { level: ThinkingLevel; label: string; hint: string }[] = [
  { level: "off", label: "Default", hint: "The model's own default; some models think anyway" },
  { level: "minimal", label: "Minimal", hint: "Barely think before answering" },
  { level: "low", label: "Low", hint: "Quick reasoning" },
  { level: "medium", label: "Medium", hint: "Balanced" },
  { level: "high", label: "High", hint: "Think hard; slower and uses more tokens" },
];

/** How hard the session's model thinks on the next run (ADR-012); shown only for models that can reason. */
export function ThinkingPicker({
  sessionId,
  model,
  level,
  running,
}: {
  sessionId: string;
  model: string;
  level: ThinkingLevel;
  running: boolean;
}) {
  const [open, setOpen] = useState(false);
  const catalog = useModels();
  const update = useUpdateSession(sessionId);

  const reasons = catalog.data?.models.find((m) => m.id === model)?.reasoning ?? false;
  // Still shown when a level is set, so a session moved to a model that can't reason can turn it back off.
  if (!reasons && level === "off") return null;

  const shown = (update.isPending ? update.variables.thinking_level : undefined) ?? level;
  const label = LEVELS.find((l) => l.level === shown)?.label ?? shown;

  function select(next: string) {
    if (next === level) return setOpen(false);
    update.mutate({ thinking_level: next as ThinkingLevel }, { onSuccess: () => setOpen(false) });
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="secondary"
          size="sm"
          disabled={running || update.isPending}
          title={running ? "Wait for the run to finish to change thinking" : "How hard the model thinks"}
        >
          {update.isPending ? <LoaderCircle className="animate-spin" /> : <Brain />}
          {label}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-72 p-0">
        <Command defaultValue={level}>
          {update.isError && <p className="px-3 pt-2 text-xs text-destructive">{update.error.message}</p>}
          <CommandList>
            {LEVELS.map((l) => (
              <CommandItem key={l.level} value={l.level} data-checked={l.level === level} onSelect={select}>
                <div className="min-w-0 flex-1">
                  <p>{l.label}</p>
                  <p className="text-xs text-pretty text-muted-foreground">{l.hint}</p>
                </div>
              </CommandItem>
            ))}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}
