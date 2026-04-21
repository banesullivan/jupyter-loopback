# loopback-demo

End-to-end reference for integrating `jupyter-loopback`.

## What's in the package

- `loopback_demo/_jupyter/__init__.py` mounts the proxy. Two lines of actual code.
- `jupyter-config/jupyter_server_config.d/loopback-demo.json` auto-enables the extension on install.
- `example.ipynb` holds the in-kernel server and the browser HTML so readers see every step without opening a Python package.

## Try it

```bash
docker build -t jupyter-loopback-demo ..
docker run --rm -it -p 8888:8888 jupyter-loopback-demo
```

Open the printed token URL, open `example.ipynb`, run all cells. You should see a JSON response, an inline red square, and a working WebSocket echo box. Everything flows through `/loopback-demo-proxy/<port>/...` with no user configuration.

Outside Jupyter (plain Python REPL), the notebook's `autodetect_prefix` call returns `None` and the code falls back to hitting `127.0.0.1:<port>` directly.
