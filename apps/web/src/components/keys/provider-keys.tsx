"use client";

import { CircleAlert, CircleCheck, ExternalLink, KeyRound, Loader2, Trash2 } from "lucide-react";
import { useState } from "react";

import { Badge } from "@/components/ui/badge";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import type { ProviderKey } from "@/lib/api/client";
import { useDeleteProviderKey, useProviderKeys, useSaveProviderKey } from "@/lib/api/queries";

// Where each provider hands out keys, for the "get a key" link.
const KEY_PAGES: Record<string, string> = { openrouter: "https://openrouter.ai/settings/keys" };

// The API keys page: one card per provider with the user's saved key, and add, replace and remove (ADR-026).
export function ProviderKeys() {
  const keys = useProviderKeys();

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 p-6">
      <header className="flex flex-col gap-1">
        <h1 className="text-xl font-semibold tracking-tight text-balance">API keys</h1>
        <p className="text-sm text-pretty text-muted-foreground">
          Your sessions run on your own model account, so the provider bills you for what the agent uses. Keys are
          stored encrypted and never shown again after you save them.
        </p>
      </header>

      {keys.isPending && <Skeleton className="h-40 w-full" />}
      {keys.isError && (
        <p className="flex items-center gap-2 text-sm text-destructive">
          <CircleAlert className="size-4" />
          Can&apos;t load your keys: {keys.error.message}
        </p>
      )}
      <ul className="flex flex-col gap-3">
        {keys.data?.map((entry) => (
          <KeyCard key={entry.provider} entry={entry} />
        ))}
      </ul>
    </div>
  );
}

// One provider: the saved key's last four characters, a field to add or replace it, and remove.
function KeyCard({ entry }: { entry: ProviderKey }) {
  const save = useSaveProviderKey();
  const remove = useDeleteProviderKey();
  const [value, setValue] = useState("");
  const [confirming, setConfirming] = useState(false);
  const saved = entry.last4 !== null;
  const fieldId = `key-${entry.provider}`;
  const keyPage = KEY_PAGES[entry.provider];
  const canSave = value.trim().length >= 8 && !save.isPending;

  function submit() {
    if (!canSave) return;
    remove.reset();
    save.mutate({ provider: entry.provider, apiKey: value }, { onSuccess: () => setValue("") });
  }

  return (
    <li className="flex flex-col gap-4 border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 items-center gap-3">
          <span className="flex size-[34px] shrink-0 items-center justify-center border bg-muted">
            <KeyRound className="size-4" strokeWidth={1.75} />
          </span>
          <div className="flex min-w-0 flex-col gap-0.5">
            <span className="text-sm font-semibold">{entry.label}</span>
            {saved ? (
              <span className="text-xs text-muted-foreground">
                Saved{" "}
                {entry.updated_at && <time dateTime={entry.updated_at}>{formatDate(entry.updated_at)}</time>}
              </span>
            ) : (
              <span className="text-xs text-muted-foreground">No key yet: your sessions can&apos;t run.</span>
            )}
          </div>
        </div>
        {saved ? (
          <Badge variant="outline" className="font-mono" aria-label={`Saved key ending in ${entry.last4}`}>
            •••• {entry.last4}
          </Badge>
        ) : (
          <Badge variant="destructive">Missing</Badge>
        )}
      </div>

      <form
        className="flex flex-col gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          submit();
        }}
      >
        <Label htmlFor={fieldId}>{saved ? "Replace with a new key" : `Your ${entry.label} API key`}</Label>
        <div className="flex gap-2">
          <Input
            id={fieldId}
            type="password"
            autoComplete="off"
            spellCheck={false}
            placeholder="sk-or-v1-…"
            value={value}
            onChange={(event) => {
              setValue(event.target.value);
              save.reset();
            }}
            className="font-mono"
            aria-describedby={`${fieldId}-hint`}
            aria-invalid={save.isError || undefined}
          />
          <Tooltip>
            {/* A span keeps the tooltip working: a disabled button gets no hover events. */}
            <TooltipTrigger asChild>
              <span tabIndex={canSave ? undefined : 0}>
                <Button type="submit" variant={saved ? "outline" : "default"} disabled={!canSave}>
                  {save.isPending && <Loader2 className="animate-spin" data-icon="inline-start" />}
                  {save.isPending ? "Checking…" : saved ? "Replace" : "Save key"}
                </Button>
              </span>
            </TooltipTrigger>
            <TooltipContent side="top">
              {save.isPending
                ? `Asking ${entry.label} whether the key works…`
                : canSave
                  ? `Checks the key with ${entry.label}, then saves it`
                  : "Paste a key first"}
            </TooltipContent>
          </Tooltip>
        </div>
        <p id={`${fieldId}-hint`} className="text-xs text-pretty text-muted-foreground">
          {keyPage && (
            <>
              Make one at{" "}
              <a href={keyPage} target="_blank" rel="noreferrer" className="inline-flex items-center gap-0.5 text-link underline-offset-4 hover:underline">
                {new URL(keyPage).host}
                <ExternalLink className="size-3" />
              </a>
              . Set a credit limit on it there to cap what the agent can spend.
            </>
          )}
        </p>
        <SaveResult label={entry.label} save={save} />
      </form>

      {saved && (
        <div className="flex items-center justify-end gap-2 border-t pt-3">
          {confirming ? (
            <>
              <span className="mr-auto text-xs text-pretty text-muted-foreground">
                Remove this key? Your sessions can&apos;t run until you add one again.
              </span>
              <Button variant="ghost" size="sm" onClick={() => setConfirming(false)} disabled={remove.isPending}>
                Cancel
              </Button>
              <Button
                variant="destructive"
                size="sm"
                disabled={remove.isPending}
                onClick={() => {
                  save.reset();
                  remove.mutate(entry.provider, { onSuccess: () => setConfirming(false) });
                }}
              >
                {remove.isPending && <Loader2 className="animate-spin" data-icon="inline-start" />}
                Remove key
              </Button>
            </>
          ) : (
            <Button variant="ghost" size="sm" className="text-destructive" onClick={() => setConfirming(true)}>
              <Trash2 data-icon="inline-start" />
              Remove
            </Button>
          )}
        </div>
      )}
      {remove.isError && (
        <p role="alert" className="text-sm text-destructive">
          Can&apos;t remove the key: {remove.error.message}
        </p>
      )}
    </li>
  );
}

// What the last save did: accepted, saved without a check, or refused (and why).
function SaveResult({ label, save }: { label: string; save: ReturnType<typeof useSaveProviderKey> }) {
  if (save.isError) {
    return (
      <p role="alert" className="flex items-start gap-1.5 text-sm text-destructive">
        <CircleAlert className="mt-0.5 size-4 shrink-0" />
        {save.error.message}
      </p>
    );
  }
  if (!save.isSuccess) return null;
  return save.data.checked ? (
    <p role="status" className="flex items-center gap-1.5 text-sm text-success">
      <CircleCheck className="size-4 shrink-0" />
      {label} accepted the key. Your sessions use it from the next message.
    </p>
  ) : (
    <p role="status" className="flex items-start gap-1.5 text-sm text-muted-foreground">
      <CircleAlert className="mt-0.5 size-4 shrink-0" />
      Saved, but {label} couldn&apos;t be reached to check it. If a run fails, check the key here.
    </p>
  );
}

// A saved time as a short local date, e.g. "Nov 21, 2026".
function formatDate(iso: string): string {
  return new Date(iso).toLocaleDateString(undefined, { month: "short", day: "numeric", year: "numeric" });
}
