# loopback-demo

End-to-end reference for integrating `jupyter-loopback`.

## What's in the package

- `loopback_demo/_jupyter/__init__.py` mounts the basic-demo proxy. Two lines of actual code.
- `jupyter-config/jupyter_server_config.d/loopback-demo.json` auto-enables the basic-demo extension on install.
- `example.ipynb` holds the in-kernel server and the browser HTML so readers see every step without opening a Python package.
- `pyvista_loopback_demo/_jupyter/__init__.py` does the same for the `pyvista` namespace, so PyVista's trame Jupyter backend can route through `jupyter-loopback` instead of `jupyter-server-proxy`.
- `jupyter-config/jupyter_server_config.d/pyvista-loopback-demo.json` auto-enables that extension.
- `pyvista_example.ipynb` walks through `set_jupyter_backend('trame')` over the loopback proxy and renders an interactive plot via the proxied iframe.
- `test_pyvista_proxy.py` is a self-contained smoke test that asserts HTTP and WebSocket round-trip through the proxy with a real PyVista trame upstream. No browser, no JupyterLab; ~5s end-to-end.

## Try it

```bash
docker build -t jupyter-loopback-demo ..
docker run --rm -it -p 8888:8888 jupyter-loopback-demo
```

Open the printed token URL, then either:

- Open `example.ipynb`, run all cells. You should see a JSON response, an inline red square, and a working WebSocket echo box. Everything flows through `/loopback-demo-proxy/<port>/...` with no user configuration.
- Open `pyvista_example.ipynb`, run all cells. The iframe `src` should be `/pyvista-proxy/<port>/index.html?...` and the plot should be interactive (rotate it with the mouse).

Run the smoke test inside the image to validate both halves of the proxy without a browser:

```bash
docker run --rm jupyter-loopback-demo python /home/demo/test_pyvista_proxy.py
```

Outside Jupyter (plain Python REPL), the notebook's `autodetect_prefix` call returns `None` and the code falls back to hitting `127.0.0.1:<port>` directly.
