"""
Anywidget comm bridge for environments without a Jupyter server.

VS Code Remote, Google Colab, Shiny for Python, Solara, and marimo
don't run a jupyter-server, so they can't host the HTTP/WS proxy
mounted by :mod:`jupyter_loopback._server`. They do expose kernel
comms: the websocket pipe JupyterLab itself uses for widget state
sync. This module uses ``anywidget`` to stand up a bidirectional
request/response bridge on top of that pipe.

Protocol
--------
Requests are JSON objects plus optional binary buffers. Each carries:

- ``id``: opaque request id assigned by the frontend.
- ``namespace``: library identifier matching the string passed to
  :func:`on_request` on the Python side.
- ``kind``: action name within that namespace.
- ``data``: JSON-serializable payload.

Responses echo the ``id`` and carry either
``{"status": "ok", "data": ...}`` with buffers or
``{"status": "error", "error": "..."}``.
"""

from collections.abc import Callable
import concurrent.futures
import json
import logging
import pathlib
import threading
from typing import Any

# ``anywidget`` / ``IPython.display`` are optional runtime deps. Each
# try/except below leaves the name bound to either the imported module
# (happy path) or ``None`` (missing install). The ``type: ignore`` on
# the None branch is the standard mypy-strict escape for this
# optional-dependency pattern: mypy otherwise flags the ``None`` as
# incompatible with the ``ModuleType`` inferred from the successful
# import.
try:
    import anywidget
except ImportError:  # pragma: no cover - optional dependency
    anywidget = None  # type: ignore[assignment]

try:
    from IPython.display import display as _ipython_display
except ImportError:  # pragma: no cover - optional dependency
    _ipython_display = None  # type: ignore[assignment]

__all__ = [
    "CommBridge",
    "RequestHandler",
    "enable_comm_bridge",
    "intercept_localhost",
    "is_comm_bridge_enabled",
    "off_request",
    "on_request",
]

logger = logging.getLogger(__name__)

_THIS_DIR = pathlib.Path(__file__).parent.absolute()
_WIDGET_ESM = _THIS_DIR / "static" / "widget.js"

# Type alias for user handlers. Returns ``(response_data, response_buffers)``.
RequestHandler = Callable[[dict[str, Any], list[bytes]], tuple[dict[str, Any], list[bytes]]]

# Module-level registry of ``(namespace, kind) -> handler`` so libraries
# can register independently and still share a single comm channel.
_HANDLER_LOCK = threading.Lock()
_HANDLERS: dict[tuple[str, str], RequestHandler] = {}

# Thread pool that services incoming requests so one slow handler
# doesn't block the kernel's message loop.
_POOL_LOCK = threading.Lock()
_POOL: concurrent.futures.ThreadPoolExecutor | None = None

# Singleton state for :func:`enable_comm_bridge`. Guarded by a lock so
# concurrent callers on free-threaded Python 3.13t can't create two
# bridges.
_BRIDGE_LOCK = threading.Lock()
_BRIDGE: "CommBridge | None" = None
_ENABLED = False


def _get_pool() -> concurrent.futures.ThreadPoolExecutor:
    """
    Return the shared request-serving thread pool, creating it on first call.
    """
    global _POOL
    with _POOL_LOCK:
        if _POOL is None:
            _POOL = concurrent.futures.ThreadPoolExecutor(
                max_workers=4, thread_name_prefix="jupyter-loopback"
            )
        return _POOL


def on_request(namespace: str, kind: str) -> Callable[[RequestHandler], RequestHandler]:
    """
    Register a handler for a ``(namespace, kind)`` request pair.

    Usable as a decorator or called directly::

        @on_request("mylib", "get_tile")
        def _(data, buffers):
            return {"ok": True}, [png_bytes]

    Registering a ``(namespace, kind)`` pair that already has a handler
    replaces the old one. Library authors that need clean teardown
    should call :func:`off_request` with the same key.

    Parameters
    ----------
    namespace : str
        Library identifier, matching the frontend's ``namespace`` field
        and the string passed to :func:`jupyter_loopback.setup_proxy_handler`.
    kind : str
        Action name within the namespace.

    Returns
    -------
    callable
        A decorator that returns the handler unchanged.
    """

    if namespace == "__loopback__":
        msg = (
            "jupyter_loopback: the '__loopback__' namespace is reserved for "
            "built-in fetch / ws_* kinds. Use a library-specific namespace."
        )
        raise ValueError(msg)

    def wrap(fn: RequestHandler) -> RequestHandler:
        with _HANDLER_LOCK:
            _HANDLERS[(namespace, kind)] = fn
        return fn

    return wrap


