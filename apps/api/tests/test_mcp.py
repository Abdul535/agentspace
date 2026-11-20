"""MCP (ADR-017): our stdio client, tool wrapping, per-session connections and the settings routes."""

import asyncio
import sys
from collections.abc import Iterator
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from agentspace.agent.tool import ToolContext, ToolError
from agentspace.ai.messages import ImageContent, TextContent, ToolCall
from agentspace.ai.providers.fake import FakeProvider, text_reply, tool_reply
from agentspace.api.deps import provider_lookup
from agentspace.config import Settings
from agentspace.main import create_app
from agentspace.mcp.client import CallToolResult, McpError, StdioClient
from agentspace.mcp.manager import McpManager, ServerConfig
from agentspace.mcp.tools import McpArgs, McpTool, convert_result, cut_middle, mcp_tool_name

FAKE_SERVER = str(Path(__file__).parent / "fixtures" / "fake_mcp_server.py")
# The test's own Python runs the fake server, so it works without anything installed.
PYTHON = sys.executable

pytestmark = pytest.mark.anyio


def fake_config(name: str = "fake", env: dict[str, str] | None = None) -> ServerConfig:
    """The fake server as a saved config."""
    return ServerConfig(id=f"id-{name}", name=name, command=PYTHON, args=(FAKE_SERVER,), env=tuple((env or {}).items()))


async def test_client_handshakes_lists_and_calls(tmp_path: Path) -> None:
    client = StdioClient(PYTHON, [FAKE_SERVER], {}, tmp_path)
    await client.start()
    try:
        assert client.server_info["name"] == "fake" and client.instructions == "A fake server."
        tools = await client.list_tools()
        assert [t.name for t in tools][:2] == ["echo", "cwd"]
        assert tools[0].input_schema["required"] == ["text"]
        # Concurrent calls are matched to their replies by id.
        one, two = await asyncio.gather(
            client.call_tool("echo", {"text": "one"}, timeout=5), client.call_tool("echo", {"text": "two"}, timeout=5)
        )
        assert one.content[0]["text"] == "one" and two.content[0]["text"] == "two"
        cwd = await client.call_tool("cwd", {}, timeout=5)
        assert Path(cwd.content[0]["text"]).resolve() == tmp_path.resolve()
    finally:
        await client.close()
    assert not client.alive


async def test_a_crash_fails_the_call_with_the_servers_last_output(tmp_path: Path) -> None:
    client = StdioClient(PYTHON, [FAKE_SERVER], {}, tmp_path)
    await client.start()
    with pytest.raises(McpError, match="exited with code 3") as exc:
        await client.call_tool("crash", {}, timeout=5)
    assert "crashing on purpose" in str(exc.value)
    await client.close()


async def test_a_command_that_does_not_exist_is_an_mcp_error(tmp_path: Path) -> None:
    with pytest.raises(McpError, match="Could not start"):
        await StdioClient("definitely-not-a-command-xyz", [], {}, tmp_path).start()


