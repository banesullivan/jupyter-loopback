"""
Integration tests for :mod:`jupyter_loopback._server`.

Covers:

- Route mounting, namespace validation, duplicate protection.
- HTTP proxying end-to-end, including binary bodies and query strings.
- Connection failures surface as HTTP 502 (not 404, not a hang).
- WebSocket upgrades relay messages bidirectionally.
- Authentication inheritance: the real handler (not the test
  subclass) redirects anonymous requests.
- Cross-process correctness: the upstream HTTP server must work when
  it lives in a different process from the proxy.
"""

import asyncio
import json
import multiprocessing
import socket as _socket

import pytest
from tornado.httpclient import AsyncHTTPClient
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port
from tornado.web import Application, RequestHandler
from tornado.websocket import WebSocketHandler, websocket_connect

from jupyter_loopback._server import LoopbackProxyHandler, setup_proxy_handler

# ---------------------------------------------------------------------------
# Route mounting / validation
# ---------------------------------------------------------------------------


def test_setup_proxy_handler_returns_namespace_specific_subclass() -> None:
    app = Application([], base_url="/", cookie_secret="x")
    cls = setup_proxy_handler(app, namespace="mylib")
    assert issubclass(cls, LoopbackProxyHandler)
    assert cls is not LoopbackProxyHandler
    assert cls.namespace == "mylib"
    assert "Mylib" in cls.__name__


def _mounted_patterns(app: Application) -> list[str]:
    """Collect the URL regex patterns mounted on ``app`` via ``add_handlers``."""
    patterns: list[str] = []
    for outer in app.default_router.rules:
        target = outer.target
        if hasattr(target, "rules"):
            for inner in target.rules:
                regex = getattr(inner.matcher, "regex", None)
                if regex is not None:
                    patterns.append(regex.pattern)
    return patterns


def test_setup_proxy_handler_registers_route() -> None:
    app = Application([], base_url="/", cookie_secret="x")
    setup_proxy_handler(app, namespace="mylib")
    assert any("mylib-proxy" in p for p in _mounted_patterns(app))


def test_setup_proxy_handler_rejects_invalid_namespace() -> None:
    app = Application([], base_url="/", cookie_secret="x")
    for bad in ("My_Lib", "my lib", "MyLib", "-bad", ""):
        with pytest.raises(ValueError, match="invalid namespace"):
            setup_proxy_handler(app, namespace=bad)


def test_setup_proxy_handler_accepts_namespace_with_hyphens() -> None:
    app = Application([], base_url="/", cookie_secret="x")
    setup_proxy_handler(app, namespace="my-lib-v2")


def test_namespaces_are_isolated() -> None:
    """Two registered namespaces must not collide on routes."""
    app = Application([], base_url="/", cookie_secret="x")
    setup_proxy_handler(app, namespace="a")
    setup_proxy_handler(app, namespace="b")
    patterns = _mounted_patterns(app)
    # Each namespace mounts one main proxy route plus one probe route
    # (``<namespace>-proxy/__probe__``), so both strings appear twice.
    assert sum("a-proxy" in p for p in patterns) == 2
    assert sum("b-proxy" in p for p in patterns) == 2
    assert sum("a-proxy/__probe__" in p for p in patterns) == 1
    assert sum("b-proxy/__probe__" in p for p in patterns) == 1


def test_setup_proxy_handler_duplicate_namespace_raises() -> None:
    """Double-registering the same namespace on one web_app raises."""
    app = Application([], base_url="/", cookie_secret="x")
    setup_proxy_handler(app, namespace="dup")
    with pytest.raises(RuntimeError, match="already registered"):
        setup_proxy_handler(app, namespace="dup")


def test_setup_proxy_handler_accepts_custom_handler_cls() -> None:
    """The ``handler_cls`` override path produces a subclass of the override."""

    class _Custom(LoopbackProxyHandler):
        extra: int = 7

    app = Application([], base_url="/", cookie_secret="x")
    cls = setup_proxy_handler(app, namespace="custom", handler_cls=_Custom)
    assert issubclass(cls, _Custom)
    assert cls.namespace == "custom"
    assert cls.extra == 7


