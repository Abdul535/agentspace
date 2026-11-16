# Kortix CompanyOS: how it works

Study notes on `../CompanyOS` (Kortix v0.15.7, Elastic License 2.0), written
2026-11-15 from its docs and source layout. Paths are relative to the
CompanyOS repo root. These notes describe the design as documented; behavior
was not run or verified.

Main sources:

- `README.md`, `apps/web/public/docs/guide.md` (product guide),
  `apps/web/public/docs/developers.md` (reference, ~9k lines)
- `apps/web/content/posts/agi-ready-architecture.md` (design thesis)
- `apps/kortix-sandbox-agent-server/{README,ARCHITECTURE}.md` (the in-VM daemon)
- `apps/sandbox/MACHINE.md` (what the agent is told about its machine)
- `apps/api/src/platform/providers/contract.ts` (sandbox provider interface)
- `apps/api/src/projects/session-lifecycle/README.md` (prompt queue rules)
- `packages/manifest-schema/src/index.v3.ts` (`kortix.yaml` schema)
- `packages/db/src/schema/kortix.ts` (Postgres schema)

---

## 1. The thesis in one paragraph

The model is a replaceable reasoning engine; the platform around it is the
product. Four invariants carry the design:

1. **State lives outside the model and outside the machine.** Config is files
   in git, session history is a stored record, work is pushed to a branch.
   Compute is disposable.
2. **The model is never the authority on what it may do.** Grants, policies,
   and approvals are enforced by the control plane, not by the prompt.
3. **Execution is isolated from the control plane.** Every session runs on
   its own microVM; agent-generated code is untrusted.
4. **Changes to shared state go through review.** An agent pushes only to its
   session branch; `main` changes only through a change request a person
   merges.

---

## 2. The big picture

```
 Clients: web (Next.js) · desktop (Electron) · mobile · CLI · TUI · SDK · MCP
 Channels: Slack · Teams · Telegram · Discord · email
                 │
                 ▼
 ┌──────────────────────────── control plane ────────────────────────────┐
 │ apps/api (Bun + Hono), one image, two process roles:                  │
 │   web    : REST /v1, WebSockets, sandbox proxy, per-replica loops     │
 │   worker : scheduled jobs, holds a leader lease (one active)          │
 │ apps/llm-gateway : the only process that calls model providers        │
 │ Postgres (Supabase) · object store (S3 / MinIO) · git hosting         │
 └──────────────┬───────────────────────────────▲────────────────────────┘
                │ provider API: create/start/   │ callbacks with session
                │ stop/exec/ingress             │ token (KORTIX_TOKEN)
                ▼                               │
 ┌──────────────────────── one microVM per session ──────────────────────┐
 │ kortixd (apps/kortix-sandbox-agent-server), a single compiled binary: │
 │   pi agent harness (in-process) · HTTP+SSE runtime API on :8000       │
 │   static web :3211 · egress proxy (secret swap) · LLM proxy           │
 │ XFCE desktop on :1, streamed on :6901 · agent-browser (headed)        │
 │ /workspace (session files) · /drives (Drive, cross-session)           │
 └───────────────────────────────────────────────────────────────────────┘
```

Repository layout:

| Path | What it is |
|---|---|
| `apps/api` | Control plane: auth, projects, sessions, connectors, secrets, triggers, billing, sandbox proxy, git proxy |
| `apps/llm-gateway`, `packages/llm-gateway` | Standalone inference relay |
| `apps/kortix-sandbox-agent-server` | `kortixd`, the daemon inside every sandbox |
| `apps/sandbox` | The sandbox image: `Dockerfile`, `entrypoint.sh`, `MACHINE.md` |
| `apps/web`, `apps/frontend` | Web app and docs site |
| `apps/cli`, `apps/tui`, `apps/desktop-electron`, `apps/mobile` | Other clients |
| `packages/manifest-schema` | `kortix.yaml` schema (v3), policy rules, trigger filters |
| `packages/db` | Drizzle schema and migrations |
| `packages/sdk`, `packages/api-contract` | TypeScript SDK and the shared wire contracts |
| `backend/` | Legacy Python (`agentpress`, the older Suna-era stack); not on the current path |
| `self-host/`, `infra/` | Docker Compose self-host, Terraform for their cloud |

---

## 3. Core concepts

