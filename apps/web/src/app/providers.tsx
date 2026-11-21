"use client";

import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { ReactQueryDevtools } from "@tanstack/react-query-devtools";
import { useEffect, useState } from "react";

import { TooltipProvider } from "@/components/ui/tooltip";
import { onSessionEnded, supabase } from "@/lib/auth/browser";

export function Providers({ children }: { children: React.ReactNode }) {
  // One client per browser tab; created in state so it survives re-renders.
  const [queryClient] = useState(
    () => new QueryClient({ defaultOptions: { queries: { staleTime: 30_000 } } }),
  );

  // A session that ends outside this page (sign-out in another tab, a refresh that failed) sends us to sign in.
  useEffect(() => {
    const listener = supabase?.auth.onAuthStateChange((event) => {
      if (event === "SIGNED_OUT") onSessionEnded();
    });
    return () => listener?.data.subscription.unsubscribe();
  }, []);

  return (
    <QueryClientProvider client={queryClient}>
      {/* One provider so every tooltip shares the same open delay. */}
      <TooltipProvider delayDuration={300}>{children}</TooltipProvider>
      <ReactQueryDevtools initialIsOpen={false} />
    </QueryClientProvider>
  );
}
