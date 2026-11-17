"""One lock per file, so parallel `write`/`edit` calls to the same file don't lose each other's changes.

After pi's `withFileMutationQueue` (`packages/coding-agent/src/core/tools/file-mutation-queue.ts`).
Calls on different files still run at the same time. `asyncio.Lock` serves waiters in arrival
order and the lookup below never awaits, so pi's extra registration queue isn't needed.
"""

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class _Entry:
    """A file's lock, and how many calls hold it or wait for it."""

    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    users: int = 0


# Keyed by the fully resolved path, so a symlink and its target share one lock.
_entries: dict[Path, _Entry] = {}


# TS: an async context manager; `async with file_lock(p):` is like `await using _ = await fileLock(p)`.
@asynccontextmanager
async def file_lock(path: Path) -> AsyncIterator[None]:
    """Hold `path`'s lock for the body of the `async with`; other calls on the same file wait their turn."""
    key = path.resolve()  # follows symlinks, and works for files that don't exist yet
    entry = _entries.setdefault(key, _Entry())  # TS: map.get(key) ?? map.set(key, new Entry()).get(key)
    entry.users += 1
    try:
        async with entry.lock:
            yield
    finally:
        entry.users -= 1
        if entry.users == 0:  # nobody holds or waits for it: drop it so the dict doesn't grow forever
            del _entries[key]
