// Standalone Node smoke test for the A2A relay in snapgent-extension/background.js
// (run: node snapgent-extension/test-a2a.js). Not shipped, no npm.
//
// background.js is a browser service worker, so it is evaluated here against a
// minimal `chrome` + `WebSocket` stub - just enough surface for the relay path:
//   bridge "submit_prompt" -> tabs.sendMessage "zs-a2a-prompt"
//   runtime "a2a_result"   -> socket frame "prompt_result"
// Anything the relay does not touch stays undefined, so a future change that
// needs more browser API fails loudly here instead of silently passing.
//
// Scope: covers the background.js half of the relay only. The content script
// (core/main.js) and its real DOM loop are not exercised here.
const fs = require("fs");

// ── Stub WebSocket ──────────────────────────────────────────────────────────
// Captures the single instance background.js creates; the test fires onopen()
// by hand to drive `connected` true. Every outgoing frame is recorded parsed.
const frames = [];
class StubWebSocket {
  constructor(url) {
    this.url = url;
    this.readyState = StubWebSocket.CONNECTING;
    this.onopen = null;
    this.onmessage = null;
    this.onclose = null;
    this.onerror = null;
    StubWebSocket.instance = this;
  }
  send(s) { frames.push(JSON.parse(s)); }
  close() { this.readyState = StubWebSocket.CLOSED; }
}
StubWebSocket.CONNECTING = 0;
StubWebSocket.OPEN = 1;
StubWebSocket.CLOSING = 2;
StubWebSocket.CLOSED = 3;

// ── Stub chrome ─────────────────────────────────────────────────────────────
const tabSends = [];   // {tabId, msg} from chrome.tabs.sendMessage
let onRuntimeMessage = null;
// Error a real tabs.sendMessage hands back on its callback. The relay must
// treat "message port closed" as SUCCESS (the content script replies later by
// runtime message) and only "no receiver" as failure.
let nextSendError = null;
let lastError = null;
const providerTab = { id: 7, active: true };
const chrome = {
  runtime: {
    sendMessage: () => Promise.resolve(),
    onMessage: { addListener: (fn) => { onRuntimeMessage = fn; } },
    onStartup: { addListener: () => {} },
    onInstalled: { addListener: () => {} },
    get lastError() { return lastError; },
  },
  // background.js reads the persisted pairing state before its first dial.
  storage: {
    local: {
      get: (key, cb) => {
        const values = typeof key === "string" ? { [key]: undefined } : {};
        if (cb) cb(values);
        return Promise.resolve(values);
      },
      set: (_obj, cb) => { if (cb) cb(); return Promise.resolve(); },
      remove: (_key, cb) => { if (cb) cb(); return Promise.resolve(); },
    },
  },
  tabs: {
    query: (_info, cb) => {
      const tabs = [providerTab];
      if (cb) cb(tabs);
      return Promise.resolve(tabs);
    },
    sendMessage: (tabId, msg, cb) => {
      tabSends.push({ tabId, msg });
      if (cb) { lastError = nextSendError; nextSendError = null; cb(); }
      return Promise.resolve();
    },
  },
};

// ── Load background.js against the stubs ────────────────────────────────────
// background.js pulls core/pairing.js in with importScripts. The stub exposes
// it on the global, which is where the worker's bare `ZSPairing` resolves.
// new Function is safe here for the same reason as test-pairing.js: the
// evaluated string is this repo's own source read from disk, `relpath` is a
// literal, and no part of it is user input.
const importScripts = (relpath) => {
  globalThis.ZSPairing = new Function(
    fs.readFileSync(__dirname + "/" + relpath, "utf8") + "; return ZSPairing;"
  )();
};

const src = fs.readFileSync(__dirname + "/background.js", "utf8");
const api = new Function("chrome", "WebSocket", "importScripts",
  src + "; return { handleBridgeMessage, handleSubmitPrompt };"
)(chrome, StubWebSocket, importScripts);

const results = [];
const ok = (name, cond) => { results.push(cond); console.log((cond ? "PASS" : "FAIL") + "  " + name); };
const until = async (cond, ms = 1000) => {
  const t0 = Date.now();
  while (!cond()) {
    if (Date.now() - t0 > ms) return false;
    await new Promise((r) => setTimeout(r, 10));
  }
  return true;
};

