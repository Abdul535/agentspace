"use client";

import { Archive, BookOpen, LoaderCircle, Plug, Search } from "lucide-react";
import Link from "next/link";
import { useParams, usePathname, useRouter } from "next/navigation";
import { useState } from "react";

import { NewSessionButton } from "@/components/new-session-button";
import { ThemeToggle } from "@/components/theme-toggle";
import { Input } from "@/components/ui/input";
import { Skeleton } from "@/components/ui/skeleton";
import { useArchiveSession, useSessions } from "@/lib/api/queries";
import { cn } from "@/lib/utils";

// The app's left column: new session, the searchable session list, and links to the settings pages.
export function SessionSidebar() {
  const { id: activeId } = useParams<{ id?: string }>();
  const pathname = usePathname();
  const router = useRouter();
  const onSkills = pathname.startsWith("/skills");
  const sessions = useSessions();
  const archive = useArchiveSession();
  const [query, setQuery] = useState("");

  const needle = query.trim().toLowerCase();
  const shown = sessions.data?.filter((s) => (s.title ?? "Untitled session").toLowerCase().includes(needle));

  // Archiving the open session leaves its page, which no longer has a place in the list.
  function onArchive(id: string) {
    archive.mutate(id, { onSuccess: () => id === activeId && router.push("/") });
  }

  return (
    <aside className="flex w-64 shrink-0 flex-col gap-3 border-r bg-sidebar p-3">
      <div className="flex items-center justify-between">
        <Link href="/" className="px-2 text-sm font-semibold tracking-tight">
          agentspace
        </Link>
        <ThemeToggle />
      </div>
      <NewSessionButton className="w-full" />
      <div className="relative">
        <Search
          className="pointer-events-none absolute top-1/2 left-2.5 size-3.5 -translate-y-1/2 text-muted-foreground"
          strokeWidth={1.5}
        />
        <Input
          type="search"
          value={query}
          onChange={(e) => setQuery(e.target.value)}
          placeholder="Search sessions"
          aria-label="Search sessions"
          className="pl-8"
        />
      </div>
      <nav className="-mx-1 flex min-h-0 flex-1 flex-col gap-0.5 overflow-y-auto px-1">
        {sessions.isPending &&
          Array.from({ length: 4 }, (_, i) => <Skeleton key={i} className="h-8 w-full" />)}
        {sessions.isError && <p className="px-2 text-sm text-destructive">Can&apos;t load sessions.</p>}
        {sessions.data?.length === 0 && <p className="px-2 text-sm text-muted-foreground">No sessions yet.</p>}
        {sessions.data?.length !== 0 && shown?.length === 0 && (
          <p className="px-2 text-sm text-muted-foreground">No matching sessions.</p>
        )}
        {shown?.map((session) => (
          <div key={session.id} className="group relative">
            <Link
              href={`/sessions/${session.id}`}
              aria-current={session.id === activeId ? "page" : undefined}
              className={cn(
                "flex items-center gap-2 rounded-md py-2 pr-8 pl-2 text-sm text-muted-foreground transition-colors hover:bg-accent hover:text-foreground",
                "aria-[current=page]:bg-accent aria-[current=page]:text-foreground aria-[current=page]:shadow-[inset_2px_0_0_var(--primary)]",
              )}
            >
              <span className="min-w-0 flex-1 truncate">{session.title ?? "Untitled session"}</span>
              {session.running && (
                <LoaderCircle
                  aria-label="Running"
                  className="size-3.5 shrink-0 animate-spin group-hover:hidden group-focus-within:hidden"
                  strokeWidth={1.5}
                />
              )}
            </Link>
            {/* A sibling of the link, not inside it: a button can't sit in an <a>. Shown on hover or focus. */}
            <button
              type="button"
              onClick={() => onArchive(session.id)}
              disabled={session.running || archive.isPending}
              aria-label={session.running ? "Can't archive while running" : "Archive session"}
              title={session.running ? "Can't archive while running" : "Archive"}
              className={cn(
                "absolute top-1/2 right-1 flex size-6 -translate-y-1/2 items-center justify-center rounded-sm text-muted-foreground opacity-0 transition-[opacity,color,background-color]",
                "group-hover:opacity-100 focus-visible:opacity-100 hover:bg-background hover:text-foreground",
                "disabled:cursor-not-allowed disabled:hover:bg-transparent disabled:hover:text-muted-foreground",
                "group-hover:disabled:opacity-40",
              )}
            >
              <Archive className="size-3.5" strokeWidth={1.5} />
            </button>
          </div>
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
