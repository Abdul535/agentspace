"use client";

import { BookOpen, Plug } from "lucide-react";
import Link from "next/link";
import { useParams, usePathname } from "next/navigation";

import { NewSessionButton } from "@/components/new-session-button";
import { ThemeToggle } from "@/components/theme-toggle";
import { Skeleton } from "@/components/ui/skeleton";
import { useSessions } from "@/lib/api/queries";
import { cn } from "@/lib/utils";

export function SessionSidebar() {
  const { id: activeId } = useParams<{ id?: string }>();
  const pathname = usePathname();
  const onSkills = pathname.startsWith("/skills");
  const sessions = useSessions();

  return (
    <aside className="flex w-64 shrink-0 flex-col gap-3 border-r bg-sidebar p-3">
      <div className="flex items-center justify-between">
        <Link href="/" className="px-2 text-sm font-semibold tracking-tight">
          agentspace
        </Link>
        <ThemeToggle />
      </div>
      <NewSessionButton className="w-full" />
      <nav className="-mx-1 flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-1">
        {sessions.isPending &&
          Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-8 w-full" />)}
        {sessions.isError && <p className="px-2 text-sm text-destructive">Can&apos;t load sessions.</p>}
        {sessions.data?.length === 0 && <p className="px-2 text-sm text-muted-foreground">No sessions yet.</p>}
        {sessions.data?.map((session) => (
          <Link
            key={session.id}
            href={`/sessions/${session.id}`}
            aria-current={session.id === activeId ? "page" : undefined}
            className={cn(
              "truncate rounded-md px-2 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
              "aria-[current=page]:bg-accent aria-[current=page]:text-foreground aria-[current=page]:shadow-[inset_2px_0_0_var(--primary)]",
            )}
          >
            {session.title ?? "Untitled session"}
          </Link>
        ))}
      </nav>
      {/* App-wide settings pages, under the session list. */}
      <div className="flex flex-col gap-0.5">
        <Link
          href="/skills"
          aria-current={onSkills ? "page" : undefined}
          className={cn(
            "flex items-center gap-2 rounded-md px-2 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
            "aria-[current=page]:bg-accent aria-[current=page]:text-foreground aria-[current=page]:shadow-[inset_2px_0_0_var(--primary)]",
          )}
        >
          <BookOpen className="size-4" strokeWidth={1.5} />
          Skills
        </Link>
        <Link
          href="/mcp"
          aria-current={pathname === "/mcp" ? "page" : undefined}
          className={cn(
            "flex items-center gap-2 rounded-md px-2 py-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
            "aria-[current=page]:bg-accent aria-[current=page]:text-foreground aria-[current=page]:shadow-[inset_2px_0_0_var(--primary)]",
          )}
        >
          <Plug className="size-4" strokeWidth={1.5} />
          MCP servers
        </Link>
      </div>
    </aside>
  );
}