(async () => {
  // Load fires connect(), which awaits the stored pairing token before it dials,
  // so the socket only exists a microtask later.
  await until(() => !!StubWebSocket.instance);
  // Drive the handshake so the relay can send frames. A real WebSocket is
  // already OPEN when onopen fires; mirror that here or send() would treat the
  // socket as not yet open and buffer forever.
  StubWebSocket.instance.readyState = StubWebSocket.OPEN;
  StubWebSocket.instance.onopen();
  ok("socket connected after onopen", StubWebSocket.instance.readyState === StubWebSocket.OPEN);
  ok("runtime listener registered", typeof onRuntimeMessage === "function");

  // ── bridge "submit_prompt" -> tab "zs-a2a-prompt" ─────────────────────────
  // Simulate Chrome's port-closed callback: the content script does NOT respond
  // on the channel (it replies later by runtime message). The relay must NOT
  // report a failure here, or it double-replies and fails a healthy task.
  nextSendError = { message: "The message port closed before a response was received." };
  api.handleBridgeMessage({ type: "submit_prompt", id: "p1", prompt: "hi" });
  await until(() => tabSends.some((s) => s.msg.type === "zs-a2a-prompt"));
  const relayed = tabSends.filter((s) => s.msg.type === "zs-a2a-prompt");
  ok("submit_prompt relays exactly one zs-a2a-prompt", relayed.length === 1);
  ok("relayed prompt carries the prompt text", relayed[0] && relayed[0].msg.prompt === "hi");
  ok("relayed prompt carries promptId = bridge id", relayed[0] && relayed[0].msg.promptId === "p1");
  ok("relayed prompt targets the provider tab", relayed[0] && relayed[0].tabId === providerTab.id);
  await new Promise((r) => setTimeout(r, 50));
  ok("port-closed callback does not send a failure", frames.filter((f) => f.type === "prompt_result").length === 0);

  // ── runtime "a2a_result" -> socket frame "prompt_result" ──────────────────
  onRuntimeMessage({ type: "a2a_result", promptId: "p1", ok: true, text: "hello" }, {}, () => {});
  await until(() => frames.some((f) => f.type === "prompt_result"));
  const result = frames.find((f) => f.type === "prompt_result");
  ok("a2a_result forwards one prompt_result frame", frames.filter((f) => f.type === "prompt_result").length === 1);
  ok("prompt_result carries prompt_id", result && result.prompt_id === "p1");
  ok("prompt_result carries ok", result && result.ok === true);
  ok("prompt_result carries the text", result && result.text === "hello");
  ok("prompt_result carries an empty error on success", result && result.error === "");

  // ── no receiving end -> immediate failure ────────────────────────────────
  nextSendError = { message: "Could not establish connection. Receiving end does not exist." };
  api.handleBridgeMessage({ type: "submit_prompt", id: "p2", prompt: "hi again" });
  await until(() => frames.some((f) => f.type === "prompt_result" && f.prompt_id === "p2"));
  const missing = frames.find((f) => f.type === "prompt_result" && f.prompt_id === "p2");
  ok("missing content script reports failure", missing && missing.ok === false);
  ok("missing content script sends exactly one failure",
     frames.filter((f) => f.type === "prompt_result" && f.prompt_id === "p2").length === 1);

  // ── a2a_result failure path ───────────────────────────────────────────────
  onRuntimeMessage({ type: "a2a_result", promptId: "p3", ok: false, error: "boom" }, {}, () => {});
  await until(() => frames.some((f) => f.type === "prompt_result" && f.prompt_id === "p3"));
  const failed = frames.find((f) => f.type === "prompt_result" && f.prompt_id === "p3");
  ok("failed a2a_result forwards ok:false", failed && failed.ok === false);
  ok("failed a2a_result forwards the error", failed && failed.error === "boom");

  // ── prompt_ack is swallowed (fire-and-forget relay) ───────────────────────
  const before = frames.length;
  api.handleBridgeMessage({ type: "prompt_ack", id: 5 });
  await new Promise((r) => setTimeout(r, 50));
  ok("prompt_ack sends nothing back", frames.length === before);

  const failedCount = results.filter((r) => !r).length;
  console.log("\n" + (failedCount ? "FAIL" : "PASS") + "  " + (results.length - failedCount) + "/" + results.length + " checks passed");
  process.exit(failedCount ? 1 : 0);
})().catch((error) => {
  console.error(error);
  process.exit(1);
});
