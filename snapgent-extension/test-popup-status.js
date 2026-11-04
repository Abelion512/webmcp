// Quick Node smoke test for popup.js bridge-status rendering (run: node test-popup-status.js).
// Not shipped. Evaluates popup.js against a minimal stub DOM + chrome stub and
// checks render() output for: diagnostic snapshot, degraded (old bridge),
// error truncation, and offline state. textContent only - no innerHTML path.
const fs = require("fs");

const elements = {};
const makeEl = (id) => ({
  id, textContent: "", className: "", text: "",
  addEventListener: () => {},
});
["state", "tools", "servers", "dot", "ver", "reconnect", "restart", "settings"]
  .forEach((id) => { elements[id] = makeEl(id); });

global.document = { getElementById: (id) => elements[id] || null };
global.chrome = {
  runtime: {
    getManifest: () => ({ version: "3.9.4" }),
    sendMessage: () => {},
    onMessage: { addListener: () => {} },
  },
  tabs: { query: () => {}, create: () => {}, update: () => {} },
};
global.setInterval = () => 0; // popup schedules a 2s refresh; ignore in test

const popupSrc = fs.readFileSync(__dirname + "/popup.js", "utf8");
// popup.js has no exports; evaluate it and capture render() from closure scope.
// Safe here: the evaluated string is this repo's own popup.js read from disk
// (same pattern as test-parser.js/test-chatgpt.js), never user input.
const render = new Function(popupSrc + "; return render;")();

const ok = (name, cond) => {
  console.log((cond ? "PASS" : "FAIL") + "  " + name);
  if (!cond) process.exitCode = 1;
};

const base = {
  connected: true, mcpAlive: true, bridgeVersion: "3.9.4", studio: null,
  tools: 28, servers: [{ id: "repo", alive: true, tools: 28 }],
};

// 1. With a diagnostic snapshot: pid, RSS, uptime.
render({ ...base, bridgeStatus: {
  uptime_seconds: 754, clients_connected: 1, tools_available: 28,
  servers: [{ id: "repo", alive: true, ready: true, tools: 28, pid: 1719322,
    error: null, restart_attempts: 0, next_restart_in_seconds: 0, rss_kib: 15588 }],
}});
ok("diag line has pid + tools + rss",
  elements.servers.textContent.includes("pid 1719322")
  && elements.servers.textContent.includes("28 tools")
  && elements.servers.textContent.includes("15.2 MB"));
ok("tools line has bridge uptime", elements.tools.textContent.includes("bridge up 12m"));

// 2. Dead server with backoff + error: retry countdown and truncated error.
const longError = "x".repeat(200);
render({ ...base, bridgeStatus: {
  uptime_seconds: 30, clients_connected: 1, tools_available: 0,
  servers: [{ id: "repo", alive: false, ready: false, tools: 0, pid: null,
    error: longError, restart_attempts: 2, next_restart_in_seconds: 40, rss_kib: null }],
}});
ok("dead server shows retry countdown",
  elements.servers.textContent.includes("retry 2 in 40s"));
ok("error truncated to 60 chars",
  !elements.servers.textContent.includes("x".repeat(61))
  && elements.servers.textContent.includes("x".repeat(60)));

// 3. Old bridge (no bridgeStatus): legacy line, no crash.
render({ ...base, bridgeStatus: null });
ok("old-bridge fallback renders legacy line",
  elements.servers.textContent === "● repo (28 tools)");

// 4. Offline: state says Bridge offline, servers empty.
render({ ...base, connected: false, bridgeStatus: null });
ok("offline state", elements.state.textContent === "Bridge offline"
  && elements.servers.textContent === "");
