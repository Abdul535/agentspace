# ADR-017 — MCP servers: tools from other programs

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
ADR-003 step 24 asks to "turn an MCP server's tools into `Tool`s". ADR-014 adds
step 50 (MCP sign-in with OAuth) and step 51 (tool search) on top of it. The
[Model Context Protocol](https://modelcontextprotocol.io) is JSON-RPC 2.0
between a client (us) and a server, which is either a local process we start
(stdio) or a remote URL (streamable HTTP). A server offers tools, resources and
prompts. GitHub, Sentry and the filesystem are all reachable this way.

pi (`../pi-upstream` at `9cd49d2`) has full support, read for this ADR:

- `packages/mcp/`: pi's **own** MCP client. It does not use the official SDK.
  `client.ts` does the `initialize` handshake, `tools/list`, `tools/call`,
  resources, cancellation and progress. `transports/` has stdio and streamable
  HTTP; `oauth/` has sign-in. `naming.ts` names tools `mcp__<server>__<tool>`.
- `coding-agent/src/extensions/mcp/`: connects servers from `mcp.json` (user
  and project level) when a session starts (`index.ts`), wraps each MCP tool
  as a pi tool (`tools.ts`), manages connections, reconnects and timeouts
  (`runtime.ts`), and adds resource tools (`resources.ts`) and a `/mcp`
  manager UI (`ui.ts`).
- Behavior (`docs/mcp.md`):
  - All servers connect in the background at session start. The first prompt
    waits up to 10 s, and only for servers whose tools are declared directly.
  - Each call has a 60 s timeout, which progress notifications reset.
  - A failed call is a tool error for the model. A dropped connection
    reconnects on the next call. Tool calls are never retried.
  - Text results over 20 KB keep their start and end; the full text is saved
    to a temp file.
  - Stopping a stdio server closes its stdin, then sends SIGTERM and SIGKILL to
    its process group.
  - **Exposure** per server or tool: `direct` (declared to the model),
    `deferred` (found through `tool_search`), `codemode` (callable only from
    model-written scripts) or `hidden`.

The owner's rules apply: no agent framework and no vendored packages, so we
write our own client, like pi does. The owner also wants servers configured from
the web app, not only from a file.

## Decision
MCP support is built in the stages below. Each stage is one change and is ticked
here when it lands.

1. [x] **Minimal stdio MCP (this change).**
   - **Client.** Our own client (`agentspace/mcp/client.py`): newline-delimited
     JSON-RPC over the stdin and stdout of a child process.
     - It sends `initialize`, then `notifications/initialized`, then calls
       `tools/list` (following pagination cursors) and `tools/call`.
     - It answers the server's `ping` and refuses its other requests with
       "method not found".
     - A stdio server is started in its own process group and stopped like
       pi's: close stdin, then SIGTERM, then SIGKILL.
   - **Config.** Servers are stored in the database (`mcp_servers`: name,
     command, args, env, enabled). The API has `/mcp-servers` routes to list,
     add, change and delete servers, and to test one (connect, then list its
     tools). The web app has an "MCP servers" page that uses them. Every enabled
     server applies to every session. There are no per-session server lists yet.
   - **Lifetime.** Connections are made once per session (the owner's
     prediction, matching pi). They start lazily on the session's first run,
     with the session's workspace as the working folder, because a filesystem
     server must see that session's files. They close after 10 idle minutes,
     when the server's settings change, or when the API shuts down.
   - **Tools.** Each MCP tool becomes a `Tool`:
     - Its name is `mcp__<server>__<tool>`. Anything that isn't
       `[A-Za-z0-9_]` becomes `_`, and a name over 64 characters (or one that
       clashes) is shortened and given a hash suffix, as in pi's `naming.ts`.
     - Its JSON Schema is passed to the model unchanged, and its arguments are
       passed on to the server unchanged; the server validates them.
     - Results become text and images. Other block types become a short note.
       Text over 50 KB (our `read` cap) is cut in the middle.
     - All MCP tools are `direct`: they are declared to the model like built-in
       tools.
   - **Failures.**
     - A run waits up to 10 s for servers to connect. A server that fails or
       is still starting has no tools for that run, and the run goes on
       without it.
     - Each call has a 60 s timeout; when it expires we send
       `notifications/cancelled`.
     - A crash or timeout is a tool error for the model. A dead connection is
       made again on the next call or run. Calls are never retried.
   - **Safety.** Starting a stdio server runs a command on the host, just like
     `bash`, so MCP is off unless `AGENTSPACE_ENABLE_MCP=true`. While it is
     off, servers can still be saved, but the test route answers 403 and runs
     get no MCP tools. A server inherits only `HOME`, `PATH`, `USER`,
     `LOGNAME`, `SHELL`, `TERM`, `LANG` and `TMPDIR` from the API, plus its
     own `env`, so the API's keys never reach it. Env values are stored in
     plain text in the database and shown to anyone who can reach the API.
     That is acceptable only until step 27 (API auth).
   Built as follows: `agentspace/mcp/` (`client.py`, `tools.py`,
   `manager.py`), `api/mcp_servers.py`, the `mcp_servers` table, the settings
   `enable_mcp`, `mcp_wait_seconds`, `mcp_call_timeout_seconds` and
   `mcp_idle_seconds`, and the web page at `/mcp`, linked from the sidebar.
   A connect that misses the run's 10 s wait keeps going in the background
   for up to 120 s (`npx` downloads), so a later run can use it. Tested
   against `@modelcontextprotocol/server-filesystem`.
2. [ ] **Streamable HTTP transport.** For remote servers by URL: POST JSON-RPC,
   read JSON or SSE replies, keep the `Mcp-Session-Id` header, and send static
   headers (bearer tokens) from settings. Retry 408, 429 and 5xx twice for
   connects and lists, never for calls.
   *pi:* `transports/streamable-http.ts`.
3. [ ] **OAuth sign-in (ADR-014 step 50).** Discover the auth server
   (RFC 9728 → RFC 8414), register the client (dynamic registration or a
   client metadata document), run the authorization-code flow with PKCE through
   a callback route on the API, store tokens per server, and refresh them.
   The web page gets a "Sign in" button. *pi:* `packages/mcp/src/oauth/`.
4. [ ] **Change notifications and progress.** Handle
   `notifications/tools/list_changed` (re-list, then announce the change to the
   model through ADR-003 step 22), and pass progress notifications on as tool
   updates that also reset the call timeout. *pi:* `runtime.ts`, `tools.ts`.
5. [ ] **Exposure and tool search (ADR-014 step 51).** `exposure` and
   `toolExposure` per server: `direct`, `deferred` (loaded through a
   `tool_search` tool) or `hidden`, plus a one-line server summary in the
   system prompt. `codemode` waits for step 53. *pi:* `docs/mcp.md` "Control
   tool exposure".
6. [ ] **Resources.** The `list_mcp_resources`, `list_mcp_resource_templates`
   and `read_mcp_resource` tools, and resource links in results.
   *pi:* `extensions/mcp/resources.ts`.
7. [ ] **Per-session servers and approval.** Let a session pick which servers
   it uses, and route MCP calls through tool approval (ADR-014 step 26), using
   the server's `readOnlyHint` and `destructiveHint` annotations.
8. [ ] **Secrets.** Keep env and header values out of API responses, and
   support `${VAR}` references to the API's environment, as pi's
   `resolve-config-value.ts` does.

## Alternatives considered
- **The official `mcp` Python SDK.** Less code and spec-complete, but the
  project rules ask for our own implementation, and pi shows that the client
  part (handshake, list, call) is small.
- **An `mcp.json` file, like pi and Claude Desktop.** Familiar and easy to copy
  between clients, but the owner wants to configure servers from the web app,
  and the backend owns all state in SQLite. An import from `mcp.json` can come
  later.
- **Connect once when the API boots and share connections across sessions.**
  Cheaper, but every session has its own workspace and a stdio server's working
  folder is fixed when it starts. A shared server can't see the right files.
- **Start servers on every run.** Simple, but `npx` startups take seconds, and
  a server's in-memory state would reset between messages.
- **Validate MCP arguments with a generated Pydantic model.** JSON Schema can
  express things Pydantic models can't cover cleanly, and the server validates
  anyway. The arguments are passed through as an open object instead.

## Consequences
- MCP tools reach the loop as ordinary `Tool`s, so hooks, parallel execution,
  events and the UI's tool cards work unchanged.
- `Tool.name`, `description` and `snippet` can now be set per instance, not
  only per class, so one class serves every MCP tool.
- The API now keeps child processes alive between requests. They are tied to
  the app's lifespan and closed on shutdown.
- A session's tool set now depends on app-wide state that can change between
  runs, which step 22 (announcing tool changes) will have to account for.
- Tool names begin with `mcp__`, which a future approval or exposure rule can
  match on.
