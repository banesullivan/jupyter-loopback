"""
Unit tests for :mod:`jupyter_loopback._autodetect`.
"""

from jupyter_loopback import autodetect_prefix, is_in_jupyter_kernel

_CLEAR = (
    "JPY_SESSION_NAME",
    "JPY_PARENT_PID",
    "JUPYTERHUB_SERVICE_PREFIX",
    "JPY_BASE_URL",
)


def _clear(monkeypatch):
    for var in _CLEAR:
        monkeypatch.delenv(var, raising=False)


def test_is_in_jupyter_kernel_false_by_default(monkeypatch):
    _clear(monkeypatch)
    assert is_in_jupyter_kernel() is False


def test_is_in_jupyter_kernel_true_with_session_name(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JPY_SESSION_NAME", "nb.ipynb")
    assert is_in_jupyter_kernel() is True


def test_is_in_jupyter_kernel_true_with_parent_pid(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JPY_PARENT_PID", "1234")
    assert is_in_jupyter_kernel() is True


def test_autodetect_returns_none_outside_jupyter(monkeypatch):
    _clear(monkeypatch)
    assert autodetect_prefix("mylib") is None


def test_autodetect_returns_namespaced_prefix(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JPY_SESSION_NAME", "nb.ipynb")
    assert autodetect_prefix("mylib") == "/mylib-proxy/{port}"


def test_autodetect_returns_root_absolute_path(monkeypatch):
    """
    Regression guard: without a hub prefix we still return a path with
    a leading ``/``. Relative paths resolve against the notebook's view
    URL (e.g. ``/lab/tree/nb.ipynb``), which produces wrong proxy URLs
    in every embed (``<img>``, ``<a>``, ``<iframe>``, WebSocket, fetch).
    """
    _clear(monkeypatch)
    monkeypatch.setenv("JPY_SESSION_NAME", "nb.ipynb")
    assert autodetect_prefix("mylib").startswith("/")


def test_autodetect_prepends_jupyterhub_prefix(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JPY_SESSION_NAME", "nb.ipynb")
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/alice/")
    assert autodetect_prefix("mylib") == "/user/alice/mylib-proxy/{port}"


def test_autodetect_prepends_jpy_base_url_when_no_hub(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JPY_SESSION_NAME", "nb.ipynb")
    monkeypatch.setenv("JPY_BASE_URL", "/some/base/")
    assert autodetect_prefix("mylib") == "/some/base/mylib-proxy/{port}"


def test_autodetect_hub_prefix_wins_over_base_url(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JPY_SESSION_NAME", "nb.ipynb")
    monkeypatch.setenv("JUPYTERHUB_SERVICE_PREFIX", "/user/alice/")
    monkeypatch.setenv("JPY_BASE_URL", "/hub-overrides-this/")
    assert autodetect_prefix("mylib") == "/user/alice/mylib-proxy/{port}"


def test_autodetect_custom_template(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JPY_SESSION_NAME", "nb.ipynb")
    assert autodetect_prefix("mylib", template="api/{namespace}/{{port}}") == "/api/mylib/{port}"


def test_autodetect_namespace_isolation(monkeypatch):
    _clear(monkeypatch)
    monkeypatch.setenv("JPY_SESSION_NAME", "nb.ipynb")
    assert autodetect_prefix("a") == "/a-proxy/{port}"
    assert autodetect_prefix("b") == "/b-proxy/{port}"
