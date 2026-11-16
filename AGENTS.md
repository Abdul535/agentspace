## Tech stack

One monorepo:

- `apps/api/`: Python backend with FastAPI.
- `apps/web/`: Next.js 16 frontend (App Router, TypeScript, Tailwind CSS 4, `src/` dir). UI components from shadcn/ui (Radix base, added with `pnpm dlx shadcn@latest add <name>` into `src/components/ui/`); icons from lucide-react. Server state with TanStack Query (`QueryClientProvider` in `src/app/providers.tsx`); don't fetch API data in `useEffect`. The web app calls the API at `/api/*` (rewritten to FastAPI in `next.config.ts`) through the typed client in `src/lib/api/`; after changing an API route or model, run `pnpm gen:api` from the root to regenerate `src/lib/api/schema.d.ts`, and commit it. JS deps managed with pnpm workspaces from the repo root (`pnpm-workspace.yaml`, root `pnpm-lock.yaml`); run it with `pnpm dev:web`. Read `apps/web/AGENTS.md` before writing Next.js code: this Next.js version differs from older ones.
- Database: SQLite, accessed from Python with SQLModel (async, aiosqlite). Schema migrations with Alembic in `apps/api/migrations/`. The backend owns the schema; the frontend reaches data only through the API.
- Package manager for Python: uv. Add deps with `uv add`, run commands with `uv run`; never use pip, poetry or bare `python`/`venv`. Commit `uv.lock`.

Coding-agent skills live in `.agents/skills/`, with a symlink per skill in `.claude/skills/`. Manage them with the `skills` CLI, never by copying files: `npx skills add <owner/repo> --skill <name> -a universal claude-code -y` to add, `npx skills update` to update, `npx skills install` to restore from `skills-lock.json` (commit it). Use the `fastapi` skill for backend work and the `make-interfaces-feel-better` skill for frontend UI work. The app is self-hosted: ignore its FastAPI Cloud deployment suggestions.

## Rules

Build agents from scratch, with no agent framework (no LangChain, LlamaIndex, CrewAI, Agent SDKs, etc.): the agent loop, tool calling, provider adapters, skills and sessions are our own code. Use pi (MIT, upstream clone at `../pi-upstream`, from https://github.com/earendil-works/pi) as the reference design: read how it does a thing before building ours, then write our own version. Don't vendor or import pi packages. Build only what the user asks for, one step at a time.

Comment the code with short one-line comments: every module, class and function gets a one-line docstring (Python) or `//` comment (TypeScript) saying what it does, and a non-obvious block gets a one-line comment before it saying why. Explain intent, not syntax; don't comment lines that already read clearly.

The project owner is new to Python and comes from TypeScript. In Python code, when a construct has a TypeScript counterpart or behaves differently from what a TS developer would expect, add a one-line `# TS:` comment comparing them (e.g. `# TS: like an interface — any class with these members fits, no "implements" needed`). Cover each idiom at its first use in a file, not on every line: Pydantic models (≈ zod schema + type), `Protocol` (≈ interface), `async def` + `yield` (≈ `async function*`), `async with` (≈ `await using`/try-finally), FastAPI `Depends` (≈ dependency injection), decorators, comprehensions (≈ `map`/`filter`), `match`, `:=`, `or` vs `??`.

Learnings live in `.agents/learnings/`, one Markdown file per topic named after the problem (e.g. `python-editor-shows-any.md`). When the user says "add to my learnings" (or similar), write the topic just discussed as a new file there, or update the existing file on that topic, with these sections: **Date**, **Symptom** (what was seen), **Cause** (why it happened, with a TypeScript comparison where it helps), **Fix** (exact steps or config), and **Not a bug** (related behavior that is expected), skipping sections that don't apply. Check `.agents/learnings/` before fixing a problem that looks familiar.

Decisions live in .agents/adr/ as ADRs. Read the ADRs for a feature before changing it. Record each new architectural decision as `ADR-NNN-<slug>.md` (next number in sequence), copied from `.agents/adr/ADR-template.md`. Don't rewrite an accepted ADR; supersede it with a new one and set the old one's status to "Superseded by ADR-XXX".
