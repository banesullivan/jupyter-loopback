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