async def test_the_server_gets_only_safe_variables_plus_its_own_env(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("OPENROUTER_API_KEY", "secret")
    client = StdioClient(PYTHON, [FAKE_SERVER], {"MY_TOKEN": "abc"}, tmp_path)
    await client.start()
    try:
        assert (await client.call_tool("env", {"key": "MY_TOKEN"}, timeout=5)).content[0]["text"] == "abc"
        assert (await client.call_tool("env", {"key": "OPENROUTER_API_KEY"}, timeout=5)).content[0]["text"] == "<unset>"
    finally:
        await client.close()


def test_tool_names_are_sanitized_and_hashed_when_long_or_taken() -> None:
    assert mcp_tool_name("my-server", "get.item", set()) == "mcp__my_server__get_item"
    # `a-b` and `a_b` sanitize alike; the second gets a hash suffix.
    first = mcp_tool_name("s", "a-b", set())
    second = mcp_tool_name("s", "a_b", {first})
    assert first == "mcp__s__a_b" and second != first and second.startswith("mcp__s__a_b_")
    long = mcp_tool_name("server", "x" * 100, set())
    assert len(long) == 64


def test_results_become_text_and_images() -> None:
    result = CallToolResult.model_validate(
        {
            "content": [
                {"type": "text", "text": "a"},
                {"type": "text", "text": "b"},
                {"type": "image", "data": "AAAA", "mimeType": "image/png"},
                {"type": "audio", "data": "AAAA", "mimeType": "audio/wav"},
                {"type": "resource_link", "uri": "file:///x"},
            ]
        }
    )
    content = convert_result(result)
    assert content[0] == TextContent(text="a\nb")
    assert content[1] == ImageContent(data="AAAA", mime_type="image/png")
    assert content[2] == TextContent(text="[audio content omitted]\n[Resource link: file:///x]")
    # Structured output alone is shown as JSON.
    structured = convert_result(CallToolResult.model_validate({"content": [], "structuredContent": {"n": 1}}))
    assert structured == [TextContent(text='{\n  "n": 1\n}')]


def test_long_text_keeps_its_start_and_end() -> None:
    text = "S" + "x" * 1000 + "E"
    cut = cut_middle(text, limit=100)
    assert cut.startswith("S") and cut.endswith("E") and "bytes truncated" in cut


async def test_manager_gives_tools_reconnects_after_a_crash_and_drops_removed_servers(tmp_path: Path) -> None:
    manager = McpManager(wait_timeout=10)
    try:
        tools = await manager.tools_for("s1", tmp_path, [fake_config()])
        by_name = {t.name: t for t in tools}
        assert "mcp__fake__echo" in by_name
        echo, crash = by_name["mcp__fake__echo"], by_name["mcp__fake__crash"]
        assert echo.spec().parameters["required"] == ["text"]
        ctx = ToolContext(workspace=tmp_path)

        result = await echo.execute(McpArgs.model_validate({"text": "hi"}), ctx)
        assert result.content == [TextContent(text="hi")] and result.details == {"server": "fake", "tool": "echo"}
        # The server dying is a tool error, not a crash of the run...
        with pytest.raises(ToolError, match="exited with code 3"):
            await crash.execute(McpArgs(), ctx)
        # ...and the next call starts it again.
        assert (await echo.execute(McpArgs.model_validate({"text": "back"}), ctx)).content[0] == TextContent(
            text="back"
        )

        # A server no longer configured is stopped; a tool still holding it can't start it again.
        assert await manager.tools_for("s1", tmp_path, []) == []
        with pytest.raises(ToolError, match="changed or removed"):
            await echo.execute(McpArgs.model_validate({"text": "x"}), ctx)
    finally:
        await manager.shutdown()


async def test_a_server_error_result_and_a_timeout_are_tool_errors(tmp_path: Path) -> None:
    manager = McpManager(call_timeout=0.3)
    try:
        tools = {t.name: t for t in await manager.tools_for("s1", tmp_path, [fake_config()])}
        assert isinstance(tools["mcp__fake__fail"], McpTool)
        failed = await tools["mcp__fake__fail"].execute(McpArgs(), ToolContext(workspace=tmp_path))
        assert failed.is_error and failed.content == [TextContent(text="it broke")]
        with pytest.raises(ToolError, match="timed out"):
            await tools["mcp__fake__slow"].execute(McpArgs(), ToolContext(workspace=tmp_path))
    finally:
        await manager.shutdown()


async def test_a_broken_server_is_skipped_and_the_others_still_work(tmp_path: Path) -> None:
    manager = McpManager()
    broken = ServerConfig(id="id-broken", name="broken", command="definitely-not-a-command-xyz", args=(), env=())
    try:
        tools = await manager.tools_for("s1", tmp_path, [broken, fake_config()])
        assert tools and all(t.name.startswith("mcp__fake__") for t in tools)
    finally:
        await manager.shutdown()


async def test_a_slow_server_is_not_waited_for(tmp_path: Path) -> None:
    manager = McpManager(wait_timeout=0.2)
    slow = ServerConfig(id="id-slow", name="slow", command=PYTHON, args=("-c", "import time; time.sleep(30)"), env=())
    try:
        assert await manager.tools_for("s1", tmp_path, [slow]) == []
    finally:
        await manager.shutdown()


@pytest.fixture
def fake() -> FakeProvider:
    return FakeProvider([])


@pytest.fixture
def client(tmp_path: Path, fake: FakeProvider) -> Iterator[TestClient]:
    app = create_app(Settings(_env_file=None, data_dir=tmp_path, enable_mcp=True))
    app.dependency_overrides[provider_lookup] = lambda: lambda name: fake
    with TestClient(app) as client:
        yield client


def test_servers_can_be_added_changed_tested_and_deleted(client: TestClient) -> None:
    body = {"name": "fake", "command": PYTHON, "args": [FAKE_SERVER], "env": {"A": "1"}}
    created = client.post("/mcp-servers", json=body)
    assert created.status_code == 201
    server = created.json()
    assert client.post("/mcp-servers", json={**body, "name": "fake"}).status_code == 409
    # `-` and `_` would give the same tool names, so they count as the same name.
    client.post("/mcp-servers", json={**body, "name": "my-server"})
    assert client.post("/mcp-servers", json={**body, "name": "my_server"}).status_code == 409
    assert client.post("/mcp-servers", json={**body, "name": "bad name"}).status_code == 422

    listed = client.get("/mcp-servers").json()
    assert listed["mcp_enabled"] is True and [s["name"] for s in listed["servers"]] == ["fake", "my-server"]

    test = client.post(f"/mcp-servers/{server['id']}/test").json()
    assert test["ok"] is True and test["server_name"] == "fake" and "echo" in [t["name"] for t in test["tools"]]

    updated = client.patch(f"/mcp-servers/{server['id']}", json={"enabled": False})
    assert updated.json()["enabled"] is False and updated.json()["command"] == PYTHON

    broken = client.patch(f"/mcp-servers/{server['id']}", json={"command": "definitely-not-a-command-xyz"}).json()
    failed = client.post(f"/mcp-servers/{broken['id']}/test").json()
    assert failed["ok"] is False and "Could not start" in failed["error"]

    assert client.delete(f"/mcp-servers/{server['id']}").status_code == 204
    assert client.post(f"/mcp-servers/{server['id']}/test").status_code == 404


def test_testing_a_server_is_refused_while_mcp_is_off(tmp_path: Path) -> None:
    with TestClient(create_app(Settings(_env_file=None, data_dir=tmp_path))) as client:
        server = client.post("/mcp-servers", json={"name": "fake", "command": PYTHON, "args": [FAKE_SERVER]}).json()
        assert client.get("/mcp-servers").json()["mcp_enabled"] is False
        assert client.post(f"/mcp-servers/{server['id']}/test").status_code == 403


def test_a_run_offers_mcp_tools_and_calls_them(client: TestClient, fake: FakeProvider) -> None:
    client.post("/mcp-servers", json={"name": "fake", "command": PYTHON, "args": [FAKE_SERVER]})
    off = client.post("/mcp-servers", json={"name": "off", "command": PYTHON, "args": [FAKE_SERVER], "enabled": False})
    assert off.status_code == 201
    session_id = client.post("/sessions", json={"tool_set": "read_only"}).json()["id"]
    fake.replies = [
        tool_reply(ToolCall(id="c1", name="mcp__fake__echo", arguments={"text": "from mcp"})),
        text_reply("Done."),
    ]
    client.post(f"/sessions/{session_id}/messages", json={"content": "echo"})

    names = [t.name for t in fake.contexts[0].tools]
    assert names[:4] == ["read", "grep", "find", "ls"] and "mcp__fake__echo" in names
    assert not any(n.startswith("mcp__off__") for n in names)  # disabled servers stay out
    assert "- mcp__fake__echo: [fake] Echo the text back." in fake.contexts[0].system_prompt
    saved = client.get(f"/sessions/{session_id}").json()["messages"]
    result = next(m for m in saved if m["role"] == "tool_result")
    assert result["content"][0]["text"] == "from mcp" and not result["is_error"]
