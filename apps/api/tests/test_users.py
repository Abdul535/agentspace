"""Users own their data (ADR-025): two signed-in users can't see each other's sessions or MCP servers."""

from collections.abc import Iterator
from pathlib import Path

import pg
import pytest
from alembic import command
from alembic.config import Config
from fastapi.testclient import TestClient
from pg import db_rows
from test_auth import SUPABASE_URL, FixedKeys, auth, make_token

from agentspace.auth import TokenVerifier
from agentspace.config import Settings
from agentspace.encryption import SecretBox, SecretError, load_server_secret
from agentspace.main import create_app
from agentspace.session.migrate import API_ROOT

ADMIN_TOKEN = "test-admin-token"
ADMIN = {"X-MCP-Admin-Token": ADMIN_TOKEN}
ALICE = auth(make_token(sub="alice", email="alice@example.com")) | ADMIN
BOB = auth(make_token(sub="bob", email="bob@example.com")) | ADMIN


@pytest.fixture
def client(tmp_path: Path) -> Iterator[TestClient]:
    """A signed-in app; each request says who it is with ALICE or BOB."""
    settings = Settings(_env_file=None, data_dir=tmp_path, supabase_url=SUPABASE_URL, mcp_admin_token=ADMIN_TOKEN)
    app = create_app(settings)
    app.state.auth = TokenVerifier(SUPABASE_URL, keys=FixedKeys())
    with TestClient(app) as client:
        yield client


def test_each_user_lists_only_their_own_sessions(client: TestClient) -> None:
    alice_id = client.post("/sessions", json={}, headers=ALICE).json()["id"]
    bob_id = client.post("/sessions", json={}, headers=BOB).json()["id"]
    assert [s["id"] for s in client.get("/sessions", headers=ALICE).json()] == [alice_id]
    assert [s["id"] for s in client.get("/sessions", headers=BOB).json()] == [bob_id]


@pytest.mark.parametrize(
    ("method", "path", "body"),
    [
        ("GET", "", None),
        ("PATCH", "", {"model": "other/model"}),
        ("POST", "/archive", None),
        ("POST", "/messages", {"content": "hi"}),
        ("POST", "/continue", None),
        ("POST", "/compact", None),
        ("GET", "/events", None),
        ("POST", "/steer", {"content": "hi"}),
        ("POST", "/follow-up", {"content": "hi"}),
        ("POST", "/abort", None),
    ],
)
def test_another_users_session_is_a_404_like_a_missing_one(
    client: TestClient, method: str, path: str, body: dict | None
) -> None:
    alice_id = client.post("/sessions", json={}, headers=ALICE).json()["id"]
    theirs = client.request(method, f"/sessions/{alice_id}{path}", json=body, headers=BOB)
    missing = client.request(method, f"/sessions/{'0' * 32}{path}", json=body, headers=BOB)
    assert theirs.status_code == missing.status_code == 404
    # Same wording too, so Bob can't tell "someone else's" from "doesn't exist".
    assert theirs.json()["detail"] == missing.json()["detail"].replace("0" * 32, alice_id)
    # And Alice's session is untouched.
    assert client.get(f"/sessions/{alice_id}", headers=ALICE).json()["model"] != "other/model"


def test_a_user_row_is_saved_on_first_request(client: TestClient) -> None:
    client.get("/sessions", headers=ALICE)
    client.get("/sessions", headers=ALICE)
    assert db_rows("SELECT id, email FROM users ORDER BY id") == [
        ("alice", "alice@example.com"),
        ("local", None),  # made by the migration; owns the rows from before users
    ]


