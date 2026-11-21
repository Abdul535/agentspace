# ADR-023 — Landing page as a separate static app

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
agentspace is becoming a "learn by visualization" product, agents first, with one subdomain per topic later. It needs a public page that explains what it is, ranks in search, and gets quoted by AI assistants (SEO and AEO), and that sends people to the app ("Try the app"). Crawlers and AI fetchers often don't run JavaScript, so the words must be in the HTML.

## Decision
- A new Next.js app, `apps/landing`, in the pnpm workspace, separate from `apps/web`.
- `output: "export"`: `next build` writes plain files to `apps/landing/out/`, so any static host can serve it. No server, no API calls.
- Workshop design (DESIGN.md, ADR-020) with the same tokens as `apps/web`. Dark by default; light follows the OS through `prefers-color-scheme` (no toggle, no script).
- All copy lives in `src/lib/site.ts`. The page, its JSON-LD and `/llms.txt` read from it, so they never disagree.
- For search and answer engines: title, description, canonical, Open Graph and Twitter tags (`metadata` in `layout.tsx`); JSON-LD with `WebSite`, `SoftwareApplication` and `FAQPage`; `robots.txt` allowing all crawlers, naming AI ones (GPTBot, ClaudeBot, PerplexityBot…); `sitemap.xml`; `/llms.txt` (llmstxt.org) as Markdown; `/og.png` share image drawn at build time.
- The FAQ is visible text, not hidden in an accordion, so people and crawlers read the same answers.
- `SITE_URL` (the page's own address, for canonical and absolute URLs) and `APP_URL` (where "Try the app" goes) are read at build time. Neither has a production value yet.

## Alternatives considered
- **A route inside `apps/web`** — one app, but the landing page would need the whole app (API rewrites, server) running and couldn't sit on its own subdomain.
- **Static HTML by hand** — no build step, but no shared components or design tokens, and the SEO files would be copied text that drifts.
- **`opengraph-image.tsx`** — the file convention exports a file named `opengraph-image` with no extension, which static hosts serve with the wrong type. A `/og.png` route handler exports a real `.png`.

## Consequences
- A third CI job and a `check:landing` script; `pnpm check` runs it.
- Anything needing a server (forms, a waitlist, redirects, headers) can't live here without dropping `output: "export"` or adding a separate service.
- "Try the app" only works for visitors once the app is hosted; until then it points at a local URL.
- Later topics (networks, system design) can copy this app or turn it into a template.
