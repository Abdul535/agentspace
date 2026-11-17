"use client";

import { ChevronsUpDown, LoaderCircle } from "lucide-react";
import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Command,
  CommandEmpty,
  CommandGroup,
  CommandInput,
  CommandItem,
  CommandList,
} from "@/components/ui/command";
import { Popover, PopoverContent, PopoverTrigger } from "@/components/ui/popover";
import type { ModelInfo } from "@/lib/api/client";
import { useModels, useSetSessionModel } from "@/lib/api/queries";

/** The session's model, as a searchable picker that switches it for the next run (ADR-005). */
export function ModelPicker({ sessionId, model, running }: { sessionId: string; model: string; running: boolean }) {
  const [open, setOpen] = useState(false);
  const catalog = useModels();
  const setModel = useSetSessionModel(sessionId);

  const models = catalog.data?.models ?? [];
  const favoriteIds = new Set(catalog.data?.favorites ?? []);
  // Configured order, and only ids the catalog knows, so a stale favourite doesn't show up as a blank row.
  const favorites = (catalog.data?.favorites ?? []).flatMap((id) => models.find((m) => m.id === id) ?? []);
  // Favourites are left out of "All models": cmdk needs each item's value to be unique.
  const rest = models.filter((m) => !favoriteIds.has(m.id));

  function select(id: string) {
    if (id === model) return setOpen(false);
    setModel.mutate(id, { onSuccess: () => setOpen(false) });
  }

  return (
    <Popover open={open} onOpenChange={setOpen}>
      <PopoverTrigger asChild>
        <Button
          variant="secondary"
          size="sm"
          className="ml-auto max-w-72 font-mono"
          disabled={running || setModel.isPending}
          title={running ? "Wait for the run to finish to switch models" : "Switch model"}
        >
          <span className="truncate">{setModel.isPending ? setModel.variables : model}</span>
          {setModel.isPending ? <LoaderCircle className="animate-spin" /> : <ChevronsUpDown />}
        </Button>
      </PopoverTrigger>
      <PopoverContent align="end" className="w-96 p-0">
        {/* Opens on the current model, so the list starts scrolled to it. */}
        <Command defaultValue={model}>
          <CommandInput placeholder="Search models…" />
          {setModel.isError && <p className="px-3 pt-2 text-xs text-destructive">{setModel.error.message}</p>}
          <CommandList>
            {catalog.isPending && <p className="py-6 text-center text-sm text-muted-foreground">Loading models…</p>}
            {catalog.isError && (
              <p className="px-3 py-6 text-center text-sm text-pretty text-destructive">{catalog.error.message}</p>
            )}
            {catalog.isSuccess && <CommandEmpty>No model matches.</CommandEmpty>}
            {favorites.length > 0 && (
              <CommandGroup heading="Favorites">
                {favorites.map((m) => (
                  <ModelItem key={m.id} model={m} current={m.id === model} onSelect={select} />
                ))}
              </CommandGroup>
            )}
            {rest.length > 0 && (
              <CommandGroup heading="All models">
                {rest.map((m) => (
                  <ModelItem key={m.id} model={m} current={m.id === model} onSelect={select} />
                ))}
              </CommandGroup>
            )}
          </CommandList>
        </Command>
      </PopoverContent>
    </Popover>
  );
}

// One row: name, then id and context size; prices on the right. The check mark comes from CommandItem's data-checked.
function ModelItem({ model, current, onSelect }: { model: ModelInfo; current: boolean; onSelect: (id: string) => void }) {
  return (
    <CommandItem value={model.id} keywords={[model.name]} data-checked={current} onSelect={onSelect}>
      <div className="min-w-0 flex-1">
        <p className="truncate">{model.name}</p>
        <p className="truncate font-mono text-xs text-muted-foreground">
          {model.id}
          {model.context_length ? ` · ${formatContext(model.context_length)}` : ""}
        </p>
      </div>
      <span
        className="shrink-0 text-xs text-muted-foreground tabular-nums"
        title="US dollars per million input / output tokens"
      >
        {formatPrice(model.input_price, model.output_price)}
      </span>
    </CommandItem>
  );
}

// 1048576 -> "1M", 200000 -> "200K".
function formatContext(tokens: number): string {
  return tokens >= 1_000_000 ? `${+(tokens / 1_000_000).toFixed(1)}M` : `${Math.round(tokens / 1000)}K`;
}

// "$3 / $15", "Free", or "Varies" for routers whose price depends on the model they pick.
function formatPrice(input: number | null, output: number | null): string {
  if (input === null || output === null) return "Varies";
  if (input === 0 && output === 0) return "Free";
  return `$${+input.toFixed(2)} / $${+output.toFixed(2)}`;
}