def off_request(namespace: str, kind: str) -> bool:
    """
    Remove a previously-registered handler.

    Parameters
    ----------
    namespace : str
        Library identifier passed to :func:`on_request`.
    kind : str
        Action name passed to :func:`on_request`.

    Returns
    -------
    bool
        ``True`` if a handler was removed, ``False`` if there was none
        registered for the given pair.
    """
    with _HANDLER_LOCK:
        return _HANDLERS.pop((namespace, kind), None) is not None


def _dispatch(msg: dict[str, Any], buffers: list[bytes]) -> tuple[dict[str, Any], list[bytes]]:
    """
    Look up the handler matching ``msg`` and return its reply tuple.
    """
    namespace = msg.get("namespace", "")
    kind = msg.get("kind", "")
    with _HANDLER_LOCK:
        handler = _HANDLERS.get((namespace, kind))
    if handler is None:
        return {"status": "error", "error": f"no handler for {namespace!r}/{kind!r}"}, [b""]
    # Broad catch: any handler error (rendering bugs, KeyError, IO) is
    # forwarded to the frontend as a structured ``status: "error"``
    # response rather than raised into the thread pool where it would
    # be silently swallowed.
    try:
        data, out_buffers = handler(msg.get("data") or {}, list(buffers))
    except Exception as exc:
        logger.debug("jupyter_loopback handler %s/%s failed: %s", namespace, kind, exc)
        return {"status": "error", "error": str(exc)}, [b""]
    return {"status": "ok", "data": data}, list(out_buffers or [])


if anywidget is None:

    class CommBridge:
        """
        Placeholder that raises ``ImportError`` when ``anywidget`` is missing.
        """

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            msg = (
                "jupyter_loopback.CommBridge requires `anywidget`. "
                "Install with: pip install jupyter-loopback[comm]"
            )
            raise ImportError(msg)

else:
    import traitlets

    class CommBridge(anywidget.AnyWidget):  # type: ignore[no-redef]
        """
        Singleton comm bridge exposed to the browser as ``window.__jupyter_loopback__``.

        Obtained via :func:`enable_comm_bridge`, not constructed directly.
        """

        _esm = _WIDGET_ESM

        # Ports the browser should reroute through this bridge. Synced
        # so that every rendered view of the widget can call
        # ``interceptLocalhost`` on its local ``window`` -- necessary in
        # frontends where notebook outputs sit in separate iframes or
        # where HTML ``<script>`` tags are sanitized (VS Code Jupyter).
        intercepted_ports = traitlets.List(traitlets.Int()).tag(sync=True)

        # Root-relative URL path prefixes the browser should reroute
        # through this bridge when the HTTP proxy at that prefix is
        # unreachable. Keyed by ``str(port)`` because traitlets ``Dict``
        # coerces JSON object keys to strings at serialization. Each
        # value is the absolute path prefix with ``{port}`` already
        # substituted (e.g. ``"/user/alice/mylib-proxy/41029"``). The
        # JS half probes each prefix once and only intercepts when the
        # probe confirms the HTTP path is absent.
        intercepted_prefixes = traitlets.Dict(
            value_trait=traitlets.Unicode(),
            key_trait=traitlets.Unicode(),
        ).tag(sync=True)

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.on_msg(self._on_msg)

        def add_intercepted_port(self, port: int) -> None:
            """
            Register a port so every rendered view intercepts its URLs.
            """
            port_int = int(port)
            if port_int in self.intercepted_ports:
                return
            # Traitlets only fires ``change`` on identity change for
            # List; mutate via new-list assignment.
            self.intercepted_ports = [*self.intercepted_ports, port_int]

        def add_intercepted_prefix(self, port: int, prefix: str) -> None:
            """
            Register a root-relative URL prefix as a fallback target for a port.

            The browser-side interceptor probes the prefix once to see
            whether the HTTP proxy handler is actually mounted on the
            jupyter-server serving the current page. If the probe
            returns 404, subsequent requests to that prefix are routed
            through the comm bridge instead. Idempotent.

            Parameters
            ----------
            port : int
                The loopback port the prefix forwards to.
            prefix : str
                The absolute URL path (e.g.
                ``"/user/alice/mylib-proxy/41029"``) with ``{port}``
                already substituted. Trailing slashes are stripped
                for consistent matching.
            """
            key = str(int(port))
            normalized = prefix.rstrip("/")
            if not normalized:
                return
            if self.intercepted_prefixes.get(key) == normalized:
                return
            # Dict trait sync fires on identity change, same pattern
            # as ``intercepted_ports`` above.
            self.intercepted_prefixes = {**self.intercepted_prefixes, key: normalized}

        def _on_msg(
            self,
            _widget: Any,
            msg: dict[str, Any],
            buffers: list[bytes],
        ) -> None:
            """Thread-dispatch an incoming frontend request."""
            if msg.get("type") != "request":
                return
            req_id = msg.get("id")

            def task() -> None:
                payload, out_buffers = _dispatch(msg, list(buffers))
                payload["id"] = req_id
                payload["type"] = "response"
                self.send(payload, out_buffers)

            _get_pool().submit(task)


