// jupyter-loopback comm bridge (browser half).
//
// When rendered, this widget installs window.__jupyter_loopback__,
// a global request/response client that any page-level script can use
// to talk to the kernel over the notebook's comm websocket. The API is:
//
//   await window.__jupyter_loopback__.request(
//       namespace,      // string, e.g. "mylib"
//       kind,           // string, e.g. "get_tile"
//       data,           // JSON-serializable object
//       buffers,        // optional array of ArrayBuffer / TypedArray
//   )
//   // -> { status: "ok" | "error", data, buffers, error? }
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
 * @typedef {Object} LoopbackAPI
 * @property {(bridge: Bridge) => void} registerBridge
 * @property {(bridge: Bridge) => void} removeBridge
 * @property {(msg: any, buffers: ArrayBuffer[]) => void} _resolve
 * @property {(
 *     namespace: string,
 *     kind: string,
 *     data?: any,
 *     buffers?: ArrayBuffer[],
 * ) => Promise<{ status: string, data: any, buffers: ArrayBuffer[] }>} request
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
 * Lazily create and return the window-scoped jupyter-loopback API.
 * @returns {LoopbackAPI | null}
 */
function ensureGlobal() {
    if (typeof window === "undefined") return null;
    /** @type {any} */
    const w = window;
    if (w.__jupyter_loopback__) return w.__jupyter_loopback__;

    /** @type {Bridge[]} */
    const bridges = [];
    /** @type {Map<string, PendingEntry>} */
    const pending = new Map();

    /**
     * @param {any} req
     * @param {ArrayBuffer[]} buffers
     * @returns {boolean}
     */
    function send(req, buffers) {
        // Prefer the newest bridge that still has a connected comm.
        for (let i = bridges.length - 1; i >= 0; i--) {
            const bridge = bridges[i];
            if (bridge.alive()) {
                bridge.send(req, buffers || []);
                return true;
            }
        }
        return false;
    }

    /**
     * @param {any} msg
     * @param {ArrayBuffer[]} buffers
     */
    function resolve(msg, buffers) {
        if (!msg || msg.type !== "response" || !msg.id) return;
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
    }

    /**
     * @param {string} namespace
     * @param {string} kind
     * @param {any} [data]
     * @param {ArrayBuffer[]} [buffers]
     * @returns {Promise<{ status: string, data: any, buffers: ArrayBuffer[] }>}
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

    /** @type {LoopbackAPI} */
    const api = {
        registerBridge(bridge) {
            bridges.push(bridge);
        },
        removeBridge(bridge) {
            const i = bridges.indexOf(bridge);
            if (i >= 0) bridges.splice(i, 1);
        },
        _resolve: resolve,
        request,
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
     * @param {any} msg
     * @param {ArrayBuffer[]} buffers
     */
    function onMsg(msg, buffers) {
        api._resolve(msg, buffers || []);
    }
    model.on("msg:custom", onMsg);

    return () => {
        model.off("msg:custom", onMsg);
        bridge.alive = () => false;
        api.removeBridge(bridge);
    };
}

export default { render };
