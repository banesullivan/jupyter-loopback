"""
Built-in HTTP and WebSocket proxy handlers for the comm bridge.

The :mod:`jupyter_loopback._comm` module defines a generic
request/response RPC over kernel comms. This module installs a
reserved namespace (``__loopback__``) on that RPC with built-in kinds
that forward browser traffic to ``127.0.0.1:<port>`` in the kernel.

Why this exists
---------------
VS Code's Jupyter webview (and a handful of other frontends) never
reaches the jupyter-server origin for URLs embedded in cell outputs.
Root-relative paths that Path A's :class:`LoopbackProxyHandler` serves
are therefore unreachable. The bridge already has a live channel to
the kernel, so we reuse it: the frontend asks this module to perform
the HTTP fetch (or WebSocket open/send/close) and streams the result
back over the comm.

Design notes
------------
- Async I/O runs on a dedicated daemon-thread event loop so the sync
  :class:`RequestHandler` contract that user handlers rely on is
  preserved. One loop is shared across fetch and all WebSockets.
- ``__loopback__`` is reserved; user code cannot register it via
  :func:`jupyter_loopback.on_request`. The enforcement lives in
  :func:`install` below, called from :func:`enable_comm_bridge`.
- WebSocket state is tracked by a frontend-assigned id so one comm
  channel can multiplex many concurrent WebSockets without per-socket
  handshaking overhead.
"""

import asyncio
import logging
import threading
from typing import TYPE_CHECKING, Any
from urllib.parse import urlencode

from tornado import httpclient
from tornado.websocket import websocket_connect

if TYPE_CHECKING:
    from tornado.websocket import WebSocketClientConnection

    from jupyter_loopback._comm import CommBridge, RequestHandler

__all__ = ["BUILTIN_NAMESPACE", "install"]

logger = logging.getLogger(__name__)

# Reserved namespace for built-in kinds. User :func:`on_request` calls
# with this namespace are rejected so user code can't accidentally
# shadow the plumbing below.
BUILTIN_NAMESPACE = "__loopback__"

# Per-request timeout for the built-in HTTP proxy. Mirrors the Path A
# ``LoopbackProxyHandler`` default so both paths time out consistently.
_HTTP_TIMEOUT = 60.0
_WS_OPEN_TIMEOUT = 10.0

# Hop-by-hop headers (RFC 7230 §6.1) dropped when relaying responses.
# Duplicated from :mod:`_server` rather than imported to keep this
# module independent of Path A's imports.
_HOP_BY_HOP: frozenset[str] = frozenset(
    h.lower()
    for h in (
        "connection",
        "content-length",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailers",
        "transfer-encoding",
        "upgrade",
    )
)

# Shared background event loop for async I/O. Created lazily on first
# use so importing this module has no side effects.
_LOOP_LOCK = threading.Lock()
_LOOP: asyncio.AbstractEventLoop | None = None

# Active upstream WebSocket connections, keyed by the id the frontend
# assigned at open time. Guarded by a lock because open/send/close
# requests run on the bridge's thread pool and can interleave.
_WS_LOCK = threading.Lock()
_WS_CONNS: dict[str, "WebSocketClientConnection"] = {}


def _get_loop() -> asyncio.AbstractEventLoop:
    """
    Return the shared async I/O event loop, starting its thread on first call.
    """
    global _LOOP
    with _LOOP_LOCK:
        if _LOOP is None:
            loop = asyncio.new_event_loop()
            thread = threading.Thread(
                target=loop.run_forever,
                name="jupyter-loopback-io",
                daemon=True,
            )
            thread.start()
            _LOOP = loop
        return _LOOP


def _run(coro: Any, *, timeout: float) -> Any:
    """
    Submit ``coro`` to the shared loop and block until it resolves.

    Raises any exception the coroutine raised; the caller's broad
    ``except`` in :func:`jupyter_loopback._comm._dispatch` converts it
    to a structured error response on the wire.
    """
    future = asyncio.run_coroutine_threadsafe(coro, _get_loop())
    return future.result(timeout=timeout)