| Concept | What it is |
|---|---|
| **Workspace / project** | One git repo plus a `kortix.yaml`. Row in `projects` (`repo_url`, `default_branch`, `manifest_path`). |
| **Agent** | A block in `kortix.yaml`: prompt, model, permission rules, and grants. Not a process; a definition. |
| **Session** | One piece of agent work. Runs one agent, on its own branch, on its own sandbox. Row in `project_sessions`. |
| **Sandbox / computer** | The microVM a session runs on. Row in `sandboxes` (`provider`, `external_id`, `status`). |
| **Sandbox template** | Image + size + settings for sandboxes (`sandbox.templates` in `kortix.yaml`). |
| **Config release** | A compiled, immutable snapshot of the base branch's config, which a session boots from. Rows in `config_releases`. |
| **Skill** | A folder (`SKILL.md` + files) registered in `kortix.yaml`, enabled per agent. |
| **Connector** | Governed access to an outside app (Composio catalog, MCP, OpenAPI, GraphQL, HTTP). |
| **Trigger** | Schedule, webhook, connector event, App event, or merged change request that starts or wakes a session. |
| **Drive** | Files that outlive sessions, mounted at `/drives`; also holds memory as Markdown. |
| **Change request** | Kortix's PR: merges a session branch into the base branch after review. |
| **My Kortix** | Built-in router agent the user talks to; it hands work to project agents in separate sessions. |

---

## 4. Agent definition (`kortix.yaml`)

The manifest uses **registries and grants**. The workspace registers what
exists; each agent enables what it uses by name. Registering grants nothing.

```yaml
skills:     { research: { path: skills/research } }  # registry
connectors: [ { slug: slack, policies: [...] } ]     # registry
agents:
  worker:
    description: General-purpose worker
    model: kortix/claude-...          # gateway model ref
    prompt_file: agents/worker.md
    permission:                       # tool-call rules (section 7)
      bash: { "*": allow, "git push *": ask, "rm -rf *": deny }
    tools: "*"                        # open by default
    skills: [research]                # omitted = none
    connectors: [slack]
    policy_sets: { slack: read_only }
    secrets: [OPENAI_API_KEY]
    subagents: [explore]
    kortix_permissions: [project.gitops.read]
    sandbox: node                     # template slug
    workspace:                        # what the session starts with
      repos: [ { path: /workspace, sparse: [apps/web] } ]
      setup: [ "pnpm install" ]
    network_egress: { version: 1, default: deny, rules: [] }
```

The full agent block (`AgentBlockV3`, `index.v3.ts:131`) also has `mode`
(primary/subagent), `temperature`, `steps`, `codemode`, `context`
(`record` vs `summary` on a full context), `commands`, `extensions`, `apps`.

Grant rules:

- A grant is `"*"`, `none`, or a list of names. **Omitted means none**, except
  `tools` (`"*"`), `permission` (`allow`) and `codemode` (`on`).
- Names must exist in a registry or be built in; otherwise validation fails.
- Built-in tools: `bash read write edit glob grep question task`, plus Kortix
  tools (`web_search`, `scrape_webpage`, `image_gen`, `show`, ...).

### Config flows from git, not from the machine

```
 commit on base branch ──► API compiles a config release ──► stored (S3 + DB)
                                                                  │
 session boot / next turn ◄── kortixd downloads release ◄─────────┘
```

- A session runs the config release of its **base branch**, never the files
  in its own `/workspace`. Editing an agent's prompt in a session does nothing
  until it merges.
- Each base-branch commit is a new release. Running sessions converge to it at
  the start of their next turn, or on **Reload config**.
- A session with `base_ref` on a feature branch runs that branch's *behavior*
  (prompts, models, skills), but **governance** (which agents exist, their
  secret/connector/permission grants) always comes from the default branch.
  A branch cannot widen its own access before review.
- A broken release falls back to the last good one ("Older config").
- The compiled agent set reaches the VM as `KORTIX_COMPILED_AGENT_CONFIG`.

---

## 5. Sessions and computers

### One session = one branch = one microVM

- Session id = branch name = sandbox id.
- `/workspace` starts **empty**. An agent that needs the repo runs
  `kortix workspaces clone` (needs `project.gitops.read`), or declares
  `workspace.repos` to have it cloned in the background at start.
- The git proxy accepts pushes only to the session's own branch.

### What persists

| Location | Lifetime |
|---|---|
| `/workspace` | Life of the session (kept across sleep/wake; deleted with the session) |
| `/drives/me`, `/drives/<shared>` | Across sessions until deleted (Drive volumes) |
| Session record (messages, log) | Stored by the API; readable while the VM is stopped |
| Pushed branch | Forever, even after the session is deleted |
| Everything else (`$HOME`, `/tmp`, apt packages) | May vanish on wake |

