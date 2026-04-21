"""
Shared fixtures and state resets for :mod:`jupyter_loopback` tests.
"""

import pytest

from jupyter_loopback._server import _REGISTERED


@pytest.fixture(autouse=True)
def _reset_comm_state() -> None:
    """
    Reset all :mod:`jupyter_loopback._comm` module-level state between tests.

    Registered handlers, the bridge singleton, and the enabled flag
    would otherwise leak test-to-test and cause order-dependent
    failures.
    """
    try:
        from jupyter_loopback import _comm
    except ImportError:  # pragma: no cover — anywidget missing
        return
    _comm._HANDLERS.clear()
    _comm._BRIDGE = None
    _comm._ENABLED = False


@pytest.fixture(autouse=True)
def _reset_server_state() -> None:
    """
    Forget which namespaces have been registered on web_apps.

    :func:`setup_proxy_handler` raises on duplicate registration, so
    tests that build fresh ``Application`` instances need the
    per-``id(web_app)`` registry cleared.
    """
    _REGISTERED.clear()
