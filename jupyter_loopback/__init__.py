"""
Make kernel-local HTTP/WS servers reachable from the notebook browser.

Two independent paths:

- HTTP/WS proxy (primary). A jupyter-server extension your library
  mounts at ``<base_url>/<namespace>-proxy/<port>/...``. Autodetected
  on the kernel side via :func:`autodetect_prefix`.
- Comm bridge (fallback). An anywidget that routes request/response
  pairs over kernel comms for frontends that don't run jupyter-server
  (VS Code Remote, Colab, Shiny, Solara, marimo).

See the README for integration examples.
"""

from jupyter_loopback._autodetect import autodetect_prefix, is_in_jupyter_kernel
from jupyter_loopback._comm import (
    CommBridge,
    RequestHandler,
    enable_comm_bridge,
    is_comm_bridge_enabled,
    off_request,
    on_request,
)
from jupyter_loopback._server import LoopbackProxyHandler, setup_proxy_handler

__all__ = [
    "CommBridge",
    "LoopbackProxyHandler",
    "RequestHandler",
    "autodetect_prefix",
    "enable_comm_bridge",
    "is_comm_bridge_enabled",
    "is_in_jupyter_kernel",
    "off_request",
    "on_request",
    "setup_proxy_handler",
]

try:
    from jupyter_loopback._version import __version__
except ImportError:  # pragma: no cover - unbuilt source tree
    __version__ = "0.0.0+unknown"