To keep an installed tool for every session, the agent adds it to the sandbox
template in `kortix.yaml` and opens a change request. After merge, a new image
is built (`apps/api/src/snapshots/`) and future sessions get it.

### Lifecycle

```
 create ──► queued ──► provisioning ──► running ──(15–20 min idle)──► stopped
                                          ▲                              │
                                          └── message / event / preview ─┘
 lost (provider removed the VM) ──► recover() on a new computer from the stored log
 delete ──► restorable for 30 days ──► gone
```

- Template resolution: request → agent's `sandbox` → `sandbox.default` →
  platform image.
- A browser tab holds a **presence lease** (`session_presence_leases`) that
  keeps the computer awake while someone watches.

### Sandbox provider interface

`apps/api/src/platform/providers/contract.ts` (`SandboxProvider`):

```
create(opts) → ProvisionResult      createFromExternalId?(...)
start(id, opts) / stop(id) / remove(id) / ensureRunning(id)
getStatus(id) / getProvisioningStatus(id) / recoverInPlace?(id)
renewLifecycle(id)                  exec?(id, cmd, opts)
resolveEndpoint(id) / routeIngress(req) / resolveIngress(...)
ensureAppRuntimeStarted(id)         listManagedRunningSandboxes?()
```

Workload types: `session`, `app`, `monitor`. Implementations: **Platinum**
(their managed microVM cloud, ~1.6k lines) and **Daytona** (deprecated: keeps
existing boxes, no new ones). There is no local Docker or Firecracker driver;
self-hosters still need `PLATINUM_API_KEY`.

### Inside the machine

- Ubuntu 24.04, user `kortix` with passwordless sudo; Node, pnpm, Python + uv,
  Bun, git, ripgrep, Chromium, LibreOffice, ffmpeg, Pandoc, LaTeX, etc.
- `MACHINE.md` is shipped into the VM and tells the agent what the machine
  is, what persists, and how to use the desktop. A pattern worth copying.
- **Desktop**: XFCE on `DISPLAY=:1`, streamed from port 6901, which also
  serves `/screenshot`, `/wait` (screen still for 300 ms) and `/record`.
  `agent-browser` runs headed in the one shared browser; the user can **Take
  over** and **Return control**. Project agents get a desktop; My Kortix runs
  on a smaller headless box.
- **Previews**: any port is reachable at `p<port>-<sandboxId>` subdomains or
  `/proxy/<port>/`, behind Kortix sign-in. Public share links are opt-in.

---

## 6. kortixd: the in-VM daemon

One compiled Bun binary (`kortixd`, also emitted as `kortix-agent`), strictly
layered and lint-enforced:

```
 app       src/main.ts, src/app/, src/routes/   HTTP controllers (Hono)
   ↓
 harness   src/harness/                         the session runtime: pi adapter
   ↓
 services  src/services/<name>/                 config-provider, egress-shim,
                                                llm-proxy, sandbox-env, skills,
                                                tools, event-bus, resources...
   ↓
 shared    src/lib/, src/types/
```

Boot sequence:

1. Read env.
2. Start the static web server (:3211), so previews work while booting.
3. Download the config release (S3 project snapshot first, API archive second).
4. Resolve skill directories: managed Kortix skills from an image overlay, then
   exactly the skills the release registers. Nothing written into `/workspace`.
5. Start the HTTP server (:8000).
6. Start **pi** in-process. pi is an open-source agent harness; it replaced
   OpenCode on 2026-10-23. The harness boundary (`harness.ts`) allows other
   adapters but only `pi` is registered.

Key routes: `GET /kortix/health` (readiness + capability list such as
`session.steer`, `session.compact`, `session.subagents`), `POST
/kortix/refresh`, `/kortix/runtime/*` (transcript, turn verbs, workflows),
plus file and proxy routes.

What the daemon deliberately does **not** do: triggers, channels, connectors,
secret decisions. Those live in the API; the VM only gets env vars and handles.

### Models

pi sends every model call to the **Kortix LLM gateway** under one provider id
(`kortix/<model>`) via a localhost credential-injecting proxy. No provider keys
live in the VM; without gateway env, pi refuses to start. (Exception: Claude
subscription models via the Claude Agent SDK.)

The gateway (`apps/llm-gateway`) authenticates, reserves billing, resolves one
model/upstream, relays provider bytes, and settles usage. It does **not**
retry; the client (pi) owns retries. Routing policy (default model, fallbacks)
and budgets are per workspace.

---

