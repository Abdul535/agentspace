import json
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentspace.ai.messages import ToolCall
from agentspace.ai.providers.fake import FakeProvider, text_reply, tool_reply
from agentspace.api.deps import provider_lookup
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.runs import ActiveRun


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider([])


@pytest.fixture
def client(tmp_path: Path, fake: FakeProvider) -> Iterator[TestClient]:
    # _env_file=None: tests must not pick up the developer's apps/api/.env (e.g. AGENTSPACE_ENABLE_BASH).
    app = create_app(Settings(_env_file=None, data_dir=tmp_path))
    app.dependency_overrides[provider_lookup] = lambda: lambda name: fake
    with TestClient(app) as client:
        yield client


def sse_events(text: str) -> list[dict]:
    """Parse an SSE body into the JSON object of each event's `data` field."""
    return [
        json.loads(line.removeprefix("data: "))
        for block in text.strip().split("\n\n")
        for line in block.splitlines()
        if line.startswith("data: ")
    ]


def mark_running(client: TestClient, session_id: str) -> None:
    """Make the session look busy, as if a run had claimed it."""
    client.app.state.runs.runs[session_id] = ActiveRun(session_id=session_id, messages=[], history_size=0)


def test_create_list_and_get_session(client: TestClient) -> None:
    created = client.post("/sessions", json={"model": "some/model"})
    assert created.status_code == 201
    session = created.json()
    assert session["provider"] == "openrouter" and session["model"] == "some/model"

    assert [s["id"] for s in client.get("/sessions").json()] == [session["id"]]
    detail = client.get(f"/sessions/{session['id']}").json()
    assert detail["messages"] == [] and detail["running"] is False


def test_unknown_session_and_provider(client: TestClient) -> None:
    assert client.get("/sessions/nope").status_code == 404
    assert client.post("/sessions/nope/messages", json={"content": "hi"}).status_code == 404
    assert client.post("/sessions", json={"provider": "nope"}).status_code == 422


def test_send_message_streams_events_and_persists_the_transcript(
    client: TestClient, fake: FakeProvider, tmp_path: Path
) -> None:
    fake.replies = [
        tool_reply(ToolCall(id="c1", name="write", arguments={"path": "notes.md", "content": "# Notes"})),
        text_reply("Saved."),
    ]
    session_id = client.post("/sessions", json={}).json()["id"]

    response = client.post(f"/sessions/{session_id}/messages", json={"content": "Write notes"})
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    # Without no-transform, the Next.js proxy gzips the stream and the browser gets every event at the end.
    assert "no-transform" in response.headers["cache-control"]
    events = sse_events(response.text)
    assert events[0]["type"] == "agent_start"
    assert events[-1]["type"] == "agent_end" and events[-1]["reason"] == "done"

    assert (tmp_path / "workspaces" / session_id / "notes.md").read_text() == "# Notes"
    detail = client.get(f"/sessions/{session_id}").json()
    assert [m["role"] for m in detail["messages"]] == ["user", "assistant", "tool_result", "assistant"]
    assert detail["title"] == "Write notes"

    # The next prompt sends the stored transcript back to the model.
    fake.replies = [text_reply("Still here.")]
    client.post(f"/sessions/{session_id}/messages", json={"content": "Again"})
    assert [m.role for m in fake.contexts[-1].messages] == ["user", "assistant", "tool_result", "assistant", "user"]
    assert len(client.get(f"/sessions/{session_id}").json()["messages"]) == 6


def test_a_running_session_refuses_a_second_prompt(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    mark_running(client, session_id)
    assert client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}).status_code == 409
    assert client.get(f"/sessions/{session_id}").json()["running"] is True


def test_empty_prompt_is_rejected(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    assert client.post(f"/sessions/{session_id}/messages", json={"content": ""}).status_code == 422


def test_tool_sets_are_stored_as_names_and_limit_what_the_model_gets(client: TestClient, fake: FakeProvider) -> None:
    default = client.post("/sessions", json={}).json()
    assert default["tools"] == ["read", "bash", "edit", "write", "grep", "find", "ls"]
    read_only = client.post("/sessions", json={"tool_set": "read_only"}).json()
    assert read_only["tools"] == ["read", "grep", "find", "ls"]
    assert client.post("/sessions", json={"tool_set": "everything"}).status_code == 422

    # The model is offered only the session's tools, and never bash while the server keeps it off.
    fake.replies = [text_reply("ok")]
    client.post(f"/sessions/{read_only['id']}/messages", json={"content": "hi"})
    assert [t.name for t in fake.contexts[-1].tools] == ["read", "grep", "find", "ls"]
    # The system prompt is the stored preamble plus sections for exactly those tools.
    prompt = fake.contexts[-1].system_prompt
    assert prompt.startswith(read_only["system_prompt"]) and "- grep: " in prompt and "- edit: " not in prompt
    fake.replies = [text_reply("ok")]
    client.post(f"/sessions/{default['id']}/messages", json={"content": "hi"})
    assert "bash" not in [t.name for t in fake.contexts[-1].tools]


def test_switching_model_applies_to_the_next_run(client: TestClient, fake: FakeProvider) -> None:
    session_id = client.post("/sessions", json={"model": "old/model"}).json()["id"]

    updated = client.patch(f"/sessions/{session_id}", json={"model": "new/model"})
    assert updated.status_code == 200 and updated.json()["model"] == "new/model"
    assert client.get(f"/sessions/{session_id}").json()["model"] == "new/model"

    fake.replies = [text_reply("Hi.")]
    client.post(f"/sessions/{session_id}/messages", json={"content": "hi"})
    reply = client.get(f"/sessions/{session_id}").json()["messages"][-1]
    assert reply["role"] == "assistant" and reply["model"] == "new/model"


def test_switching_model_is_refused_mid_run_and_validated(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    assert client.patch(f"/sessions/{session_id}", json={"model": ""}).status_code == 422
    assert client.patch("/sessions/nope", json={"model": "a/b"}).status_code == 404

    mark_running(client, session_id)
    assert client.patch(f"/sessions/{session_id}", json={"model": "a/b"}).status_code == 409


def test_running_bash_streams_tool_execution_updates_over_sse(tmp_path: Path, fake: FakeProvider) -> None:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, enable_bash=True))
    app.dependency_overrides[provider_lookup] = lambda: lambda name: fake
    fake.replies = [
        tool_reply(ToolCall(id="c1", name="bash", arguments={"command": "echo one; sleep 0.25; echo two"})),
        text_reply("Done."),
    ]
    with TestClient(app) as client:
        session_id = client.post("/sessions", json={}).json()["id"]
        events = sse_events(client.post(f"/sessions/{session_id}/messages", json={"content": "run it"}).text)
        saved = client.get(f"/sessions/{session_id}").json()["messages"]

    updates = [e for e in events if e["type"] == "tool_execution_update"]
    assert updates and updates[0]["tool_call_id"] == "c1"
    assert updates[0]["partial"]["content"][0]["text"].startswith("one")
    # Live updates are streamed, never saved: the transcript only has the final result.
    assert [m["role"] for m in saved] == ["user", "assistant", "tool_result", "assistant"]
