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
import logging
import pathlib
import threading
from typing import Any

# ``anywidget`` / ``IPython`` are optional runtime deps. Declaring them
# as ``Any | None`` at module scope lets the rest of the file reassign
# ``None`` in the ImportError branch without mypy complaining that
# we're shadowing a Module type.
anywidget: Any | None = None
_ipython_display: Any | None = None

try:
    import anywidget
except ImportError:  # pragma: no cover - optional dependency
    pass

try:
    from IPython.display import display as _ipython_display
except ImportError:  # pragma: no cover - optional dependency
    pass

__all__ = [
    "CommBridge",
    "RequestHandler",
    "enable_comm_bridge",
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

    class CommBridge(anywidget.AnyWidget):  # type: ignore[no-redef,misc,name-defined]
        """
        Singleton comm bridge exposed to the browser as ``window.__jupyter_loopback__``.

        Obtained via :func:`enable_comm_bridge`, not constructed directly.
        """

        _esm = _WIDGET_ESM

        def __init__(self, **kwargs: Any) -> None:
            super().__init__(**kwargs)
            self.on_msg(self._on_msg)

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
    with _BRIDGE_LOCK:
        if _BRIDGE is None:
            _BRIDGE = CommBridge()
        _ENABLED = True
        bridge = _BRIDGE
    if display and _ipython_display is not None:
        # IPython.display.display raises when no frontend is attached
        # (e.g. a plain Python script calling enable for a test). The
        # bridge is still valid and whoever later displays it will boot
        # the JS side, so swallow the error.
        try:
            _ipython_display(bridge)
        except RuntimeError:
            pass
    return bridge