## 7. Permissions: three separate systems

### a. Tool-call rules (inside the agent loop)

`agents.<name>.permission`, evaluated by the harness:

```yaml
permission:
  bash: { "*": allow, "git push *": ask, "rm -rf *": deny }
  edit: { "*": allow, "secrets/*": deny }
  external_directory: deny
  webfetch: ask
```

- Keys are capabilities (`read edit glob grep bash external_directory question
  webfetch websearch`) or tool names; a tool name wins over its capability.
- Bash globs apply to **each command** in a pipeline/chain; strictest wins
  (`deny` > `ask` > `allow`).
- Most specific (longest) pattern wins regardless of order.
- `ask` produces a permission request the user answers `once`, `always`, or
  `reject`.

### b. Connector policies (in the control plane)

Every connector call is decided server-side, first match wins, in order:

1. Workspace `policies`
2. The agent's **policy set** for that connector
3. The connector's base `policies`
4. No match → a `sensitive` connector asks; otherwise `policy.default_mode`
   (e.g. `risk`: reads run, writes ask)

Rules are `{ match: "gmail.send_email", action: allow|require_approval|block,
conditions: [{ arg: to, match: "*@example.com" }] }`. A lower level can never
loosen a workspace rule. Read vs write is inferred from HTTP method / GraphQL
op / MCP annotation, or from verb prefixes for catalog apps.

Approvals: one approval = one exact call, executed once by the server as the
agent; rules are re-checked at execution; TTL default 24h; no "approve all".

### c. Platform permissions (who can do what)

- `kortix_permissions` on the agent (e.g. `project.gitops.read`) gate what the
  agent can do with the platform itself.
- A session runs with the **agent's** permissions, not the launching person's,
  but some things need both (repo read needs the agent grant *and* the person's
  `project.file.read`).
- People: roles, groups, per-resource grants, SSO/SCIM, audit log.

---

## 8. Secrets and connectors: credentials never enter the VM

Secrets have an audience (workspace / only me / chosen people, groups, agents)
and a **delivery strategy**:

| Strategy | What the VM sees |
|---|---|
| `runtime` | Real value as an env var |
| `egress` | A worthless stand-in handle; kortixd's **egress shim** swaps in the real value only on requests to allowed hosts |
| `broker` | Nothing; the agent asks the API to make one policy-bound HTTPS request |
| `denied` | Nothing |

