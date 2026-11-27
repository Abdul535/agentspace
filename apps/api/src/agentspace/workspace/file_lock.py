"""One lock per file, so parallel `write`/`edit` calls to the same file don't lose each other's changes.

After pi's `withFileMutationQueue` (`packages/coding-agent/src/core/tools/file-mutation-queue.ts`).
Calls on different files still run at the same time. `asyncio.Lock` serves waiters in arrival
order and the lookup below never awaits, so pi's extra registration queue isn't needed.
"""

import asyncio
from collections.abc import AsyncGenerator
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _Entry:
    """A file's lock, and how many calls hold it or wait for it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


# Keyed by a string naming one file: the resolved host path, or a sandbox id plus its path.
_entries: dict[str, _Entry] = {}


# TS: an async context manager; `async with file_lock(p):` is like `await using _ = await fileLock(p)`.
def file_lock(path: Path) -> AbstractAsyncContextManager[None]:
    """Hold a host file's lock; a symlink and its target share one lock."""
    return key_lock(str(path.resolve()))  # resolve follows symlinks, and works for files that don't exist yet


@asynccontextmanager
async def key_lock(key: str) -> AsyncGenerator[None]:
    """Hold the lock named `key` for the body of the `async with`; other calls with the same key wait their turn."""
    entry = _entries.setdefault(key, _Entry())  # TS: map.get(key) ?? map.set(key, new Entry()).get(key)
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0:  # nobody holds or waits for it: drop it so the dict doesn't grow forever
            del _entries[key]
