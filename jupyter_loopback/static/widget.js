// jupyter-loopback comm bridge (browser half).
//
// When rendered, this widget installs window.__jupyter_loopback__,
// a global API that any page-level script can use to talk to the
// kernel over the notebook's comm websocket. Two layers:
//
//   1. Raw request/response RPC:
//        await window.__jupyter_loopback__.request(
//            namespace, kind, data, buffers,
//        )
//        // -> { status: "ok" | "error", data, buffers, error? }
//
//   2. Built-in HTTP / WebSocket proxy, mirroring Path A but routed
//      through the comm channel. Use these for frontends (e.g. VS Code
//      Jupyter) where the webview can't reach the jupyter-server
//      origin for root-relative URLs:
//        await window.__jupyter_loopback__.fetch(port, path, init?)
//                                                        // -> Response
//        await window.__jupyter_loopback__.resolveUrl(port, path, opt)
//                                                        // -> blob URL
//        window.__jupyter_loopback__.openWebSocket(port, path)
//                                                        // -> WebSocket-like
//
// Multiple rendered widgets register into a shared bridge stack so
// re-running the cell that hosts the widget cuts over transparently
// to the fresh comm channel.

/**
 * @typedef {Object} Bridge
 * @property {() => boolean} alive
 * @property {(msg: any, buffers: ArrayBuffer[]) => void} send
 */

/**
 * @typedef {Object} PendingEntry
 * @property {(value: any) => void} resolve
 * @property {(reason: Error) => void} reject
 */

/**
 * @typedef {Object} WsEntry
 * @property {(evt: string, msg: any, buffers: ArrayBuffer[]) => void} handle
 */

/**
 * @typedef {Object} ResolveResult
 * @property {string} status
 * @property {any} data
 * @property {ArrayBuffer[]} buffers
 * @property {string} [error]
 */

/**
 * @typedef {Object} LoopbackAPI
 * @property {(bridge: Bridge) => void} registerBridge
 * @property {(bridge: Bridge) => void} removeBridge
 * @property {(bridge: Bridge) => boolean} _isActiveBridge
 * @property {(msg: any, buffers: ArrayBuffer[]) => void} _resolve
 * @property {(
 *     namespace: string,
 *     kind: string,
 *     data?: any,
 *     buffers?: ArrayBuffer[],
 * ) => Promise<ResolveResult>} request
 * @property {(
 *     port: number | string,
 *     path: string,
 *     init?: RequestInit,
 * ) => Promise<Response>} fetch
 * @property {(
 *     port: number | string,
 *     path: string,
 *     opts?: { mime?: string },
 * ) => Promise<string>} resolveUrl
 * @property {(
 *     port: number | string,
 *     path: string,
 *     protocols?: string | string[],
 * ) => LoopbackWebSocket} openWebSocket
 * @property {(
 *     port: number | string,
 *     pathPrefix?: string | null,
 * ) => void} interceptLocalhost
 */

/**
 * @typedef {Object} AnyWidgetModel
 * @property {(evt: string, fn: (...args: any[]) => void) => void} on
 * @property {(evt: string, fn: (...args: any[]) => void) => void} off
 * @property {(msg: any, callbacks?: any, buffers?: ArrayBuffer[]) => void} send
 * @property {(key: string) => any} get
 */

/**
 * Produce a short random id for correlating requests and responses.
 * @returns {string}
 */
function randomId() {
    return Math.random().toString(36).slice(2, 10) + Math.random().toString(36).slice(2, 10);
}

/**
 * Split a path-with-query into its two components.
 * @param {string} path
 * @returns {{ path: string, query: string }}
 */
function splitQuery(path) {
    const idx = path.indexOf("?");
    if (idx < 0) return { path, query: "" };
    return { path: path.slice(0, idx), query: path.slice(idx + 1) };
}

/**
 * Normalize a RequestInit body into an ArrayBuffer suitable for
 * transmission as a comm buffer. Returns null when there is no body.
 * @param {RequestInit | undefined} init
 * @returns {Promise<ArrayBuffer | null>}
 */