def is_comm_bridge_enabled() -> bool:
    """
    Return ``True`` once :func:`enable_comm_bridge` has run successfully.
    """
    return _ENABLED


def enable_comm_bridge(*, display: bool = True) -> "CommBridge":
    """
    Install the comm bridge for the current kernel.

    Idempotent; subsequent calls return the same singleton. Normally
    called once near the top of a notebook::

        import jupyter_loopback
        jupyter_loopback.enable_comm_bridge()

    Parameters
    ----------
    display : bool, keyword-only, optional
        If ``True`` (default), ``IPython.display.display`` the bridge
        widget so the JS side boots in the browser. Pass ``False`` for
        silent activation (tests, CLI scripts).

    Returns
    -------
    CommBridge
        The singleton bridge instance.

    Raises
    ------
    ImportError
        If ``anywidget`` is not installed.
    """
    global _BRIDGE, _ENABLED
    if anywidget is None:
        msg = (
            "jupyter_loopback.enable_comm_bridge() requires `anywidget`. "
            "Install with: pip install jupyter-loopback[comm]"
        )
        raise ImportError(msg)
    # Import lazily so ``anywidget``-less installs can still import
    # ``jupyter_loopback._comm`` to raise the nice ImportError above.
    from jupyter_loopback import _bridge_proxy

    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = CommBridge()
        _bridge_proxy.install()
        _ENABLED = True
        bridge = _BRIDGE
    if display and _ipython_display is not None:
        # IPython.display.display raises when no frontend is attached
        # (e.g. a plain Python script calling enable for a test). The
        # bridge is still valid and whoever later displays it will boot
        # the JS side, so swallow the error. ``IPython`` has no stubs,
        # so the call looks ``Untyped`` to mypy under strict mode.
        try:
            _ipython_display(bridge)  # type: ignore[no-untyped-call]
        except RuntimeError:
            pass
    return bridge


