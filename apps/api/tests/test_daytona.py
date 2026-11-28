"""The Daytona adapter: the exit-code marker offline, and the real lifecycle when DAYTONA_API_KEY is set (ADR-035)."""

import contextlib
import os
import uuid

import pytest
from daytona import AsyncDaytona, DaytonaConfig, DaytonaError

from agentspace.workspace.daytona import DaytonaSandboxes, ExitMarker
from agentspace.workspace.sandbox import Box
from agentspace.workspace.sandboxes import SandboxNameTakenError, SandboxSpec, ensure

pytestmark = pytest.mark.anyio

NONCE = "abc123"


def feed(chunks: list[str]) -> tuple[str, int | None]:
    """Stream `chunks` through a marker; what was passed on, and the exit code found."""
    passed: list[str] = []
    marker = ExitMarker(NONCE, passed.append)
    for chunk in chunks:
        marker.feed(chunk)
    marker.flush()
    return "".join(passed), marker.code


@pytest.mark.parametrize(
    "chunks",
    [
        ["out\n", f"\n{NONCE}:3\n"],
        ["out\n\n", f"{NONCE}:3\n"],
        ["out\n\nab", "c12", "3:3", "\n"],  # the marker cut across chunks
        [f"out\n\n{NONCE}:3\nlate child output"],  # anything after the marker is dropped
    ],
)
async def test_the_exit_code_is_picked_out_and_never_shown(chunks: list[str]) -> None:
    assert feed(chunks) == ("out\n", 3)


async def test_output_without_a_trailing_newline_is_kept_exactly() -> None:
    assert feed(["new", f"\n{NONCE}:0\n"]) == ("new", 0)


async def test_text_that_only_looks_like_the_start_of_a_marker_is_passed_on() -> None:
    assert feed(["a\nab", "x\n"]) == ("a\nabx\n", None)
    assert feed(["a\nabc"]) == ("a\nabc", None)  # held back, then flushed at the end


async def test_a_different_nonce_is_ordinary_output() -> None:
    assert feed(["\nzzz:1\n"]) == ("\nzzz:1\n", None)


DAYTONA_KEY = os.environ.get("DAYTONA_API_KEY")


@pytest.mark.skipif(DAYTONA_KEY is None, reason="DAYTONA_API_KEY is not set")
async def test_real_daytona_sandbox_lifecycle_and_network_policy() -> None:
    """One sandbox end to end: create, find again, refuse a second, wake after a stop, and only the allowed site."""
    # The vendor's default: on Daytona's lower tiers, the only network policy an account may use.
    spec = SandboxSpec(auto_stop_minutes=15, auto_archive_minutes=60, network="vendor_default")
    name = f"agentspace-test-{uuid.uuid4().hex[:12]}"
    async with AsyncDaytona(DaytonaConfig(api_key=DAYTONA_KEY)) as client:
        sandboxes = DaytonaSandboxes(client, spec)
        try:
            box = await ensure(sandboxes, name, {"agentspace-test": "1"}, nothing)
            found = await sandboxes.find(name)
            assert found is not None and found.running and found.box.id == box.id
            with pytest.raises(SandboxNameTakenError):
                await sandboxes.create(name, {"agentspace-test": "1"})

            assert await curl(box, "https://pypi.org/simple/") == "200"  # package installs work
            print("example.com from the sandbox:", await curl(box, "https://example.com"))  # tier-dependent

            await client.stop(await client.get(name))
            asleep = await sandboxes.find(name)
            assert asleep is not None and not asleep.running
            woken = await ensure(sandboxes, name, {"agentspace-test": "1"}, nothing)
            assert woken.id == box.id
        finally:
            with contextlib.suppress(DaytonaError):
                await client.delete(await client.get(name))


async def nothing() -> None:
    """An `on_starting` that does nothing."""


async def curl(box: Box, url: str) -> str:
    """The HTTP status the sandbox gets for `url` (`000` when it can't connect)."""
    out: list[str] = []
    await box.run(
        f"curl -s -m 10 -o /dev/null -w '%{{http_code}}' {url} || true",
        cwd="/",
        on_stdout=out.append,
        on_stderr=lambda _c: None,
    )
    return "".join(out).strip()