async function bodyToBuffer(init) {
    if (!init || init.body == null) return null;
    const b = init.body;
    if (b instanceof ArrayBuffer) return b;
    if (ArrayBuffer.isView(b)) {
        // ``ArrayBufferView.buffer`` is typed ``ArrayBuffer |
        // SharedArrayBuffer`` in newer lib.dom; we only traffic in
        // regular ``ArrayBuffer`` over the comm protocol so the cast
        // is safe. SharedArrayBuffer bodies would fail at structured-
        // clone time anyway.
        return /** @type {ArrayBuffer} */ (
            b.buffer.slice(b.byteOffset, b.byteOffset + b.byteLength)
        );
    }
    if (typeof Blob !== "undefined" && b instanceof Blob) return await b.arrayBuffer();
    if (typeof b === "string") {
        return /** @type {ArrayBuffer} */ (new TextEncoder().encode(b).buffer);
    }
    if (typeof FormData !== "undefined" && b instanceof FormData) {
        // Serialize as multipart by hand? Out of scope for loopback;
        // library authors wanting multipart should pre-serialize.
        throw new Error("jupyter_loopback.fetch: FormData bodies unsupported");
    }
    if (typeof URLSearchParams !== "undefined" && b instanceof URLSearchParams) {
        return /** @type {ArrayBuffer} */ (new TextEncoder().encode(b.toString()).buffer);
    }
    throw new Error("jupyter_loopback.fetch: unsupported body type");
}

/**
 * Convert a RequestInit headers value into a plain {name: value} object.
 * @param {HeadersInit | undefined} headers
 * @returns {Record<string, string>}
 */
function headersToObject(headers) {
    /** @type {Record<string, string>} */
    const out = {};
    if (!headers) return out;
    if (typeof Headers !== "undefined" && headers instanceof Headers) {
        headers.forEach((value, name) => {
            out[name] = value;
        });
        return out;
    }
    if (Array.isArray(headers)) {
        for (const [name, value] of headers) out[name] = value;
        return out;
    }
    for (const name of Object.keys(headers)) {
        out[name] = /** @type {Record<string, string>} */ (headers)[name];
    }
    return out;
}

/**
 * Build a standard Response from the fetch RPC reply.
 * @param {any} data
 * @param {ArrayBuffer[]} buffers
 * @returns {Response}
 */
function buildResponse(data, buffers) {
    const headers = new Headers();
    for (const pair of data.headers || []) {
        try {
            headers.append(pair[0], pair[1]);
        } catch (_) {
            // Some header names (e.g. those with non-token chars) will
            // reject via Headers.append; dropping them mirrors what
            // browsers do for malformed upstream headers.
        }
    }
    const body = buffers?.length ? buffers[0] : null;
    return new Response(body, {
        status: data.code || 200,
        statusText: data.reason || "",
        headers,
    });
}

/**
 * WebSocket-like object backed by the comm bridge.
 *
 * Implements enough of the standard WebSocket surface to drop into
 * most library code: ``send``, ``close``, ``readyState``, four ``on*``
 * event handlers, plus EventTarget-style addEventListener / removeEventListener.
 *
 * @typedef {Object} LoopbackWebSocket
 * @property {number} readyState
 * @property {string} url
 * @property {string} protocol
 * @property {((ev: Event) => void) | null} onopen
 * @property {((ev: MessageEvent) => void) | null} onmessage
 * @property {((ev: CloseEvent) => void) | null} onclose
 * @property {((ev: Event) => void) | null} onerror
 * @property {(data: string | ArrayBuffer | ArrayBufferView) => void} send
 * @property {(code?: number, reason?: string) => void} close
 * @property {(type: string, fn: (ev: any) => void) => void} addEventListener
 * @property {(type: string, fn: (ev: any) => void) => void} removeEventListener
 * @property {string} binaryType
 */

/**
 * Lazily create and return the window-scoped jupyter-loopback API.
 * @returns {LoopbackAPI | null}
 */
