/**
 * Browser-control gateway-tools message handler.
 *
 * Exposes a single message type — HERMES_BROWSER_CONTROL_WAKE — that
 * Hermes companion-plugin tools (or the side panel) can send to ensure:
 *
 *   1. The controller worker is awake (MV3 service workers get killed).
 *   2. The settings have browserControlEnabled = true written to storage
 *      so the controller worker's executor is live on next reconcile.
 *   3. A reconcile is triggered immediately so the gateway WebSocket
 *      re-registers before the first browser command arrives.
 *
 * This is the extension-side complement to gateway_bridge.py.  When
 * Hermes calls browser_controller_status (or any other browser-control
 * tool), it sends this message first so the service worker is definitely
 * running before the gateway bridge tries to POST /v1/browser-control/register.
 *
 * MV3 keepalive strategy
 * ----------------------
 * The background service worker can be suspended at any time.  The
 * controller worker already sets CONTROLLER_HEARTBEAT_ALARM (1 min) and
 * CONTROLLER_RECONCILE_ALARM (5 min) via chrome.alarms, which re-wake it
 * periodically.  This module additionally:
 *   - Responds to HERMES_BROWSER_CONTROL_WAKE with a fast reconcile so
 *     the first tool call after a SW sleep still works in < 1 s.
 *   - Writes browserControlEnabled to chrome.storage.local (persists
 *     across SW kills) so the setting survives restarts.
 *
 * Security
 * --------
 * - Only extension pages (sidepanel, app) and the background SW itself
 *   can send extension messages.  Content scripts that receive injected
 *   messages from web pages cannot impersonate an extension sender.
 * - We never read arbitrary fields from the message body; only the
 *   well-typed `enable` boolean is accepted.
 * - The `reason` field is bounded and sanitised before logging.
 */

export const HERMES_BROWSER_CONTROL_WAKE = 'HERMES_BROWSER_CONTROL_WAKE';
export const HERMES_BROWSER_CONTROL_STATUS = 'HERMES_BROWSER_CONTROL_STATUS';

const MAX_REASON_LEN = 120;

function compact(value, limit = MAX_REASON_LEN) {
  return String(value || '').replace(/\s+/g, ' ').trim().slice(0, limit);
}

/**
 * Create the gateway-tools message handler.
 *
 * @param {object} options
 * @param {object} options.storageArea      - chrome.storage.local (or compatible)
 * @param {object|null} options.controllerWorker - the controllerWorker instance (may be null)
 * @param {Function} options.reconcile      - async (reason) => reconcileControllerWorker(reason)
 * @returns {{ handles: (type: string) => boolean, handleMessage: (message, sender) => Promise }}
 */
export function createBrowserControlGatewayToolsHandler({
  storageArea,
  controllerWorker,
  reconcile,
} = {}) {
  if (!storageArea?.get || !storageArea?.set) {
    throw new TypeError('storageArea with get/set is required.');
  }
  if (typeof reconcile !== 'function') {
    throw new TypeError('reconcile must be a function.');
  }

  const HANDLED_TYPES = new Set([
    HERMES_BROWSER_CONTROL_WAKE,
    HERMES_BROWSER_CONTROL_STATUS,
  ]);

  function handles(type) {
    return HANDLED_TYPES.has(type);
  }

  /**
   * Enable browser control in persisted settings.
   * Writes browserControlEnabled: true into hermesBrowserSettings so the
   * controller service worker executor activates on next reconcile.
   * Idempotent — safe to call on every tool invocation.
   */
  async function enableBrowserControlInStorage() {
    const stored = await storageArea.get('hermesBrowserSettings');
    const current = stored?.hermesBrowserSettings && typeof stored.hermesBrowserSettings === 'object'
      ? stored.hermesBrowserSettings
      : {};
    if (current.browserControlEnabled === true) return; // already set
    await storageArea.set({
      hermesBrowserSettings: {
        ...current,
        browserControlEnabled: true,
      },
    });
  }

  async function handleWake(message) {
    const reason = compact(message?.reason || 'gateway-tool-wake');
    const enable = message?.enable !== false; // default true

    try {
      if (enable) {
        await enableBrowserControlInStorage();
      }
      // Kick the controller worker reconcile so the WS re-registers promptly.
      const reconcileResult = await reconcile(reason);
      const workerStatus = controllerWorker ? (await controllerWorker.status?.() ?? null) : null;
      return {
        ok: true,
        enabled: enable,
        reason,
        reconcile: reconcileResult ?? { ok: true },
        connected: Boolean(workerStatus?.connected),
        controllerId: String(workerStatus?.controllerId || ''),
        leasedTabIds: Array.isArray(workerStatus?.leasedTabIds) ? workerStatus.leasedTabIds : [],
      };
    } catch (error) {
      console.warn('[Hermes Browser] Gateway tools wake failed:', error);
      return {
        ok: false,
        reason,
        error: compact(error?.message || String(error)),
      };
    }
  }

  async function handleStatus() {
    try {
      const stored = await storageArea.get('hermesBrowserSettings');
      const settings = stored?.hermesBrowserSettings || {};
      const workerStatus = controllerWorker ? (await controllerWorker.status?.() ?? null) : null;
      return {
        ok: true,
        browserControlEnabled: settings.browserControlEnabled === true,
        connected: Boolean(workerStatus?.connected),
        controllerId: String(workerStatus?.controllerId || ''),
        leasedTabIds: Array.isArray(workerStatus?.leasedTabIds) ? workerStatus.leasedTabIds : [],
        pendingCommands: Number(workerStatus?.pendingCommands ?? 0),
        activeAction: workerStatus?.activeAction ?? null,
        lastConnectFailure: workerStatus?.lastConnectFailure ?? null,
      };
    } catch (error) {
      console.warn('[Hermes Browser] Gateway tools status failed:', error);
      return {
        ok: false,
        error: compact(error?.message || String(error)),
      };
    }
  }

  async function handleMessage(message) {
    if (message?.type === HERMES_BROWSER_CONTROL_WAKE) return handleWake(message);
    if (message?.type === HERMES_BROWSER_CONTROL_STATUS) return handleStatus();
    return { ok: false, error: 'unknown_type' };
  }

  return Object.freeze({ handles, handleMessage });
}
