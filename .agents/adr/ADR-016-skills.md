# ADR-016 — Skills: a shared folder, listed in the prompt, read on demand

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
ADR-003 step 23. A skill is a folder with a `SKILL.md` (YAML frontmatter, then
instructions) and optional `scripts/`, `references/` and `assets/`. With 30
skills of ~2,000 words each, putting them all in the prompt would cost ~80k
tokens on every turn, most of it unused.

Constraints in our code:

- The system prompt is built on each run by `build_system_prompt(preamble,
  tools)` (`agent/prompt.py`), with `<tools>`, `<rules>` and `<workspace>`
  sections. It is not stored, so a change shows on the next run.
- Every file tool resolves paths with `resolve_in_workspace`
  (`tools/paths.py`), which refuses anything outside the session's workspace.
  A skill folder outside the workspace can't be read today.
- `bash` (off by default) runs unsandboxed and can already read anything.

How pi does it (`coding-agent/src/core/skills.ts`, `docs/skills.md`), after
the Agent Skills spec (agentskills.io):

- **Discovery.** Scans user and project skill dirs (`~/.pi/agent/skills`,
  `.pi/skills`, `~/.agents/skills`, `.agents/skills`). A folder holding
  `SKILL.md` is one skill and is not searched further; otherwise it recurses,
  skipping dot-folders and `node_modules`, following symlinks.
- **Frontmatter.** `name` (lowercase letters, digits, single hyphens, ≤64
  chars) and `description` (required, ≤1024 chars). A missing description or
  broken frontmatter skips the skill with a diagnostic; other problems are
  warnings. On a name clash the first one found wins.
- **The prompt** (`formatSkillsForPrompt`, line 358): only name, description
  and location, as `<available_skills>` XML, with "Use the read tool to load a
  skill's file when the task matches its description" and "Resolve relative
  paths against the skill's directory". The body is never in the prompt.
- **Reading.** The model calls the normal `read` tool on the location. pi's
  `read` can open any path, so no extra access is needed. There is no search
  tool.
- `disable-model-invocation: true` hides a skill from the list; it is then
  reachable only by the user typing `/skill:name`.

## Decision
**1. One shared folder.** A new setting `skills_dir` (env
`AGENTSPACE_SKILLS_DIR`, default `data/skills`) holds every skill, shared by
all sessions. Skills are added there with the file system (or `npx skills add
... -a universal` pointed at it); there is no upload API. A missing folder
means no skills.

**2. Loading** (`agent/skills.py`). `load_skills(root) -> (skills,
diagnostics)` walks the folder as pi does: a folder with `SKILL.md` is a skill
and is not searched further; dot-folders and `node_modules` are skipped;
symlinked folders are followed. Each `Skill` holds `name`, `description`,
`file_path` and `base_dir`, both resolved (symlinks followed) and absolute.

- Frontmatter is parsed with PyYAML (`uv add pyyaml`, `yaml.safe_load`).
- Rules as pi: description required (else skipped), name defaults to the folder
  name, name and length rules give a warning, first name wins on a clash.
- Diagnostics are logged as warnings; nothing fails the run.
- It is loaded on each run, next to `build_system_prompt`, run with `asyncify`
  since it touches the disk. A skill added or edited shows on the next run,
  without a restart. 30 small files is cheap to rescan.

**3. The prompt.** `build_system_prompt` takes the skills and adds a `<skills>`
section, worded after pi's:

```
The following skills give specialized instructions for specific tasks.
Use the read tool to load a skill's file when the task matches its description.
Resolve relative paths in a skill against its folder (the parent of SKILL.md). Use absolute paths in tool calls.
<available_skills>
  <skill>
    <name>pdf-tools</name>
    <description>Extract text and tables from PDFs. Use when ...</description>
    <location>/abs/path/data/skills/pdf-tools/SKILL.md</location>
  </skill>
</available_skills>
```

