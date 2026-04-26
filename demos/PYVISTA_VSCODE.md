# PyVista trame in VS Code Jupyter

The `pyvista_example.ipynb` demo works in JupyterLab, JupyterHub, MyBinder, and Notebook 7 because those frontends serve the notebook from a real jupyter-server origin, and the loopback proxy can mount routes on it. VS Code Jupyter doesn't, and the demo will not work there as written. This file records why, what works there today, and what it would take to extend `jupyter-loopback` to cover that frontend.

## Why it breaks in VS Code

VS Code Jupyter (local or Remote-SSH) doesn't run a jupyter-server. The notebook lives in a webview that talks to a kernel directly. Two consequences:

1. The HTTP proxy mount has nowhere to attach. `setup_proxy_handler(server_app.web_app, namespace="pyvista")` runs in the jupyter-server process. There is no jupyter-server, so the route `<base>/pyvista-proxy/<port>/...` doesn't exist on any origin the webview can reach. PyVista's iframe (`<iframe src="/pyvista-proxy/<port>/index.html">`) fails to load.
2. The comm bridge that covers `localtileserver` doesn't apply here. `intercept_localhost(port, prefix)` patches three things in the rendering document: `HTMLImageElement.prototype.src`, `window.fetch`, and `XMLHttpRequest.prototype.open/send`. That's enough for `localtileserver` because Leaflet creates `<img>` tiles in the same document as the comm bridge widget. trame renders inside an `<iframe>` whose document has its own copies of all those prototypes; the parent's patches don't reach inside it, and the iframe has to load a real URL before any of its JS runs. The bridge also doesn't patch `window.WebSocket`, and trame's frontend opens a real `new WebSocket("ws://hostname:port/<prefix>/ws")`.

`localtileserver` works in VS Code through the comm bridge because tile loading is HTTP-only, in-document, and image-shaped. trame needs an iframe and a WebSocket.

## What works in VS Code today

`trame-jupyter-extension`. It tunnels the wslink protocol over the kernel comm channel and ships the trame frontend assets as a bundled extension. PyVista already auto-detects it: `_TrameConfig.__init__` flips `jupyter_extension_enabled` on when `TRAME_JUPYTER_WWW` is in env, and `launch_server` switches to `wslink_backend='jupyter'`. Install the extension and PyVista's trame backend works in VS Code with no further configuration.

| Environment                        | Mechanism                                |
| ---------------------------------- | ---------------------------------------- |
| Local notebook                     | direct loopback                          |
| JupyterLab / Hub / Binder / NB 7   | `jupyter-loopback` HTTP+WS proxy (this demo) |
| VS Code Jupyter / Colab / Solara   | `trame-jupyter-extension` (kernel comm)  |

## What it would take to make `jupyter-loopback` cover VS Code too

1. Patch `window.WebSocket` in `widget.js` the same way `window.fetch` is patched. `interceptMatch(url)` already produces the right `(port, pathAndQuery)`; `openWebSocket(port, path)` already returns a WebSocket-like object close enough that wslink won't notice.
2. Cross the iframe document boundary. Either fork PyVista's `build_url` to render the trame frontend in the parent document instead of an iframe, or build the iframe with `srcdoc=` carrying a prelude script that installs the patches before trame's bundle runs. Either way the bridge has to fetch `index.html` and the bundled assets through `__loopback__/fetch`, rewrite their relative URLs, and inject the patches inside the new document context.
3. Wire kernel-side detection so `configure()` picks the right path: HTTP proxy when there is a jupyter-server origin, comm bridge otherwise. The detection signal is already in `is_in_jupyter_kernel()` plus the absence of `JPY_BASE_URL` / `JUPYTERHUB_SERVICE_PREFIX` plus a probe attempt.

Scope is comparable to what `trame-jupyter-extension` already does end-to-end. Worth doing only if the goal is consolidating both proxy paths under one library.

## Verifying the failure mode (optional)

If you want to confirm the analysis hands-on:

```bash
docker run --rm -it -p 8888:8888 jupyter-loopback-demo
```

Then in VS Code (Remote-SSH or Containers): connect to that kernel, open `pyvista_example.ipynb`, add `import jupyter_loopback; jupyter_loopback.enable_comm_bridge()` at the top, and run all cells. The kernel-side cells (probe via `requests`, `autodetect_prefix`, `configure`, `pl.show` returning a `Widget`) all succeed. The iframe rendered inline in the output area fails to load because the webview can't reach `/pyvista-proxy/<port>/...`. That's the symptom a user would see.
