"""The FastAPI app: wires settings, the database and the routers together."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from asyncer import asyncify
from fastapi import FastAPI

from agentspace.ai.catalog import OpenRouterCatalog
from agentspace.api.mcp_servers import router as mcp_router
from agentspace.api.models import router as models_router
from agentspace.api.sessions import router as sessions_router
from agentspace.api.skills import router as skills_router
from agentspace.config import Settings, get_settings
from agentspace.mcp.manager import McpManager
from agentspace.runs import RunManager
from agentspace.session.db import create_engine, session_factory
from agentspace.session.migrate import upgrade_to_head
from agentspace.tracing import Tracer


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app; tests pass their own settings, e.g. a temporary data folder."""
    settings = settings or get_settings()

    # TS: code before `yield` runs at startup and code after it at shutdown, like a setup that returns a cleanup.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """Startup: make the data folder, migrate the DB, open the engine. Shutdown: stop runs, MCP, tracing, the DB."""
        settings.data_dir.mkdir(parents=True, exist_ok=True)
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
    app.include_router(sessions_router)
    app.include_router(models_router)
    app.include_router(skills_router)
    app.include_router(mcp_router)
    return app


# The app `fastapi dev` / `fastapi run` serve (see `[tool.fastapi]` in pyproject.toml).
app = create_app()
