# ADR-019 — Cost on every reply: OpenRouter's bill, else a catalog estimate

- **Status:** Accepted
- **Date:** 2026-11-21
- **Deciders:** project owner

## Context
ADR-014 step 34: price each model reply, save it on `Usage`, and show a
per-session total in the web app. `Usage` held only token counts, and Langfuse
traces (ADR-018) priced generations from its own table.

pi (`../pi-upstream`), read for this ADR:

- `ai/src/types.ts`: `Usage.cost` is `{input, output, cacheRead, cacheWrite,
  total, totalFromProvider}`.
- `ai/src/models.ts` `calculateCost`: tokens × the model's catalog price per
  million, always computed first.
- `ai/src/utils/openrouter-cost.ts` `applyOpenRouterReportedCost`: when the
  OpenRouter response carries a valid `usage.cost`, it replaces `total` and sets
  `totalFromProvider`. With the user's own provider key (BYOK), `cost` is only
  OpenRouter's fee, so `cost_details.upstream_inference_cost` is added.
- `coding-agent/src/core/usage-totals.ts`: session totals are sums over the
  stored replies, not a counter kept on the session.

A live check showed OpenRouter puts `cost`, `is_byok` and `cost_details` in the
last streamed chunk's `usage` with no extra request flag.

## Decision
- **`Usage.cost` is a `Cost`** (`input`, `output`, `total` in US dollars, and
  `from_provider`). It's saved inside the message JSON, so no migration: older
  rows load with a $0 cost.
- **The adapter prices the reply, as pi's does.** The request `Context` carries
  the model's catalog `Price` (per million tokens; `ModelInfo.price()`, None when
  the catalog lists none or it varies). `parse_usage` estimates from it, then
  `billed_cost` swaps in OpenRouter's `usage.cost` when it's a valid number
  (BYOK adds the upstream cost; an invalid or incomplete figure keeps the
  estimate). No extra call to OpenRouter.
- **The session total is a sum in the web app** over the transcript it already
  has (`SessionCost` in the chat header), so it updates live as replies finish.
  Compaction never deletes messages, so the sum covers the whole session. A `≈`
  marks a total with any reply that wasn't billed.
- **Langfuse gets our cost** as `cost_details` on each `model` generation (plus
  `cost_from_provider` in its metadata). With no cost we send none, and Langfuse
  falls back to its own estimate.

## Alternatives considered
- **A running total stored on the session** — cheap to read in a session list,
  but a second copy of the truth that can drift (a crash between saving the
  reply and the total) and needs a migration. Not needed until a list view
  shows costs.
- **Pricing in the API layer after the reply** — keeps the adapter unaware of
  prices, but the billed figure is wire-format detail only the adapter sees, and
  the estimate would need a second place that touches every reply.
- **Asking OpenRouter's `/generation` endpoint for each reply's cost** — exact,
  but one more request per reply when the stream already carries it.

## Consequences
- A model the catalog doesn't know, and that isn't billed through OpenRouter
  (a future second provider), shows $0 until it has a price.
- Cached tokens are priced as normal input until step 36 splits them out.
- Compaction's summary request isn't counted in the total: its reply isn't
  saved as a message. pi counts it under "Tools/summaries"; we'd need to store
  its usage on the compaction entry first.
