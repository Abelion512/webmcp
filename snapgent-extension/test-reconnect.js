// Standalone Node test for the reconnect policy in snapgent-extension/background.js
// (run: node snapgent-extension/test-reconnect.js). Not shipped, no npm.
//
// The bridge refuses a client with a missing or wrong token by closing with 1008.
// The extension used to redial unconditionally, and the content script's 5 s
// `status` poll dialled too whenever the socket was down, so an install with no
// stored token hit the bridge ~71 times a minute forever ("rejected client with a
// missing or invalid bridge token" in the bridge log). This test pins the policy:
// park on 1008, dial again on any other close, and unpark when the popup saves a
// token and asks for a reconnect.
//
// background.js is a browser service worker, so it is evaluated against a minimal
// `chrome` + `WebSocket` + `importScripts` stub - just enough surface for the
// socket lifecycle. Anything else stays undefined, so a future change that needs
// more browser API fails loudly here instead of silently passing.
const fs = require("fs");

// Every socket background.js opens lands here, so "did it redial?" is a count.
const created = [];
class StubWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = StubWebSocket.CONNECTING;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    created.push(this);
  }
  send() {}
  close() { this.readyState = StubWebSocket.CLOSED; }
}
StubWebSocket.CONNECTING = 0;
StubWebSocket.OPEN = 1;
StubWebSocket.CLOSING = 2;
StubWebSocket.CLOSED = 3;

const store = {};
let onRuntimeMessage = null;
const chrome = {
  runtime: {
    sendMessage: () => Promise.resolve(),
    onMessage: { addListener: (fn) => { onRuntimeMessage = fn; } },
    onStartup: { addListener: () => {} },
    onInstalled: { addListener: () => {} },
    get lastError() { return null; },
  },
  storage: {
    local: {
      get: (key, cb) => {
        const values = typeof key === "string" ? { [key]: store[key] } : { ...store };
        if (cb) cb(values);
        return Promise.resolve(values);
      },
      set: (obj, cb) => {
        Object.assign(store, obj);
        if (cb) cb();
        return Promise.resolve();
      },
      remove: (key, cb) => {
        delete store[key];
        if (cb) cb();
        return Promise.resolve();
      },
    },
  },
  tabs: {
    query: (_info, cb) => { if (cb) cb([]); return Promise.resolve([]); },
    sendMessage: () => Promise.resolve(),
  },
};

// background.js pulls core/pairing.js in with importScripts. The stub exposes it
// on the global, which is where the service worker's bare `ZSPairing` resolves.
// new Function is safe here for the same reason as test-pairing.js: the evaluated
// string is this repo's own source read from disk, and `relpath` is a literal in
// this file - never user input.
const importScripts = (relpath) => {
  globalThis.ZSPairing = new Function(
    fs.readFileSync(__dirname + "/" + relpath, "utf8") + "; return ZSPairing;"
  )();
};

const src = fs.readFileSync(__dirname + "/background.js", "utf8");
new Function("chrome", "WebSocket", "importScripts", src)(chrome, StubWebSocket, importScripts);

const ok = (name, cond) => {
  console.log((cond ? "PASS" : "FAIL") + "  " + name);
  if (!cond) process.exitCode = 1;
};
const wait = (ms) => new Promise((r) => setTimeout(r, ms));

const send = (msg) => new Promise((resolve) => onRuntimeMessage(msg, {}, resolve));

(async () => {
  await wait(50); // let the storage read behind the first dial land
  ok("dials once on startup", created.length === 1);

  // ── the bridge refuses our token ─────────────────────────────────────────
  const first = created[0];
  first.readyState = StubWebSocket.OPEN;
  first.onopen();
  ok("a successful open is not armed as a refusal", !!store.bridgeAuthRequired === false);

  first.readyState = StubWebSocket.CLOSED; // a real socket is CLOSED before onclose
  first.onclose({ code: 1008 });
  ok("the pairing state is persisted for the popup", store.bridgeAuthRequired === true);

  const status = await send({ type: "status" });
  ok("status reports the refusal to the popup", status && status.authRequired === true);
  ok("the 5 s status poll does not redial while parked", created.length === 1);

  await wait(1600); // RECONNECT_MIN is 1 s: a redial would have happened by now
  ok("nothing redials for 1.6 s after a 1008 close", created.length === 1);

  // ── the popup saves a token and asks for a reconnect ─────────────────────
  await send({ type: "reconnect" });
  await wait(50);
  ok("saving a token dials again", created.length === 2);
  ok("saving a token clears the parked state", store.bridgeAuthRequired === undefined);

  // ── a transient drop still heals itself ─────────────────────────────────
  const second = created[1];
  second.readyState = StubWebSocket.OPEN;
  second.onopen();
  second.readyState = StubWebSocket.CLOSED;
  second.onclose({ code: 1006 });
  await wait(1600);
  ok("a 1006 network drop still redials on its own", created.length === 3);

  // ── a parked worker stays parked across an eviction ─────────────────────
  // Same storage, fresh worker: the state must come back from storage, not from
  // a variable that died with the previous instance.
  created.length = 0;
  store.bridgeAuthRequired = true;
  new Function("chrome", "WebSocket", "importScripts", src)(chrome, StubWebSocket, importScripts);
  await wait(50);
  ok("a worker waking up parked does not dial", created.length === 0);

  // ── a message that lands before the storage read resolves ────────────────
  // Chrome can deliver a queued message (a tab polls `status` every 5 s) on the
  // same wake-up that re-runs this file, so that read is deliberately slow here.
  // Without the gate in connect(), the handler sees the default
  // `authRequired = false` and redials a bridge that already refused us.
  created.length = 0;
  const slowChrome = Object.assign({}, chrome, {
    storage: {
      local: {
        get: (key, cb) => { setTimeout(() => cb({ [key]: store[key] }), 30); return Promise.resolve({}); },
        set: (obj, cb) => { Object.assign(store, obj); if (cb) cb(); return Promise.resolve(); },
        remove: (key, cb) => { delete store[key]; if (cb) cb(); return Promise.resolve(); },
      },
    },
  });
  new Function("chrome", "WebSocket", "importScripts", src)(slowChrome, StubWebSocket, importScripts);
  onRuntimeMessage({ type: "status" }, {}, () => {});
  await wait(150);
  ok("a status poll racing the storage read does not dial", created.length === 0);

  process.exit(process.exitCode || 0); // the heartbeat interval would keep us alive
})();
