"""
Tests for :mod:`jupyter_loopback._comm`.
"""

from pathlib import Path
import threading
from unittest.mock import patch

import pytest

pytest.importorskip("anywidget")

from jupyter_loopback import (
    CommBridge,
    _comm as comm_module,
    enable_comm_bridge,
    intercept_localhost,
    is_comm_bridge_enabled,
    off_request,
    on_request,
)


def test_bridge_instantiates() -> None:
    bridge = CommBridge()
    assert isinstance(bridge, CommBridge)


def test_enable_comm_bridge_idempotent() -> None:
    w1 = enable_comm_bridge(display=False)
    w2 = enable_comm_bridge(display=False)
    assert w1 is w2
    assert is_comm_bridge_enabled()


def test_enable_comm_bridge_display_is_keyword_only() -> None:
    """Boolean parameters are keyword-only per API policy."""
    with pytest.raises(TypeError):
        enable_comm_bridge(False)  # type: ignore[misc]


def test_enable_comm_bridge_without_anywidget_raises() -> None:
    """
    If ``anywidget`` is unavailable, ``enable_comm_bridge`` raises a
    clear ``ImportError`` telling the user how to fix it.
    """
    with patch.object(comm_module, "anywidget", None):
        with pytest.raises(ImportError, match="anywidget"):
            enable_comm_bridge(display=False)


def test_on_request_registers_handler() -> None:
    @on_request("test-reg", "ping")
    def _(_data: dict, _buffers: list[bytes]) -> tuple[dict, list[bytes]]:
        return {"pong": True}, []

    assert ("test-reg", "ping") in comm_module._HANDLERS


def test_off_request_removes_handler() -> None:
    @on_request("test-off", "ping")
    def _(_data: dict, _buffers: list[bytes]) -> tuple[dict, list[bytes]]:
        return {}, []

    assert off_request("test-off", "ping") is True
    assert ("test-off", "ping") not in comm_module._HANDLERS


def test_off_request_returns_false_for_unknown() -> None:
    assert off_request("never-registered", "nope") is False


def test_dispatch_routes_to_registered_handler() -> None:
    @on_request("test-dispatch", "echo")
    def _(data: dict, _buffers: list[bytes]) -> tuple[dict, list[bytes]]:
        return {"you_said": data.get("msg")}, []

    reply, buffers = comm_module._dispatch(
        {"namespace": "test-dispatch", "kind": "echo", "data": {"msg": "hi"}},
        [],
    )
    assert reply == {"status": "ok", "data": {"you_said": "hi"}}
    assert buffers == []


def test_dispatch_unknown_namespace_returns_error() -> None:
    reply, buffers = comm_module._dispatch(
        {"namespace": "nope", "kind": "whatever", "data": {}},
        [],
    )
    assert reply["status"] == "error"
    assert "no handler" in reply["error"]
    assert buffers == [b""]


def test_dispatch_forwards_handler_exceptions_as_errors() -> None:
    @on_request("test-err", "boom")
    def _(_data: dict, _buffers: list[bytes]) -> tuple[dict, list[bytes]]:
        msg = "upstream failure"
        raise RuntimeError(msg)

    reply, buffers = comm_module._dispatch(
        {"namespace": "test-err", "kind": "boom", "data": {}},
        [],
    )
    assert reply["status"] == "error"
    assert "upstream failure" in reply["error"]
    assert buffers == [b""]


def test_dispatch_passes_binary_buffers() -> None:
    @on_request("test-bin", "reverse")
    def _(_data: dict, buffers: list[bytes]) -> tuple[dict, list[bytes]]:
        return {}, [buffers[0][::-1]]

    reply, buffers = comm_module._dispatch(
        {"namespace": "test-bin", "kind": "reverse", "data": {}},
        [b"abcdef"],
    )
    assert reply["status"] == "ok"
    assert buffers == [b"fedcba"]


