"use client";

import { CircleCheck, CircleX, KeyRound, Loader2, Pencil, Plug, Plus, Trash2 } from "lucide-react";
import { useState, useSyncExternalStore } from "react";

import { McpServerForm } from "@/components/mcp/mcp-server-form";
import { Alert, AlertDescription, AlertTitle } from "@/components/ui/alert";
import { Button } from "@/components/ui/button";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Skeleton } from "@/components/ui/skeleton";
import { Switch } from "@/components/ui/switch";
import type { McpServer, McpServerCreate } from "@/lib/api/client";
import { getMcpAdminToken, setMcpAdminToken, subscribeMcpAdminToken } from "@/lib/api/mcp-admin-token";
import {
  useCreateMcpServer,
  useDeleteMcpServer,
  useMcpServers,
  useTestMcpServer,
  useUpdateMcpServer,
} from "@/lib/api/queries";

// The MCP settings page: the saved servers, a switch per server, and add, edit, test and delete (ADR-017).
export function McpServers() {
  const servers = useMcpServers();
  const create = useCreateMcpServer();
  const update = useUpdateMcpServer();
  // `undefined` = dialog closed, `null` = adding, a server = editing it.
  const [editing, setEditing] = useState<McpServer | null>();

  const saving = editing ? update : create;

  function submit(body: McpServerCreate) {
    const done = { onSuccess: () => setEditing(undefined) };
    if (editing) update.mutate({ id: editing.id, change: body }, done);
    else create.mutate(body, done);
  }

  function openForm(server: McpServer | null) {
    create.reset();
    update.reset();
    setEditing(server);
  }

  return (
    <div className="mx-auto flex w-full max-w-2xl flex-col gap-6 p-6">
      <header className="flex items-start justify-between gap-4">
        <div className="flex flex-col gap-1">
          <h1 className="text-xl font-semibold tracking-tight text-balance">MCP servers</h1>
          <p className="text-sm text-pretty text-muted-foreground">
            Programs that give the agent more tools. Every enabled server joins every session&apos;s next message.
          </p>
        </div>
        <Button onClick={() => openForm(null)}>
          <Plus data-icon="inline-start" />
          Add server
        </Button>
      </header>

      {servers.data && !servers.data.mcp_enabled && (
        <Alert>
          <AlertTitle>MCP is off</AlertTitle>
          <AlertDescription className="text-pretty">
            Starting a server runs its command on this machine, so the API starts none until you set{" "}
            <code className="font-mono">AGENTSPACE_ENABLE_MCP=true</code> in <code className="font-mono">apps/api/.env</code>{" "}
            and restart it. You can still add servers now.
          </AlertDescription>
        </Alert>
      )}

      {servers.data && !servers.data.admin_token_set && (
        <Alert>
          <AlertTitle>No admin token</AlertTitle>
          <AlertDescription className="text-pretty">
            Adding, changing or testing a server needs a secret only you know. Set{" "}
            <code className="font-mono">AGENTSPACE_MCP_ADMIN_TOKEN</code> in <code className="font-mono">apps/api/.env</code>,
            restart the API, then enter it below.
          </AlertDescription>
        </Alert>
      )}
      {servers.data?.admin_token_set && <AdminTokenField />}

      {servers.isPending && <Skeleton className="h-24 w-full" />}
      {servers.isError && <p className="text-sm text-destructive">Can&apos;t load MCP servers.</p>}
      {servers.data?.servers.length === 0 && (
        <div className="flex flex-col items-center gap-2 rounded-xl border border-dashed p-10 text-center">
          <Plug className="text-muted-foreground" />
          <p className="text-sm text-muted-foreground">No servers yet.</p>
        </div>
      )}
      <ul className="flex flex-col gap-3">
        {servers.data?.servers.map((server) => (
          <ServerCard
            key={server.id}
            server={server}
            canTest={servers.data.mcp_enabled}
            onEdit={() => openForm(server)}
          />
        ))}
      </ul>

      <McpServerForm
        open={editing !== undefined}
        onOpenChange={(open) => !open && setEditing(undefined)}
        server={editing ?? undefined}
        pending={saving.isPending}
        error={saving.error?.message}
        onSubmit={submit}
      />
    </div>
  );
}

