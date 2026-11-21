"use client";

import { AlertCircle, FoldVertical, KeyRound, Loader2, PanelRightClose, PanelRightOpen, RotateCw } from "lucide-react";
import Link from "next/link";
import { useEffect, useRef, useState } from "react";

import { ActivityPanel } from "@/components/chat/activity-panel";
import { Composer } from "@/components/chat/composer";
import { MessageList } from "@/components/chat/message-list";
import { ModelPicker } from "@/components/chat/model-picker";
import { SessionCost } from "@/components/chat/session-cost";
import { ThinkingPicker } from "@/components/chat/thinking-picker";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { useModels, useProviderKeys, useSession } from "@/lib/api/queries";
import { useAgentRun } from "@/lib/api/use-agent-run";

export function Chat({ sessionId }: { sessionId: string }) {
  const session = useSession(sessionId);
  const [input, setInput] = useState("");
  // Queued messages the run never reached go back into the input, ahead of anything typed since.
  const run = useAgentRun(sessionId, session.data?.running ?? false, (texts) =>
    setInput((current) => [...texts, current].filter(Boolean).join("\n\n")),
  );
  // While attached to a run, its snapshot is the transcript; the fetched one may be missing what it saved since.
  const messages = [...(run.base ?? session.data?.messages ?? []), ...run.messages];
  // Closed by default so the chat gets the full width; the header button opens it.
  const [activityOpen, setActivityOpen] = useState(false);
  const catalog = useModels();
  // A model the catalog doesn't know (or a catalog that's down) may attach; the provider then decides.
  const canAttach = catalog.data?.models.find((m) => m.id === session.data?.model)?.image_input ?? true;
  // Runs are paid with the user's own key (ADR-026): with none saved, say so before they type, not after they send.
  const keys = useProviderKeys();
  const providerKey = keys.data?.find((k) => k.provider === session.data?.provider);
  const missingKey = providerKey && providerKey.last4 === null ? providerKey.label : null;

  // Keep the newest output in view while it streams, unless the reader scrolled up.
  const scroller = useRef<HTMLDivElement>(null);
  const liveOutputSize = [...run.toolOutput.values()].reduce((total, text) => total + text.length, 0);
  const contentSize =
    messages.length + (run.draft?.length ?? 0) + (run.draftThinking?.length ?? 0) + run.pendingTools.size + liveOutputSize;
  useEffect(() => {
    const el = scroller.current;
    if (el && el.scrollHeight - el.scrollTop - el.clientHeight < 160) el.scrollTop = el.scrollHeight;
  }, [contentSize]);

  if (session.isError) {
    return (
      <div className="p-6">
        <Alert variant="destructive">
          <AlertCircle />
          <AlertTitle>Can&apos;t open this session</AlertTitle>
          <AlertDescription>{session.error.message}</AlertDescription>
        </Alert>
      </div>
    );
  }

  return (
    <div className="flex h-full">
      <div className="flex min-w-0 flex-1 flex-col">
        <header className="flex h-12 shrink-0 items-center gap-3 border-b px-6">
          {session.data ? (
            <>
              <h1 className="truncate text-sm font-medium">{session.data.title ?? "Untitled session"}</h1>
              <ModelPicker sessionId={sessionId} model={session.data.model} running={run.running} />
              <ThinkingPicker
                sessionId={sessionId}
                model={session.data.model}
                level={session.data.thinking_level}
                running={run.running}
              />
              <SessionCost messages={messages} />
            </>
          ) : (
            <Skeleton className="mr-auto h-4 w-48" />
          )}
          {session.data && (
            <Button
              variant="ghost"
              size="sm"
              disabled={run.running}
              title="Summarize older messages so the model reads less"
              onClick={run.compact}
            >
              <FoldVertical />
              Compact
            </Button>
          )}
          {/* Hidden below lg, where the panel itself is hidden. */}
          <Button
            variant="ghost"
            size="icon"
            className="-mr-2 hidden lg:inline-flex"
            aria-label={activityOpen ? "Hide activity" : "Show activity"}
            aria-expanded={activityOpen}
            aria-controls="activity-panel"
            onClick={() => setActivityOpen((open) => !open)}
          >
            {activityOpen ? <PanelRightClose /> : <PanelRightOpen />}
          </Button>
        </header>

        <div ref={scroller} className="min-h-0 flex-1 overflow-y-auto">
          <div className="mx-auto w-full max-w-3xl px-6 py-8">
            {session.isPending ? (
              <div className="flex flex-col gap-4">
                <Skeleton className="h-10 w-1/2 self-end" />
                <Skeleton className="h-20 w-full" />
              </div>
            ) : messages.length === 0 && !run.running ? (
              <p className="pt-16 text-center text-balance text-muted-foreground">
                The agent works in this session&apos;s own folder. Ask it to write a file, then read it back.
              </p>
            ) : (
              <MessageList
                messages={messages}
                draft={run.draft}
                draftThinking={run.draftThinking}
                pendingTools={run.pendingTools}
                toolOutput={run.toolOutput}
              />
            )}
          </div>
        </div>

        <div className="mx-auto w-full max-w-3xl px-6 pb-6">
          {run.error && (
            <Alert variant="destructive" className="mb-3">
              <AlertCircle />
              {/* Neutral: a prompt, Continue, Compact or a compaction after a run can each land here. */}
              <AlertTitle>Something went wrong</AlertTitle>
              <AlertDescription>{run.error}</AlertDescription>
            </Alert>
          )}
          {run.reconnecting && (
            <p role="status" className="mb-3 flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              Connection lost. Reconnecting…
            </p>
          )}
          {run.compacting && (
            <p role="status" className="mb-3 flex items-center gap-2 text-sm text-muted-foreground">
              <Loader2 className="size-4 animate-spin" />
              Compacting older messages…
            </p>
          )}
          {/* The last run stopped (error, Stop, server restart) before the agent replied: offer to pick it up. */}
          {!run.running && session.data?.can_continue && (
            <div className="mb-3 flex items-center justify-between gap-3 rounded-lg border px-3 py-2 text-sm">
              <span className="text-muted-foreground">The agent stopped before it finished replying.</span>
              <Button size="sm" variant="outline" onClick={run.resume}>
                <RotateCw />
                Continue
              </Button>
            </div>
          )}
          {missingKey && (
            <div className="mb-3 flex items-center justify-between gap-3 border px-3 py-2 text-sm">
              <span className="flex items-center gap-2 text-pretty text-muted-foreground">
                <KeyRound className="size-4 shrink-0" />
                Add your {missingKey} API key to start. Runs are billed to your own {missingKey} account.
              </span>
              <Button size="sm" asChild>
                <Link href="/keys">Add key</Link>
              </Button>
            </div>
          )}
          <Composer
            value={input}
            blockedReason={missingKey ? `Add your ${missingKey} API key first (API keys, in the sidebar).` : null}
            onChange={setInput}
            running={run.running}
            canAttach={canAttach}
            queued={run.queued}
            onSend={run.send}
            onQueue={run.queue}
            onStop={run.stop}
          />
        </div>
      </div>
      {activityOpen && <ActivityPanel messages={messages} draft={run.draft} pendingTools={run.pendingTools} />}
    </div>
  );
}