# ---------------------------------------------------------------------------
# Test harness — a proxy app that bypasses JupyterHandler's auth so we can
# exercise the proxy logic without the Jupyter login flow. The real auth
# path is covered by ``test_auth_inheritance_*`` below + jupyter-server's
# own tests.
# ---------------------------------------------------------------------------


class _NoAuthProxyHandler(LoopbackProxyHandler):
    """Test-only subclass that short-circuits JupyterHandler's auth prepare."""

    def prepare(self) -> None:
        return None

    def check_xsrf_cookie(self) -> None:
        return None

    def get_current_user(self) -> dict[str, str]:
        return {"name": "test"}


_COMMON_JUPYTER_SETTINGS: dict = {
    "base_url": "/",
    "default_url": "/",
    "disable_check_xsrf": True,
    "login_url": "/login",
    "static_path": "/tmp",
    "cookie_secret": "test",
    "token": "",
    "allow_remote_access": True,
    "local_hostnames": ["127.0.0.1", "localhost"],
}


def _build_proxy_app(
    namespace: str = "mylib",
    *,
    handler_cls: type[LoopbackProxyHandler] = _NoAuthProxyHandler,
) -> Application:
    """Tornado app with the proxy handler mounted for ``namespace``."""
    app = Application([], **_COMMON_JUPYTER_SETTINGS)
    setup_proxy_handler(app, namespace=namespace, handler_cls=handler_cls)
    return app


class _ProxyServer:
    """Context manager that runs the proxy on a random loopback port."""

    def __init__(
        self,
        namespace: str = "mylib",
        *,
        handler_cls: type[LoopbackProxyHandler] = _NoAuthProxyHandler,
    ):
        self.namespace = namespace
        self._handler_cls = handler_cls

    async def __aenter__(self) -> "_ProxyServer":
        self.sock, self.port = bind_unused_port()
        self.server = HTTPServer(_build_proxy_app(self.namespace, handler_cls=self._handler_cls))
        self.server.add_sockets([self.sock])
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.server.stop()
        await self.server.close_all_connections()
        self.sock.close()


# ---------------------------------------------------------------------------
# Upstream fixture: a plain Tornado app the proxy forwards to.
# ---------------------------------------------------------------------------


class _Echo(RequestHandler):
    async def get(self) -> None:
        self.set_header("Content-Type", "application/json")
        self.write(
            json.dumps(
                {
                    "path": self.request.path,
                    "query": self.request.query,
                    "method": self.request.method,
                }
            )
        )

    async def post(self) -> None:
        self.set_header("Content-Type", "application/octet-stream")
        self.write(self.request.body)


class _Binary(RequestHandler):
    async def get(self) -> None:
        # Arbitrary binary payload including NULs and non-UTF-8 bytes.
        self.set_header("Content-Type", "image/png")
        self.write(bytes(range(256)))


class _EchoWS(WebSocketHandler):
    def check_origin(self, origin: str) -> bool:
        return True

    async def on_message(self, message: str | bytes) -> None:
        await self.write_message(message, binary=isinstance(message, bytes))


def _upstream_app() -> Application:
    return Application([(r"/echo", _Echo), (r"/binary", _Binary), (r"/ws", _EchoWS)])


class _UpstreamServer:
    async def __aenter__(self) -> "_UpstreamServer":
        self.sock, self.port = bind_unused_port()
        self.server = HTTPServer(_upstream_app())
        self.server.add_sockets([self.sock])
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self.server.stop()
        await self.server.close_all_connections()
        self.sock.close()


# ---------------------------------------------------------------------------
# HTTP proxy
# ---------------------------------------------------------------------------


async def test_http_proxy_forwards_get() -> None:
    async with _ProxyServer() as proxy, _UpstreamServer() as upstream:
        client = AsyncHTTPClient()
        resp = await client.fetch(
            f"http://127.0.0.1:{proxy.port}/mylib-proxy/{upstream.port}/echo?k=v",
            raise_error=False,
        )
    assert resp.code == 200, resp.body
    body = json.loads(resp.body)
    assert body["path"] == "/echo"
    assert body["query"] == "k=v"
    assert body["method"] == "GET"