def intercept_localhost(
    port: int,
    *,
    path_prefix: str | None = None,
    display: bool = True,
) -> Any:
    """
    Route ``http://127.0.0.1:<port>/*`` URLs through the comm bridge.

    Many libraries (notably ``ipyleaflet``) build loopback URLs and hand
    them to the browser as ``<img src>`` / ``fetch()`` / ``XMLHttpRequest``
    calls that jupyter-loopback never sees directly. In jupyter-server
    environments those URLs hit the HTTP proxy handler; in VS Code
    Jupyter (and other webview-based frontends) they fail outright
    because the webview origin isn't the jupyter-server origin.

    This function emits a small JS shim that patches the three entry
    points above so URLs matching the given loopback ``port`` are
    rerouted through the already-enabled comm bridge. The rewrite is
    idempotent per port and additive across ports, so a notebook that
    spins up multiple ``TileClient`` instances can call this once per
    client.

    Requires :func:`enable_comm_bridge` to have been called earlier in
    the same kernel; without it, the browser half of the bridge is not
    installed and the shim is a no-op.

    Parameters
    ----------
    port : int
        The loopback port whose URLs should be intercepted.
    path_prefix : str or None, keyword-only, optional
        Absolute URL path prefix (with ``{port}`` already substituted,
        e.g. ``"/user/alice/mylib-proxy/41029"``) that also forwards to
        this loopback port via the :func:`setup_proxy_handler` HTTP
        route. When supplied, the browser-side interceptor probes the
        prefix once; if the probe comes back ``404`` -- the single-user
        server doesn't have the extension installed, which is the usual
        case on JupyterHub deployments where the kernel env and server
        env diverge -- subsequent requests to that prefix are routed
        through the comm bridge instead. If the probe succeeds the
        prefix is left alone so the faster HTTP path keeps serving
        tiles.
    display : bool, keyword-only, optional
        If ``True`` (default), ``IPython.display.display`` the HTML
        snippet so the shim installs immediately in the current
        notebook. Pass ``False`` to get the ``HTML`` value back for
        manual placement.

    Returns
    -------
    IPython.display.HTML
        The HTML object that was displayed (or would be, when
        ``display=False``). Useful when composing richer cell outputs.

    Raises
    ------
    ImportError
        If ``IPython`` is not available.

    Examples
    --------
    Typical use next to a localtileserver ``TileClient``::

        import jupyter_loopback
        jupyter_loopback.enable_comm_bridge()

        from localtileserver import TileClient
        client = TileClient("path/to/raster.tif")
        jupyter_loopback.intercept_localhost(client.server_port)
    """
    try:
        from IPython.display import HTML
    except ImportError as exc:  # pragma: no cover - optional dep
        msg = (
            "jupyter_loopback.intercept_localhost requires IPython. "
            "Install IPython or call the JS directly: "
            "window.__jupyter_loopback__.interceptLocalhost(<port>)"
        )
        raise ImportError(msg) from exc

    port_int = int(port)
    prefix_clean = (path_prefix or "").rstrip("/") or None

    # Primary install path: update the bridge's synced ``intercepted_ports``
    # (and ``intercepted_prefixes``) traits so every rendered widget
    # view (regardless of iframe) receives the change and calls
    # ``interceptLocalhost`` in its own ``HTMLImageElement.prototype``
    # context. This survives VS Code's <script>-tag sanitization in
    # HTML outputs.
    trait_handled = False
    if _BRIDGE is not None:
        try:
            _BRIDGE.add_intercepted_port(port_int)  # type: ignore[attr-defined]
            if prefix_clean:
                _BRIDGE.add_intercepted_prefix(port_int, prefix_clean)  # type: ignore[attr-defined]
            trait_handled = True
        except Exception:
            # Best-effort: if the trait update fails (e.g. stale bridge
            # after a serialization weirdness), fall through to the HTML
            # script. Debug-log rather than swallow silently.
            logger.debug(
                "jupyter_loopback: add_intercepted_port failed, falling back to <script>",
                exc_info=True,
            )

    # Fallback install path: an inline <script> that polls briefly for
    # the global. Still returned always so callers can embed it manually
    # in custom HTML outputs; only auto-displayed when the trait path
    # isn't available, otherwise we'd emit a redundant <script> output
    # per call and clutter notebooks that construct many tile layers.
    #
    # ``q`` is a JSON-encoded string (or ``null``); embedding via
    # ``json.dumps`` keeps quoting correct for arbitrary prefixes.
    prefix_js = json.dumps(prefix_clean) if prefix_clean else "null"
    script = (
        "<script>(function(){"
        f"var p={port_int};"
        f"var q={prefix_js};"
        "function g(){return window.__jupyter_loopback__;}"
        "function go(){"
        "var a=g();"
        "if(a&&a.interceptLocalhost){a.interceptLocalhost(p,q);return true}"
        "return false}"
        "if(!go()){"
        "var n=0;"
        "var t=setInterval(function(){if(go()||++n>50)clearInterval(t)},100)}"
        "})();</script>"
    )
    html = HTML(script)  # type: ignore[no-untyped-call]
    if display and not trait_handled and _ipython_display is not None:
        try:
            _ipython_display(html)  # type: ignore[no-untyped-call]
        except RuntimeError:
            pass
    return html
