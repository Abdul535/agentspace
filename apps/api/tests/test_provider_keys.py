"""Users bring their own key (ADR-026): saved checked and encrypted, used by runs, capped runs at once."""

import sqlite3
from collections.abc import Iterator
from pathlib import Path
from typing import cast

import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel.ext.asyncio.session import AsyncSession
from test_auth import SUPABASE_URL, FixedKeys, auth, make_token

from agentspace.ai.key_check import KeyVerdict, check_openrouter_key
from agentspace.ai.providers import MissingApiKeyError, get_provider
from agentspace.ai.providers.fake import FakeProvider, text_reply
from agentspace.ai.providers.openai_compat import OpenAICompatProvider
from agentspace.api.deps import provider_lookup
from agentspace.api.provider_keys import key_checker
from agentspace.auth import TokenVerifier
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.runs import ActiveRun, RunManager, TooManyRunsError
from agentspace.session.models import UserRow

KEY = "sk-or-v1-0123456789abcdef"
ALICE = auth(make_token(sub="alice"))
BOB = auth(make_token(sub="bob"))


class Verdicts:
    """A key check that answers what the test set, and remembers what it was asked."""

    def __init__(self) -> None:
        self.verdict: KeyVerdict = "valid"
        self.checked: list[str] = []

    async def __call__(self, provider: str, api_key: str) -> KeyVerdict:
        self.checked.append(api_key)
        return self.verdict


@pytest.fixture
def verdicts() -> Verdicts:
    return Verdicts()


def make_client(settings: Settings, verdicts: Verdicts) -> TestClient:
    """An app whose key check never calls OpenRouter; signed in when the settings name a Supabase project."""
    app = create_app(settings)
    if settings.supabase_url:
        app.state.auth = TokenVerifier(SUPABASE_URL, keys=FixedKeys())
    app.dependency_overrides[key_checker] = lambda: verdicts
    return TestClient(app)


@pytest.fixture
def client(tmp_path: Path, verdicts: Verdicts) -> Iterator[TestClient]:
    """Signed-in mode, so two users can be told apart."""
    settings = Settings(_env_file=None, data_dir=tmp_path, supabase_url=SUPABASE_URL)
    with make_client(settings, verdicts) as client:
        yield client


def entry(client: TestClient, headers: dict[str, str]) -> dict:
    """The caller's OpenRouter entry on the settings page."""
    [openrouter] = client.get("/provider-keys", headers=headers).json()
    return openrouter


def test_a_new_user_has_no_key(client: TestClient) -> None:
    assert entry(client, ALICE) == {"provider": "openrouter", "label": "OpenRouter", "last4": None, "updated_at": None}


def test_a_saved_key_shows_only_its_last_four_and_is_encrypted(client: TestClient, tmp_path: Path) -> None:
    saved = client.put("/provider-keys/openrouter", json={"api_key": f"  {KEY}\n"}, headers=ALICE)
    assert saved.status_code == 200
    assert saved.json()["last4"] == "cdef" and saved.json()["checked"] is True
    assert entry(client, ALICE)["last4"] == "cdef"
    assert KEY not in saved.text
    with sqlite3.connect(tmp_path / "agentspace.db") as db:
        [(stored,)] = db.execute("SELECT api_key FROM provider_keys").fetchall()
    assert stored.startswith("v1:") and KEY not in stored


def test_keys_are_per_user(client: TestClient) -> None:
    client.put("/provider-keys/openrouter", json={"api_key": KEY}, headers=ALICE)
    assert entry(client, BOB)["last4"] is None
    assert client.delete("/provider-keys/openrouter", headers=BOB).status_code == 404
    assert entry(client, ALICE)["last4"] == "cdef"


def test_a_refused_key_is_not_saved(client: TestClient, verdicts: Verdicts) -> None:
    verdicts.verdict = "invalid"
    refused = client.put("/provider-keys/openrouter", json={"api_key": KEY}, headers=ALICE)
    assert refused.status_code == 422 and "refused this key" in refused.json()["detail"]
    assert entry(client, ALICE)["last4"] is None


def test_an_unchecked_key_is_saved_and_says_so(client: TestClient, verdicts: Verdicts) -> None:
    verdicts.verdict = "unknown"
    saved = client.put("/provider-keys/openrouter", json={"api_key": KEY}, headers=ALICE).json()
    assert saved["last4"] == "cdef" and saved["checked"] is False


def test_replace_and_remove_a_key(client: TestClient) -> None:
    client.put("/provider-keys/openrouter", json={"api_key": KEY}, headers=ALICE)
    client.put("/provider-keys/openrouter", json={"api_key": "sk-or-v1-newkey9876"}, headers=ALICE)
    assert entry(client, ALICE)["last4"] == "9876"
    assert client.delete("/provider-keys/openrouter", headers=ALICE).status_code == 204
    assert entry(client, ALICE)["last4"] is None


