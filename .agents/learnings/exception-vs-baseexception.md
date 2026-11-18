# `except BaseException` swallows cancellation

## Date
2026-11-18 (found in debug session 1, `.agents/debug-sessions/debug-session-1.md`)

## Symptom
A run was cancelled while `before_tool_call` waited for approval, yet the loop
called the model a second time before stopping. The tool never ran, and the
caller still got `asyncio.CancelledError`, so the original test passed; only an
added `assert len(provider.contexts) == 1` caught it (`2 == 1`).

## Cause
`except Exception` in `prepare_tool_call` (`apps/api/src/agentspace/agent/loop.py`)
was changed to `except BaseException`. Python's exceptions are a class tree:

```
BaseException                  everything that can be raised
├── Exception                  ordinary errors: ValueError, KeyError, ToolError, ValidationError, ...
├── asyncio.CancelledError     "stop this task" (task.cancel())
├── KeyboardInterrupt          Ctrl+C
└── SystemExit                 sys.exit()
```

The ones outside `Exception` are requests to stop, not errors. `task.cancel()`
raises `CancelledError` at the `await` the task is paused on; it stops the task
only if it travels up the stack untouched. `except BaseException` caught it and
turned it into a "Tool call blocked" result, so the loop treated cancellation as
a failed tool call and asked the model again.

TS comparison: `catch (e)` catches everything, and abort arrives as an ordinary
rejection you filter by hand (`if (e.name === "AbortError") throw e`). Python
does that filtering through the hierarchy: `except Exception` is that `if`.

## Fix
- Handle errors with `except Exception:`, never `except BaseException:` or a bare
  `except:`, especially around an `await`.
- If you must touch a stop request (to clean up), re-raise it, or use `finally:`
  instead, which runs on cancellation without catching it.
- In tests of cancellation, assert that nothing ran after the cancel (here,
  `len(provider.contexts) == 1`), not only that `CancelledError` came out.

## Not a bug
- Nothing "crashes" when a cancellation goes up the stack: whoever cancelled
  expects it. In `run_agent`, `contextlib.suppress(asyncio.CancelledError)` around
  `await loop_task` handles the loop's cancellation; in tests, `pytest.raises`
  does; in production, Starlette does when the browser disconnects.
- The test still sees `CancelledError` even when the loop swallowed its own: it
  comes from the reader task (`run_agent`), which the test cancelled directly.