Connectors (OAuth logins, API keys, MCP servers) are stored by the API. The
agent calls them through the API (the in-VM `kortix` CLI/tools with the
session's scoped token); the server picks the account, applies policy, makes
the call, records it. The VM never holds the connector credential. Connector
requests to private/internal addresses are refused.

Account scope: private (default, only in your own sessions), workspace-shared,
or chosen audience. Triggers can only use workspace-shared accounts.

Each session gets one scoped token (`KORTIX_TOKEN`) carrying principal,
project, session, and agent grant.

---

## 9. Prompt delivery: a durable queue, not an HTTP call

A user message is not POSTed straight into the VM. It becomes a row in
`session_lifecycle_commands` (`continue_session`) and a drain delivers it:

```
 send() ──► row queued (durable; send resolves here)
              │ NOTIFY / poll
              ▼
         worker claims row (5 min lease + heartbeat)
              │ wake VM if stopped, wait for readiness (≤5 min)
              ▼
         POST to kortixd via sandbox proxy  ──► turn runs
              │
              ├─ live turn + same user → "steer" into the running turn
              ├─ runtime down → park 30 s / 2 m / 8 m, then dead-letter
              ├─ failure → up to 5 attempts, backoff min(60 s, 2 s × n)
              └─ forwarded but never consumed → redeliver ≤3 times
```

Dedupe by delivery key and wire id; large prompts are read back to prove they
landed; interrupted turns get one automatic continuation. Every limit is in
`apps/api/src/projects/session-lifecycle/README.md`. This queue is what makes
"sleep, wake on message, never lose a prompt" work.

---

## 10. Multi-agent: sessions talking to sessions

Every session has the same multi-agent tools:

| Mechanism | How it works |
|---|---|
| **Hand-off** (`agent` / `task`) | Starts a child agent in its own session (own VM). From a top-level session it runs in the background; the result arrives later as a message that starts a new turn. |
| **Workflow** (`workflow`) | A deterministic JS script (`agent()`, `parallel()`, `pipeline()`, JSON-schema outputs) run by a background runner session. ≤50 agents, ≤8 concurrent, 2 h cap, resumable with cached results. Saved as `.kortix/workflows/<name>.js`. |
| **Peer messages** | `send_message` / `list_agents` between sessions; inbound policy `accept` / `hold` / `refuse` (`session_peer_messages`). |
| **Teams** (experimental) | A lead plus teammates sharing a task list (`session_tasks`). |
| **Loops / reminders** | An agent schedules its own wake-up before ending a turn ("follow-through"). |

A child never gets more tools than its caller. Depth is capped
(`KORTIX_PI_CLOUD_SUBAGENTS_MAX`, default 4).

---

## 11. Triggers

Sources: `schedule` (cron / every / run_at), `webhook` (signed), `event`
(connector events), `app`, `change_request` (on merge). Targets: start a new
session of an agent, wake an existing session, or call an App.

- A filter in code runs before any model, to drop noise.
- Session strategy per trigger: fresh session each run, reused loop session,
  or pinned session.
- Fires are durable rows with a lease and per-slot idempotency key, run by the
  leader-elected worker (`apps/api/src/worker/`).

---

## 12. Data model (core tables)

All in the `kortix` Postgres schema (`packages/db/src/schema/kortix.ts`):

| Table | Key columns |
|---|---|
| `projects` | `project_id`, `account_id`, `repo_url`, `default_branch`, `manifest_path`, `secret_default_strategy` |
| `project_sessions` | `session_id` (= branch), `project_id`, `branch_name`, `base_ref`, `sandbox_provider`, `sandbox_id`, `agent_name`, `status`, `visibility`, `origin` (user/trigger/channel/api/system), `parent_session_id` |
| `sandboxes` | `sandbox_id`, `provider`, `external_id`, `status`, `base_url`, `config`, `last_used_at` |
| `session_lifecycle_commands` | `command_type`, `status`, `payload`, `attempts`, `available_at`, `locked_by`, `locked_until`, `idempotency_key` |
| `config_releases` / `config_release_heads` | `release_id`, `variant`, `source_commit`, `proven_at` |
| `connectors` | `slug`, `provider_type`, `config`, `auth_secret`, `share_scope` |
| `session_peer_messages`, `session_tasks` | Multi-agent messaging and team tasks |
| `session_presence_leases`, `session_idle_watches` | Keep-awake and idle-stop tracking |
| `project_session_secret_handles` | Egress secret stand-ins per session |
| `drives`, `drive_grants`, `artifacts` | Drive and shared artifacts |

---

## 13. Self-host reality

`kortix self-host init/start` writes a Docker Compose stack: frontend,
kortix-api, llm-gateway, a migrate job, an updater (mounts the Docker socket),
MinIO (snapshots/capture), the full Supabase distribution, Caddy (TLS) or
cloudflared.

**Off the box:** sandboxes, Drive volumes and App volumes all live at
Platinum. Without `PLATINUM_API_KEY`, sessions don't start and there is no
Drive. "Local" mode (no domain/tunnel) cannot run sessions because the VM must
call back to the API's public URL.

---

## 14. Takeaways for agentspace

**Worth adopting**

- Agent = declarative config; registries + grants with "omitted means none".
- Config compiled into immutable releases; behavior can be tested on a branch,
  but governance only comes from the default branch.
- Credentials never enter the VM: egress-proxy secret swap, server-side
  connector execution, a localhost LLM proxy with no provider keys inside.
- Durable prompt queue in Postgres in front of the VM, with leases, idempotency
  and wake-on-deliver.
- Thin in-VM daemon with a strict boundary: it runs the loop and serves files,
  the control plane owns everything else.
- `MACHINE.md`-style machine description handed to the agent.
- Capability flags on the runtime health endpoint, so clients gate features
  on what the runtime serves.

**Where agentspace can differ**

- **Truly self-hosted computers**: a pluggable driver with local Docker (and
  later Firecracker) implementations of a provider interface like Kortix's.
- **Python control plane** (FastAPI) instead of Bun/Hono.
- **Simpler state model to start**: Kortix's git-repo-as-source-of-truth and
  change-request flow is powerful but heavy; agentspace can start with agents
  stored in Postgres and add git-backed config later.
- **Persistence choice**: Kortix keeps only `/workspace` and makes installed
  packages ephemeral (template rebuild to keep them). agentspace's planned
  copy-on-write clone of an agent template disk, with explicit "save to
  template", is a different trade-off worth stating in the spec.

**Licensing note**: Elastic License 2.0 forbids offering it as a managed
service and circumventing license keys. Use it for design ideas, not copied
code, in an open-source agentspace.