# --------------------------------------------------------------------------- #
# HTTP fetch                                                                  #
# --------------------------------------------------------------------------- #


async def _fetch(
    port: int,
    path: str,
    query: str,
    method: str,
    headers: dict[str, str],
    body: bytes | None,
) -> dict[str, Any]:
    """
    Perform the upstream HTTP fetch and return a JSON-serializable result.
    """
    upstream = f"http://127.0.0.1:{port}{path}"
    if query:
        upstream = f"{upstream}?{query}"
    client = httpclient.AsyncHTTPClient()
    try:
        response = await client.fetch(
            httpclient.HTTPRequest(
                upstream,
                method=method,
                headers=headers,
                body=body if method in ("POST", "PUT", "PATCH") else None,
                allow_nonstandard_methods=True,
                decompress_response=False,
                follow_redirects=False,
                request_timeout=_HTTP_TIMEOUT,
            ),
            raise_error=False,
        )
    except (OSError, ConnectionError) as exc:
        msg = f"loopback fetch failed: 127.0.0.1:{port}{path}: {exc}"
        raise ConnectionError(msg) from exc

    out_headers: list[list[str]] = []
    for name, value in response.headers.get_all():
        if name.lower() in _HOP_BY_HOP:
            continue
        out_headers.append([name, value])

    return {
        "code": response.code,
        "reason": response.reason or "",
        "headers": out_headers,
        "body": response.body or b"",
    }


def _builtin_fetch(
    data: dict[str, Any],
    buffers: list[bytes],
) -> tuple[dict[str, Any], list[bytes]]:
    """
    Sync-facing wrapper used by :mod:`_comm`'s request dispatcher.

    The payload shape mirrors what ``fetch``-style frontend code sends:
    port + path + method + headers + query + optional request body
    (passed as the first buffer). The response body comes back as the
    first response buffer so binary content survives JSON transit.
    """
    port = int(data["port"])
    path = data.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    query_raw = data.get("query")
    if isinstance(query_raw, dict):
        query = urlencode(query_raw, doseq=True)
    else:
        query = str(query_raw or "")
    method = str(data.get("method") or "GET").upper()
    headers_raw = data.get("headers") or {}
    headers: dict[str, str] = {str(k): str(v) for k, v in dict(headers_raw).items()}
    body = buffers[0] if buffers else None

    result = _run(
        _fetch(port, path, query, method, headers, body),
        timeout=_HTTP_TIMEOUT + 5.0,
    )
    response_body = result.pop("body")
    return result, [response_body]


# --------------------------------------------------------------------------- #
# WebSocket                                                                   #
# --------------------------------------------------------------------------- #


def _require_bridge() -> "CommBridge":
    """
    Fetch the live bridge singleton. Raises if the bridge isn't enabled.
    """
    from jupyter_loopback import _comm

    if _comm._BRIDGE is None:
        msg = "jupyter_loopback bridge is not enabled"
        raise RuntimeError(msg)
    return _comm._BRIDGE


def _send_event(event: str, ws_id: str, extra: dict[str, Any], buffers: list[bytes]) -> None:
    """
    Push a server-initiated event frame to the frontend.

    Unlike responses, events have no ``id`` field matching a pending
    request; the frontend routes them by ``event`` + ``ws_id``.
    """
    bridge = _require_bridge()
    payload = {"type": "event", "event": event, "ws_id": ws_id, **extra}
    # ``bridge.send`` comes from ``anywidget.AnyWidget``; mypy can't see
    # its signature because anywidget is in ``ignore_missing_imports``.
    bridge.send(payload, buffers)  # type: ignore[attr-defined]


