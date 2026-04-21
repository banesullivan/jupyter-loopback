<p align="center">
  <img src="https://github.com/banesullivan/jupyter-loopback/raw/main/assets/logo.svg" alt="jupyter-loopback"  />
</p>

**Make kernel-local HTTP and WebSocket servers reachable from the notebook browser. Zero user config.**

If your library runs a web server inside a Jupyter kernel (a tile server, a trame app, Bokeh, Dash, a custom debug UI, anything bound to `127.0.0.1:<port>`), your users hit the same wall every time. Works locally. Breaks on JupyterHub, MyBinder, VS Code Remote, Colab, Shiny. The usual fix is a README paragraph telling users to install `jupyter-server-proxy`, export `LIBRARY_CLIENT_PREFIX='proxy/{port}'`, and prepend `$JUPYTERHUB_SERVICE_PREFIX` on Hub. Most users skip it, get a broken notebook, and file an issue.

`jupyter-loopback` replaces that paragraph. Libraries register once. End users configure nothing.

## Who this is for

Library authors who spin up an HTTP or WebSocket server inside a Jupyter kernel and need the browser to reach it without asking every user to configure a proxy. If you've ever written "make sure to install jupyter-server-proxy and set `FOO_CLIENT_PREFIX='proxy/{port}'`" in your README, this is for you.

## Install

```bash
pip install jupyter-loopback            # HTTP/WS proxy
pip install jupyter-loopback[comm]      # + anywidget comm bridge fallback
```

## The 30-second demo

```bash
docker build -t jupyter-loopback-demo .
docker run --rm -it -p 8888:8888 jupyter-loopback-demo
```

Open the printed token URL, run `example.ipynb`. You'll see:

- A JSON response fetched through the proxy.
- A red square rendered inline (binary-body correctness check).
- A live WebSocket echo box you can type into.

All of it flowing through `<base_url>/loopback-demo-proxy/<port>/…` with no config.

## For library authors

Suppose your library is `mylib` and it spins up a server at `127.0.0.1:<port>` inside the kernel. Three files wire it up.

### 1. Server-side: register the proxy

```python
# mylib/_jupyter/__init__.py
from jupyter_loopback import setup_proxy_handler

def _jupyter_server_extension_points():
    return [{"module": "mylib._jupyter"}]

def _load_jupyter_server_extension(server_app):
    setup_proxy_handler(server_app.web_app, namespace="mylib")
```

### 2. Auto-enable the extension

Ship `jupyter-config/jupyter_server_config.d/mylib.json`:

```json
{
  "ServerApp": {
    "jpserver_extensions": {
      "mylib._jupyter": true
    }
  }
}
```

And wire it into `pyproject.toml`:

```toml
[tool.setuptools.data-files]
"etc/jupyter/jupyter_server_config.d" = [
  "jupyter-config/jupyter_server_config.d/mylib.json",
]
```

### 3. Kernel-side: build browser-reachable URLs

```python
from jupyter_loopback import autodetect_prefix

def browser_url(port: int, path: str) -> str:
    prefix = autodetect_prefix("mylib")  # None outside Jupyter
    if prefix is None:
        return f"http://127.0.0.1:{port}/{path.lstrip('/')}"
    return f"{prefix.format(port=port)}/{path.lstrip('/')}"
```

That's it. In a local Python REPL, `autodetect_prefix` returns `None` and you hit `127.0.0.1` directly. In JupyterLab, Hub, Binder, or any jupyter-server environment, it returns `mylib-proxy/{port}` (with any per-user Hub prefix already attached) and the browser loads through the proxy.

## For users on VS Code Remote, Colab, Shiny, Solara, marimo

These frontends don't run a jupyter-server, so the HTTP proxy above isn't available. They do have kernel comms (the WebSocket the notebook widgets use). `jupyter-loopback` ships an `anywidget` that tunnels request/response pairs over that comm channel.

Users enable it once at the top of a notebook:

```python
import jupyter_loopback
jupyter_loopback.enable_comm_bridge()
```

Library authors register handlers:

```python
from jupyter_loopback import on_request

@on_request("mylib", "get_tile")
def _(data, buffers):
    z, x, y = data["z"], data["x"], data["y"]
    return {"ok": True}, [make_tile(z, x, y)]   # (json, buffers)
```

Frontend code calls through `window.__jupyter_loopback__`:

```js
const { status, data, buffers } = await window.__jupyter_loopback__.request(
  "mylib",
  "get_tile",
  { z: 8, x: 71, y: 110 },
);
if (status === "ok") {
  const blob = new Blob([buffers[0]], { type: "image/png" });
  imgElement.src = URL.createObjectURL(blob);
}
```

The bridge carries JSON plus binary buffers. Use it for anything request/response shaped. Streaming and server-push are out of scope; use the WS proxy for that.

## What works where

| Environment               | Path            | User does              |
| ------------------------- | --------------- | ---------------------- |
| Local notebook            | direct loopback | nothing                |
| JupyterLab / Notebook 7+  | HTTP/WS proxy   | nothing                |
| JupyterHub / MyBinder     | HTTP/WS proxy   | nothing                |
| VS Code Remote            | comm bridge     | `enable_comm_bridge()` |
| Google Colab              | comm bridge     | `enable_comm_bridge()` |
| Shiny for Python / Solara | comm bridge     | `enable_comm_bridge()` |
| marimo                    | comm bridge     | `enable_comm_bridge()` |

## Relationship to `jupyter-server-proxy`

`jupyter-server-proxy` proxies arbitrary subprocesses. It handles HTTP/WS wire formatting, subprocess lifecycle, URL rewriting, auth.

`jupyter-loopback` does less, on purpose:

- Proxies loopback only. No cross-host surface.
- No subprocess management. You bring your own server on any port.
- Autodetects the URL prefix from Jupyter's own env vars.
- Ships a comm-based fallback for frontends without a jupyter-server.

The two can coexist. Set `LIBRARY_CLIENT_PREFIX` explicitly and jupyter-loopback's autodetect steps out of the way.

## Status

Extracted from [`localtileserver`](https://github.com/banesullivan/localtileserver) after the same pattern solved its long tail of remote-Jupyter issues. Generalized so other libraries can adopt it without reinventing the wheel.

See [`ARCHITECTURE.md`](ARCHITECTURE.md) for the design walkthrough. MIT licensed.
