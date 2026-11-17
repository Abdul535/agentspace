# ADR-005 — Model catalog from OpenRouter, pinned favourites, switching mid-session

- **Status:** Proposed
- **Date:** 2026-11-17
- **Deciders:** project owner

## Context
Each session stores `provider` and `model` (ADR-001). The model is set when the
session is created, from the request or `AGENTSPACE_DEFAULT_MODEL`. The web app
sends no model, so every session gets the default, and there is no way to change
it. The owner wants to pick a model in the web app at any point in a session.

pi keeps a catalog of models per provider (`packages/ai/src/models.generated.ts`,
built by `packages/ai/catalog/` from OpenRouter's `/api/v1/models` and models.dev)
and refreshes it at runtime. Its selector
(`coding-agent/src/modes/interactive/components/model-selector.ts`) searches by
provider, id and name, shows only providers with credentials, and can narrow the
list to "scoped" favourite models. `AgentSession.setModel` swaps the model for the
next turn and logs the change.

Our only provider is OpenRouter. The agent needs tool calling, and not every
OpenRouter model supports it.

## Decision
- **The catalog is OpenRouter's live list, fetched by the API.**
  `GET /api/models` calls OpenRouter's public `GET /models` endpoint and keeps the
  result in memory for an hour. It returns only models whose
  `supported_parameters` include `tools`, trimmed to id, name, context length and
  price per million input and output tokens. If OpenRouter can't be reached and
  nothing is cached, the route answers 502. The web app never calls OpenRouter
  directly.
- **Favourites come from config.** `AGENTSPACE_FAVORITE_MODELS` (a JSON list of
  model ids) is returned with the catalog, along with the default model. The
  picker pins favourites at the top, like pi's scoped models.
- **The model can change between runs.** `PATCH /api/sessions/{id}` updates
  `model`, and the next run uses it, because each run reads `session.model`. It
  returns 409 while a run is in progress. The transcript does not record the
  change yet.
- **Picker in the chat header.** The model badge becomes a searchable combobox,
  with favourites first, then every model.

## Alternatives considered
- **A generated catalog checked into the repo (pi's approach)** — works offline and
  can carry hand-tuned metadata. Needs a generate script and goes stale between
  runs. Overkill for one provider.
- **Only a curated list in config** — simple, with no network call. The owner
  would have to edit config to try any new model.
- **Fetching OpenRouter from the browser** — no backend code. The model rules
  (tool filter, favourites) would end up in the frontend, and adding providers
  later would need the frontend to change too.

## Consequences
- The API needs outbound access to openrouter.ai to show the catalog. Sessions
  keep running without it; only the picker is affected.
- Ids are OpenRouter's (`vendor/model`). A second provider will need the catalog
  keyed by provider, which the response shape (`provider` on every model) allows.
- Mid-session switches are invisible in the transcript until we add a
  model-change entry, as pi does.
