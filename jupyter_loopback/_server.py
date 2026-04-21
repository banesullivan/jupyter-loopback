"""
Tornado proxy handlers for :mod:`jupyter_loopback`.

One handler class handles both HTTP and WebSocket traffic for a
registered namespace. Downstream libraries mount the handler with
:func:`setup_proxy_handler` from their jupyter-server extension's
``_load_jupyter_server_extension`` hook. See the README for a full
integration example.

Design notes
------------

- The handler forwards only to loopback (``127.0.0.1``). Cross-host
  proxying is out of scope; use ``jupyter-server-proxy`` for that.
- The handler inherits authentication from :class:`JupyterHandler`, so
  Jupyter's token/cookie protects the loopback port the same way it
  protects its own APIs.
- There is no in-process registry check on the upstream port, because
  jupyter-server and the kernel are different processes. Connection
  failures surface as HTTP 502 at request time instead.
"""

import asyncio
import logging
import re
from typing import TYPE_CHECKING, Any, ClassVar

from jupyter_server.base.handlers import JupyterHandler
from jupyter_server.utils import url_path_join
from tornado import httpclient, web
from tornado.httputil import HTTPServerRequest
from tornado.iostream import StreamClosedError
from tornado.websocket import WebSocketHandler, websocket_connect

if TYPE_CHECKING:
    from tornado.web import Application
    from tornado.websocket import WebSocketClientConnection

__all__ = ["LoopbackProxyHandler", "setup_proxy_handler"]

logger = logging.getLogger(__name__)

# Hop-by-hop headers (RFC 7230 §6.1) plus ``Content-Length`` which
# Tornado computes automatically from the response body we ``write``.
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

# Headers dropped when forwarding to upstream. The notebook server's
# credentials must not leak into the loopback service.
_STRIP_ON_FORWARD: frozenset[str] = frozenset(("host", "authorization", "cookie"))

# HTTP methods that carry a request body worth forwarding.
_METHODS_WITH_BODY: frozenset[str] = frozenset(("POST", "PUT", "PATCH"))

# WebSocket handshake headers Tornado regenerates itself on the
# upstream connection. Forwarding the browser's copies confuses the
# upstream handshake.
_WS_HANDSHAKE_HEADERS: frozenset[str] = frozenset(
    (
        "sec-websocket-key",
        "sec-websocket-version",
        "sec-websocket-extensions",
        "sec-websocket-protocol",
    )
)

# Tracks namespaces already registered on a given web_app so that
# :func:`setup_proxy_handler` raises on double-registration rather
# than silently appending duplicate routes. Keyed by ``id(web_app)``
# to avoid pinning the Tornado ``Application`` alive.
_REGISTERED: dict[int, set[str]] = {}


def _is_websocket_upgrade(request: HTTPServerRequest) -> bool:
    """
    Return ``True`` if ``request`` is a WebSocket upgrade handshake.

    Parameters
    ----------
    request : tornado.httputil.HTTPServerRequest
        The incoming request to inspect.

    Returns
    -------
    bool
        ``True`` if the ``Connection`` header contains ``upgrade`` and
        the ``Upgrade`` header is ``websocket``.
    """
    connection = request.headers.get("Connection", "")
    upgrade = request.headers.get("Upgrade", "")
    return "upgrade" in connection.lower() and upgrade.lower() == "websocket"


