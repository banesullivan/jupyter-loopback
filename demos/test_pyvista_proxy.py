"""
Self-contained smoke test for the PyVista + jupyter-loopback wiring.

Runs entirely in one Python process on a single asyncio event loop:
stands up a Tornado server with the loopback proxy mounted under the
``pyvista`` namespace, launches a real PyVista trame server on the same
loop, and exercises both the HTTP and WebSocket halves of the proxy
against the live trame upstream.

This intentionally avoids spinning up JupyterLab so it's the cheapest
possible end-to-end check. Use it to catch breakage in CI or after a
``docker run`` to verify the image's PyVista deps work::

    python demos/test_pyvista_proxy.py

Single-event-loop matters: trame's aiohttp backend installs signal
handlers via ``loop.add_signal_handler``, which only works on the main
thread of the main interpreter. Driving everything from
``asyncio.run(...)`` in ``__main__`` satisfies that constraint without
threads.

Exits non-zero on any failure; on success prints ``OK`` and the iframe
URL the demo notebook would have built.
"""

from __future__ import annotations

import asyncio
import os
import sys

# PyVista must be in off-screen mode before any rendering import path
# touches VTK, otherwise it tries to open an X display in headless envs.
os.environ.setdefault("PYVISTA_OFF_SCREEN", "true")

import pyvista as pv
from pyvista.trame.jupyter import initialize, launch_server
from tornado.httpclient import AsyncHTTPClient
from tornado.httpserver import HTTPServer
from tornado.testing import bind_unused_port
from tornado.web import Application
from tornado.websocket import websocket_connect

from jupyter_loopback._server import LoopbackProxyHandler, setup_proxy_handler


class _NoAuthHandler(LoopbackProxyHandler):
    """Skip JupyterHandler's auth so the smoke test doesn't need a token."""

    def prepare(self) -> None:
        return None

    def check_xsrf_cookie(self) -> None:
        return None

    def get_current_user(self) -> dict[str, str]:
        return {"name": "test"}


_JUPYTER_LIKE_SETTINGS: dict = {
    "base_url": "/",
    "default_url": "/",
    "disable_check_xsrf": True,
    "login_url": "/login",
    "static_path": "/tmp",
    "cookie_secret": "smoke-test",
    "token": "",
    "allow_remote_access": True,
    "local_hostnames": ["127.0.0.1", "localhost"],
}


def _build_proxy_app() -> Application:
    app = Application([], **_JUPYTER_LIKE_SETTINGS)
    setup_proxy_handler(app, namespace="pyvista", handler_cls=_NoAuthHandler)
    return app


async def _start_trame_server() -> int:
    """Launch a PyVista trame server on the running loop. Returns its port."""
    plotter = pv.Plotter(notebook=True, off_screen=True)
    plotter.add_mesh(pv.Wavelet(), show_edges=True)

    server = launch_server(server=pv.global_theme.trame.jupyter_server_name)
    await server.ready
    initialize(server, plotter)
    return int(server.port)


def _start_proxy() -> int:
    sock, port = bind_unused_port()
    server = HTTPServer(_build_proxy_app())
    server.add_sockets([sock])
    return port


async def _assert_probe(proxy_port: int) -> None:
    client = AsyncHTTPClient()
    resp = await client.fetch(
        f"http://127.0.0.1:{proxy_port}/pyvista-proxy/__probe__",
        method="HEAD",
        raise_error=False,
    )
    assert resp.code == 204, f"expected 204, got {resp.code}"
    namespace = resp.headers.get("X-Jupyter-Loopback-Namespace")
    assert namespace == "pyvista", namespace
    print(f"  probe: {resp.code} namespace={namespace}")


