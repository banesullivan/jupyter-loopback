"""
Jupyter-server extension that mounts the jupyter-loopback proxy for
the ``loopback-demo`` namespace.
"""

from jupyter_loopback import setup_proxy_handler


def _jupyter_server_extension_points():
    return [{"module": "loopback_demo._jupyter"}]


def _load_jupyter_server_extension(server_app):
    setup_proxy_handler(server_app.web_app, namespace="loopback-demo")
    server_app.log.info(
        "loopback-demo: proxy mounted at %sloopback-demo-proxy/<port>/...",
        server_app.web_app.settings.get("base_url", "/"),
    )
