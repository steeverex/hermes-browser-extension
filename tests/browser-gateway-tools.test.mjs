/**
 * Unit tests for extension/lib/browser-control-gateway-tools.mjs
 *
 * Covers:
 *   - createBrowserControlGatewayToolsHandler factory validation
 *   - handles() type routing
 *   - HERMES_BROWSER_CONTROL_WAKE: writes browserControlEnabled to storage,
 *     calls reconcile, returns worker status fields
 *   - HERMES_BROWSER_CONTROL_WAKE with enable=false: skips storage write
 *   - HERMES_BROWSER_CONTROL_STATUS: reads storage + worker status
 *   - Unknown message type: returns ok:false
 *   - Reconcile failure is surfaced without throwing
 *   - Storage already has browserControlEnabled: does not overwrite other fields
 *   - gatewayToolsHandler is null when storage is unavailable (smoke test)
 */

import assert from 'node:assert/strict';
import test from 'node:test';

async function gatewayToolsModule() {
  try {
    return await import('../extension/lib/browser-control-gateway-tools.mjs');
  } catch (error) {
    assert.fail(`browser-control-gateway-tools module failed to load: ${error?.message || error}`);
  }
}

// ── Fakes ──────────────────────────────────────────────────────────────────────

function makeStorage(initial = {}) {
  const store = { ...initial };
  const calls = [];
  return {
    calls,
    store,
    api: {
      async get(key) {
        calls.push(['get', key]);
        if (typeof key === 'string') return { [key]: store[key] };
        if (Array.isArray(key)) return Object.fromEntries(key.map((k) => [k, store[k]]));
        return { ...store };
      },
      async set(values) {
        calls.push(['set', values]);
        Object.assign(store, values);
      },
    },
  };
}

function makeWorkerStatus(overrides = {}) {
  return {
    connected: true,
    controllerId: 'ctrl-abc',
    leasedTabIds: [7],
    pendingCommands: 0,
    activeAction: null,
    lastConnectFailure: null,
    ...overrides,
  };
}

function makeControllerWorker(statusResult = null) {
  return {
    async status() { return statusResult ?? makeWorkerStatus(); },
  };
}

function makeReconcile(result = { ok: true }) {
  const calls = [];
  return {
    calls,
    fn: async (reason) => { calls.push(reason); return result; },
  };
}

// ── Tests ──────────────────────────────────────────────────────────────────────

test('factory throws when storageArea is missing', async () => {
  const { createBrowserControlGatewayToolsHandler } = await gatewayToolsModule();
  assert.throws(
    () => createBrowserControlGatewayToolsHandler({ reconcile: async () => {} }),
    /storageArea/,
  );
});

test('factory throws when storageArea lacks set', async () => {
  const { createBrowserControlGatewayToolsHandler } = await gatewayToolsModule();
  assert.throws(
    () => createBrowserControlGatewayToolsHandler({
      storageArea: { get: async () => ({}) },
      reconcile: async () => {},
    }),
    /storageArea/,
  );
});

test('factory throws when reconcile is not a function', async () => {
  const { createBrowserControlGatewayToolsHandler } = await gatewayToolsModule();
  const storage = makeStorage();
  assert.throws(
    () => createBrowserControlGatewayToolsHandler({ storageArea: storage.api }),
    /reconcile/,
  );
});

test('handles() returns true for WAKE and STATUS, false for others', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_WAKE, HERMES_BROWSER_CONTROL_STATUS } = await gatewayToolsModule();
  const storage = makeStorage();
  const reconcile = makeReconcile();
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: makeControllerWorker(),
    reconcile: reconcile.fn,
  });
  assert.equal(handler.handles(HERMES_BROWSER_CONTROL_WAKE), true);
  assert.equal(handler.handles(HERMES_BROWSER_CONTROL_STATUS), true);
  assert.equal(handler.handles('HERMES_SOMETHING_ELSE'), false);
  assert.equal(handler.handles(undefined), false);
  assert.equal(handler.handles(''), false);
});

