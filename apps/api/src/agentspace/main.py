"""The FastAPI app: wires settings, the database and the routers together."""

import logging
from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from asyncer import asyncify
from fastapi import Depends, FastAPI

from agentspace.ai.catalog import OpenRouterCatalog
from agentspace.api.mcp_servers import router as mcp_router
from agentspace.api.models import router as models_router
from agentspace.api.sessions import router as sessions_router
from agentspace.api.skills import router as skills_router
from agentspace.auth import TokenVerifier, require_user
from agentspace.config import Settings, get_settings
from agentspace.mcp.manager import McpManager
from agentspace.runs import RunManager
from agentspace.session.db import create_engine, session_factory
from agentspace.session.migrate import upgrade_to_head
from agentspace.tracing import Tracer

log = logging.getLogger(__name__)


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app; tests pass their own settings, e.g. a temporary data folder."""
    settings = settings or get_settings()

    # TS: code before `yield` runs at startup and code after it at shutdown, like a setup that returns a cleanup.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Startup: make the data folder, migrate the DB, open the engine. Shutdown: stop runs, MCP, tracing, the DB."""
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if app.state.auth is None:
            log.warning("AGENTSPACE_SUPABASE_URL is not set: no login, anyone who reaches the API can use it.")
        if settings.auto_migrate:
            await asyncify(upgrade_to_head)(settings.resolved_database_url)
        engine = create_engine(settings.resolved_database_url)
        # TS: app.state is like Express's app.locals: values every request can reach.
        app.state.settings = settings
        app.state.db_factory = session_factory(engine)
        # Langfuse traces of each run (ADR-018); a no-op without keys.
        tracer = Tracer.from_settings(settings)
        # Every session's background run (ADR-007).
        app.state.runs = RunManager(app.state.db_factory, tracer)
        app.state.model_catalog = OpenRouterCatalog(settings.openrouter_base_url)
        # Each session's MCP server processes (ADR-017); a running session's are never stopped as idle.
        app.state.mcp = McpManager(
            wait_timeout=settings.mcp_wait_seconds,
            call_timeout=settings.mcp_call_timeout_seconds,
            idle_timeout=settings.mcp_idle_seconds,
            is_busy=app.state.runs.is_running,
        )
        app.state.mcp.start()
        yield
        await app.state.runs.shutdown()
        await app.state.mcp.shutdown()
        # After the runs stop, so their last observations are sent too; off the event loop, since it blocks.
        await asyncify(tracer.shutdown)()
        await engine.dispose()

    app = FastAPI(title="agentspace", lifespan=lifespan)
    # Set here, not in lifespan, so tests can swap in a verifier with their own keys (ADR-024).
    app.state.auth = TokenVerifier(settings.supabase_url) if settings.supabase_url else None

    @app.get("/health", tags=["health"])
    async def health() -> dict[str, str]:
        """For load balancers and uptime checks; the only route open without a sign-in."""
        return {"status": "ok"}

    # Every other route needs a signed-in user (a no-op in local mode).
    signed_in = [Depends(require_user)]
    app.include_router(sessions_router, dependencies=signed_in)
    app.include_router(models_router, dependencies=signed_in)
    app.include_router(skills_router, dependencies=signed_in)
    app.include_router(mcp_router, dependencies=signed_in)
    return app


# The app `fastapi dev` / `fastapi run` serve (see `[tool.fastapi]` in pyproject.toml).
app = create_app()
