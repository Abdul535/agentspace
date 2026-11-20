"""A tiny stdio MCP server for tests: `echo`, `cwd`, `env`, `fail`, `crash` and `slow` tools."""

import json
import os
import sys
import time

TOOLS = [
    {
        "name": "echo",
        "description": "Echo the text back.",
        "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]},
    },
    {"name": "cwd", "description": "The working folder.", "inputSchema": {"type": "object"}},
    {
        "name": "env",
        "description": "One environment variable.",
        "inputSchema": {"type": "object", "properties": {"key": {"type": "string"}}},
    },
    {"name": "fail", "description": "Report an error.", "inputSchema": {"type": "object"}},
    {"name": "crash", "description": "Exit the process.", "inputSchema": {"type": "object"}},
    {"name": "slow", "description": "Sleep a while.", "inputSchema": {"type": "object"}},
]


def send(message: dict) -> None:
    """Write one JSON-RPC message as a line."""
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def text(value: str, is_error: bool = False) -> dict:
    """A tool result with one text block."""
    return {"content": [{"type": "text", "text": value}], "isError": is_error}


def call(name: str, args: dict) -> dict:
    """Run one tool."""
    if name == "echo":
        return text(args["text"])
    if name == "cwd":
        return text(os.getcwd())
    if name == "env":
        return text(os.environ.get(args["key"], "<unset>"))
    if name == "fail":
        return text("it broke", is_error=True)
    if name == "crash":
        print("crashing on purpose", file=sys.stderr, flush=True)
        os._exit(3)
    if name == "slow":
        time.sleep(5)
        return text("finally")
    raise KeyError(name)


print("fake server starting", file=sys.stderr, flush=True)
print("a log line that is not JSON", flush=True)  # clients must skip these
for line in sys.stdin:
    message = json.loads(line)
    method, request_id = message.get("method"), message.get("id")
    if request_id is None:
        continue  # a notification
    if method == "initialize":
        send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "result": {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {"tools": {}},
                    "serverInfo": {"name": "fake", "version": "1.0"},
                    "instructions": "A fake server.",
                },
            }
        )
    elif method == "tools/list":
        send({"jsonrpc": "2.0", "id": request_id, "result": {"tools": TOOLS}})
    elif method == "tools/call":
        params = message["params"]
        send({"jsonrpc": "2.0", "id": request_id, "result": call(params["name"], params.get("arguments", {}))})
    else:
        send({"jsonrpc": "2.0", "id": request_id, "error": {"code": -32601, "message": "nope"}})