def test_unknown_provider_is_a_404(client: TestClient) -> None:
    assert client.put("/provider-keys/nope", json={"api_key": KEY}, headers=ALICE).status_code == 404
    assert client.delete("/provider-keys/nope", headers=ALICE).status_code == 404


def test_a_run_without_a_key_says_where_to_add_one(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}, headers=ALICE).json()["id"]
    response = client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}, headers=ALICE)
    assert response.status_code == 409
    assert response.json()["detail"] == {
        "code": "no_api_key",
        "message": "Add your OpenRouter API key in Settings → API keys to start.",
    }
    # The refused prompt never claimed the run slot.
    assert client.get(f"/sessions/{session_id}", headers=ALICE).json()["running"] is False


@pytest.mark.anyio
async def test_runs_are_built_with_the_owners_key(client: TestClient) -> None:
    client.put("/provider-keys/openrouter", json={"api_key": KEY}, headers=ALICE)
    app = cast(FastAPI, client.app)
    async with app.state.db_factory() as db:
        alice = await db.get(UserRow, "alice")
        assert alice is not None
        lookup = await provider_lookup(app.state.settings, db, alice, app.state.secret_box)
    provider = lookup("openrouter")
    assert isinstance(provider, OpenAICompatProvider) and provider.api_key == KEY


def test_get_provider_needs_a_key(tmp_path: Path) -> None:
    with pytest.raises(MissingApiKeyError):
        get_provider("openrouter", Settings(_env_file=None, data_dir=tmp_path), None)


def test_local_mode_seeds_the_env_key_once(tmp_path: Path, verdicts: Verdicts) -> None:
    def local(key: str) -> Settings:
        # TS: `**{...}` passes the env var's name as the keyword, since the field reads it by that alias.
        return Settings(_env_file=None, data_dir=tmp_path, **{"OPENROUTER_API_KEY": key})

    with make_client(local(KEY), verdicts) as client:
        assert entry(client, {})["last4"] == "cdef"
    # A later `.env` change doesn't overwrite the key the local user now owns (and may have changed).
    with make_client(local("sk-or-v1-other0000"), verdicts) as client:
        assert entry(client, {})["last4"] == "cdef"


def test_signed_in_mode_never_seeds_the_env_key(tmp_path: Path, verdicts: Verdicts) -> None:
    settings = Settings(_env_file=None, data_dir=tmp_path, supabase_url=SUPABASE_URL, **{"OPENROUTER_API_KEY": KEY})
    with make_client(settings, verdicts) as client:
        assert entry(client, ALICE)["last4"] is None


@pytest.mark.anyio
async def test_run_manager_caps_runs_per_user() -> None:
    # Claiming never opens the DB, so there is none.
    runs = RunManager(cast(async_sessionmaker[AsyncSession], None), max_per_user=1)

    async def no_history() -> list:
        return []

    first = await runs.claim("s1", no_history, "alice")
    with pytest.raises(TooManyRunsError):
        await runs.claim("s2", no_history, "alice")
    # Another user's runs don't count against Alice.
    await runs.claim("s3", no_history, "bob")
    runs.release(first)
    await runs.claim("s2", no_history, "alice")


def test_one_run_too_many_is_a_429(tmp_path: Path, verdicts: Verdicts) -> None:
    fake = FakeProvider([text_reply("hi")])
    settings = Settings(_env_file=None, data_dir=tmp_path, max_runs_per_user=1)
    with make_client(settings, verdicts) as client:
        cast(FastAPI, client.app).dependency_overrides[provider_lookup] = lambda: lambda name: fake
        busy, other = (client.post("/sessions", json={}).json()["id"] for _ in range(2))
        runs = cast(FastAPI, client.app).state.runs
        runs.runs[busy] = ActiveRun(session_id=busy, messages=[], history_size=0)
        response = client.post(f"/sessions/{other}/messages", json={"content": "hi"})
        assert response.status_code == 429 and response.json()["detail"]["code"] == "too_many_runs"
        del runs.runs[busy]
        assert client.post(f"/sessions/{other}/messages", json={"content": "hi"}).status_code == 200


@pytest.mark.anyio
@pytest.mark.parametrize(("status", "verdict"), [(200, "valid"), (401, "invalid"), (403, "invalid"), (500, "unknown")])
async def test_openrouter_key_check(status: int, verdict: KeyVerdict) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/key" and request.headers["authorization"] == f"Bearer {KEY}"
        return httpx.Response(status, json={})

    transport = httpx.MockTransport(handler)
    assert await check_openrouter_key("https://openrouter.ai/api/v1", KEY, transport) == verdict


@pytest.mark.anyio
async def test_openrouter_key_check_offline_is_unknown() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline")

    assert await check_openrouter_key("https://openrouter.ai/api/v1", KEY, httpx.MockTransport(handler)) == "unknown"
