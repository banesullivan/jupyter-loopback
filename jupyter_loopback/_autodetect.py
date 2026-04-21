"""
Kernel-side autodetection of the browser-reachable proxy URL.

The jupyter-server extension half of :mod:`jupyter_loopback` mounts a
proxy handler at ``<base_url>/<namespace>-proxy/<port>/...``. Kernels
need that template to build URLs their frontend code can reach.

This module reads the environment variables Jupyter populates for
every kernel and returns the fully-qualified prefix, including any
per-user ``JUPYTERHUB_SERVICE_PREFIX``. Library authors build
browser-visible URLs without asking users to set anything.
"""

import os

__all__ = ["autodetect_prefix", "is_in_jupyter_kernel"]

# Env vars set by jupyter-server when it spawns a kernel. ``JPY_SESSION_NAME``
# is the modern variant (jupyter-server 2.x+); ``JPY_PARENT_PID`` is the older
# fallback still set by classic Notebook in some deployments.
_KERNEL_ENV_SIGNALS = ("JPY_SESSION_NAME", "JPY_PARENT_PID")

# Env vars that carry the base URL prefix in multi-tenant setups.
# ``JUPYTERHUB_SERVICE_PREFIX`` is set by JupyterHub; ``JPY_BASE_URL`` is
# the more general base-url hint some spawners populate.
_BASE_URL_ENV_SIGNALS = ("JUPYTERHUB_SERVICE_PREFIX", "JPY_BASE_URL")


def is_in_jupyter_kernel() -> bool:
    """
    Check whether the current process looks like a Jupyter kernel.

    The check is structural. It inspects the environment only, not
    whether a jupyter-server is actually reachable. Use it to decide
    whether to attempt autodetection at all.

    Returns
    -------
    bool
        ``True`` if jupyter-server's kernel env vars are present.
    """
    return any(os.environ.get(var) for var in _KERNEL_ENV_SIGNALS)


def autodetect_prefix(
    namespace: str, *, template: str = "{namespace}-proxy/{{port}}"
) -> str | None:
    """
    Build the browser-reachable URL prefix for a registered namespace.

    Call this from the kernel side of a library that has registered a
    proxy handler with :func:`jupyter_loopback.setup_proxy_handler` to
    construct URLs that resolve through the notebook server.

    Parameters
    ----------
    namespace : str
        The namespace registered on the server side. Must match the
        ``namespace`` passed to :func:`setup_proxy_handler`.
    template : str, optional
        Prefix template. ``{namespace}`` is substituted immediately;
        ``{{port}}`` is left intact so callers can ``.format(port=...)``
        later when they know which loopback port they're targeting.

    Returns
    -------
    str or None
        Fully-qualified prefix (e.g. ``"/user/alice/mylib-proxy/{port}"``)
        if we are inside a Jupyter kernel; ``None`` otherwise.

    Examples
    --------
    .. code:: python

        from jupyter_loopback import autodetect_prefix

        prefix = autodetect_prefix("mylib")
        if prefix:
            url = f"{prefix.format(port=server_port)}/api/data"
        else:
            url = f"http://127.0.0.1:{server_port}/api/data"
    """
    if not is_in_jupyter_kernel():
        return None
    prefix = template.format(namespace=namespace)
    for var in _BASE_URL_ENV_SIGNALS:
        base = os.environ.get(var)
        if base:
            # Hub / custom-base-URL spawners set these with a leading
            # slash (e.g. "/user/alice/"), so the result is already
            # root-absolute after the join.
            return f"{base.rstrip('/')}/{prefix}"
    # Plain JupyterLab / Notebook 7 with the default base URL of "/".
    # Return a root-absolute path so the browser resolves it against
    # the Jupyter origin rather than the notebook's view URL
    # (e.g. "/lab/tree/…/foo" would 404).
    return f"/{prefix}"