function ensureGlobal() {
    if (typeof window === "undefined") return null;
    /** @type {any} */
    const w = window;
    if (w.__jupyter_loopback__) return w.__jupyter_loopback__;

    // Diagnostic log: helps track which iframe the bridge booted in.
    // Each notebook-output renderer is its own iframe in VS Code; seeing
    // this line in the devtools console tells us whether the widget
    // rendered in the same context as Leaflet's tile <img> elements.
    try {
        console.info("[jupyter_loopback] bridge initializing in", location.href);
    } catch (_) {
        /* console may be absent in exotic hosts */
    }

    /** @type {Bridge[]} */
    const bridges = [];
    /** @type {Map<string, PendingEntry>} */
    const pending = new Map();
    /** @type {Map<string, WsEntry>} */
    const wsByWsId = new Map();

    /**
     * Return the newest bridge whose comm is still alive, or null.
     * @returns {Bridge | null}
     */
    function newestAliveBridge() {
        for (let i = bridges.length - 1; i >= 0; i--) {
            if (bridges[i].alive()) return bridges[i];
        }
        return null;
    }

    /**
     * @param {any} req
     * @param {ArrayBuffer[]} buffers
     * @returns {boolean}
     */
    function send(req, buffers) {
        const active = newestAliveBridge();
        if (!active) return false;
        active.send(req, buffers || []);
        return true;
    }

    /**
     * @param {any} msg
     * @param {ArrayBuffer[]} buffers
     */
    function resolve(msg, buffers) {
        if (!msg) return;
        if (msg.type === "response" && msg.id) {
            const entry = pending.get(msg.id);
            if (!entry) return;
            pending.delete(msg.id);
            if (msg.status === "ok") {
                entry.resolve({
                    status: "ok",
                    data: msg.data ?? null,
                    buffers: buffers || [],
                });
            } else {
                entry.reject(new Error(msg.error || "jupyter_loopback error"));
            }
            return;
        }
        if (msg.type === "event" && msg.ws_id) {
            const entry = wsByWsId.get(msg.ws_id);
            if (!entry) return;
            entry.handle(msg.event, msg, buffers || []);
        }
    }

    /**
     * @param {string} namespace
     * @param {string} kind
     * @param {any} [data]
     * @param {ArrayBuffer[]} [buffers]
     * @returns {Promise<ResolveResult>}
     */
    function request(namespace, kind, data, buffers) {
        return new Promise((resolveFn, rejectFn) => {
            const id = randomId();
            pending.set(id, { resolve: resolveFn, reject: rejectFn });
            const ok = send(
                { type: "request", id, namespace, kind, data: data || {} },
                buffers || [],
            );
            if (!ok) {
                pending.delete(id);
                rejectFn(
                    new Error(
                        "jupyter_loopback: no live bridge. Call " +
                            "jupyter_loopback.enable_comm_bridge() in a kernel cell first.",
                    ),
                );
                return;
            }
            // 30s watchdog so kernel hangs don't leak pending entries.
            setTimeout(() => {
                if (pending.has(id)) {
                    pending.delete(id);
                    rejectFn(new Error(`jupyter_loopback: request ${namespace}/${kind} timed out`));
                }
            }, 30000);
        });
    }

    /**
     * Run an HTTP request against 127.0.0.1:<port><path> via the kernel.
     * @param {number | string} port
     * @param {string} path
     * @param {RequestInit} [init]
     * @returns {Promise<Response>}
     */
    async function loopbackFetch(port, path, init) {
        const split = splitQuery(path || "/");
        const headers = headersToObject(init?.headers);
        const body = await bodyToBuffer(init);
        const result = await request(
            "__loopback__",
            "fetch",
            {
                port: Number(port),
                path: split.path || "/",
                query: split.query,
                method: init?.method || "GET",
                headers,
            },
            body ? [body] : [],
        );
        return buildResponse(result.data || {}, result.buffers || []);
    }

    /**
     * Fetch a URL via the comm bridge and return a blob: URL usable as an
     * ``<img src>`` or ``<iframe src>``. Caller is responsible for
     * ``URL.revokeObjectURL`` when the URL is no longer needed.
     * @param {number | string} port
     * @param {string} path
     * @param {{ mime?: string }} [opts]
     * @returns {Promise<string>}
     */
    async function resolveUrl(port, path, opts) {
        const response = await loopbackFetch(port, path);
        if (!response.ok) {
            throw new Error(
                `jupyter_loopback.resolveUrl(${port}, ${path}) -> HTTP ${response.status}`,
            );
        }
        const blob = await response.blob();
        if (opts?.mime && blob.type !== opts.mime) {
            // Browsers occasionally default to application/octet-stream
            // when the upstream omits Content-Type; force the caller's
            // hint so <img> MIME-sniffing picks the right decoder.
            const retyped = blob.slice(0, blob.size, opts.mime);
            return URL.createObjectURL(retyped);
        }
        return URL.createObjectURL(blob);
    }

    /**
     * WebSocket-like object backed by the comm bridge.
     * @param {number | string} port
     * @param {string} path
     * @returns {LoopbackWebSocket}
     */
    function openWebSocket(port, path) {
        const wsId = randomId();
        const split = splitQuery(path || "/");
        const listeners = {
            open: /** @type {Array<(ev: any) => void>} */ ([]),
            message: /** @type {Array<(ev: any) => void>} */ ([]),
            close: /** @type {Array<(ev: any) => void>} */ ([]),
            error: /** @type {Array<(ev: any) => void>} */ ([]),
        };
        /** @type {LoopbackWebSocket} */
        const ws = {
            readyState: 0, // CONNECTING
            url: `ws://127.0.0.1:${port}${path}`,
            protocol: "",
            binaryType: "arraybuffer",
            onopen: null,
            onmessage: null,
            onclose: null,
            onerror: null,
            send(data) {
                if (ws.readyState !== 1) {
                    throw new Error(
                        "jupyter_loopback.WebSocket: cannot send before open / after close",
                    );
                }
                /** @type {ArrayBuffer[]} */
                let buffers = [];
                /** @type {any} */
                const payload = { ws_id: wsId };
                if (typeof data === "string") {
                    payload.text = data;
                } else if (data instanceof ArrayBuffer) {
                    buffers = [data];
                } else if (ArrayBuffer.isView(data)) {
                    const view = /** @type {ArrayBufferView} */ (data);
                    // Cast the slice: ``view.buffer`` is typed
                    // ``ArrayBuffer | SharedArrayBuffer`` in newer
                    // lib.dom; only regular ``ArrayBuffer`` survives
                    // the comm protocol's structured clone anyway.
                    buffers = [
                        /** @type {ArrayBuffer} */ (
                            view.buffer.slice(view.byteOffset, view.byteOffset + view.byteLength)
                        ),
                    ];
                } else {
                    throw new Error("jupyter_loopback.WebSocket.send: unsupported data type");
                }
                request("__loopback__", "ws_send", payload, buffers).catch((err) => {
                    dispatch("error", { type: "error", message: String(err) });
                });
            },
            close(_code, _reason) {
                if (ws.readyState === 3) return;
                ws.readyState = 2; // CLOSING
                request("__loopback__", "ws_close", { ws_id: wsId }).finally(() => {
                    finalize({ wasClean: true, code: _code || 1000, reason: _reason || "" });
                });
            },
            addEventListener(type, fn) {
                if (listeners[/** @type {keyof typeof listeners} */ (type)]) {
                    listeners[/** @type {keyof typeof listeners} */ (type)].push(fn);
                }
            },
            removeEventListener(type, fn) {
                const arr = listeners[/** @type {keyof typeof listeners} */ (type)];
                if (!arr) return;
                const i = arr.indexOf(fn);
                if (i >= 0) arr.splice(i, 1);
            },
        };

        /**
         * @param {"open"|"message"|"close"|"error"} type
         * @param {any} event
         */
        function dispatch(type, event) {
            const handler = /** @type {any} */ (ws)[`on${type}`];
            if (typeof handler === "function") {
                try {
                    handler.call(ws, event);
                } catch (_) {
                    /* ignore handler errors */
                }
            }
            for (const fn of listeners[type]) {
                try {
                    fn(event);
                } catch (_) {
                    /* ignore listener errors */
                }
            }
        }

        /** @param {{ wasClean?: boolean, code?: number, reason?: string }} init */
        function finalize(init) {
            ws.readyState = 3; // CLOSED
            wsByWsId.delete(wsId);
            dispatch("close", {
                type: "close",
                wasClean: !!init.wasClean,
                code: init.code ?? 1005,
                reason: init.reason ?? "",
            });
        }

        wsByWsId.set(wsId, {
            handle(event, msg, buffers) {
                if (event === "ws_message") {
                    if (msg.binary && buffers.length) {
                        dispatch("message", { type: "message", data: buffers[0] });
                    } else {
                        dispatch("message", { type: "message", data: msg.text ?? "" });
                    }
                } else if (event === "ws_close") {
                    finalize({ wasClean: true, code: 1000, reason: "upstream closed" });
                }
            },
        });

        request("__loopback__", "ws_open", {
            ws_id: wsId,
            port: Number(port),
            path: split.path || "/",
            query: split.query,
        })
            .then(() => {
                if (ws.readyState === 0) {
                    ws.readyState = 1; // OPEN
                    dispatch("open", { type: "open" });
                }
            })
            .catch((err) => {
                dispatch("error", { type: "error", message: String(err) });
                finalize({ wasClean: false, code: 1006, reason: String(err) });
            });

        return ws;
    }

    /** @type {Set<string>} */
    const interceptedPorts = new Set();
    /**
     * Path-prefix forwarders. Keyed by normalized prefix (no trailing
     * slash); the value is the port the prefix routes to. Populated by
     * ``interceptLocalhost(port, prefix)`` callers that want the comm
     * bridge to cover for a registered HTTP proxy on deployments where
     * the proxy extension isn't loaded server-side (e.g. JupyterHub
     * with a kernel env that differs from the single-user server env).
     *
     * @type {Map<string, number>}
     */
    const prefixToPort = new Map();
    /**
     * Per-prefix probe outcome. Values:
     *   "probing"  -- probe in flight; interceptors must await
     *                 :data:`prefixReady` before deciding how to
     *                 route, otherwise the guess is wrong on one of
     *                 Hub / mybinder.
     *   "working"  -- probe confirmed the HTTP proxy is mounted;
     *                 don't intercept, let the fast Path A handle it.
     *   "broken"   -- probe returned 404; comm bridge takes over.
     *
     * @type {Map<string, "probing" | "working" | "broken">}
     */
    const prefixStatus = new Map();
    /**
     * Per-prefix settlement signal. Keyed by the same normalized
     * prefix as :data:`prefixStatus`; the Promise resolves (never
     * rejects) when the probe's ``then``/``catch`` has flipped the
     * status to ``"working"`` or ``"broken"``. Interceptors that land
     * on a ``"probing"`` match defer their routing decision by
     * awaiting this instead of guessing, which is what lets us
     * satisfy both deployment shapes at once: JupyterHub (probe → 404
     * → route through comm) and Lab / mybinder (probe → 204 → pass
     * through to direct HTTP). Optimistic pass-through during probing
     * silently 404s on Hub; pessimistic comm-route during probing
     * times out on mybinder when the comm bridge isn't warm yet. The
     * probe latency is far shorter than either failure window, so
     * waiting is strictly better than guessing.
     *
     * @type {Map<string, Promise<void>>}
     */
    const prefixReady = new Map();
    /**
     * Original ``window.fetch`` captured before we patch it, so the
     * probe can reach the jupyter-server without our own interceptor
     * catching it and recursing through the comm bridge.
     *
     * @type {typeof fetch | null}
     */
    let origFetch = null;
    let interceptorInstalled = false;

    /**
     * Classify a URL against the registered loopback ports and same-origin
     * prefixes. Returns ``null`` for URLs the interceptors should leave
     * alone. Returns a shape carrying the loopback ``port`` / rewritten
     * ``pathAndQuery`` plus a ``status`` that tells the caller how to
     * route:
     *
     * - ``"working"``: the HTTP proxy is confirmed live; the caller
     *   should still pass through to direct HTTP (interception is a
     *   no-op). This shape exists so the caller can distinguish
     *   "matches an intercepted prefix but probe said HTTP works" from
     *   "URL doesn't match at all", which matters for XHR where we
     *   otherwise would have short-circuited to ``origOpen`` before
     *   knowing the probe outcome.
     * - ``"broken"``: route through the comm bridge.
     * - ``"probing"``: probe result isn't in yet; caller must await
     *   :data:`prefixReady` and re-ask before routing. Guessing here
     *   silently breaks one of Hub / mybinder.
     *
     * 127.0.0.1 / localhost matches always return ``"broken"`` because
     * the frontend can't reach those ports directly -- the whole
     * reason those ports are in :data:`interceptedPorts` is that comm
     * is the only route that works.
     *
     * @param {unknown} url
     * @returns {{ port: number, pathAndQuery: string, prefix: string | null, status: "probing" | "working" | "broken" } | null}
     */
    function interceptMatch(url) {
        if (typeof url !== "string" || url.length === 0) return null;
        if (url[0] === "?") return null;
        /** @type {URL} */
        let parsed;
        try {
            parsed = new URL(url, window.location.href);
        } catch (_) {
            return null;
        }
        // 127.0.0.1 / localhost — the path for frontends that can't
        // reach jupyter-server (VS Code webview, Colab, etc.) or for
        // bare TileClient users who never wired up a proxy prefix.
        if (parsed.hostname === "127.0.0.1" || parsed.hostname === "localhost") {
            if (!parsed.port) return null;
            if (!interceptedPorts.has(parsed.port)) return null;
            return {
                port: Number(parsed.port),
                pathAndQuery: parsed.pathname + parsed.search,
                prefix: null,
                status: "broken",
            };
        }
        // Same-origin prefix match — only relevant if the URL lives on
        // the jupyter-server origin that served this page. Cross-origin
        // URLs that happen to start with a registered prefix string are
        // unrelated and must pass through.
        if (prefixToPort.size === 0) return null;
        if (parsed.origin !== window.location.origin) return null;
        for (const [prefix, port] of prefixToPort) {
            if (parsed.pathname !== prefix && !parsed.pathname.startsWith(`${prefix}/`)) {
                continue;
            }
            /** @type {"probing" | "working" | "broken"} */
            const status = prefixStatus.get(prefix) || "broken";
            const rest = parsed.pathname.slice(prefix.length) + parsed.search;
            return {
                port,
                pathAndQuery: rest || "/",
                prefix,
                status,
            };
        }
        return null;
    }

    /**
     * Probe ``<prefix>/__probe__`` on the jupyter-server origin to
     * learn whether :func:`setup_proxy_handler` is mounted on the
     * page's server. The probe endpoint answers ``204`` when the
     * extension is loaded and ``404`` when it isn't, giving the
     * interceptor a reliable signal without forwarding upstream.
     *
     * Runs once per prefix; cached status drives matching decisions in
     * :func:`interceptMatch`. Uses :data:`origFetch` so the probe
     * doesn't recurse back through the patched ``window.fetch``.
     *
     * @param {string} prefix normalized prefix (no trailing slash)
     */
    function probePrefix(prefix) {
        if (prefixStatus.has(prefix)) return;
        prefixStatus.set(prefix, "probing");
        /** @type {(value?: void) => void} */
        let markReady = () => {};
        const ready = /** @type {Promise<void>} */ (
            new Promise((resolveReady) => {
                markReady = resolveReady;
            })
        );
        prefixReady.set(prefix, ready);
        const fetcher = origFetch || (typeof fetch === "function" ? fetch : null);
        if (!fetcher) {
            // No fetch at all (exotic host): treat as broken so the
            // comm bridge still has a shot via XHR / <img> interceptors.
            prefixStatus.set(prefix, "broken");
            markReady();
            return;
        }
        // ``__probe__`` is reserved by ``setup_proxy_handler`` and
        // returns ``204`` when mounted. The trailing segment lets us
        // distinguish "extension loaded" (non-404) from "no handler
        // registered at all" (404 from the outer jupyter-server).
        const probeUrl = `${prefix}/__probe__`;
        const probe = fetcher(probeUrl, {
            method: "HEAD",
            credentials: "include",
            cache: "no-store",
        });
        probe
            .then((resp) => {
                const status = resp.status === 404 ? "broken" : "working";
                prefixStatus.set(prefix, status);
                try {
                    console.info(
                        "[jupyter_loopback] probe",
                        probeUrl,
                        "->",
                        resp.status,
                        status === "broken"
                            ? "(routing this prefix through comm bridge)"
                            : "(HTTP proxy is live)",
                    );
                } catch (_) {
                    /* console may be absent */
                }
                markReady();
            })
            .catch((err) => {
                // Network failures (CORS, offline, etc.) mean we can't
                // reach the HTTP path at all; the comm bridge is the
                // safer bet even if it turns out the extension was
                // registered after all.
                prefixStatus.set(prefix, "broken");
                try {
                    console.warn(
                        "[jupyter_loopback] probe",
                        probeUrl,
                        "failed; falling back to comm bridge.",
                        err,
                    );
                } catch (_) {
                    /* console may be absent */
                }
                markReady();
            });
    }

    /**
     * Install the global prototype patches that reroute intercepted
     * URLs through the comm bridge. Runs once, regardless of how many
     * ports later register via interceptLocalhost.
     */
    function installInterceptors() {
        if (typeof HTMLImageElement !== "undefined") {
            const proto = HTMLImageElement.prototype;
            const desc = Object.getOwnPropertyDescriptor(proto, "src");
            if (desc?.set && desc.get) {
                const origSet = desc.set;
                const origGet = desc.get;
                /**
                 * @param {HTMLImageElement} imgEl
                 * @param {{ port: number, pathAndQuery: string }} decided
                 * @param {string} origValue
                 */
                function routeImgThroughComm(imgEl, decided, origValue) {
                    try {
                        console.debug(
                            "[jupyter_loopback] intercept img.src",
                            origValue,
                            "-> comm bridge",
                        );
                    } catch (_) {
                        /* console may be absent */
                    }
                    resolveUrl(decided.port, decided.pathAndQuery)
                        .then((blobUrl) => {
                            origSet.call(imgEl, blobUrl);
                        })
                        .catch((err) => {
                            // eslint-disable-next-line no-console
                            console.error(
                                "jupyter_loopback.interceptLocalhost: image fetch failed",
                                err,
                            );
                            imgEl.dispatchEvent(new Event("error"));
                        });
                }
                Object.defineProperty(proto, "src", {
                    configurable: true,
                    get() {
                        return origGet.call(this);
                    },
                    set(value) {
                        const match = interceptMatch(value);
                        if (!match || match.status === "working") {
                            origSet.call(this, value);
                            return;
                        }
                        if (match.status === "probing" && match.prefix) {
                            const ready = prefixReady.get(match.prefix);
                            if (ready) {
                                ready.then(() => {
                                    const decided = interceptMatch(value);
                                    if (!decided || decided.status === "working") {
                                        origSet.call(this, value);
                                    } else {
                                        routeImgThroughComm(this, decided, value);
                                    }
                                });
                                return;
                            }
                        }
                        routeImgThroughComm(this, match, value);
                    },
                });
            }
        }

        if (typeof window.fetch === "function") {
            origFetch = window.fetch.bind(window);
            window.fetch = function patchedFetch(input, init) {
                /** @type {string} */
                let url;
                if (typeof input === "string") {
                    url = input;
                } else if (input instanceof URL) {
                    url = input.href;
                } else if (input && typeof input.url === "string") {
                    url = input.url;
                } else {
                    return /** @type {typeof fetch} */ (origFetch)(input, init);
                }
                const match = interceptMatch(url);
                if (!match || match.status === "working") {
                    return /** @type {typeof fetch} */ (origFetch)(input, init);
                }
                if (match.status === "probing" && match.prefix) {
                    const ready = prefixReady.get(match.prefix);
                    if (ready) {
                        return ready.then(() => {
                            const decided = interceptMatch(url);
                            if (!decided || decided.status === "working") {
                                return /** @type {typeof fetch} */ (origFetch)(input, init);
                            }
                            return loopbackFetch(decided.port, decided.pathAndQuery, init);
                        });
                    }
                }
                return loopbackFetch(match.port, match.pathAndQuery, init);
            };
        }

        if (typeof XMLHttpRequest !== "undefined") {
            const xhrProto = XMLHttpRequest.prototype;
            const origOpen = xhrProto.open;
            const origSend = xhrProto.send;
            /**
             * Per-XHR metadata stashed in a WeakMap so we don't have to
             * pollute the XMLHttpRequest instance with custom fields
             * (which TypeScript's built-in lib.dom types reject).
             * @type {WeakMap<XMLHttpRequest, { match: NonNullable<ReturnType<typeof interceptMatch>>, method: string }>}
             */
            const xhrMeta = new WeakMap();

            /**
             * Native XHR.open is variadic (async, user, password). Typed
             * as (...any[]) because spreading into the native overload
             * set otherwise takes contortions.
             * @this {XMLHttpRequest}
             * @param {...any} args
             */
            xhrProto.open = function patchedOpen(...args) {
                const method = args[0];
                const url = args[1];
                const u = typeof url === "string" ? url : (url?.href ?? "");
                const match = interceptMatch(u);
                // XHR can't defer its decision the way ``img.src`` and
                // ``fetch`` can: callers are allowed to call
                // :meth:`setRequestHeader` between ``open`` and
                // ``send``, which only works once the XHR is in
                // ``OPENED`` state. So we collapse the decision to two
                // buckets and bias ``"probing"`` to comm (the correct
                // answer on Hub, a small perf detour on Lab). In
                // practice tile libraries drive ``<img>`` rather than
                // XHR, so this barely matters.
                if (!match || match.status === "working") {
                    return /** @type {any} */ (origOpen).apply(this, args);
                }
                xhrMeta.set(this, { match, method: String(method || "GET") });
                return;
            };

            /** @this {XMLHttpRequest} */
            xhrProto.send = function patchedSend(body) {
                const meta = xhrMeta.get(this);
                if (!meta) return origSend.call(this, body ?? null);
                /** @type {RequestInit} */
                const init = { method: meta.method };
                if (body != null && !(body instanceof Document)) {
                    init.body = /** @type {BodyInit} */ (body);
                }
                loopbackFetch(meta.match.port, meta.match.pathAndQuery, init)
                    .then(async (response) => {
                        const text = await response.text();
                        Object.defineProperties(this, {
                            status: { configurable: true, value: response.status },
                            statusText: { configurable: true, value: response.statusText },
                            responseText: { configurable: true, value: text },
                            readyState: { configurable: true, value: 4 },
                        });
                        this.dispatchEvent(new Event("readystatechange"));
                        this.dispatchEvent(new Event("load"));
                    })
                    .catch(() => {
                        this.dispatchEvent(new Event("error"));
                    });
            };
        }
    }

    /**
     * Register a loopback port so its URLs get rerouted through the
     * comm bridge. Repeated calls for the same port are a no-op;
     * repeated calls with different ports accumulate.
     *
     * When ``pathPrefix`` is supplied (e.g.
     * ``"/user/alice/mylib-proxy/41029"``), same-origin URLs starting
     * with that prefix are also eligible for interception. The
     * interceptor probes the prefix once against ``<prefix>/__probe__``
     * and only actually routes through comm if the probe comes back
     * ``404`` -- i.e. the server extension isn't loaded on the
     * jupyter-server hosting this page. When the probe reports the
     * proxy is live, the prefix is left alone and HTTP handles tiles
     * directly.
     *
     * @param {number | string} port
     * @param {string | null | undefined} [pathPrefix]
     */
    function interceptLocalhost(port, pathPrefix) {
        interceptedPorts.add(String(port));
        /** @type {string | null} */
        let normalizedPrefix = null;
        if (typeof pathPrefix === "string" && pathPrefix.length > 0) {
            normalizedPrefix = pathPrefix.replace(/\/+$/, "");
            if (normalizedPrefix) {
                prefixToPort.set(normalizedPrefix, Number(port));
            }
        }
        try {
            console.info(
                "[jupyter_loopback] interceptLocalhost(",
                port,
                normalizedPrefix ? `, ${normalizedPrefix}` : "",
                ") registered in",
                location.href,
                "first-install:",
                !interceptorInstalled,
            );
        } catch (_) {
            /* console may be absent */
        }
        if (!interceptorInstalled) {
            installInterceptors();
            interceptorInstalled = true;
        }
        if (normalizedPrefix) probePrefix(normalizedPrefix);
    }

    /** @type {LoopbackAPI} */
    const api = {
        registerBridge(bridge) {
            bridges.push(bridge);
        },
        removeBridge(bridge) {
            const i = bridges.indexOf(bridge);
            if (i >= 0) bridges.splice(i, 1);
        },
        _isActiveBridge(bridge) {
            return newestAliveBridge() === bridge;
        },
        _resolve: resolve,
        request,
        fetch: loopbackFetch,
        resolveUrl,
        openWebSocket,
        interceptLocalhost,
    };
    w.__jupyter_loopback__ = api;
    return api;
}