// One server: its command line, an on/off switch, and its test result once tested.
function ServerCard({ server, canTest, onEdit }: { server: McpServer; canTest: boolean; onEdit: () => void }) {
  const update = useUpdateMcpServer();
  const remove = useDeleteMcpServer();
  const test = useTestMcpServer();
  const envCount = Object.keys(server.env).length;

  return (
    <li className="flex flex-col gap-3 rounded-xl border bg-card p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="flex min-w-0 flex-col gap-1">
          <span className="font-mono text-sm font-medium">{server.name}</span>
          <code className="truncate font-mono text-xs text-muted-foreground" title={commandLine(server)}>
            {commandLine(server)}
          </code>
          {envCount > 0 && (
            <span className="text-xs text-muted-foreground tabular-nums">
              {envCount} environment variable{envCount === 1 ? "" : "s"}
            </span>
          )}
        </div>
        <Switch
          checked={server.enabled}
          disabled={update.isPending}
          onCheckedChange={(enabled) => update.mutate({ id: server.id, change: { enabled } })}
          aria-label={server.enabled ? `Disable ${server.name}` : `Enable ${server.name}`}
        />
      </div>
      <div className="flex gap-2">
        <Button
          variant="outline"
          size="sm"
          disabled={!canTest || test.isPending}
          title={canTest ? "Start it once and list its tools" : "Turn MCP on in the API first"}
          onClick={() => test.mutate(server.id)}
        >
          {test.isPending ? <Loader2 className="animate-spin" data-icon="inline-start" /> : <Plug data-icon="inline-start" />}
          Test
        </Button>
        <Button variant="ghost" size="sm" onClick={onEdit}>
          <Pencil data-icon="inline-start" />
          Edit
        </Button>
        <Button
          variant="ghost"
          size="sm"
          className="ml-auto text-muted-foreground hover:text-destructive"
          disabled={remove.isPending}
          onClick={() => window.confirm(`Delete ${server.name}?`) && remove.mutate(server.id)}
        >
          <Trash2 data-icon="inline-start" />
          Delete
        </Button>
      </div>
      {test.error && <p className="text-sm text-destructive">{test.error.message}</p>}
      {test.data && <TestResult result={test.data} />}
    </li>
  );
}

// What a test found: the tools the server offers, or why it failed to start.
function TestResult({ result }: { result: NonNullable<ReturnType<typeof useTestMcpServer>["data"]> }) {
  if (!result.ok) {
    return (
      <div className="flex flex-col gap-1.5 rounded-lg bg-destructive/5 p-3">
        <span className="flex items-center gap-1.5 text-sm font-medium text-destructive">
          <CircleX className="size-4" /> Failed to start
        </span>
        <pre className="max-h-48 overflow-auto font-mono text-xs whitespace-pre-wrap text-muted-foreground">
          {result.error}
        </pre>
      </div>
    );
  }
  return (
    <div className="flex flex-col gap-2 rounded-lg bg-muted/50 p-3">
      <span className="flex items-center gap-1.5 text-sm font-medium">
        <CircleCheck className="size-4 text-success" />
        Connected{result.server_name && ` to ${result.server_name}`}
        {result.server_version && <span className="text-muted-foreground"> {result.server_version}</span>}
        <span className="text-muted-foreground tabular-nums">
          · {result.tools?.length ?? 0} tool{result.tools?.length === 1 ? "" : "s"}
        </span>
      </span>
      <ul className="flex max-h-64 flex-col gap-1.5 overflow-auto">
        {result.tools?.map((tool) => (
          <li key={tool.name} className="text-xs">
            <span className="font-mono font-medium">{tool.name}</span>
            {tool.description && (
              <span className="line-clamp-2 text-pretty text-muted-foreground">{tool.description}</span>
            )}
          </li>
        ))}
      </ul>
    </div>
  );
}

// The command as one line, for display only (the API never runs it through a shell).
function commandLine(server: McpServer): string {
  return [server.command, ...server.args].join(" ");
}

// Where the user enters the API's MCP admin token; it is kept in this browser and sent with every change.
function AdminTokenField() {
  // Read on the client only: the server render has no storage, so it starts as "" there.
  const saved = useSyncExternalStore(subscribeMcpAdminToken, getMcpAdminToken, () => "");
  const [draft, setDraft] = useState("");

  function save(event: React.FormEvent) {
    event.preventDefault();
    setMcpAdminToken(draft.trim());
    setDraft("");
  }

  return (
    <form onSubmit={save} className="flex flex-col gap-1.5">
      <Label htmlFor="mcp-admin-token" className="flex items-center gap-1.5">
        <KeyRound className="size-4" strokeWidth={1.5} />
        Admin token
      </Label>
      <div className="flex gap-2">
        <Input
          id="mcp-admin-token"
          type="password"
          autoComplete="off"
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          placeholder={saved ? "Saved in this browser" : "The value of AGENTSPACE_MCP_ADMIN_TOKEN"}
        />
        <Button type="submit" variant="outline" disabled={!draft.trim()}>
          Save
        </Button>
        {saved && (
          <Button type="button" variant="ghost" onClick={() => setMcpAdminToken("")}>
            Forget
          </Button>
        )}
      </div>
      <p className="text-xs text-pretty text-muted-foreground">
        Needed to add, change, delete or test servers, since those run commands on the API&apos;s machine.
      </p>
    </form>
  );
}
