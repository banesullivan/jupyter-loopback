# jupyter-loopback demo image.
#
# Builds jupyter-loopback + the in-tree loopback-demo package + JupyterLab
# into a single image so reviewers can validate the HTTP/WS proxy end-to-end
# in ~30 seconds without touching their host Python.
#
# The image also ships a PyVista trame demo notebook
# (``pyvista_example.ipynb``) that drives PyVista's trame Jupyter backend
# through the loopback proxy -- replacing the usual
# ``jupyter-server-proxy`` + ``PYVISTA_TRAME_SERVER_PROXY_PREFIX`` setup
# with a zero-config flow.
#
# Usage:
#   docker build -t jupyter-loopback-demo .
#   docker run --rm -it -p 8888:8888 jupyter-loopback-demo
#   # Open the printed http://127.0.0.1:8888/?token=... URL
#   # Open demos/example.ipynb (basic HTTP+WS proxy) or
#   # demos/pyvista_example.ipynb (PyVista trame through the proxy).

FROM python:3.12-slim

LABEL org.opencontainers.image.title="jupyter-loopback-demo"
LABEL org.opencontainers.image.source="https://github.com/banesullivan/jupyter-loopback"

WORKDIR /build

RUN python -m pip install --upgrade --no-cache-dir pip

# ---- system libs VTK dlopens for off-screen rendering ----
# Same set the upstream PyVista jupyter image installs: libgl1 + libegl1
# cover Mesa llvmpipe (CPU) and EGL (GPU) paths; libxrender1 covers the
# X11 fallback when neither is available.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libopengl0 \
      libgl1 \
      libegl1 \
      libxrender1 \
 && rm -rf /var/lib/apt/lists/*

# ---- build + install jupyter-loopback itself ----
# Version is computed by setuptools_scm from git; pass it via build arg
# so users without .git/ still get a usable placeholder version.
ARG SETUPTOOLS_SCM_PRETEND_VERSION_FOR_JUPYTER_LOOPBACK=0.0.0+docker
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_JUPYTER_LOOPBACK=${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_JUPYTER_LOOPBACK}

COPY pyproject.toml README.md LICENSE /build/
COPY jupyter_loopback/ /build/jupyter_loopback/
RUN pip install --no-cache-dir "/build[comm]" jupyterlab requests

# ---- install the demo package + notebooks ----
# The ``[pyvista]`` extra pulls in pyvista[jupyter] (trame, vtk, etc.)
# which the PyVista demo notebook imports. Off-screen rendering is the
# default in the PyVista demo notebook (PYVISTA_OFF_SCREEN=true) so no
# Xvfb is required at container start.
COPY demos/ /build/demos/
RUN pip install --no-cache-dir "/build/demos[pyvista]"

WORKDIR /home/demo
COPY demos/example.ipynb           /home/demo/
COPY demos/pyvista_example.ipynb   /home/demo/
COPY demos/test_pyvista_proxy.py   /home/demo/
COPY demos/README.md               /home/demo/

ENV PYVISTA_OFF_SCREEN=true

EXPOSE 8888
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--no-browser", "--allow-root", \
     "--ServerApp.root_dir=/home/demo"]
