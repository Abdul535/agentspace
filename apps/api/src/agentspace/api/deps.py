"""FastAPI dependencies shared by the routes: settings, database, store, provider, model catalog, runs and run slot."""

from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from sqlmodel.ext.asyncio.session import AsyncSession

from agentspace.ai.catalog import OpenRouterCatalog
from agentspace.ai.provider import Provider
from agentspace.ai.providers import UnknownProviderError, get_provider
from agentspace.config import Settings
from agentspace.runs import ActiveRun, RunManager, SessionBusyError
from agentspace.session.db import open_db
from agentspace.session.models import SessionRow
from agentspace.session.store import SessionStore


# TS: FastAPI dependency injection. A route parameter typed `XDep` is filled by calling the function in
# `Depends(...)` on each request, like NestJS providers or a tRPC context.
def get_settings(request: Request) -> Settings:
    """The settings the app was created with."""
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_settings)]  # TS: the type is Settings; Depends says how to get one


async def get_db(request: Request) -> AsyncIterator[AsyncSession]:
    """A DB session for one request, kept open until the response has been sent."""
    # TS: in a dependency, code after `yield` runs once the response is sent, like a `finally` block.
    async for db in open_db(request.app.state.db_factory):
        yield db


DBDep = Annotated[AsyncSession, Depends(get_db)]


def get_store(db: DBDep) -> SessionStore:
    """The session store on this request's DB session."""
    return SessionStore(db)


StoreDep = Annotated[SessionStore, Depends(get_store)]


def provider_lookup(settings: SettingsDep) -> Callable[[str], Provider]:
    """Builds a provider by name. Tests override this dependency to inject a fake provider."""
    return lambda name: get_provider(name, settings)


ProviderLookupDep = Annotated[Callable[[str], Provider], Depends(provider_lookup)]


def get_catalog(request: Request) -> OpenRouterCatalog:
    """The app-wide model catalog, so its cache is shared by every request. Tests override this."""
    return request.app.state.model_catalog


CatalogDep = Annotated[OpenRouterCatalog, Depends(get_catalog)]


async def get_session_row(session_id: str, store: StoreDep) -> SessionRow:
    """The session named in the path, or a 404."""
    session = await store.get(session_id)
    if session is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, f"Session {session_id} not found.")
    return session


SessionRowDep = Annotated[SessionRow, Depends(get_session_row)]


def get_runs(request: Request) -> RunManager:
    """The app-wide run manager (ADR-007)."""
    return request.app.state.runs


RunsDep = Annotated[RunManager, Depends(get_runs)]


@dataclass(frozen=True)
class Run:
    """What starting a prompt run needs: the session, its provider, its workspace and its claimed run slot."""

    session: SessionRow
    provider: Provider
    workspace: Path
    active: ActiveRun


async def claim_run(
    session: SessionRowDep, settings: SettingsDep, lookup: ProviderLookupDep, store: StoreDep, runs: RunsDep
) -> AsyncIterator[Run]:
    """Claim the session's run slot, with its history loaded, before the stream starts.

    Checks run here so they can still answer with an HTTP error. The route starts
    the run; if it never does (e.g. it failed first), the slot is given back.
    """
    try:
        provider = lookup(session.provider)
    except UnknownProviderError as exc:
        # TS: `raise ... from exc` is `throw new Error(msg, { cause: exc })`.
        raise HTTPException(status.HTTP_422_UNPROCESSABLE_CONTENT, str(exc)) from exc
    try:
        active = await runs.claim(session.id, lambda: store.messages(session.id))
    except SessionBusyError as exc:
        # TS: raising HTTPException ends the request with that status, like `throw new HTTPException(409)` in Hono.
        raise HTTPException(status.HTTP_409_CONFLICT, "This session is already running a prompt.") from exc
    try:
        yield Run(session=session, provider=provider, workspace=settings.workspaces_dir / session.id, active=active)
    finally:
        runs.release(active)  # no-op once the run has started: from then on the run frees its own slot


RunDep = Annotated[Run, Depends(claim_run)]
