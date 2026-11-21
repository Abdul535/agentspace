# ADR-020 — "Workshop" design system: tokens in DESIGN.md, dark by default

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** Project owner

## Context
The web app used shadcn's default neutral theme (rounded, light only, no brand color). The owner wanted a simple, boxy but modern look, inspired by industrial catalogs like McMaster-Carr. Three directions were sampled (Catalog, Blueprint, Workshop), then the Workshop direction was tried with five accent colors.

## Decision
- Adopt "Workshop": graphite dark theme, square corners, 1px borders instead of shadows, one safety-yellow accent (`#f5c518`). The rules live in `DESIGN.md` at the repo root.
- Keep shadcn's token names in `globals.css` (`:root` = light, `.dark` = dark), so the existing components restyle without edits. Brand yellow is `--primary`. New tokens: `--link` (yellow for text, deeper in light mode for contrast), `--success`, `--success-surface`.
- `--radius: 0`: every `rounded-*` class reads it, so corners go square app-wide; `rounded-full` stays round for dots and switches.
- One fixed accent, no accent picker.
- Dark is the default (`<html class="dark">`). A sun/moon button switches to light and saves the choice in `localStorage`. An inline `<head>` script applies the saved choice before first paint (the Next.js "preventing flash before hydration" guide), so there's no wrong-theme flash.

## Alternatives considered
- **Accent picker (user-selectable colors)** — flexible, but more tokens to keep readable on both themes; not needed now.
- **`next-themes`** — handles the same flash problem, but it's a dependency for about 15 lines of our own code.
- **Follow the OS theme by default** — the owner chose dark first.

## Consequences
- New UI must use tokens (`bg-primary`, `text-link`, `text-success`, …), never palette classes like `text-emerald-600`. Yellow text must use `text-link`, never `text-primary`.
- A new color means adding a token to `DESIGN.md` and `globals.css` first.
- Server rendering always emits the dark class; light-mode users depend on the inline script running.
- Body text is still shadcn's 14px; the 13px base in `DESIGN.md` is a later step.
