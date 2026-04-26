"""
Jupyter-server extension that mounts the jupyter-loopback proxy for the
``pyvista`` namespace, so the trame server PyVista launches in the
kernel becomes reachable from the notebook browser.
"""

from jupyter_loopback import setup_proxy_handler


def _jupyter_server_extension_points():
    return [{"module": "pyvista_loopback_demo._jupyter"}]


def _load_jupyter_server_extension(server_app):
    setup_proxy_handler(server_app.web_app, namespace="pyvista")
    server_app.log.info(
        "pyvista-loopback-demo: proxy mounted at %spyvista-proxy/<port>/...",
        server_app.web_app.settings.get("base_url", "/"),
    )