def test_end_to_end_request_via_widget_send() -> None:
    """
    Simulate a frontend ``request`` roundtrip through the bridge.

    The frontend posts a ``type: "request"`` message over ``on_msg``;
    the bridge dispatches to the registered handler and echoes a
    ``type: "response"`` back through ``send``. ``send`` is captured
    rather than involving a real comm channel.
    """

    @on_request("test-e2e", "get")
    def _(data: dict, _buffers: list[bytes]) -> tuple[dict, list[bytes]]:
        return {"n": data.get("n", 0) * 2}, [b"\x01\x02\x03"]

    bridge = CommBridge()

    captured: list[tuple[dict, list]] = []
    done = threading.Event()

    def fake_send(payload: dict, buffers: list[bytes] | None = None) -> None:
        captured.append((payload, buffers or []))
        done.set()

    bridge.send = fake_send  # type: ignore[method-assign]
    bridge._on_msg(
        bridge,
        {
            "type": "request",
            "id": "req-42",
            "namespace": "test-e2e",
            "kind": "get",
            "data": {"n": 21},
        },
        [],
    )
    assert done.wait(timeout=5.0), "bridge did not respond"
    payload, buffers = captured[0]
    assert payload == {
        "type": "response",
        "id": "req-42",
        "status": "ok",
        "data": {"n": 42},
    }
    assert buffers == [b"\x01\x02\x03"]


def test_non_request_messages_ignored() -> None:
    """Bridge does not respond to messages it doesn't recognize."""
    bridge = CommBridge()
    captured: list = []
    bridge.send = lambda *a, **kw: captured.append((a, kw))  # type: ignore[method-assign]
    bridge._on_msg(bridge, {"type": "something_else"}, [])
    assert captured == []


def test_widget_static_assets_exist() -> None:
    """The JS bundle is present so anywidget can load it."""
    assert comm_module._WIDGET_ESM.exists()
    contents = Path(comm_module._WIDGET_ESM).read_text()
    assert "export default" in contents
    assert "__jupyter_loopback__" in contents
    assert "request" in contents


def test_widget_js_exposes_intercept_localhost() -> None:
    """The DOM interceptor is wired into the global JS API."""
    contents = Path(comm_module._WIDGET_ESM).read_text()
    assert "interceptLocalhost" in contents
    assert "HTMLImageElement" in contents


def test_widget_js_gates_dispatch_on_active_bridge() -> None:
    """
    Every rendered widget view subscribes to the comm's ``msg:custom``,
    so kernel ``self.send`` calls fan out to N views when the widget is
    displayed N times. The frontend must dispatch at most once per send
    (otherwise WS event listeners fire twice, fetch responses resolve
    twice, etc.). The guard lives in the ``onMsg`` inside ``render`` and
    uses ``_isActiveBridge``; guarantee the bundle keeps it.
    """
    contents = Path(comm_module._WIDGET_ESM).read_text()
    assert "_isActiveBridge" in contents
    # Sanity: the guard is actually used in the render message callback,
    # not just exposed on the api surface.
    assert "if (!api._isActiveBridge(bridge))" in contents


def test_intercept_localhost_returns_html_with_port() -> None:
    html = intercept_localhost(35049, display=False)
    # The IPython.display.HTML object exposes its raw body via .data.
    assert "interceptLocalhost" in html.data
    assert "35049" in html.data


def test_intercept_localhost_coerces_port_to_int() -> None:
    """
    The port is stringified into the generated JS via ``int()``, so
    strings and floats are accepted but non-numeric input raises.
    """
    html = intercept_localhost(8000, display=False)
    assert "8000" in html.data
    with pytest.raises((ValueError, TypeError)):
        intercept_localhost("not-a-port", display=False)  # type: ignore[arg-type]


def test_intercept_localhost_updates_bridge_trait_when_enabled() -> None:
    """
    When the bridge singleton is live, ``intercept_localhost`` pushes
    the port onto the synced ``intercepted_ports`` list so every
    rendered view can install the interceptor in its own iframe.
    """
    bridge = enable_comm_bridge(display=False)
    intercept_localhost(4444, display=False)
    assert 4444 in bridge.intercepted_ports


def test_intercept_localhost_skips_redundant_display_when_trait_handled() -> None:
    """
    When the bridge is enabled, ``display=True`` must NOT emit the
    inline ``<script>`` output (the trait path already covers it).
    Avoids cluttering cells that construct many tile layers.
    """
    enable_comm_bridge(display=False)
    captured: list[object] = []
    # Patch IPython.display.display to observe what gets rendered.
    with patch.object(comm_module, "_ipython_display", captured.append):
        intercept_localhost(5555)
    assert captured == []


def test_intercept_localhost_displays_script_when_bridge_absent() -> None:
    """
    Without a live bridge singleton, ``intercept_localhost`` falls back
    to the inline ``<script>`` shim and auto-displays it under
    ``display=True``.
    """
    captured: list[object] = []
    with patch.object(comm_module, "_ipython_display", captured.append):
        intercept_localhost(6666)
    assert len(captured) == 1
    body = getattr(captured[0], "data", "")
    assert "6666" in body


