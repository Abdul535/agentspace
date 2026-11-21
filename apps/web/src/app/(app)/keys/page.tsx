import type { Metadata } from "next";

import { ProviderKeys } from "@/components/keys/provider-keys";

export const metadata: Metadata = { title: "API keys · agentspace" };

// The user's own model API keys (ADR-026); the list and forms are client components backed by TanStack Query.
export default function KeysPage() {
  return (
    <div className="h-full overflow-y-auto">
      <ProviderKeys />
    </div>
  );
}
