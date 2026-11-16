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


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider([])


@pytest.fixture
def client(tmp_path: Path, fake: FakeProvider) -> Iterator[TestClient]:
    app = create_app(Settings(data_dir=tmp_path))
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


def test_create_list_and_get_session(client: TestClient) -> None:
    created = client.post("/sessions", json={"model": "some/model"})
    assert created.status_code == 201
    session = created.json()
    assert session["provider"] == "openrouter" and session["model"] == "some/model"

    assert [s["id"] for s in client.get("/sessions").json()] == [session["id"]]
    detail = client.get(f"/sessions/{session['id']}").json()
    assert detail["messages"] == []


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
    client.app.state.active_runs.add(session_id)
    assert client.post(f"/sessions/{session_id}/messages", json={"content": "hi"}).status_code == 409


def test_empty_prompt_is_rejected(client: TestClient) -> None:
    session_id = client.post("/sessions", json={}).json()["id"]
    assert client.post(f"/sessions/{session_id}/messages", json={"content": ""}).status_code == 422