Values are XML-escaped. The section is left out when there are no skills, or
when the run has no `read` tool (with only `bash`, the line says "Use bash to
load"; with neither, the model couldn't open them). The `<workspace>` section
adds: "Skill folders listed in `<skills>` can be read, but not changed."

**4. Read-only access (owner's choice).** `ToolContext` gains `read_roots:
tuple[Path, ...]`, the resolved `base_dir` of each loaded skill.
`tools/paths.py` gains `resolve_readable(ctx, path)`: a relative path still
resolves against the workspace; an absolute path is allowed inside the
workspace or inside one of the read roots, and refused otherwise. `read`,
`ls`, `grep` and `find` use it; `write` and `edit` keep
`resolve_in_workspace`, so the model can't change a skill. Using each skill's
resolved folder (not `skills_dir`) means a symlinked skill works, and nothing
else under `skills_dir` (stray files, a broken skill) becomes readable.

**5. No search tool.** Every skill is listed. At ~1,024 chars of description
at most, 30 skills cost ~1–2k tokens per turn, and the model picks from the
list directly. A search tool can wait until there are hundreds.

**6. A skills page in the web app** (after opencode's `GET /api/skill` and its
Settings → Extensions → Skills tab; pi has no web UI).

- `GET /skills` returns `skills_dir`, each skill's name, description and
  location, and the diagnostics. `GET /skills/{name}` returns one skill plus
  its `SKILL.md` body without the frontmatter. Both rescan the folder on each
  request, as runs do. The detail route looks a skill up by name among the
  loaded ones and never takes a path, so it can serve nothing else. It also
  returns the skill's exact `<skill>` prompt entry, its files, and a size
  estimate (characters / 4). `GET /skills/{name}/files/{path}` serves a text
  file inside that skill's folder (no dot-files, binaries or files over 1 MB).
- `/skills` shows a searchable card grid (name, description, size), the
  diagnostics in an alert, a bar comparing the always-sent menu with all guides
  in full, and `skills_dir` when there are none. `/skills/[name]` renders the
  body with Streamdown beside a panel with the prompt entry, files and
  location; `/skills/[name]/[...path]` shows one of its files. Relative links
  in a skill resolve against its folder's URL instead of being blocked. A
  "Skills" link sits at the bottom of the sidebar.
- Fresh: both queries have `staleTime: 0`, so TanStack Query refetches on each
  visit and when the window regains focus. No file watcher or push event
  (opencode sends a `skill.updated` event); a skill added while the page is
  open shows on the next focus.

**Not built:** per-session skill choice, project skills inside the workspace
(`.agents/skills/`), `/skill:name` commands (ADR-014 step 37) and with them
`disable-model-invocation` (ignored for now: every valid skill is listed),
`allowed-tools`, adding or editing skills from the web app, and skill
authoring (step 49).

## Alternatives considered
- **Copy or link skills into each session's workspace (option B)** — `read`
  needs no change, but each session gets its own copy that the model can edit or
  delete, copies go stale when a skill is updated, and a link would be resolved
  and refused by `resolve_in_workspace` anyway.
- **Put every `SKILL.md` in the prompt** — no tool call needed, but ~80k tokens
  per turn for 30 skills, most of it unused.
- **A `search_skills` tool (owner's first idea)** — saves prompt space with
  hundreds of skills, but costs an extra call, and the model can only search for
  something it knows exists. pi lists them all.
- **A dedicated `load_skill(name)` tool** — no path access change, but skill
  files beyond `SKILL.md` (references, scripts) would still need `read`, so the
  read-only roots are needed either way.

## Consequences
- The model can read (never write) skill folders from every session. Anything
  put in `skills_dir` is visible to every session's model; don't put secrets
  there.
- Running a skill's `scripts/` needs `bash`, which is off by default. Without
  it, skills are instructions and reference files only.
- Adding or removing a skill changes the system prompt, which breaks the prompt
  cache for the next turn of each running session.
- Four file tools switch to `resolve_readable`; their tests need cases for a
  skill path (allowed) and a path next to it (refused).