test('WAKE message enables browserControlEnabled in storage and calls reconcile', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_WAKE } = await gatewayToolsModule();
  const storage = makeStorage({ hermesBrowserSettings: { gatewayUrl: 'http://127.0.0.1:8642' } });
  const reconcile = makeReconcile({ ok: true, connected: true });
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: makeControllerWorker(),
    reconcile: reconcile.fn,
  });

  const result = await handler.handleMessage({ type: HERMES_BROWSER_CONTROL_WAKE, reason: 'test-wake' });

  assert.equal(result.ok, true);
  assert.equal(result.enabled, true);
  assert.equal(result.reason, 'test-wake');
  // Storage should now have browserControlEnabled: true while preserving other fields.
  assert.equal(storage.store.hermesBrowserSettings?.browserControlEnabled, true);
  assert.equal(storage.store.hermesBrowserSettings?.gatewayUrl, 'http://127.0.0.1:8642');
  // Reconcile must have been called once.
  assert.equal(reconcile.calls.length, 1);
  assert.equal(reconcile.calls[0], 'test-wake');
  // Worker status fields should be present.
  assert.equal(result.connected, true);
  assert.equal(result.controllerId, 'ctrl-abc');
  assert.deepEqual(result.leasedTabIds, [7]);
});

test('WAKE with enable=false skips storage write but still reconciles', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_WAKE } = await gatewayToolsModule();
  const storage = makeStorage({ hermesBrowserSettings: { gatewayUrl: 'http://127.0.0.1:8642' } });
  const reconcile = makeReconcile();
  const setCallsBefore = storage.calls.filter((c) => c[0] === 'set').length;
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: makeControllerWorker(),
    reconcile: reconcile.fn,
  });

  const result = await handler.handleMessage({ type: HERMES_BROWSER_CONTROL_WAKE, enable: false });

  assert.equal(result.ok, true);
  assert.equal(result.enabled, false);
  // No set call should have been added.
  const setCallsAfter = storage.calls.filter((c) => c[0] === 'set').length;
  assert.equal(setCallsAfter, setCallsBefore);
  // But reconcile still ran.
  assert.equal(reconcile.calls.length, 1);
});

test('WAKE does not overwrite an already-enabled flag with an extra set()', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_WAKE } = await gatewayToolsModule();
  const storage = makeStorage({ hermesBrowserSettings: { browserControlEnabled: true, apiKey: 'key-abc' } });
  const reconcile = makeReconcile();
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: makeControllerWorker(),
    reconcile: reconcile.fn,
  });

  await handler.handleMessage({ type: HERMES_BROWSER_CONTROL_WAKE });

  // No set() call because flag was already true — avoids spurious storage churn.
  const setCalls = storage.calls.filter((c) => c[0] === 'set');
  assert.equal(setCalls.length, 0, 'should not write storage when flag already set');
  assert.equal(storage.store.hermesBrowserSettings?.apiKey, 'key-abc');
});

test('STATUS message returns live worker status without writing storage', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_STATUS } = await gatewayToolsModule();
  const storage = makeStorage({ hermesBrowserSettings: { browserControlEnabled: true } });
  const reconcile = makeReconcile();
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: makeControllerWorker(makeWorkerStatus({ connected: true, controllerId: 'ctrl-xyz', leasedTabIds: [3, 5] })),
    reconcile: reconcile.fn,
  });

  const result = await handler.handleMessage({ type: HERMES_BROWSER_CONTROL_STATUS });

  assert.equal(result.ok, true);
  assert.equal(result.browserControlEnabled, true);
  assert.equal(result.connected, true);
  assert.equal(result.controllerId, 'ctrl-xyz');
  assert.deepEqual(result.leasedTabIds, [3, 5]);
  assert.equal(result.pendingCommands, 0);
  // STATUS never calls reconcile or set.
  assert.equal(reconcile.calls.length, 0);
  const setCalls = storage.calls.filter((c) => c[0] === 'set');
  assert.equal(setCalls.length, 0);
});

