"""The FastAPI app: wires settings, the database and the routers together."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager

from asyncer import asyncify
from fastapi import FastAPI

from agentspace.ai.catalog import OpenRouterCatalog
from agentspace.api.models import router as models_router
from agentspace.api.sessions import router as sessions_router
from agentspace.config import Settings, get_settings
from agentspace.runs import RunManager
from agentspace.session.db import create_engine, session_factory
from agentspace.session.migrate import upgrade_to_head


def create_app(settings: Settings | None = None) -> FastAPI:
    """Build the app; tests pass their own settings, e.g. a temporary data folder."""
    settings = settings or get_settings()

    # TS: code before `yield` runs at startup and code after it at shutdown, like a setup that returns a cleanup.
    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        """On startup: create the data folder, migrate the DB, open the engine. On shutdown: stop runs, close it."""
        settings.data_dir.mkdir(parents=True, exist_ok=True)
        if settings.auto_migrate:
            await asyncify(upgrade_to_head)(settings.resolved_database_url)
        engine = create_engine(settings.resolved_database_url)
        # TS: app.state is like Express's app.locals: values every request can reach.
        app.state.settings = settings
        app.state.db_factory = session_factory(engine)
        # Every session's background run (ADR-007).
        app.state.runs = RunManager(app.state.db_factory)
        app.state.model_catalog = OpenRouterCatalog(settings.openrouter_base_url)
        yield
        await app.state.runs.shutdown()
        await engine.dispose()

    app = FastAPI(title="agentspace", lifespan=lifespan)
    app.include_router(sessions_router)
    app.include_router(models_router)
    return app


# The app `fastapi dev` / `fastapi run` serve (see `[tool.fastapi]` in pyproject.toml).
app = create_app()
