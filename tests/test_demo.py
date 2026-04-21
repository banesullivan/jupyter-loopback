"""
Smoke tests for :mod:`loopback_demo` and the example notebook.

The demo's moving parts split across:

- ``loopback_demo._jupyter`` (the extension stub that mounts the proxy)
- ``demos/jupyter-config/jupyter_server_config.d/loopback-demo.json``
  (auto-enables the extension on install)
- ``demos/example.ipynb`` (the in-kernel server and browser HTML)

These tests guard each piece individually. End-to-end validation
against a real JupyterLab lives in the Docker smoke-check tests.
"""

import json
from pathlib import Path

import pytest

pytest.importorskip("loopback_demo")

from loopback_demo import _jupyter as demo_ext


def test_extension_declares_itself_correctly() -> None:
    """The jupyter-server extension points function returns the right module."""
    points = demo_ext._jupyter_server_extension_points()
    assert points == [{"module": "loopback_demo._jupyter"}]


def test_extension_mounts_proxy_on_load() -> None:
    """
    Calling ``_load_jupyter_server_extension`` registers a handler at
    ``/loopback-demo-proxy/(\\d+)(?:/(.*))?`` on the web_app.
    """
    from types import SimpleNamespace

    from tornado.web import Application

    from jupyter_loopback._server import _REGISTERED

    _REGISTERED.clear()
    app = Application([], base_url="/", cookie_secret="x")
    fake_log = SimpleNamespace(info=lambda *a, **kw: None)
    fake_server_app = SimpleNamespace(web_app=app, log=fake_log)

    demo_ext._load_jupyter_server_extension(fake_server_app)

    patterns: list[str] = []
    for outer in app.default_router.rules:
        target = outer.target
        if hasattr(target, "rules"):
            for inner in target.rules:
                regex = getattr(inner.matcher, "regex", None)
                if regex is not None:
                    patterns.append(regex.pattern)
    # ``re.escape`` escapes the hyphen, so the stored pattern is
    # ``loopback\\-demo-proxy``. Check for the distinctive suffix
    # rather than the unescaped namespace.
    assert any("demo-proxy" in p for p in patterns), patterns


def test_jupyter_config_json_shipped() -> None:
    """The jupyter-server auto-enable JSON is on disk for the demo package."""
    here = Path(__file__).resolve().parents[1]
    cfg = here / "demos" / "jupyter-config" / "jupyter_server_config.d" / "loopback-demo.json"
    assert cfg.exists()
    body = json.loads(cfg.read_text())
    assert body["ServerApp"]["jpserver_extensions"]["loopback_demo._jupyter"] is True


def test_example_notebook_is_valid_json() -> None:
    """The notebook file parses as JSON and declares a Python 3 kernel."""
    here = Path(__file__).resolve().parents[1]
    nb_path = here / "demos" / "example.ipynb"
    assert nb_path.exists()
    nb = json.loads(nb_path.read_text())
    assert nb["nbformat"] == 4
    assert nb["metadata"]["kernelspec"]["language"] == "python"


def test_example_notebook_uses_autodetect_prefix() -> None:
    """
    The demo must use ``autodetect_prefix`` so URLs are root-absolute
    in Jupyter. A notebook that hardcodes a relative path would
    silently 404 behind JupyterLab's notebook view URL.
    """
    here = Path(__file__).resolve().parents[1]
    nb = json.loads((here / "demos" / "example.ipynb").read_text())
    full = "\n".join("".join(cell["source"]) for cell in nb["cells"] if cell["cell_type"] == "code")
    assert "from jupyter_loopback import autodetect_prefix" in full
    # Quote-agnostic: ruff-format may prefer single or double quotes.
    assert (
        'autodetect_prefix("loopback-demo")' in full or "autodetect_prefix('loopback-demo')" in full
    )


def test_example_notebook_resolves_ws_against_origin_not_view_url() -> None:
    """
    The WebSocket bootstrap must resolve ``ws_url`` against
    ``window.location.origin`` (not ``document.baseURI``). Resolving
    against ``baseURI`` gives ``http://host/lab/tree/ws_url`` which
    404s. This test reads the notebook cell verbatim to guard the fix.
    """
    here = Path(__file__).resolve().parents[1]
    nb = json.loads((here / "demos" / "example.ipynb").read_text())
    html_cell = next(
        cell
        for cell in nb["cells"]
        if cell["cell_type"] == "code" and "new WebSocket(" in "".join(cell["source"])
    )
    source = "".join(html_cell["source"])
    assert "window.location.origin" in source
    assert "document.baseURI" not in source
