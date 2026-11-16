"use client";

import { ChevronRight, FileText, LoaderCircle, PencilLine, Wrench } from "lucide-react";

import { Collapsible, CollapsibleContent, CollapsibleTrigger } from "@/components/ui/collapsible";
import type { ToolCall, ToolResultMessage } from "@/lib/api/client";
import { cn } from "@/lib/utils";

const icons: Record<string, typeof Wrench> = { read: FileText, write: PencilLine };

/** One tool call and, once it has run, its result (collapsed by default). */
export function ToolCallView({
  call,
  result,
  pending,
}: {
  call: ToolCall;
  result: ToolResultMessage | undefined;
  pending: boolean;
}) {
  const Icon = icons[call.name] ?? Wrench;
  const path = typeof call.arguments.path === "string" ? call.arguments.path : null;
  const output = result?.content.map((block) => block.text).join("") ?? "";
  const failed = result?.is_error ?? false;

  return (
    <Collapsible className="rounded-lg border bg-card text-sm">
      <CollapsibleTrigger
        disabled={!result}
        className="group flex min-h-9 w-full items-center gap-2 px-3 py-1.5 text-left disabled:cursor-default"
      >
        <Icon className="size-4 shrink-0 text-muted-foreground" />
        <span className="font-medium">{call.name}</span>
        {path && <span className="truncate font-mono text-xs text-muted-foreground">{path}</span>}
        <span className="ml-auto flex shrink-0 items-center gap-2">
          {pending && <LoaderCircle className="size-4 animate-spin text-muted-foreground" aria-label="Running" />}
          {failed && <span className="text-xs text-destructive">failed</span>}
          {result && (
            <ChevronRight className="size-4 text-muted-foreground transition-transform group-data-[state=open]:rotate-90" />
          )}
        </span>
      </CollapsibleTrigger>
      <CollapsibleContent>
        <pre
          className={cn(
            "max-h-80 overflow-auto border-t px-3 py-2 font-mono text-xs whitespace-pre-wrap",
            failed ? "text-destructive" : "text-muted-foreground",
          )}
        >
          {output || "(no output)"}
        </pre>
      </CollapsibleContent>
    </Collapsible>
  );
}
