"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogFooter,
  DialogHeader,
  DialogTitle,
} from "@/components/ui/dialog";
import { Input } from "@/components/ui/input";
import { Label } from "@/components/ui/label";
import { Textarea } from "@/components/ui/textarea";
import type { McpServer, McpServerCreate } from "@/lib/api/client";

// Arguments one per line, so spaces inside an argument need no quoting.
function parseArgs(text: string): string[] {
  return text
    .split("\n")
    .map((line) => line.trim())
    .filter(Boolean);
}

// `KEY=value` per line; the first `=` splits, so values may contain more.
function parseEnv(text: string): Record<string, string> | string {
  const env: Record<string, string> = {};
  for (const line of text.split("\n").map((l) => l.trim()).filter(Boolean)) {
    const at = line.indexOf("=");
    if (at <= 0) return `"${line}" needs the form KEY=value.`;
    env[line.slice(0, at).trim()] = line.slice(at + 1);
  }
  return env;
}

// A dialog to add a stdio MCP server or edit one; `server` set means edit.
export function McpServerForm({
  open,
  onOpenChange,
  server,
  pending,
  error,
  onSubmit,
}: {
  open: boolean;
  onOpenChange: (open: boolean) => void;
  server?: McpServer;
  pending: boolean;
  error?: string;
  onSubmit: (body: McpServerCreate) => void;
}) {
  // Keyed on the server so reopening for another one starts from its saved values.
  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent className="sm:max-w-lg">
        {open && (
          <FormBody key={server?.id ?? "new"} server={server} pending={pending} error={error} onSubmit={onSubmit} />
        )}
      </DialogContent>
    </Dialog>
  );
}

// The fields; state lives here so it resets each time the dialog opens.
function FormBody({
  server,
  pending,
  error,
  onSubmit,
}: {
  server?: McpServer;
  pending: boolean;
  error?: string;
  onSubmit: (body: McpServerCreate) => void;
}) {
  const [name, setName] = useState(server?.name ?? "");
  const [command, setCommand] = useState(server?.command ?? "");
  const [args, setArgs] = useState(server?.args.join("\n") ?? "");
  const [env, setEnv] = useState(
    Object.entries(server?.env ?? {})
      .map(([k, v]) => `${k}=${v}`)
      .join("\n"),
  );
  const [localError, setLocalError] = useState<string>();

  function submit(event: React.FormEvent) {
    event.preventDefault();
    const parsedEnv = parseEnv(env);
    if (typeof parsedEnv === "string") return setLocalError(parsedEnv);
    setLocalError(undefined);
    onSubmit({
      name: name.trim(),
      command: command.trim(),
      args: parseArgs(args),
      env: parsedEnv,
      enabled: server?.enabled ?? true,
    });
  }

  const shown = localError ?? error;
  return (
    <form onSubmit={submit} className="flex flex-col gap-4">
      <DialogHeader>
        <DialogTitle>{server ? `Edit ${server.name}` : "Add MCP server"}</DialogTitle>
        <DialogDescription className="text-pretty">
          A local server the API starts as a process, once per session, in that session&apos;s workspace.
        </DialogDescription>
      </DialogHeader>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="mcp-name">Name</Label>
        <Input
          id="mcp-name"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="filesystem"
          pattern="[A-Za-z0-9_\-]{1,32}"
          title="Letters, digits, _ and - only"
          required
          autoFocus
        />
        <p className="text-xs text-muted-foreground">Its tools are named mcp__name__tool.</p>
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="mcp-command">Command</Label>
        <Input
          id="mcp-command"
          className="font-mono"
          value={command}
          onChange={(e) => setCommand(e.target.value)}
          placeholder="npx"
          required
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="mcp-args">Arguments, one per line</Label>
        <Textarea
          id="mcp-args"
          className="min-h-20 font-mono text-sm"
          value={args}
          onChange={(e) => setArgs(e.target.value)}
          placeholder={"-y\n@modelcontextprotocol/server-filesystem\n."}
        />
      </div>
      <div className="flex flex-col gap-1.5">
        <Label htmlFor="mcp-env">Environment, KEY=value per line</Label>
        <Textarea
          id="mcp-env"
          className="min-h-16 font-mono text-sm"
          value={env}
          onChange={(e) => setEnv(e.target.value)}
          placeholder="GITHUB_TOKEN=…"
        />
        <p className="text-xs text-muted-foreground">Encrypted in the database and visible only to you. The server gets these plus PATH and HOME.</p>
      </div>
      {shown && <p className="text-sm text-pretty text-destructive">{shown}</p>}
      <DialogFooter>
        <Button type="submit" disabled={pending}>
          {server ? "Save" : "Add server"}
        </Button>
      </DialogFooter>
    </form>
  );
}