def test_local_mode_belongs_to_the_local_user(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        session_id = client.post("/sessions", json={}).json()["id"]
    assert db_rows("SELECT id, user_id FROM sessions") == [(session_id, "local")]


def server(name: str = "github", token: str = "ghp_alice") -> dict:
    """A server body with a secret in its env."""
    return {"name": name, "command": "npx", "args": ["server-github"], "env": {"GITHUB_TOKEN": token}}


def test_mcp_servers_are_per_user_and_names_only_clash_within_one_user(client: TestClient) -> None:
    alice_server = client.post("/mcp-servers", json=server(), headers=ALICE).json()
    assert client.post("/mcp-servers", json=server(token="ghp_bob"), headers=BOB).status_code == 201
    assert client.post("/mcp-servers", json=server(), headers=ALICE).status_code == 409

    bob_list = client.get("/mcp-servers", headers=BOB).json()["servers"]
    assert [s["env"] for s in bob_list] == [{"GITHUB_TOKEN": "ghp_bob"}]

    url = f"/mcp-servers/{alice_server['id']}"
    assert client.patch(url, json={"enabled": False}, headers=BOB).status_code == 404
    assert client.post(f"{url}/test", headers=BOB).status_code == 404
    assert client.delete(url, headers=BOB).status_code == 404
    assert client.get("/mcp-servers", headers=ALICE).json()["servers"][0]["enabled"] is True


def test_mcp_env_is_encrypted_in_the_database_and_readable_by_its_owner(client: TestClient) -> None:
    created = client.post("/mcp-servers", json=server(), headers=ALICE).json()
    assert created["env"] == {"GITHUB_TOKEN": "ghp_alice"}
    [(stored,)] = db_rows("SELECT env FROM mcp_servers")
    assert "ghp_alice" not in stored and '"v1:' in stored

    # A changed value is encrypted again.
    client.patch(f"/mcp-servers/{created['id']}", json={"env": {"GITHUB_TOKEN": "ghp_new"}}, headers=ALICE)
    [(stored,)] = db_rows("SELECT env FROM mcp_servers")
    assert "ghp_new" not in stored
    assert client.get("/mcp-servers", headers=ALICE).json()["servers"][0]["env"] == {"GITHUB_TOKEN": "ghp_new"}


def test_secret_box_round_trips_and_locks_values_to_their_user() -> None:
    box = SecretBox("x" * 32)
    sealed = box.encrypt("alice", "ghp_secret")
    assert sealed.startswith("v1:") and sealed != box.encrypt("alice", "ghp_secret")  # a fresh nonce each time
    assert box.decrypt("alice", sealed) == "ghp_secret"
    # Copied into Bob's row, or read with another server secret, it can't be opened.
    with pytest.raises(SecretError):
        box.decrypt("bob", sealed)
    with pytest.raises(SecretError):
        SecretBox("y" * 32).decrypt("alice", sealed)
    with pytest.raises(SecretError):
        box.decrypt("alice", "ghp_plain_text")


def test_without_a_secret_key_one_is_generated_once_for_this_user_only(tmp_path: Path) -> None:
    first = load_server_secret(None, tmp_path)
    assert load_server_secret(None, tmp_path) == first
    assert (tmp_path / "secret.key").stat().st_mode & 0o777 == 0o600
    assert load_server_secret("z" * 32, tmp_path) == "z" * 32


def test_the_migration_gives_old_rows_to_the_local_user_and_encrypts_their_env(database_url: str) -> None:
    # The test's database is migrated already; start this one from nothing.
    pg.drop_database(name := database_url.rsplit("/", 1)[1])
    pg.create_database(name)
    config = Config(API_ROOT / "alembic.ini")
    config.set_main_option("script_location", str(API_ROOT / "migrations"))
    config.set_main_option("sqlalchemy.url", database_url)
    config.attributes["configure_logger"] = False
    box = SecretBox("k" * 32)
    config.attributes["secret_box"] = box
    command.upgrade(config, "b7e2a9c41d05")  # the last revision before users
    db_rows(
        "INSERT INTO sessions (id, provider, model, system_prompt, tools, thinking_level, created_at, updated_at)"
        " VALUES ('s1', 'p', 'm', '', '[]', 'off', CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)"
    )
    db_rows(
        "INSERT INTO mcp_servers (id, name, command, args, env, enabled, created_at, updated_at)"
        " VALUES ('m1', 'gh', 'npx', '[]', $1, true, CURRENT_TIMESTAMP, CURRENT_TIMESTAMP)",
        '{"TOKEN": "ghp_old"}',
    )

    command.upgrade(config, "head")
    assert db_rows("SELECT user_id FROM sessions") == [("local",)]
    [(user_id, env)] = db_rows("SELECT user_id, env->>'TOKEN' FROM mcp_servers")
    assert user_id == "local" and box.decrypt("local", env) == "ghp_old"

    command.downgrade(config, "b7e2a9c41d05")
    assert db_rows("SELECT env->>'TOKEN' FROM mcp_servers") == [("ghp_old",)]