def test_intercept_localhost_path_prefix_is_keyword_only() -> None:
    """Positional ``path_prefix`` is rejected so the shape can't drift."""
    with pytest.raises(TypeError):
        intercept_localhost(4242, "/user/alice/mylib-proxy/4242")  # type: ignore[misc]


def test_intercept_localhost_stores_prefix_on_bridge_trait() -> None:
    """
    ``path_prefix`` flows through ``CommBridge.intercepted_prefixes``
    so every rendered widget view (even in a separate iframe) learns
    which same-origin paths to probe.
    """
    bridge = enable_comm_bridge(display=False)
    intercept_localhost(
        41029,
        path_prefix="/user/alice/mylib-proxy/41029",
        display=False,
    )
    assert 41029 in bridge.intercepted_ports
    assert bridge.intercepted_prefixes["41029"] == "/user/alice/mylib-proxy/41029"


def test_intercept_localhost_strips_trailing_slashes_on_prefix() -> None:
    """
    Normalized storage lets the JS side compare paths without worrying
    about caller-supplied trailing slashes (``/...-proxy/41029`` vs
    ``/...-proxy/41029/``).
    """
    bridge = enable_comm_bridge(display=False)
    intercept_localhost(
        3030,
        path_prefix="/user/alice/mylib-proxy/3030///",
        display=False,
    )
    assert bridge.intercepted_prefixes["3030"] == "/user/alice/mylib-proxy/3030"


def test_intercept_localhost_script_embeds_prefix_for_bridgeless_install() -> None:
    """
    When the bridge isn't live yet, the fallback ``<script>`` still
    carries the prefix (as a JSON literal) so it reaches the global
    ``interceptLocalhost`` API once the widget boots.
    """
    html = intercept_localhost(
        2020,
        path_prefix="/srv/proxy/2020",
        display=False,
    )
    body = html.data
    assert "2020" in body
    # JSON encoding keeps quoting correct for arbitrary prefixes
    # (paths with quotes, unicode, etc.).
    assert '"/srv/proxy/2020"' in body


def test_intercept_localhost_without_prefix_passes_null() -> None:
    """
    Omitting ``path_prefix`` must not leak an empty string into the
    fallback shim; it has to be ``null`` so JS treats it as "no prefix".
    """
    html = intercept_localhost(1010, display=False)
    body = html.data
    assert "1010" in body
    assert "q=null" in body


def test_add_intercepted_prefix_is_idempotent() -> None:
    """Duplicate registration doesn't churn the synced dict."""
    bridge = CommBridge()
    bridge.add_intercepted_prefix(9001, "/srv/proxy/9001")
    first = bridge.intercepted_prefixes
    bridge.add_intercepted_prefix(9001, "/srv/proxy/9001")
    assert bridge.intercepted_prefixes is first


def test_widget_js_wires_prefix_interception_and_probe() -> None:
    """
    Regression guard: the bundled JS must carry both the prefix match
    logic and the probe call. Losing either turns the JupyterHub
    fallback back into a silent 404.
    """
    contents = Path(comm_module._WIDGET_ESM).read_text()
    assert "prefixToPort" in contents
    assert "prefixStatus" in contents
    assert "__probe__" in contents
    assert "probePrefix" in contents


def test_widget_js_defers_routing_while_probing() -> None:
    """
    Regression guard for the first-plot races on both supported
    deployment shapes. The bundle must neither bias probing → HTTP
    (silently 404s on JupyterHub when the single-user server lacks
    the extension) nor bias probing → comm (times out on mybinder
    when the comm bridge hasn't warmed up yet). It must wait for the
    probe to settle, then re-ask before routing.
    """
    contents = Path(comm_module._WIDGET_ESM).read_text()
    # The probe-completion signal must exist so img/fetch interceptors
    # can await it before deciding.
    assert "prefixReady" in contents
    # The "working" short-circuit stays -- that's the fast path for
    # post-probe direct HTTP fetches on Lab / mybinder.
    assert 'status === "working"' in contents
    # But probing must NOT be collapsed into the same short-circuit
    # (that's the Hub-breaking bias) or into an unconditional comm
    # route (that's the mybinder-breaking bias).
    assert 'status === "working" || status === "probing"' not in contents


def test_widget_js_binds_intercepted_prefixes_trait() -> None:
    """
    The widget reads ``intercepted_prefixes`` at render and listens
    for trait changes, so late ``intercept_localhost`` calls still
    land in re-rendered views.
    """
    contents = Path(comm_module._WIDGET_ESM).read_text()
    assert "intercepted_prefixes" in contents
    assert "change:intercepted_prefixes" in contents
