# The API's production image (ADR-030): one image for the service and the one-off migration task.
# Build from apps/api: `docker build -t agentspace-api apps/api`.
FROM ghcr.io/astral-sh/uv:0.9.7-python3.13-bookworm-slim AS build
WORKDIR /app
# Compiled bytecode starts faster; copy mode because the cache mount is another filesystem.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=0
# Dependencies first, in their own layer, so a code-only change reuses them.
RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --locked --no-dev --no-install-project
COPY . .
RUN --mount=type=cache,target=/root/.cache/uv uv sync --locked --no-dev --no-editable

FROM python:3.13-slim-bookworm
# ripgrep backs the `grep` and `find` tools.
RUN apt-get update && apt-get install -y --no-install-recommends ripgrep && rm -rf /var/lib/apt/lists/*
RUN useradd --create-home --uid 1000 app
WORKDIR /app
COPY --from=build --chown=app:app /app /app
ENV PATH="/app/.venv/bin:$PATH" AGENTSPACE_DATA_DIR=/app/data
RUN mkdir -p /app/data && chown app:app /app/data
USER app
EXPOSE 8000
# A short graceful timeout closes open streams quickly so clients reconnect to the new server; the runs themselves
# get AGENTSPACE_SHUTDOWN_GRACE_SECONDS in the app's shutdown, which comes after it.
CMD ["uvicorn", "agentspace.main:app", "--host", "0.0.0.0", "--port", "8000", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--timeout-graceful-shutdown", "5"]
