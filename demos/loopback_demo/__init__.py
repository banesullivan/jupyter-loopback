"""
Integration reference: the server extension stub for the example notebook.

The actual demo server code lives in ``demos/example.ipynb`` so readers
can see every piece (Tornado handlers, ``autodetect_prefix``, HTML
display) instead of opening a package. Keeping the package around gives
``jupyter-server`` something to auto-load via
``jupyter-config/jupyter_server_config.d/loopback-demo.json``.
"""
