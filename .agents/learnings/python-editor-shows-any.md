# Python hover types show `Any` in the editor

**Date:** 2026-11-16

## Symptom
Hovering a function or value in a Python file in VS Code / Cursor shows `Any`
instead of its real type (e.g. `run_agent`, Pydantic models, FastAPI objects).
TypeScript in the same repo shows full types.

## Cause
The editor's Python language server was not using the project's environment.
The backend's uv environment lives in `apps/api/.venv`, but VS Code only
auto-detects a `.venv` at the root of the opened folder (the repo root). Without
the environment it can't find the installed libraries (FastAPI, Pydantic,
SQLModel, httpx) or the `agentspace` package, so everything they type becomes
`Any`. The TS equivalent: opening a project without `node_modules`.

## Fix
`.vscode/settings.json` points the Python extension at the interpreter:

```jsonc
{ "python.defaultInterpreterPath": "${workspaceFolder}/apps/api/.venv/bin/python" }
```

Then reload the window (Cmd+Shift+P → **Developer: Reload Window**) with the
**Python** extension installed (plus **Pylance** in VS Code). If hovers still
show `Any`, run **Python: Select Interpreter** and pick `apps/api/.venv`: an
interpreter chosen earlier for the folder overrides `defaultInterpreterPath`.

## Not a bug
Some `Any` is real: values that hold arbitrary JSON, such as tool arguments
(`dict[str, Any]`), are Python's `Record<string, unknown>` and stay `Any`.