async def _assert_http_through_proxy(proxy_port: int, trame_port: int) -> None:
    client = AsyncHTTPClient()
    resp = await client.fetch(
        f"http://127.0.0.1:{proxy_port}/pyvista-proxy/{trame_port}/index.html",
        raise_error=False,
        request_timeout=15,
    )
    assert resp.code == 200, f"expected 200, got {resp.code}; body={resp.body[:200]!r}"
    body = resp.body.decode("utf-8", errors="replace")
    assert "<html" in body.lower(), "expected HTML body"
    assert "trame" in body.lower(), "expected trame index"
    print(f"  HTTP through proxy: {resp.code} {len(body)} bytes")


async def _assert_ws_through_proxy(proxy_port: int, trame_port: int) -> None:
    """Drive a real wslink hello/handshake through the loopback WS proxy.

    wslink frames are msgpack-encoded and chunk-prefixed (12-byte header
    of id/offset/size in little-endian). The simplest valid message is
    one chunk carrying the full payload, so we hand-roll that here and
    decode the reply the same way. If both directions of the WS proxy
    are working, ``handleSystemMessage`` on the server replies with a
    ``result.clientID`` for us to read back.
    """
    import msgpack
    from wslink.chunking import HEADER_LENGTH, generate_chunks

    url = f"ws://127.0.0.1:{proxy_port}/pyvista-proxy/{trame_port}/ws"
    ws = await websocket_connect(url, connect_timeout=10.0)
    try:
        # rpcid prefix ``system:`` routes wslink's ``handleSystemMessage``
        # branch, which produces the ``hello`` reply with a clientID
        # when auth succeeds, or a structured error otherwise. Both
        # shapes prove the WS proxy round-tripped: client->server frames
        # parsed (msgpack and chunking made it through) AND server->client
        # frames came back. That's the whole proxy contract; trame's
        # auth secret isn't part of it.
        rpcid = "system:c0:0"
        hello = {
            "wslink": "1.0",
            "id": rpcid,
            "method": "wslink.hello",
            "args": [{"secret": "wslink-secret"}],
            "kwargs": {},
        }
        packed = msgpack.packb(hello)
        # ``max_size=0`` -> single chunk; trame's hello reply is small
        # enough to come back as a single chunk too, so we don't need a
        # reassembly loop here.
        for chunk in generate_chunks(packed, max_size=0):
            await ws.write_message(chunk, binary=True)

        reply = await asyncio.wait_for(ws.read_message(), timeout=15.0)
        assert reply is not None, "trame closed the WS without a hello reply"
        assert isinstance(reply, (bytes, bytearray)), f"expected binary, got {type(reply)}"
        # Server reply is also a chunked-msgpack frame; strip the 12-byte
        # header and unpack the payload.
        payload = msgpack.unpackb(bytes(reply)[HEADER_LENGTH:], raw=False)
        assert payload.get("id") == rpcid, payload
        if "result" in payload and payload["result"].get("clientID"):
            client_id = payload["result"]["clientID"]
            print(f"  WS through proxy: clientID={client_id} (full hello succeeded)")
        elif "error" in payload:
            # Auth rejected -> the proxy still successfully relayed both
            # directions; the secret mismatch is a wslink-level concern,
            # not a proxy concern.
            print(f"  WS through proxy: round-trip OK; wslink error={payload['error']}")
        else:
            msg = f"unexpected wslink reply shape: {payload}"
            raise AssertionError(msg)
    finally:
        ws.close()


async def main() -> int:
    print("Starting trame server (in-process)...")
    trame_port = await _start_trame_server()
    print(f"  trame ready on 127.0.0.1:{trame_port}")

    print("Starting loopback proxy...")
    proxy_port = _start_proxy()
    print(f"  proxy ready on 127.0.0.1:{proxy_port}")

    print("Probing namespace registration...")
    await _assert_probe(proxy_port)

    print("Validating HTTP through proxy...")
    await _assert_http_through_proxy(proxy_port, trame_port)

    print("Validating WebSocket through proxy...")
    await _assert_ws_through_proxy(proxy_port, trame_port)

    iframe_url = f"http://127.0.0.1:{proxy_port}/pyvista-proxy/{trame_port}/index.html"
    print(f"\nOK -- iframe url that exercises both halves: {iframe_url}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
