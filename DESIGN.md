# DESIGN.md: agentspace "Workshop" design system

The visual rules for `apps/web`. Read this before building or restyling UI. Inspired by industrial catalogs like McMaster-Carr: simple, boxy and dense, made modern with a graphite dark theme and one safety-yellow accent.

## Principles

1. **Boxes and lines, not shadows.** Structure comes from 1px borders and flat fills. Nothing floats except overlays (dialogs, popovers).
2. **Square corners.** Radius is `0` everywhere. The only round things are status dots.
3. **One accent, used sparingly.** Safety yellow marks the single most important action or state on screen: the primary button, focus, the selected item, the active run. If everything is yellow, nothing is.
4. **Dense but readable.** Small text (13px base), tight padding, tabular numbers. A screen should show a lot without feeling cramped.
5. **Labels speak mono.** Small uppercase mono text names things (section labels, roles, metadata). Content stays in the sans font.

## Themes

Dark is the default. Light mode follows the OS setting and can be toggled. Both themes share every rule below; only the color values change.

## Color palette

Tokens use shadcn's names, so the components in `src/components/ui/` pick them up without changes. Note that shadcn's `accent` is the **hover fill**, not the brand color. The brand yellow is `primary`.

| Token | Dark | Light | Used for |
|---|---|---|---|
| `--background` | `#141414` | `#f4f4f2` | Page |
| `--card` / `--popover` | `#1c1c1c` | `#ffffff` | Panels, cells, inputs, overlays |
| `--foreground` | `#ececec` | `#141414` | Body text |
| `--muted` | `#222222` | `#efefec` | Header strips, table heads |
| `--muted-foreground` | `#9a9a9a` | `#6b6b6b` | Secondary text, labels |
| `--accent` | `#262626` | `#f6f6f3` | Hover / selected fill |
| `--secondary` | `#262626` | `#efefec` | Secondary buttons |
| `--border` | `#2e2e2e` | `#dcdcd8` | Hairlines between boxes |
| `--input` | `#454545` | `#b5b5b0` | Input and button outlines (stronger) |
| `--primary` | `#f5c518` | `#f5c518` | Primary button, selection bar, top stripe |
| `--primary-foreground` | `#111111` | `#111111` | Text on yellow |
| `--link` | `#f5c518` | `#8a6d00` | Yellow **text** (links, "done" marks) |
| `--ring` | `#f5c518` | `#f5c518` | Focus outline |
| `--destructive` | `#f87171` | `#c62828` | Delete, errors |
| `--success` | `#7bd88f` | `#1f6f3a` | Connected, passed |
| `--success-surface` | `#1f3324` | `#e4efe6` | Badge fill behind success text |
| `--sidebar` | `#0d0d0d` | `#ffffff` | App sidebar and top bar |

**Why `--link` exists:** bright yellow reads well on graphite but fails contrast as text on white. Light mode fills with `#f5c518` but writes with the deeper `#8a6d00`. Never use `text-primary` for text; use `text-link`.

**Red is only for danger.** Don't use it for emphasis or branding.

**Charts:** `--chart-1` yellow `#f5c518`, `--chart-2` blue `#3b82f6`, `--chart-3` lime `#a3e635`, `--chart-4` orange `#ff6a13`, `--chart-5` gray `#9a9a9a`.

## Typography

| Role | Font | Size / weight |
|---|---|---|
| Page title | Geist | 18px / 700, tracking -0.01em |
| Section heading | Geist | 12–13px / 700, with a 1px rule underneath |
| Body, UI | Geist | 13px / 400, line-height 1.5 |
| Item title, button | Geist | 12.5px / 600 |
| Label, metadata | Geist Mono | 10.5px / 400, uppercase, tracking 0.06em, muted |
| Code, tool calls, paths | Geist Mono | 11.5–12px |

Use `tabular-nums` wherever numbers line up (counts, tables, timings).

## Spacing, radius, borders

- **Spacing scale:** 4, 6, 8, 10, 14, 16, 24px. Cells and table rows pad 5–10px; panels pad 14px.
- **Radius:** `--radius: 0`. Status dots are the only exception (`rounded-full`).
- **Borders:** 1px solid `--border` between boxes; `--input` for controls. Dashed `--input` for tool-call boxes, to show they come from the machine and not the person.
- **Grids share borders:** adjacent cells draw one line between them, never two (top/left on the grid, right/bottom on each cell).
- **Elevation:** none on the page. Dialogs and popovers get one soft shadow (`0 8px 24px rgb(0 0 0 / .35)`) plus a border.

## Motion

Short and quiet: color and background changes 150ms ease, button press `scale(0.97)`, collapsibles as they are now. Respect `prefers-reduced-motion`.

## Components

**Top bar / sidebar:** `--sidebar` fill, 1px bottom (or right) border, and a **3px yellow stripe** across the very top of the app. The stripe is the brand mark; don't repeat it elsewhere.

**Button**
- *Primary:* yellow fill, `#111` text, 600 weight. At most one per view.
- *Default/outline:* `--card` fill, `--input` border, `--accent` on hover.
- *Ghost:* no border until hover. Use it in toolbars and lists.
- *Destructive:* outline style with `--destructive` text; fills red only in the confirm step.
- Height 28–32px, padding 6px × 12px, square corners.

**Input / textarea:** `--card` fill, `--input` border. On focus, a 2px `--ring` outline inset by 1px, so the box doesn't change size.

**Badge:** mono 10.5px, 2px × 6px padding, square. Neutral = `--input` border. Status = tinted surface fill (`--success-surface` with `--success` text) and no border.

**Category grid / list cell:** a bordered cell with a 34px square icon box, a 600-weight `--link` title, and a muted count below it. Hover = `--accent` fill. Selected = `--accent` fill plus a 2px yellow bar on the left (`box-shadow: inset 2px 0 0 var(--primary)`).

**Table:** every cell bordered. Head row on `--muted` with muted 11px 600 labels. Rows on `--card`, hover `--accent`, numbers tabular.

**Chat message:** a bordered box with a header strip (`--muted`) holding the mono role label on the left and timing on the right. The body is padded 8px.

**Tool call:** a dashed mono box inside the message: the command on the left, status on the right (`✓` in `--link`, `✕` in `--destructive`, a spinner while running).

**Dialog / popover:** `--popover` fill, 1px border, the overlay shadow, square corners. Title in the page-title style.

**Icons:** lucide-react at 16px with stroke width 1.75, in `currentColor`.

## Do / don't

- Do separate things with lines and fills. Don't add shadows or rounded cards.
- Do keep yellow for one thing per view. Don't use yellow text on light backgrounds; use `--link`.
- Do use mono uppercase for labels. Don't use it for sentences.
- Don't add new colors in components. Add a token here first.

## Implementation notes

- Tokens live in `apps/web/src/app/globals.css`: `:root` holds the light values and `.dark` the dark ones. `--link`, `--success` and `--success-surface` are new and need `--color-*` entries in `@theme inline`.
- Dark by default: render `<html class="dark">`; a theme toggle removes the class and follows the OS setting.
- Set `--radius: 0`: every `rounded-*` class (and the `min()`/`calc()` ones in shadcn components) reads it, so all corners go square with no component edits. `rounded-full` doesn't read it and stays round, which is intended for dots and switches.
- Replace hard-coded palette colors with tokens: `text-emerald-600` in `components/mcp/mcp-servers.tsx` → `text-success`.
- Fonts: keep the Geist and Geist Mono already loaded in `layout.tsx`.