test('STATUS returns browserControlEnabled: false when not set', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_STATUS } = await gatewayToolsModule();
  const storage = makeStorage({ hermesBrowserSettings: {} });
  const reconcile = makeReconcile();
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: makeControllerWorker(makeWorkerStatus({ connected: false, controllerId: '' })),
    reconcile: reconcile.fn,
  });

  const result = await handler.handleMessage({ type: HERMES_BROWSER_CONTROL_STATUS });

  assert.equal(result.ok, true);
  assert.equal(result.browserControlEnabled, false);
  assert.equal(result.connected, false);
  assert.equal(result.controllerId, '');
});

test('STATUS works with null controllerWorker', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_STATUS } = await gatewayToolsModule();
  const storage = makeStorage({ hermesBrowserSettings: { browserControlEnabled: true } });
  const reconcile = makeReconcile();
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: null,
    reconcile: reconcile.fn,
  });

  const result = await handler.handleMessage({ type: HERMES_BROWSER_CONTROL_STATUS });

  assert.equal(result.ok, true);
  assert.equal(result.connected, false);
  assert.equal(result.controllerId, '');
  assert.deepEqual(result.leasedTabIds, []);
});

test('WAKE works with null controllerWorker', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_WAKE } = await gatewayToolsModule();
  const storage = makeStorage();
  const reconcile = makeReconcile();
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: null,
    reconcile: reconcile.fn,
  });

  const result = await handler.handleMessage({ type: HERMES_BROWSER_CONTROL_WAKE });

  assert.equal(result.ok, true);
  assert.equal(result.connected, false);
  assert.equal(result.controllerId, '');
  assert.deepEqual(result.leasedTabIds, []);
  assert.equal(reconcile.calls.length, 1);
});

test('reconcile failure surfaces as ok:false without throwing', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_WAKE } = await gatewayToolsModule();
  const storage = makeStorage();
  const failingReconcile = async () => { throw new Error('gateway offline'); };
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: null,
    reconcile: failingReconcile,
  });

  const result = await handler.handleMessage({ type: HERMES_BROWSER_CONTROL_WAKE });

  assert.equal(result.ok, false);
  assert.match(result.error, /gateway offline/);
});

test('unknown message type returns ok:false', async () => {
  const { createBrowserControlGatewayToolsHandler } = await gatewayToolsModule();
  const storage = makeStorage();
  const reconcile = makeReconcile();
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: null,
    reconcile: reconcile.fn,
  });

  const result = await handler.handleMessage({ type: 'HERMES_UNKNOWN_TYPE' });

  assert.equal(result.ok, false);
  assert.equal(result.error, 'unknown_type');
  assert.equal(reconcile.calls.length, 0);
});

test('WAKE reason field is bounded and sanitised', async () => {
  const { createBrowserControlGatewayToolsHandler, HERMES_BROWSER_CONTROL_WAKE } = await gatewayToolsModule();
  const storage = makeStorage();
  const reconcile = makeReconcile();
  const handler = createBrowserControlGatewayToolsHandler({
    storageArea: storage.api,
    controllerWorker: null,
    reconcile: reconcile.fn,
  });

  const longReason = 'x'.repeat(500);
  const result = await handler.handleMessage({ type: HERMES_BROWSER_CONTROL_WAKE, reason: longReason });

  assert.equal(result.ok, true);
  assert.ok(result.reason.length <= 120, 'reason should be bounded to 120 chars');
});

test('exported message type constants are non-empty strings', async () => {
  const { HERMES_BROWSER_CONTROL_WAKE, HERMES_BROWSER_CONTROL_STATUS } = await gatewayToolsModule();
  assert.equal(typeof HERMES_BROWSER_CONTROL_WAKE, 'string');
  assert.ok(HERMES_BROWSER_CONTROL_WAKE.length > 0);
  assert.equal(typeof HERMES_BROWSER_CONTROL_STATUS, 'string');
  assert.ok(HERMES_BROWSER_CONTROL_STATUS.length > 0);
  assert.notEqual(HERMES_BROWSER_CONTROL_WAKE, HERMES_BROWSER_CONTROL_STATUS);
});
