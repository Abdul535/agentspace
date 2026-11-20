import type { Metadata } from "next";

import { McpServers } from "@/components/mcp/mcp-servers";

export const metadata: Metadata = { title: "MCP servers · agentspace" };

// The MCP settings page (ADR-017); the list and forms are client components backed by TanStack Query.
export default function McpPage() {
  return (
    <div className="h-full overflow-y-auto">
      <McpServers />
    </div>
  );
}
