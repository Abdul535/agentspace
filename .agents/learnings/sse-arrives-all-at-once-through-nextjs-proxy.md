# Streamed replies arrive all at once through the Next.js proxy

**Date:** 2026-11-17

## Symptom
In the browser, the agent's answer didn't stream: nothing showed while the model
was writing, then the whole reply appeared at once. The API itself streamed fine:
`curl -N` straight to FastAPI (`:8000`) and even through Next.js
(`:3000/api/...`) showed `text_delta` events spread over several seconds.

## Cause
**Gzip.** The Next.js server proxies `/api/*` to FastAPI (the rewrite in
`apps/web/next.config.ts`) and gzips responses for clients that send
`Accept-Encoding: gzip`. Browsers always send it; plain `curl` doesn't, which is
why curl looked fine. A gzip encoder holds data back until it has enough to
compress, so the SSE events only got through when the stream closed. With the
browser's headers (`curl --compressed -H 'accept-encoding: gzip, br'`) every
event arrived together at the end, and the response had `Content-Encoding: gzip`.

Next.js compresses with the `compression` package, which skips any response whose
`Cache-Control` contains `no-transform`. FastAPI's SSE response only sends
`Cache-Control: no-cache` (plus `X-Accel-Buffering: no`, which only nginx reads).

**Second trap: setting the header inside the route did nothing.** The route is an
async generator, and FastAPI sends the headers before the generator body runs.
TS comparison: code in an `async function*` only runs when someone calls
`.next()`, and by then the `Response` has already been created.

## Fix
`apps/api/src/agentspace/api/sessions.py`: a dependency sets the header before the
response starts, and the SSE route uses it.

```python
def no_transform(response: Response) -> None:
    response.headers["Cache-Control"] = "no-cache, no-transform"

@router.post("/{session_id}/messages", response_class=EventSourceResponse, dependencies=[Depends(no_transform)])
```

Verify with the browser's encodings, not plain curl:

```sh
curl -sN --compressed -D /dev/stderr -X POST http://127.0.0.1:3000/api/sessions/<id>/messages \
  -H 'content-type: application/json' -H 'accept-encoding: gzip, br' -d '{"content":"..."}'
```

The headers must show no `Content-Encoding`, and events must arrive spread out.
`test_api.py` asserts `no-transform` is present.

I didn't set `compress: false` in `next.config.ts`: it turns gzip off for the
whole web app, and the Next.js docs advise against it. `no-transform` also tells
nginx and CDNs to leave the stream alone.

## Not a bug
- **Two `Cache-Control` headers** (FastAPI's `no-cache`, then ours). HTTP treats
  repeated headers as one comma-separated list, so proxies see
  `no-cache, no-cache, no-transform`.
- **`curl` without `-N`** buffers its own output and can look like the server
  is buffering. Always use `-N` when checking a stream.
- **Safari** holds back the first 1 KB of a stream. That only affects tiny
  responses, not a chat run.