class LoopbackProxyHandler(WebSocketHandler, JupyterHandler):
    """
    Tornado handler that proxies HTTP and WebSocket to a loopback port.

    One concrete subclass per registered namespace is produced by
    :func:`setup_proxy_handler` so each namespace has its own route
    and logging identity. End users do not instantiate this class.

    The MRO is ``(WebSocketHandler, JupyterHandler, ..., RequestHandler)``.
    ``WebSocketHandler`` does not override ``prepare`` or
    ``check_xsrf_cookie``, so Python resolves those to ``JupyterHandler``
    and the full notebook-server authentication runs before any of the
    methods below execute.
    """

    # Set by the factory in :func:`setup_proxy_handler`.
    namespace: ClassVar[str] = ""

    # ----- WebSocket half --------------------------------------------------

    # ``_upstream`` is only populated between ``open`` and ``on_close``;
    # access goes through ``getattr(..., None)`` so the attribute's
    # absence is a valid state rather than an error.
    _upstream: "WebSocketClientConnection"

    async def get(self, *args: str) -> None:
        """
        Route GETs to either the WS upgrade path or the HTTP proxy.
        """
        if _is_websocket_upgrade(self.request):
            # ``WebSocketHandler.get`` triggers ``open`` on successful upgrade.
            await WebSocketHandler.get(self, *args)
            return
        port, path = args[0], args[1] if len(args) > 1 else ""
        await self._proxy_http(port, path or "")

    async def open(self, *args: str, **_kwargs: str) -> None:
        """
        Open the upstream WebSocket and bridge it to the browser.
        """
        port, path = args[0], args[1] if len(args) > 1 else ""
        upstream_url = self._upstream_url(port, path, scheme="ws")
        headers = self._forward_headers(drop_ws_handshake=True)
        try:
            self._upstream = await websocket_connect(
                httpclient.HTTPRequest(upstream_url, headers=headers),
                on_message_callback=self._on_upstream_message,
            )
        except (OSError, ConnectionError) as exc:
            # 1011 Internal Error gives the client a clean close frame
            # instead of a mysteriously dropped handshake.
            self.close(code=1011, reason=f"upstream unreachable: {exc}")
            return

    def _on_upstream_message(self, message: str | bytes | None) -> None:
        """
        Forward a frame from upstream to the browser.

        Tornado's ``on_message_callback`` is synchronous, but
        ``write_message`` returns a Future. Schedule it on the running
        loop so exceptions are surfaced via the error-logging callback
        below rather than swallowed.
        """
        if message is None:
            self.close()
            return
        future = asyncio.ensure_future(
            self.write_message(message, binary=isinstance(message, bytes))
        )
        future.add_done_callback(_log_write_errors)

    async def on_message(self, message: str | bytes) -> None:
        """
        Forward a frame from the browser to upstream.
        """
        upstream = getattr(self, "_upstream", None)
        if upstream is None:
            return
        await upstream.write_message(message, binary=isinstance(message, bytes))

    def on_close(self) -> None:
        """
        Release the upstream connection when the browser disconnects.
        """
        upstream = getattr(self, "_upstream", None)
        if upstream is not None:
            upstream.close()
            # Delete rather than set to ``None`` so the typed attribute
            # stays truly absent; ``getattr(..., None)`` still works.
            try:
                del self._upstream
            except AttributeError:  # pragma: no cover
                pass

    def check_origin(self, origin_to_satisfy_tornado: str = "") -> bool:
        """
        Accept any origin the authenticated notebook opens the WS from.

        ``JupyterHandler``'s authentication has already gated the
        request by the time Tornado calls this, so deferring to the
        default same-origin policy would block legitimate widgets.
        Browser same-origin rules prevent cross-origin pages from
        reading Jupyter's cookies, so this does not widen the surface.
        """
        return True

    # ----- HTTP half -------------------------------------------------------

    async def post(self, port: str, path: str) -> None:
        """Forward the request to the upstream loopback server."""
        await self._proxy_http(port, path or "")

    async def put(self, port: str, path: str) -> None:
        """Forward the request to the upstream loopback server."""
        await self._proxy_http(port, path or "")

    async def patch(self, port: str, path: str) -> None:
        """Forward the request to the upstream loopback server."""
        await self._proxy_http(port, path or "")

    async def delete(self, port: str, path: str) -> None:
        """Forward the request to the upstream loopback server."""
        await self._proxy_http(port, path or "")

    async def head(self, port: str, path: str) -> None:
        """Forward the request to the upstream loopback server."""
        await self._proxy_http(port, path or "")

    async def _proxy_http(self, port: str, path: str) -> None:
        """
        Forward a single HTTP request to the upstream loopback server.
        """
        upstream_url = self._upstream_url(port, path, scheme="http")
        client = httpclient.AsyncHTTPClient()
        # ``self.request.method`` is ``str | None`` on Tornado's type
        # stubs but in practice is always set on a live request; coerce
        # to ``GET`` as a safe default.
        method: str = self.request.method or "GET"
        body = self.request.body if method in _METHODS_WITH_BODY else None
        req = httpclient.HTTPRequest(
            upstream_url,
            method=method,
            headers=self._forward_headers(),
            body=body,
            allow_nonstandard_methods=True,
            decompress_response=False,
            follow_redirects=False,
            request_timeout=60.0,
        )
        try:
            response = await client.fetch(req, raise_error=False)
        except (OSError, ConnectionError) as exc:
            raise web.HTTPError(
                502,
                f"{self.namespace} loopback proxy could not reach 127.0.0.1:{int(port)}: {exc}",
            ) from exc

        self.set_status(response.code, response.reason)
        for name, value in response.headers.get_all():
            if name.lower() in _HOP_BY_HOP:
                continue
            self.set_header(name, value)
        # Permit embedding the proxied response in cross-origin iframes
        # (e.g. folium HTML). Upstream is already behind Jupyter's
        # authentication, so this does not widen the surface; CORS
        # ``*`` is also the only value browsers accept without
        # credentials, which is what we want for a notebook-scoped
        # proxy.
        self.set_header("Access-Control-Allow-Origin", "*")

        # Writing and finishing can raise ``StreamClosedError`` when
        # the browser aborts in flight (e.g. Leaflet cancelling tile
        # requests as the user pans the map). The upstream fetch
        # already succeeded; there's no useful recovery beyond
        # dropping the response on the floor, so swallow + debug-log.
        try:
            if response.body:
                self.write(response.body)
            await self.finish()
        except StreamClosedError:
            logger.debug(
                "jupyter_loopback: client disconnected before response completed (%s %s)",
                self.request.method,
                self.request.uri,
            )

    # ----- helpers ---------------------------------------------------------

    def _upstream_url(self, port: str, path: str, *, scheme: str) -> str:
        """
        Build the upstream URL for a ``<port>/<path>`` route match.

        ``port`` is matched as ``\\d+`` by the route regex, so the
        ``int()`` cast is infallible.
        """
        upstream_path = path or ""
        if not upstream_path.startswith("/"):
            upstream_path = "/" + upstream_path
        url = f"{scheme}://127.0.0.1:{int(port)}{upstream_path}"
        if self.request.query:
            url = f"{url}?{self.request.query}"
        return url

    def _forward_headers(self, *, drop_ws_handshake: bool = False) -> dict[str, str]:
        """
        Copy request headers minus hop-by-hop and notebook-auth fields.
        """
        out: dict[str, str] = {}
        for name, value in self.request.headers.items():
            lname = name.lower()
            if lname in _HOP_BY_HOP or lname in _STRIP_ON_FORWARD:
                continue
            if drop_ws_handshake and lname in _WS_HANDSHAKE_HEADERS:
                continue
            out[name] = value
        return out