async def test_http_proxy_preserves_binary_body() -> None:
    async with _ProxyServer() as proxy, _UpstreamServer() as upstream:
        client = AsyncHTTPClient()
        direct = await client.fetch(f"http://127.0.0.1:{upstream.port}/binary", raise_error=False)
        proxied = await client.fetch(
            f"http://127.0.0.1:{proxy.port}/mylib-proxy/{upstream.port}/binary",
            raise_error=False,
        )
    assert direct.body == proxied.body
    assert proxied.body == bytes(range(256))


async def test_http_proxy_posts_body() -> None:
    async with _ProxyServer() as proxy, _UpstreamServer() as upstream:
        client = AsyncHTTPClient()
        payload = b"\x00\x01\x02hello\xff"
        resp = await client.fetch(
            f"http://127.0.0.1:{proxy.port}/mylib-proxy/{upstream.port}/echo",
            method="POST",
            body=payload,
            raise_error=False,
        )
    assert resp.code == 200
    assert resp.body == payload


async def test_http_proxy_502_when_upstream_down() -> None:
    async with _ProxyServer() as proxy:
        client = AsyncHTTPClient()
        resp = await client.fetch(
            f"http://127.0.0.1:{proxy.port}/mylib-proxy/1/echo",
            raise_error=False,
        )
    assert resp.code == 502


async def test_http_proxy_adds_cors_allow_origin() -> None:
    async with _ProxyServer() as proxy, _UpstreamServer() as upstream:
        client = AsyncHTTPClient()
        resp = await client.fetch(
            f"http://127.0.0.1:{proxy.port}/mylib-proxy/{upstream.port}/echo",
            raise_error=False,
        )
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


async def test_http_proxy_swallows_client_disconnect() -> None:
    """
    A client that aborts mid-response must not produce "Uncaught exception"
    logs from ``StreamClosedError``.

    Simulates what happens when Leaflet cancels a tile request as the
    user pans: the proxy's upstream fetch completes successfully, then
    the write back to the browser fails because the client has already
    closed the TCP stream. We guard against that and log at DEBUG.

    The test opens a raw TCP socket to the proxy, sends a minimal HTTP
    request, then immediately closes the socket before the response
    arrives. We capture the handler's logger and assert that no ERROR
    or WARNING records were emitted.
    """
    import logging
    import socket

    async with _ProxyServer() as proxy, _UpstreamServer() as upstream:
        # Attach a capturing handler to the tornado app loggers so we
        # can assert nothing noisier than DEBUG gets logged.
        records: list[logging.LogRecord] = []

        class _Capture(logging.Handler):
            def emit(self, record: logging.LogRecord) -> None:
                records.append(record)

        capture = _Capture(level=logging.DEBUG)
        for name in ("tornado.general", "tornado.application", "tornado.access"):
            logging.getLogger(name).addHandler(capture)
        try:
            # Open a raw TCP connection, send a minimal request, then
            # close the socket so the proxy's write() hits a closed
            # stream on the way back.
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.connect(("127.0.0.1", proxy.port))
            req = (
                f"GET /mylib-proxy/{upstream.port}/binary HTTP/1.1\r\n"
                "Host: 127.0.0.1\r\n"
                "Connection: close\r\n\r\n"
            )
            sock.sendall(req.encode())
            sock.close()

            # Give the server a moment to service the aborted request.
            await asyncio.sleep(0.5)
        finally:
            for name in ("tornado.general", "tornado.application", "tornado.access"):
                logging.getLogger(name).removeHandler(capture)

        bad = [
            r
            for r in records
            if r.levelno >= logging.WARNING
            and "StreamClosedError" in (r.getMessage() + (r.exc_text or ""))
        ]
        assert not bad, [r.getMessage() for r in bad]


# ---------------------------------------------------------------------------
# WebSocket proxy
# ---------------------------------------------------------------------------


