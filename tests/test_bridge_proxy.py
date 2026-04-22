"""
Tests for :mod:`jupyter_loopback._bridge_proxy`.

Exercises the ``__loopback__`` built-in kinds that ride on top of the
comm bridge:

- ``fetch`` forwards HTTP to an upstream loopback server and returns
  binary bodies and status codes faithfully.
- ``ws_open`` / ``ws_send`` / ``ws_close`` bridge a browser-side
  WebSocket to an upstream WebSocket and relay frames in both
  directions.
- The built-in namespace is reserved: :func:`on_request` refuses to
  let user code shadow it.
- The end-to-end shape that real frontends see (request RPC producing
  a ``response`` frame plus, for WS, server-initiated ``event``
  frames) is verified by stubbing the bridge's ``send``.
"""

import asyncio
from collections.abc import Callable
import json
import socket
import threading
import time
from typing import Any

import pytest
from tornado.httpserver import HTTPServer
from tornado.ioloop import IOLoop
from tornado.testing import bind_unused_port
from tornado.web import Application, RequestHandler
from tornado.websocket import WebSocketHandler

pytest.importorskip("anywidget")

from jupyter_loopback import _bridge_proxy, _comm, enable_comm_bridge, on_request

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #


_PNG_BYTES = (
    b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01"
    b"\x08\x02\x00\x00\x00\x90wS\xde\x00\x00\x00\x0cIDATx\x9cc\xf8\xcf"
    b"\xc0\x00\x00\x00\x03\x00\x01\x84\xd0\xf9]\x00\x00\x00\x00IEND\xaeB`\x82"
)


class _Hello(RequestHandler):
    def get(self) -> None:
        self.set_header("Content-Type", "application/json")
        self.write(json.dumps({"ok": True, "q": self.get_argument("q", "")}))


class _Image(RequestHandler):
    def get(self) -> None:
        self.set_header("Content-Type", "image/png")
        self.write(_PNG_BYTES)


class _Echo(WebSocketHandler):
    def check_origin(self, _origin: str) -> bool:
        return True

    async def on_message(self, message: str | bytes) -> None:
        if isinstance(message, bytes):
            await self.write_message(message, binary=True)
        else:
            await self.write_message(f"echo:{message}")


@pytest.fixture
def upstream_server() -> int:
    """
    Boot a real Tornado server on 127.0.0.1 for the built-ins to hit.

    Runs its own IOLoop on a daemon thread so tests don't need any
    asyncio plumbing to use it.
    """
    sock, port = bind_unused_port(address="127.0.0.1")
    loop_ready = threading.Event()
    loop_holder: list[IOLoop] = []

    def run() -> None:
        asyncio.set_event_loop(asyncio.new_event_loop())
        loop = IOLoop.current()
        app = Application([(r"/hello", _Hello), (r"/image.png", _Image), (r"/ws", _Echo)])
        HTTPServer(app).add_sockets([sock])
        loop_holder.append(loop)
        loop_ready.set()
        loop.start()

    thread = threading.Thread(target=run, name="upstream-server", daemon=True)
    thread.start()
    assert loop_ready.wait(timeout=5.0)
    yield port
    loop_holder[0].add_callback(loop_holder[0].stop)
    thread.join(timeout=5.0)


# --------------------------------------------------------------------------- #
# fetch                                                                       #
# --------------------------------------------------------------------------- #


def test_install_registers_builtins() -> None:
    enable_comm_bridge(display=False)
    for kind in ("fetch", "ws_open", "ws_send", "ws_close"):
        assert (_bridge_proxy.BUILTIN_NAMESPACE, kind) in _comm._HANDLERS


def test_on_request_refuses_reserved_namespace() -> None:
    with pytest.raises(ValueError, match="reserved"):
        on_request("__loopback__", "fetch")


def test_builtin_fetch_relays_json(upstream_server: int) -> None:
    enable_comm_bridge(display=False)
    data, buffers = _bridge_proxy._builtin_fetch({"port": upstream_server, "path": "/hello"}, [])
    assert data["code"] == 200
    assert len(buffers) == 1
    assert json.loads(buffers[0]) == {"ok": True, "q": ""}


def test_builtin_fetch_preserves_query_string(upstream_server: int) -> None:
    enable_comm_bridge(display=False)
    data, buffers = _bridge_proxy._builtin_fetch(
        {"port": upstream_server, "path": "/hello", "query": "q=abc"}, []
    )
    assert data["code"] == 200
    assert json.loads(buffers[0]) == {"ok": True, "q": "abc"}


def test_builtin_fetch_returns_binary_body(upstream_server: int) -> None:
    enable_comm_bridge(display=False)
    data, buffers = _bridge_proxy._builtin_fetch(
        {"port": upstream_server, "path": "/image.png"}, []
    )
    assert data["code"] == 200
    assert buffers[0] == _PNG_BYTES
    headers = dict(tuple(pair) for pair in data["headers"])
    assert headers.get("Content-Type") == "image/png"


def test_builtin_fetch_normalizes_path(upstream_server: int) -> None:
    """``path`` without a leading slash is promoted to ``/path``."""
    enable_comm_bridge(display=False)
    data, _buffers = _bridge_proxy._builtin_fetch({"port": upstream_server, "path": "hello"}, [])
    assert data["code"] == 200