/**
 * anywidget render entry point. Installs the global API and registers
 * this widget's comm as a fresh bridge.
 *
 * @param {{ model: AnyWidgetModel }} ctx
 * @returns {() => void}
 */
function render({ model }) {
    try {
        console.info("[jupyter_loopback] widget render in", location.href);
    } catch (_) {
        /* console may be absent */
    }
    const maybeApi = ensureGlobal();
    if (!maybeApi) return () => {};
    /** @type {LoopbackAPI} */
    const api = maybeApi;

    /** @type {Bridge} */
    const bridge = {
        alive: () => true,
        send: (msg, buffers) => model.send(msg, undefined, buffers || []),
    };
    api.registerBridge(bridge);

    /**
     * Dispatch incoming comm messages to the global resolver. When the
     * widget is rendered in more than one output (e.g. the user re-ran
     * the ``enable_comm_bridge`` cell), every live view's comm fires
     * ``msg:custom`` for every kernel-side ``self.send``. Letting each
     * view call ``_resolve`` would re-fire WebSocket events and
     * multi-resolve already-answered fetch responses. The ``send`` path
     * already picks "newest alive bridge" to avoid duplicate outbound
     * messages; gate inbound dispatch on the same criterion so incoming
     * events are delivered exactly once regardless of view count.
     * @param {any} msg
     * @param {ArrayBuffer[]} buffers
     */
    function onMsg(msg, buffers) {
        if (!api._isActiveBridge(bridge)) return;
        api._resolve(msg, buffers || []);
    }
    model.on("msg:custom", onMsg);

    // Install interceptors for any ports registered on the kernel side.
    // This is the robust path for frontends where HTML <script> tags
    // don't run (VS Code's notebook renderer sometimes sanitizes them):
    // the widget JS always runs, so we install here from the synced
    // ``intercepted_ports`` / ``intercepted_prefixes`` state. Changes
    // propagate via the traitlets sync, so subsequent
    // ``intercept_localhost`` calls also land.
    function applyIntercepts() {
        /** @type {any} */
        const rawPorts = model.get("intercepted_ports");
        /** @type {any} */
        const rawPrefixes = model.get("intercepted_prefixes");
        /** @type {Record<string, string>} */
        const prefixes = rawPrefixes && typeof rawPrefixes === "object" ? rawPrefixes : {};
        if (Array.isArray(rawPorts)) {
            for (const p of rawPorts) {
                if (typeof p !== "number" && typeof p !== "string") continue;
                const key = String(p);
                const prefix = prefixes[key];
                api.interceptLocalhost(p, typeof prefix === "string" ? prefix : null);
            }
        }
        // Handle prefixes that arrived before (or without) a
        // corresponding port entry, e.g. a pure ``add_intercepted_prefix``
        // call (unlikely but possible if a library uses the bridge
        // directly).
        for (const [key, prefix] of Object.entries(prefixes)) {
            if (typeof prefix !== "string") continue;
            api.interceptLocalhost(Number(key), prefix);
        }
    }
    applyIntercepts();
    model.on("change:intercepted_ports", applyIntercepts);
    model.on("change:intercepted_prefixes", applyIntercepts);

    return () => {
        model.off("msg:custom", onMsg);
        model.off("change:intercepted_ports", applyIntercepts);
        model.off("change:intercepted_prefixes", applyIntercepts);
        bridge.alive = () => false;
        api.removeBridge(bridge);
    };
}

export default { render };