async def test_websocket_proxy_round_trips_text() -> None:
    async with _ProxyServer() as proxy, _UpstreamServer() as upstream:
        ws = await websocket_connect(f"ws://127.0.0.1:{proxy.port}/mylib-proxy/{upstream.port}/ws")
        await ws.write_message("hello")
        reply = await asyncio.wait_for(ws.read_message(), timeout=5.0)
        assert reply == "hello"
        ws.close()


async def test_websocket_proxy_round_trips_binary() -> None:
    async with _ProxyServer() as proxy, _UpstreamServer() as upstream:
        ws = await websocket_connect(f"ws://127.0.0.1:{proxy.port}/mylib-proxy/{upstream.port}/ws")
        payload = bytes(range(128))
        await ws.write_message(payload, binary=True)
        reply = await asyncio.wait_for(ws.read_message(), timeout=5.0)
        assert reply == payload
        ws.close()


async def test_websocket_proxy_closes_when_upstream_unreachable() -> None:
    async with _ProxyServer() as proxy:
        # Port 1 is privileged and has nothing listening. The proxy
        # accepts the handshake then closes immediately.
        ws = await websocket_connect(f"ws://127.0.0.1:{proxy.port}/mylib-proxy/1/ws")
        reply = await asyncio.wait_for(ws.read_message(), timeout=5.0)
        assert reply is None


# ---------------------------------------------------------------------------
# Authentication inheritance: the real handler (not ``_NoAuthProxyHandler``)
# runs ``JupyterHandler.prepare`` and rejects anonymous requests.
# ---------------------------------------------------------------------------


async def test_auth_inheritance_redirects_anonymous_requests() -> None:
    """
    Without the ``_NoAuth`` override, ``JupyterHandler.prepare`` enforces
    token/cookie auth. A no-token request must NOT reach the proxy logic.

    The method-resolution order is
    ``(WebSocketHandler, JupyterHandler, ..., RequestHandler)``;
    ``WebSocketHandler`` does not define ``prepare``, so Python resolves
    to ``JupyterHandler.prepare``, which 302s or 403s unauthenticated
    callers. Any status code other than ``2xx`` proves auth ran.
    """
    async with _ProxyServer(handler_cls=LoopbackProxyHandler) as proxy:
        client = AsyncHTTPClient()
        resp = await client.fetch(
            f"http://127.0.0.1:{proxy.port}/mylib-proxy/1/anything",
            follow_redirects=False,
            raise_error=False,
        )
    # Anything that isn't 2xx means auth rejected the request before
    # the proxy tried to connect to port 1. A 502 would mean auth was
    # bypassed and the upstream connection actually attempted — a bug.
    assert resp.code != 502, "auth bypassed — proxy reached the upstream port"
    assert resp.code >= 300


# ---------------------------------------------------------------------------
# Cross-process correctness: real JupyterLab has the upstream server in
# a different process from the proxy. Any in-process state (module-level
# registries, etc.) is invisible across that boundary.
# ---------------------------------------------------------------------------


def _run_echo_in_subprocess(port: int, ready_port: int) -> None:
    """Child-process target: serve a fixed PNG and signal readiness."""
    import http.server as _hs

    payload = b"\x89PNG\r\n\x1a\ncross-process"

    class _Handler(_hs.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "image/png")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *_) -> None:
            return

    server = _hs.ThreadingHTTPServer(("127.0.0.1", port), _Handler)
    try:
        with _socket.socket(_socket.AF_INET, _socket.SOCK_STREAM) as s:
            s.connect(("127.0.0.1", ready_port))
    except OSError:
        pass
    server.serve_forever()


# ---------------------------------------------------------------------------
# Namespace probe — lets browser-side code detect whether the proxy
# extension is loaded on the single-user server without forwarding
# upstream.
# ---------------------------------------------------------------------------


