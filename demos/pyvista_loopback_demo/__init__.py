"""
Reference integration of jupyter-loopback for PyVista's trame Jupyter backend.

The jupyter-server extension in :mod:`pyvista_loopback_demo._jupyter`
mounts the loopback proxy under the ``pyvista`` namespace so the trame
server (launched in the kernel by ``pyvista.set_jupyter_backend('trame')``)
becomes reachable from the notebook browser at
``<base_url>/pyvista-proxy/<port>/...`` without ``jupyter-server-proxy``.

The kernel-side helper :func:`configure` wires PyVista's trame theme so
it builds iframe URLs that resolve through that proxy.
"""

from __future__ import annotations

__all__ = ["configure", "loopback_prefix_for_pyvista"]


def loopback_prefix_for_pyvista() -> str | None:
    """Return the ``server_proxy_prefix`` PyVista should use.

    PyVista's ``build_url`` concatenates the prefix directly with the
    integer port, so the prefix must end with a trailing slash and must
    not contain the ``{port}`` placeholder. ``autodetect_prefix`` returns
    a template like ``"/pyvista-proxy/{port}"``; we slice everything
    before ``{port}``, which always ends with ``"/"``.

    Returns
    -------
    str or None
        Prefix ready to assign to
        :attr:`pyvista.global_theme.trame.server_proxy_prefix`, or
        ``None`` outside a Jupyter kernel.
    """
    from jupyter_loopback import autodetect_prefix

    template = autodetect_prefix("pyvista")
    if template is None:
        return None
    head, _sep, _tail = template.partition("{port}")
    return head


def configure() -> str | None:
    """Wire PyVista's trame backend through the jupyter-loopback proxy.

    Sets ``pyvista.global_theme.trame.server_proxy_prefix`` and
    ``server_proxy_enabled`` so that ``Plotter.show(jupyter_backend='trame')``
    builds iframe URLs that resolve through this extension's proxy. Idempotent.

    Returns
    -------
    str or None
        The prefix that was applied, or ``None`` outside a kernel (in
        which case PyVista's defaults are left untouched).
    """
    import pyvista as pv

    prefix = loopback_prefix_for_pyvista()
    if prefix is None:
        return None
    pv.global_theme.trame.server_proxy_prefix = prefix
    pv.global_theme.trame.server_proxy_enabled = True
    return prefix