def _log_write_errors(future: asyncio.Future[Any]) -> None:
    """
    Log any exception raised by a scheduled ``write_message`` call.
    """
    exc = future.exception()
    if exc is not None:
        logger.debug("jupyter_loopback WS write failed: %s", exc)


def setup_proxy_handler(
    web_app: "Application",
    namespace: str,
    *,
    handler_cls: type[LoopbackProxyHandler] = LoopbackProxyHandler,
) -> type[LoopbackProxyHandler]:
    """
    Mount a loopback proxy handler for ``namespace`` on ``web_app``.

    Produces a namespace-specific subclass of ``handler_cls`` and
    registers it at ``<base_url>/<namespace>-proxy/<port>/...``.

    Parameters
    ----------
    web_app : tornado.web.Application
        The Tornado application attached to the running jupyter-server
        (i.e. ``server_app.web_app`` from the extension hook).
    namespace : str
        URL-safe library identifier matching ``[a-z0-9][a-z0-9-]*``.
        Pass the same string to :func:`jupyter_loopback.autodetect_prefix`
        on the kernel side.
    handler_cls : type, optional
        Override the base handler class. Defaults to
        :class:`LoopbackProxyHandler`. Pass a subclass to add per-library
        logic (e.g. rejecting a subset of paths).

    Returns
    -------
    type
        The generated namespace-specific handler class.

    Raises
    ------
    ValueError
        If ``namespace`` is not URL-safe.
    RuntimeError
        If ``namespace`` has already been registered on ``web_app``.
    """
    if not re.fullmatch(r"[a-z0-9][a-z0-9-]*", namespace):
        msg = (
            f"jupyter_loopback: invalid namespace {namespace!r}. "
            "Namespaces must start with a lowercase letter or digit and "
            "contain only lowercase letters, digits, and hyphens."
        )
        raise ValueError(msg)

    registered = _REGISTERED.setdefault(id(web_app), set())
    if namespace in registered:
        msg = (
            f"jupyter_loopback: namespace {namespace!r} is already "
            "registered on this web application. Call setup_proxy_handler "
            "at most once per (web_app, namespace) pair."
        )
        raise RuntimeError(msg)
    registered.add(namespace)

    pattern = rf"{re.escape(namespace)}-proxy/(\d+)(?:/(.*))?"
    route = url_path_join(web_app.settings["base_url"], pattern)
    re.compile(route)  # surface typos eagerly

    cls = type(
        f"{namespace.title().replace('-', '')}LoopbackProxyHandler",
        (handler_cls,),
        {"namespace": namespace},
    )
    web_app.add_handlers(".*$", [(route, cls)])
    return cls
