# jupyter-loopback demo image.
#
# Builds jupyter-loopback + the in-tree loopback-demo package + JupyterLab
# into a single image so reviewers can validate the HTTP/WS proxy end-to-end
# in ~30 seconds without touching their host Python.
#
# Usage:
#   docker build -t jupyter-loopback-demo .
#   docker run --rm -it -p 8888:8888 jupyter-loopback-demo
#   # Open the printed http://127.0.0.1:8888/?token=... URL
#   # Open demos/example.ipynb and run all cells.

FROM python:3.12-slim

LABEL org.opencontainers.image.title="jupyter-loopback-demo"
LABEL org.opencontainers.image.source="https://github.com/banesullivan/jupyter-loopback"

WORKDIR /build

RUN python -m pip install --upgrade --no-cache-dir pip

# ---- build + install jupyter-loopback itself ----
# Version is computed by setuptools_scm from git; pass it via build arg
# so users without .git/ still get a usable placeholder version.
ARG SETUPTOOLS_SCM_PRETEND_VERSION_FOR_JUPYTER_LOOPBACK=0.0.0+docker
ENV SETUPTOOLS_SCM_PRETEND_VERSION_FOR_JUPYTER_LOOPBACK=${SETUPTOOLS_SCM_PRETEND_VERSION_FOR_JUPYTER_LOOPBACK}

COPY pyproject.toml README.md LICENSE /build/
COPY jupyter_loopback/ /build/jupyter_loopback/
RUN pip install --no-cache-dir "/build[comm]" jupyterlab requests

# ---- install the demo package + notebook ----
COPY demos/ /build/demos/
RUN pip install --no-cache-dir /build/demos

WORKDIR /home/demo
COPY demos/example.ipynb /home/demo/
COPY demos/README.md     /home/demo/

EXPOSE 8888
CMD ["jupyter", "lab", "--ip=0.0.0.0", "--no-browser", "--allow-root", \
     "--ServerApp.root_dir=/home/demo"]