def _builtin_ws_open(
    data: dict[str, Any],
    _buffers: list[bytes],
) -> tuple[dict[str, Any], list[bytes]]:
    """
    Open an upstream WebSocket and register it under the frontend's id.
    """
    ws_id = str(data["ws_id"])
    port = int(data["port"])
    path = data.get("path") or "/"
    if not path.startswith("/"):
        path = "/" + path
    query = str(data.get("query") or "")
    upstream = f"ws://127.0.0.1:{port}{path}"
    if query:
        upstream = f"{upstream}?{query}"

    def on_upstream_message(message: str | bytes | None) -> None:
        """
        Forward one upstream frame to the frontend as an event.

        ``None`` is Tornado's signal that the upstream closed; emit a
        close event and drop the connection from the registry.
        """
        if message is None:
            with _WS_LOCK:
                _WS_CONNS.pop(ws_id, None)
            try:
                _send_event("ws_close", ws_id, {}, [])
            except Exception:  # pragma: no cover — best-effort during shutdown
                logger.debug("ws_close event send failed for %s", ws_id, exc_info=True)
            return
        if isinstance(message, bytes):
            _send_event("ws_message", ws_id, {"binary": True}, [message])
        else:
            _send_event("ws_message", ws_id, {"binary": False, "text": message}, [])

    async def do_open() -> "WebSocketClientConnection":
        return await websocket_connect(
            upstream,
            on_message_callback=on_upstream_message,
        )

    conn = _run(do_open(), timeout=_WS_OPEN_TIMEOUT)
    with _WS_LOCK:
        _WS_CONNS[ws_id] = conn
    return {"opened": True}, []


def _builtin_ws_send(
    data: dict[str, Any],
    buffers: list[bytes],
) -> tuple[dict[str, Any], list[bytes]]:
    """
    Write a frame to the upstream WebSocket identified by ``ws_id``.
    """
    ws_id = str(data["ws_id"])
    with _WS_LOCK:
        conn = _WS_CONNS.get(ws_id)
    if conn is None:
        msg = f"no open websocket for ws_id {ws_id!r}"
        raise KeyError(msg)
    if buffers:
        payload: str | bytes = buffers[0]
        binary = True
    else:
        payload = str(data.get("text") or "")
        binary = False

    async def do_send() -> None:
        await conn.write_message(payload, binary=binary)

    _run(do_send(), timeout=_HTTP_TIMEOUT)
    return {"sent": True}, []


def _builtin_ws_close(
    data: dict[str, Any],
    _buffers: list[bytes],
) -> tuple[dict[str, Any], list[bytes]]:
    """
    Close an upstream WebSocket previously opened by ``ws_open``.

    ``conn.close()`` schedules a close-frame write via the event loop,
    so it must run on the shared I/O thread. Running it on the caller's
    thread raises ``RuntimeError: no current event loop``.
    """
    ws_id = str(data["ws_id"])
    with _WS_LOCK:
        conn = _WS_CONNS.pop(ws_id, None)
    if conn is not None:

        async def do_close() -> None:
            conn.close()

        _run(do_close(), timeout=_HTTP_TIMEOUT)
    return {"closed": True}, []


# --------------------------------------------------------------------------- #
# Registration                                                                #
# --------------------------------------------------------------------------- #


_BUILTINS: tuple[tuple[str, "RequestHandler"], ...] = (
    ("fetch", _builtin_fetch),
    ("ws_open", _builtin_ws_open),
    ("ws_send", _builtin_ws_send),
    ("ws_close", _builtin_ws_close),
)


def install() -> None:
    """
    Register every built-in kind into the shared handler registry.

    Called from :func:`jupyter_loopback.enable_comm_bridge`. Idempotent;
    calling it a second time just rewrites the same entries.
    """
    from jupyter_loopback import _comm

    with _comm._HANDLER_LOCK:
        for kind, handler in _BUILTINS:
            _comm._HANDLERS[(BUILTIN_NAMESPACE, kind)] = handler