async def test_probe_endpoint_returns_204_when_registered() -> None:
    """
    ``<base>/<namespace>-proxy/__probe__`` responds 204 when the
    extension is mounted. The comm bridge uses this signal to decide
    whether to route through HTTP (fast) or the comm bridge fallback.
    """
    async with _ProxyServer(namespace="mylib") as proxy:
        client = AsyncHTTPClient()
        resp = await client.fetch(
            f"http://127.0.0.1:{proxy.port}/mylib-proxy/__probe__",
            method="HEAD",
            raise_error=False,
        )
    assert resp.code == 204
    assert resp.headers.get("X-Jupyter-Loopback-Namespace") == "mylib"
    assert resp.headers.get("Access-Control-Allow-Origin") == "*"


async def test_probe_endpoint_isolated_per_namespace() -> None:
    """
    Two namespaces on the same app don't bleed probe responses into
    each other; the ``X-Jupyter-Loopback-Namespace`` header is how
    debugging tells them apart.
    """
    app = Application([], **_COMMON_JUPYTER_SETTINGS)
    setup_proxy_handler(app, namespace="a", handler_cls=_NoAuthProxyHandler)
    setup_proxy_handler(app, namespace="b", handler_cls=_NoAuthProxyHandler)
    sock, port = bind_unused_port()
    server = HTTPServer(app)
    server.add_sockets([sock])
    try:
        client = AsyncHTTPClient()
        resp_a = await client.fetch(
            f"http://127.0.0.1:{port}/a-proxy/__probe__",
            raise_error=False,
        )
        resp_b = await client.fetch(
            f"http://127.0.0.1:{port}/b-proxy/__probe__",
            raise_error=False,
        )
    finally:
        server.stop()
        await server.close_all_connections()
        sock.close()
    assert resp_a.code == 204
    assert resp_a.headers.get("X-Jupyter-Loopback-Namespace") == "a"
    assert resp_b.code == 204
    assert resp_b.headers.get("X-Jupyter-Loopback-Namespace") == "b"


async def test_probe_endpoint_404_when_namespace_not_registered() -> None:
    """
    Probing a namespace that was never mounted returns 404 (jupyter-server
    has no handler for that route). This is the signal the browser-side
    interceptor uses to fall back to the comm bridge.
    """
    app = Application([], **_COMMON_JUPYTER_SETTINGS)
    setup_proxy_handler(app, namespace="mounted", handler_cls=_NoAuthProxyHandler)
    sock, port = bind_unused_port()
    server = HTTPServer(app)
    server.add_sockets([sock])
    try:
        client = AsyncHTTPClient()
        resp = await client.fetch(
            f"http://127.0.0.1:{port}/unmounted-proxy/__probe__",
            raise_error=False,
        )
    finally:
        server.stop()
        await server.close_all_connections()
        sock.close()
    assert resp.code == 404


async def test_http_proxy_works_across_process_boundary() -> None:
    """
    The upstream server lives in a separate process; no shared state.

    This is the scenario that actually runs under JupyterLab: the
    kernel (and its HTTP server) lives in one process, jupyter-server
    in another. A proxy that depends on any in-process registry to
    validate the upstream port would 404 every request here; this test
    regression-guards that class of bug.
    """
    upstream_sock, upstream_port = bind_unused_port()
    ready_sock, ready_port = bind_unused_port()
    upstream_sock.close()
    ready_sock.listen(1)

    ctx = multiprocessing.get_context("spawn")
    child = ctx.Process(target=_run_echo_in_subprocess, args=(upstream_port, ready_port))
    child.start()

    try:
        ready_sock.settimeout(10.0)
        conn, _ = ready_sock.accept()
        conn.close()
        ready_sock.close()

        async with _ProxyServer() as proxy:
            client = AsyncHTTPClient()
            resp = await client.fetch(
                f"http://127.0.0.1:{proxy.port}/mylib-proxy/{upstream_port}/tile.png",
                raise_error=False,
            )
        assert resp.code == 200, resp.body
        assert resp.body == b"\x89PNG\r\n\x1a\ncross-process"
    finally:
        child.terminate()
        child.join(timeout=5)
        if child.is_alive():  # pragma: no cover — hang safeguard
            child.kill()
