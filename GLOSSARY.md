# Glossary

Terms the code and docs use for agentspace's domain.

## Run recipe

What a run of a given kind does, built from a claimed run; driven by the run manager.
The kinds are a prompt run (a new user message), a continue run (no new message) and a compact run (only summarizes).
Code: `apps/api/src/agentspace/agent/run_recipe.py`.