def test_builtin_fetch_connection_refused_raises() -> None:
    """
    ``_builtin_fetch`` lets connection errors bubble up so the comm
    dispatcher can serialize them as a structured ``status: "error"``
    reply rather than silently returning 200.
    """
    enable_comm_bridge(display=False)
    # Bind a port, close the socket so the OS will actively refuse connects.
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    refused_port = s.getsockname()[1]
    s.close()
    with pytest.raises(ConnectionError):
        _bridge_proxy._builtin_fetch({"port": refused_port, "path": "/hello"}, [])


# --------------------------------------------------------------------------- #
# WebSocket lifecycle                                                         #
# --------------------------------------------------------------------------- #


CapturedFrames = list[tuple[dict[str, Any], list[bytes]]]


def _install_bridge_capture() -> CapturedFrames:
    """
    Replace the live bridge's ``send`` with a capture that collects
    every ``(payload, buffers)`` pair synchronously.
    """
    enable_comm_bridge(display=False)
    assert _comm._BRIDGE is not None
    bridge = _comm._BRIDGE
    captured: CapturedFrames = []

    def fake_send(payload: dict[str, Any], buffers: list[bytes] | None = None) -> None:
        captured.append((payload, list(buffers or [])))

    bridge.send = fake_send  # type: ignore[method-assign]
    return captured


def _wait_for(predicate: Callable[[], bool], timeout: float = 3.0) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(0.01)
    msg = "timeout waiting for predicate"
    raise AssertionError(msg)


def test_websocket_text_roundtrip(upstream_server: int) -> None:
    captured = _install_bridge_capture()

    _bridge_proxy._builtin_ws_open({"ws_id": "w1", "port": upstream_server, "path": "/ws"}, [])
    _bridge_proxy._builtin_ws_send({"ws_id": "w1", "text": "hi"}, [])

    _wait_for(lambda: any(p.get("event") == "ws_message" for p, _ in captured))
    msg_frame = next(p for p, _ in captured if p.get("event") == "ws_message")
    assert msg_frame == {
        "type": "event",
        "event": "ws_message",
        "ws_id": "w1",
        "binary": False,
        "text": "echo:hi",
    }

    _bridge_proxy._builtin_ws_close({"ws_id": "w1"}, [])


def test_websocket_binary_roundtrip(upstream_server: int) -> None:
    captured = _install_bridge_capture()
    payload = b"\x00\x01\x02\x03"

    _bridge_proxy._builtin_ws_open({"ws_id": "w2", "port": upstream_server, "path": "/ws"}, [])
    _bridge_proxy._builtin_ws_send({"ws_id": "w2"}, [payload])

    _wait_for(lambda: any(p.get("event") == "ws_message" and p.get("binary") for p, _ in captured))
    frame, buffers = next(
        (p, b) for p, b in captured if p.get("event") == "ws_message" and p.get("binary")
    )
    assert frame["ws_id"] == "w2"
    assert buffers == [payload]

    _bridge_proxy._builtin_ws_close({"ws_id": "w2"}, [])


def test_websocket_close_drops_registry_entry(upstream_server: int) -> None:
    _install_bridge_capture()
    _bridge_proxy._builtin_ws_open({"ws_id": "w3", "port": upstream_server, "path": "/ws"}, [])
    assert "w3" in _bridge_proxy._WS_CONNS
    _bridge_proxy._builtin_ws_close({"ws_id": "w3"}, [])
    assert "w3" not in _bridge_proxy._WS_CONNS


def test_websocket_send_without_open_raises() -> None:
    enable_comm_bridge(display=False)
    with pytest.raises(KeyError, match="no open websocket"):
        _bridge_proxy._builtin_ws_send({"ws_id": "missing", "text": "x"}, [])


# --------------------------------------------------------------------------- #
# End-to-end via the bridge's message handler                                 #
# --------------------------------------------------------------------------- #


def test_fetch_end_to_end_via_bridge(upstream_server: int) -> None:
    """
    Simulate a frontend ``fetch`` RPC: post a ``request`` frame with
    namespace ``__loopback__``, capture the ``response`` the bridge
    sends back. Body rides in the response buffer.
    """
    bridge = enable_comm_bridge(display=False)
    captured: list = []
    done = threading.Event()

    def fake_send(payload: dict, buffers: list[bytes] | None = None) -> None:
        captured.append((payload, list(buffers or [])))
        if payload.get("type") == "response":
            done.set()

    bridge.send = fake_send  # type: ignore[method-assign]
    bridge._on_msg(
        bridge,
        {
            "type": "request",
            "id": "req-1",
            "namespace": "__loopback__",
            "kind": "fetch",
            "data": {"port": upstream_server, "path": "/hello"},
        },
        [],
    )
    assert done.wait(timeout=5.0)
    response_frame = next(p for p, _ in captured if p.get("type") == "response")
    assert response_frame["status"] == "ok"
    assert response_frame["data"]["code"] == 200
    _response, response_buffers = next((p, b) for p, b in captured if p.get("type") == "response")
    assert json.loads(response_buffers[0]) == {"ok": True, "q": ""}
