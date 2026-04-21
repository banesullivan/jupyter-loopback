# Architecture

This document is a deep dive for maintainers and curious contributors. For user-facing docs, see [`README.md`](README.md).

## The problem

A library runs an HTTP or WebSocket server inside a Jupyter kernel, bound to `127.0.0.1:<port>`. The kernel process and the user's browser are frequently not the same host:

| Setup                              | Kernel process      | Browser               | Reachable?                         |
| ---------------------------------- | ------------------- | --------------------- | ---------------------------------- |
| Local notebook                     | localhost           | localhost             | yes, direct                        |
| JupyterLab in a container          | container network   | host                  | no, port not exposed               |
| JupyterHub / Binder                | per-user kernel pod | user's browser        | no, multi-hop                      |
| VS Code Remote                     | remote VM           | local VS Code webview | no, tunnel only for the Jupyter UI |
| Google Colab                       | Google VM           | user                  | no                                 |
| Shiny for Python / Solara / marimo | kernel process      | browser               | no, no jupyter-server at all       |

The kernel's loopback port is invisible outside its own process. Every deployment scenario above requires a different workaround. The status quo is `jupyter-server-proxy` plus a README paragraph asking users to export `LIBRARY_CLIENT_PREFIX='proxy/{port}'` and prepend `$JUPYTERHUB_SERVICE_PREFIX` on Hub. Most users skip it.

## Design goals

1. **Zero user config.** Install the library, it works.
2. **Cover both topologies.** HTTP proxy when there's a jupyter-server to host one; comm-based fallback when there isn't.
3. **Narrow surface.** Loopback only. No subprocess management. No URL rewriting.
4. **Adopt-in-a-day.** Library authors get the proxy plus the URL autodetect with three small integrations.

## Two-path architecture

```
┌────────────────────────────────────────────────────────────────────┐
│  Browser                                                           │
│  ┌──────────────────────────┐      ┌─────────────────────────────┐ │
│  │ Path A: HTTP(S)/WS       │      │ Path B: kernel comm ws      │ │
│  │ to jupyter-server origin │      │ via window.__jupyter_       │ │
│  └──────────┬───────────────┘      │ loopback__.request()        │ │
└─────────────┼──────────────────────┴──────────────┬──────────────────
              │                                     │
              ▼                                     ▼
┌─────────────────────────────┐         ┌───────────────────────────┐
│  jupyter-server PROCESS     │         │  kernel PROCESS           │
│  ┌───────────────────────┐  │  comm   │  ┌─────────────────────┐  │
│  │ LoopbackProxyHandler  │◀─┼─ ws ───▶│  │ CommBridge          │  │
│  │ <base>/<ns>-proxy/... │  │         │  │ anywidget           │  │
│  └──────────┬────────────┘  │         │  └─────────┬───────────┘  │
│             │ HTTP/WS       │         │            │ Python call  │
│             │ to loopback   │         │            ▼              │
│             ▼               │         │  ┌─────────────────────┐  │
│  ┌───────────────────────┐  │         │  │ @on_request handler │  │
│  │ 127.0.0.1:<port>  ◀───┼──┼─────────┼──│ (same kernel)       │  │
│  │ your library server   │  │         │  └─────────────────────┘  │
│  └───────────────────────┘  │         │                           │
└─────────────────────────────┘         └───────────────────────────┘
```

Both paths end in the same library code. The library author doesn't care which one the frontend used; they wrote one server and one handler.

## Path A: the HTTP / WebSocket proxy

### The route

`setup_proxy_handler(web_app, namespace="mylib")` mounts a route at
`<base_url>/mylib-proxy/(\d+)(?:/(.*))?` via `tornado.web.Application.add_handlers`. The handler is a namespace-specific subclass of `LoopbackProxyHandler` generated at registration time (one class per namespace so logs and stack traces identify who owns the route).

Patterns are anchored with `re.escape(namespace)` to prevent accidentally letting `mylib2-proxy/…` match a `mylib` route. Namespace strings are validated against `[a-z0-9][a-z0-9-]*` at registration; invalid input raises `ValueError`. Double-registration of the same `(web_app, namespace)` pair raises `RuntimeError` so misconfiguration surfaces loudly.

### The MRO

```python
class LoopbackProxyHandler(WebSocketHandler, JupyterHandler):
```

Order matters. Tornado resolves HTTP and WS method dispatch through `WebSocketHandler`; authentication (`prepare`, `check_xsrf_cookie`, `get_current_user`) resolves through `JupyterHandler`, because `WebSocketHandler` doesn't override those. The result: Jupyter's token/cookie auth runs before any proxy code, and the WS upgrade mechanics run on top.

`test_auth_inheritance_redirects_anonymous_requests` regression-guards this specifically. Any time someone refactors the MRO, if auth stops running, that test fails loudly rather than silently shipping a security hole.

### HTTP forwarding

`_proxy_http` is boring on purpose:

1. Build the upstream URL from the matched `port` and `path` plus the original query string.
2. Copy headers, minus hop-by-hop (RFC 7230 §6.1) and notebook-auth (`Host`, `Authorization`, `Cookie`). Notebook credentials don't belong on the loopback socket.
3. Forward the method + body if it's `POST`/`PUT`/`PATCH`. (Other methods don't carry bodies in practice; `allow_nonstandard_methods=True` lets exotic methods pass through.)
4. `AsyncHTTPClient.fetch` with `decompress_response=False` (preserves gzip/br for the browser to handle), `follow_redirects=False` (the upstream's redirects belong to the upstream, not us), and a 60s timeout.
5. Copy the upstream response back. Set `Access-Control-Allow-Origin: *` for iframe embedding (folium, etc). Write body, finish.

### Why 502, not 404, when the upstream is down

Earlier versions consulted a registry of "live TileClients" to validate the port before forwarding. This was wrong: the registry lives in the kernel process, the proxy runs in the jupyter-server process, and they never share memory. Every request 404'd.

The correct behavior is to attempt the connect and let Tornado tell us what happened. `ConnectionRefusedError` (nothing listening) becomes a clean HTTP 502 Bad Gateway. That's distinguishable from the upstream returning its own 404, which callers care about.

`test_http_proxy_works_across_process_boundary` uses `multiprocessing.spawn` specifically to regression-guard the registry-in-shared-memory mistake.

### WebSocket forwarding

The handler's `get()` peeks at the request headers:

- If `Connection: Upgrade` and `Upgrade: websocket`, it delegates to `WebSocketHandler.get`, which performs the handshake and calls `open()`.
- Otherwise, it's HTTP, and we dispatch to `_proxy_http`.

On `open()`:

1. Open a WS to `ws://127.0.0.1:<port>/<path>` via `websocket_connect`. Handshake headers generated by the browser (`Sec-WebSocket-Key`, `Sec-WebSocket-Version`, extensions, subprotocol) are dropped; Tornado regenerates its own for the upstream handshake.
2. Register `_on_upstream_message` as the upstream's message callback.
3. If the connect fails, close the browser WS with code 1011 (Internal Error) plus the reason string. The client sees a clean close frame instead of a mysteriously dropped handshake.

Bidirectional relay:

- `on_message` (browser → upstream): `await upstream.write_message(message, binary=…)`.
- `_on_upstream_message` (upstream → browser): this is synchronous by Tornado's contract, but `self.write_message` returns a Future. We schedule it with `asyncio.ensure_future` and attach `_log_write_errors` so exceptions don't silently drop. The happy path just runs.

`on_close` closes the upstream. Origin checks are unconditional: `JupyterHandler.prepare` already gated the request on the notebook token, and browsers' same-origin policy prevents cross-origin pages from opening authenticated WS connections anyway, so `check_origin = True` is the correct policy here.

## Path B: the anywidget comm bridge

### Why it exists

VS Code Remote's webview, Colab's kernel runtime, Shiny for Python, Solara, marimo. None of them run a jupyter-server. Path A has no surface to mount on.

They do expose kernel comms: the websocket that JupyterLab itself uses for widget state sync. `anywidget` standardizes it across frontends. We use it as a cheap request/response RPC.

### Wire protocol

```json
// Frontend → kernel
{
  "type": "request",
  "id": "abcd1234",
  "namespace": "mylib",
  "kind": "get_tile",
  "data": {"z": 8, "x": 71, "y": 110}
}

// Kernel → frontend
{
  "type": "response",
  "id": "abcd1234",
  "status": "ok",
  "data": {"ok": true}
}
// + binary buffers (anywidget's msg:custom supports binary alongside JSON)
```

A 30s watchdog on the frontend times out hung requests so pending callbacks don't leak. Errors are forwarded structurally (`{"status": "error", "error": "..."}`) rather than crashing the kernel.

### Registering handlers

```python
@on_request("mylib", "get_tile")
def _(data, buffers):
    return {"ok": True}, [tile_bytes]
```

Handlers live in `_HANDLERS: dict[tuple[str, str], RequestHandler]` keyed by `(namespace, kind)`, guarded by `_HANDLER_LOCK`. `off_request(namespace, kind)` removes one. Re-registering the same key overwrites; library authors that need clean teardown should explicitly `off_request`.

### Handler execution

`_on_msg` runs on the kernel's main thread (the comm message handler). It submits the dispatch to a thread pool (`_POOL`, 4 workers by default) so a slow handler doesn't block the message loop. The pool call returns `(response, buffers)` which the worker sends back via `self.send` (anywidget's comm send is thread-safe — it posts onto ipykernel's comm-send queue).

Broad `except Exception` in `_dispatch` is deliberate: any handler error (rendering bugs, KeyError, IO) is forwarded to the frontend as a structured `status: "error"` response rather than swallowed in the thread pool. The catch is commented; BLE001 would flag it otherwise.

### The singleton

`enable_comm_bridge()` is idempotent. First call constructs the `CommBridge`, subsequent calls return the same instance. State is guarded by `_BRIDGE_LOCK` so concurrent callers on free-threaded Python 3.13t can't create two bridges. `display=True` (keyword-only, per API policy for booleans) displays the widget via `IPython.display` to boot its frontend half; `RuntimeError` from `display()` (no IPython frontend) is swallowed so CLI scripts and tests still work.

### Frontend side

`window.__jupyter_loopback__` is a shared global. Each rendered `CommBridge` (there's typically one per notebook, but cell re-runs can create more) pushes a `Bridge` object onto a stack. Sends walk the stack newest-first and use the first live bridge. When a cell re-renders, the old bridge's `alive()` returns false and the new one takes over transparently. No manual reconnect logic needed.

The bundle is hand-written JSDoc-annotated JavaScript so `tsc --checkJs` typechecks it without a TypeScript build step. Biome handles lint and format.

## Autodetect

`autodetect_prefix("mylib")` returns a template string the kernel can `.format(port=...)` to produce a browser-reachable URL. It's a pure env-var lookup:

1. If neither `JPY_SESSION_NAME` nor `JPY_PARENT_PID` is set, we're not in a Jupyter kernel — return `None` so the caller falls back to loopback.
2. Start with `{namespace}-proxy/{port}`.
3. If `JUPYTERHUB_SERVICE_PREFIX` is set (JupyterHub, Binder, multi-tenant deployments), prepend it. `/user/alice/mylib-proxy/{port}` is the typical Hub result.
4. Otherwise if `JPY_BASE_URL` is set, prepend it.

Template-based so library authors can override the URL shape if they need to (custom CDN prefix, extra path segment). Default template is the one the HTTP proxy handler expects.

## Testing strategy

Three tiers by runtime cost:

- **Unit** (`test_autodetect.py`, `test_comm.py`): env-var logic, handler dispatch, registry behavior, protocol correctness via a fake `send`. Fast, no network.
- **Integration** (`test_server.py`): real Tornado app mounting the real handler against a real upstream Tornado app. HTTP binary round-trip, query-string preservation, 502 on connection refused, WS text + binary + upstream-unreachable. Plus `test_auth_inheritance_redirects_anonymous_requests` exercising the un-mocked `JupyterHandler` auth path.
- **Cross-process** (`test_http_proxy_works_across_process_boundary`): upstream server lives in a separate `multiprocessing.spawn` process with zero shared memory. Catches the class of bug where in-process state looks like it works but fails under real JupyterLab's kernel/server split.
- **Demo** (`test_demo.py`): smoke tests for `loopback_demo.DemoServer` so the Dockerfile demo doesn't silently break.

State-isolation is autouse in `conftest.py`: `_HANDLERS`, `_BRIDGE`, `_ENABLED`, `_REGISTERED` reset before every test. Tests can run in any order without ordering dependencies.

## Non-goals

Explicitly out of scope. This keeps the library's maintenance cost low:

- **Subprocess management.** You bring your own server. If you want `jupyter-server-proxy`'s server-launch hooks, use `jupyter-server-proxy`.
- **Cross-host proxying.** Loopback only. Hardcoded to `127.0.0.1`. No configuration for alternate upstreams.
- **URL rewriting in request/response bodies.** If your upstream embeds absolute URLs, that's your problem to handle.
- **Streaming over the comm bridge.** Request/response shape only. Use the WS proxy for server-push / SSE / chunked streaming.
- **Authentication beyond Jupyter's.** The proxy inherits the notebook token; there's no separate auth surface.

## Extension points

Library authors can subclass `LoopbackProxyHandler` and pass it via `setup_proxy_handler(..., handler_cls=MySubclass)`. Useful for:

- Rejecting a subset of paths (e.g. refusing proxy access to admin endpoints).
- Adding logging or metrics per-request.
- Injecting custom response headers.

The `handler_cls` path is tested (`test_setup_proxy_handler_accepts_custom_handler_cls`) so library authors can rely on it.

## Future work

Currently speculative, documented so it doesn't get reinvented:

- **Service Worker-backed HTTP-like fetch over the comm bridge.** Would let ipyleaflet/folium use normal URL-based tile loading in non-jupyter-server frontends. Browser security makes SW registration from blob/data URLs hard; requires the host library to serve a well-known SW at a stable URL. Tracked only as a design idea.
- **WS authentication subprotocols.** Currently the browser's `Sec-WebSocket-Protocol` is dropped on forward. Upstreams that need subprotocol negotiation can't use them today. Would need careful forwarding of the protocol list and mirroring upstream's acceptance back.
- **Response streaming.** Currently `_proxy_http` buffers the full upstream body before writing. Fine for tiles; unbearable for large downloads. Would need `streaming_callback` plumbing.

None of these are blocking v1.0. All are in-scope additions later.
