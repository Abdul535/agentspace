"use client";

import { AlertCircle } from "lucide-react";
import { useEffect, useRef } from "react";

import { Composer } from "@/components/chat/composer";
import { MessageList } from "@/components/chat/message-list";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Badge } from "@/components/ui/badge";
import { Skeleton } from "@/components/ui/skeleton";
import { useSession } from "@/lib/api/queries";
import { useAgentRun } from "@/lib/api/use-agent-run";

export function Chat({ sessionId }: { sessionId: string }) {
  const session = useSession(sessionId);
  const run = useAgentRun(sessionId);
  const messages = [...(session.data?.messages ?? []), ...run.messages];

  // Keep the newest output in view while it streams, unless the reader scrolled up.
  const scroller = useRef<HTMLDivElement>(null);
  const contentSize = messages.length + (run.draft?.length ?? 0) + run.pendingTools.size;
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
    <div className="flex h-full flex-col">
      <header className="flex h-12 shrink-0 items-center gap-3 border-b px-6">
        {session.data ? (
          <>
            <h1 className="truncate text-sm font-medium">{session.data.title ?? "Untitled session"}</h1>
            <Badge variant="secondary" className="ml-auto font-mono">
              {session.data.model}
            </Badge>
          </>
        ) : (
          <Skeleton className="h-4 w-48" />
        )}
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
            <MessageList messages={messages} draft={run.draft} pendingTools={run.pendingTools} />
          )}
        </div>
      </div>

      <div className="mx-auto w-full max-w-3xl px-6 pb-6">
        {run.error && (
          <Alert variant="destructive" className="mb-3">
            <AlertCircle />
            <AlertTitle>Couldn&apos;t send</AlertTitle>
            <AlertDescription>{run.error}</AlertDescription>
          </Alert>
        )}
        <Composer running={run.running} onSend={run.send} onStop={run.stop} />
      </div>
    </div>
  );
}
