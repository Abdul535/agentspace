"use client";

import { useMutation, useQuery } from "@tanstack/react-query";
import { LoaderCircle, LogOut } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Skeleton } from "@/components/ui/skeleton";
import { Tooltip, TooltipContent, TooltipTrigger } from "@/components/ui/tooltip";
import { signOut, supabase } from "@/lib/auth/browser";

// The signed-in user's email and a sign-out button, at the foot of the sidebar; nothing in local mode (ADR-024).
export function UserMenu() {
  const user = useQuery({
    queryKey: ["auth", "user"],
    // Read from the session cookie, no network call.
    queryFn: async () => (await supabase!.auth.getSession()).data.session?.user ?? null,
    enabled: Boolean(supabase),
    staleTime: Infinity,
  });
  const leave = useMutation({ mutationFn: signOut });

  if (!supabase) return null;
  return (
    <div className="flex items-center gap-2 border-t pt-3">
      <div className="min-w-0 flex-1 px-2">
        <p className="font-mono text-[10.5px] tracking-[0.06em] text-muted-foreground uppercase">Signed in as</p>
        {user.isPending ? (
          <Skeleton className="mt-1 h-4 w-32" />
        ) : (
          <p className="truncate text-sm" title={user.data?.email}>
            {user.data?.email ?? "Unknown user"}
          </p>
        )}
      </div>
      <Tooltip>
        <TooltipTrigger asChild>
          <Button
            variant="ghost"
            size="icon-sm"
            onClick={() => leave.mutate()}
            disabled={leave.isPending}
            aria-label={leave.isPending ? "Signing out" : "Sign out"}
          >
            {leave.isPending ? (
              <LoaderCircle className="animate-spin" strokeWidth={1.5} />
            ) : (
              <LogOut strokeWidth={1.5} />
            )}
          </Button>
        </TooltipTrigger>
        <TooltipContent>Sign out</TooltipContent>
      </Tooltip>
    </div>
  );
}
