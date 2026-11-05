// SPDX-License-Identifier: GPL-3.0-or-later
// background.js - service worker.
// Owns ONE resilient WebSocket to the local bridge (ws://127.0.0.1:PORT).
// Keeping the socket here (not in the content script) avoids https→ws mixed
// content issues and centralises reconnect / timeout logic.
//
// Contract with content.js: every sendMessage ALWAYS gets a response object,
// even when the bridge is offline. The agentic loop must never hang waiting.

// Classic service worker (no "type": "module"), so shared code is pulled in with
// importScripts. core/pairing.js builds the handshake URL and classifies closes.
importScripts("core/pairing.js");

const PORT = 17613;
const URL = `ws://127.0.0.1:${PORT}`;
// Pairing secret issued by the bridge (~/.snapgent/bridge_token). The popup saves
// it here; the bridge rejects connections without it once auth is enabled.
const TOKEN_KEY = "bridgeToken";

// Chat sites where a snapgentV3 provider content script runs. Status pushes go
// to every tab matching these. Add the new provider's URL pattern here (and in
// manifest.json content_scripts + host_permissions) when integrating another AI.
const PROVIDER_URLS = ["https://chat.deepseek.com/*", "https://chatgpt.com/*", "https://chat.openai.com/*", "https://claude.ai/*", "https://gemini.google.com/*", "https://www.kimi.ai/*", "https://kimi.ai/*", "https://chat.z.ai/*", "https://chat.qwen.ai/*", "https://arena.ai/*", "https://www.meta.ai/*", "https://meta.ai/*", "https://ptero.pro/*", "https://grok.com/*", "https://chat.mistral.ai/*"];

const RECONNECT_MIN = 1000;
const RECONNECT_MAX = 5000;
const HEARTBEAT_MS = 10000;
// If no message (incl. pong) arrives within this window while we believe we're
// connected, the socket is half-open: force a reconnect instead of letting
// pending requests slowly time out.
const STALE_SOCKET_MS = 25000;
const REQUEST_TIMEOUT_DEFAULT = 130000; // a bit above the 120s tool timeout

let ws = null;
let connected = false;
let authRequired = false; // bridge closed us with 1008: token missing or wrong
let rotatingToken = false; // bridge closed us with 1012: token changed, reconnecting
let bridgeToken = null; // cached chrome.storage value; null = not read yet
let connecting = false;
let reconnectDelay = RECONNECT_MIN;
let reconnectTimer = null;
let heartbeatTimer = null;
let lastMessageAt = 0; // timestamp of the last frame received from the bridge
let nextId = 1;
const pending = new Map(); // id -> {resolve, timer}
let toolsCache = [];
let mcpAlive = false;
let serversCache = [];
let bridgeVersion = null;
// Last get_bridge_status payload from the bridge (sanitized: no log_tail
// contents, see refreshBridgeStatus). null = old bridge or not fetched yet.
let bridgeStatusCache = null;
let bridgeStatusAt = 0; // Date.now() of the last successful fetch
// true/false = a PLACE is loaded and usable in Roblox Studio; null = unknown.
// The MCP process stays alive when Studio is closed or its MCP option is off,
// so this is probed separately (bridge "studio_status").
let studioConnected = null;
// true/false = a Roblox Studio app is connected to the MCP server at all; null =
// unknown. studioApp=true with studioConnected=false means "Studio open but no
// place"; studioApp=false means "Studio closed OR its MCP option disabled".
let studioApp = null;
// true/false = a Roblox Studio WINDOW/PROCESS exists on this machine (checked
// bridge-side via tasklist); null = unknown/old bridge. Distinguishes the two
// studioApp=false sub-cases the UI must word differently: Studio genuinely not
// launched ("open Roblox Studio") vs Studio OPEN but its MCP plugin never
// registered with the bridge - the documented fix for the latter is opening
// Assistant Settings > MCP Servers inside Studio (validated live 3x), which
// "open Roblox Studio" wording completely fails to convey.
let studioProc = null;

function log(...a) {
  console.log("[zs-bg]", ...a);
}

// ── WebSocket lifecycle ─────────────────────────────────────────────────
function readBridgeToken() {
  // Never leave connect() awaiting this: it holds the `connecting` guard, and a
  // callback that never fires (invalidated extension context) would then block
  // every future reconnect for the life of the worker. Fall back to "no token".
  return new Promise((resolve) => {
    const timer = setTimeout(() => resolve(""), 2000);
    const done = (value) => { clearTimeout(timer); resolve(value || ""); };
    try {
      chrome.storage.local.get(TOKEN_KEY, (values) => {
        done(values && values[TOKEN_KEY]);
      });
    } catch {
      done("");
    }
  });
}

async function connect() {
  if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
    return;
  }
  if (connecting) return;
  connecting = true;
  clearTimeout(reconnectTimer);
  try {
    if (bridgeToken === null) bridgeToken = await readBridgeToken();
    const url = ZSPairing.bridgeUrl(URL, bridgeToken);
    ws = new WebSocket(url);
  } catch (e) {
    log("WebSocket ctor failed", e);
    scheduleReconnect();
    return;
  } finally {
    connecting = false;
  }

  ws.onopen = () => {
    connected = true;
    authRequired = false;
    rotatingToken = false;
    reconnectDelay = RECONNECT_MIN;
    lastMessageAt = Date.now();
    log("connected to bridge");
    startHeartbeat();
    broadcastStatus();
    refreshBridgeStatus();
  };

  ws.onmessage = (ev) => {
    lastMessageAt = Date.now();
    let msg;
    try {
      msg = JSON.parse(ev.data);
    } catch {
      return;
    }
    handleBridgeMessage(msg);
  };

  ws.onclose = (ev) => {
    connected = false;
    authRequired = ZSPairing.isAuthClose(ev);
    rotatingToken = ZSPairing.isRotationClose(ev);
    mcpAlive = false;
    studioConnected = null;
    studioApp = null;
    studioProc = null;
    serversCache = [];
    bridgeVersion = null;
    bridgeStatusCache = null;
    bridgeStatusAt = 0;
    stopHeartbeat();
    failAllPending("bridge connection closed");
    broadcastStatus();
    scheduleReconnect();
  };

  ws.onerror = () => {
    // onclose will follow; nothing to do here but avoid an unhandled error.
    try { ws.close(); } catch {}
  };
}

function scheduleReconnect() {
  clearTimeout(reconnectTimer);
  reconnectTimer = setTimeout(connect, reconnectDelay);
  reconnectDelay = Math.min(reconnectDelay * 1.7, RECONNECT_MAX);
}

function startHeartbeat() {
  stopHeartbeat();
  heartbeatTimer = setInterval(() => {
    if (connected) {
      // Half-open socket: the WS still reports OPEN but nothing comes through.
      // The pong (and every other frame) refreshes lastMessageAt; if it has
      // gone stale, drop the dead socket so onclose triggers a reconnect.
      if (lastMessageAt && Date.now() - lastMessageAt > STALE_SOCKET_MS) {
        log("socket stale, forcing reconnect");
        try { ws.close(); } catch {}
        return;
      }
      // Keeps the MV3 service worker alive AND detects a half-open socket.
      send({ type: "ping" }).catch(() => {});
      refreshStudioStatus();
    }
  }, HEARTBEAT_MS);
}

function stopHeartbeat() {
  clearInterval(heartbeatTimer);
  heartbeatTimer = null;
}

// Resolve once the socket is OPEN, or false after `timeout` ms.
function waitForConnection(timeout = 8000) {
  return new Promise((resolve) => {
    if (connected && ws && ws.readyState === WebSocket.OPEN) return resolve(true);
    connect(); // nudge a (re)connection - important after a worker wake-up
    const t0 = Date.now();
    const iv = setInterval(() => {
      if (connected && ws && ws.readyState === WebSocket.OPEN) {
        clearInterval(iv);
        resolve(true);
      } else if (Date.now() - t0 > timeout) {
        clearInterval(iv);
        resolve(false);
      }
    }, 100);
  });
}

// ── request/response over the socket ────────────────────────────────────
async function send(obj, timeout = REQUEST_TIMEOUT_DEFAULT) {
  // The MV3 service worker can be suspended; the first message after a wake-up
  // arrives before the socket has re-opened. Wait for it instead of failing -
  // otherwise Kimi wrongly hears "bridge offline".
  if (!connected || !ws || ws.readyState !== WebSocket.OPEN) {
    await waitForConnection(8000);
  }
  return new Promise((resolve) => {
    if (!connected || !ws || ws.readyState !== WebSocket.OPEN) {
      resolve({ ok: false, kind: "disconnected", error: "bridge not connected" });
      return;
    }
    const id = nextId++;
    const payload = { ...obj, id };
    const timer = setTimeout(() => {
      if (pending.has(id)) {
        pending.delete(id);
        resolve({ ok: false, kind: "timeout", error: "bridge did not respond in time" });
      }
    }, timeout);
    pending.set(id, { resolve, timer });
    try {
      ws.send(JSON.stringify(payload));
    } catch (e) {
      clearTimeout(timer);
      pending.delete(id);
      resolve({ ok: false, kind: "disconnected", error: String(e) });
    }
  });
}

// Ask the bridge whether a Roblox Studio instance is actually connected to the
// MCP server. Broadcasts only on change so the UI updates promptly but quietly.
let studioProbing = false;
async function refreshStudioStatus() {
  if (studioProbing || !connected) return;
  studioProbing = true;
  try {
    const r = await send({ type: "studio_status" }, 12000);
    const v = r && r.ok && typeof r.studio === "boolean" ? r.studio : null;
    if (v !== studioConnected) {
      studioConnected = v;
      broadcastStatus();
    }
  } finally {
    studioProbing = false;
  }
}

// Ask the bridge for its diagnostic snapshot (pid, RSS, restart attempts,
// uptime). The log_tail lines are dropped here: the popup needs counts and
// per-server health, not raw log text. An old bridge that does not know
// the request answers with an error frame, which resolvePending maps to
// ok:false - the cache simply stays null and the popup keeps its old UI.
let bridgeStatusProbing = false;
async function refreshBridgeStatus(force = false) {
  if (bridgeStatusProbing || !connected) return;
  if (!force && bridgeStatusCache && Date.now() - bridgeStatusAt < 5000) return;
  bridgeStatusProbing = true;
  try {
    const r = await send({ type: "get_bridge_status" }, 12000);
    if (r && r.ok && r.status && typeof r.status === "object") {
      const s = r.status;
      bridgeStatusCache = {
        bridge_version: s.bridge_version || null,
        uptime_seconds: typeof s.uptime_seconds === "number" ? s.uptime_seconds : null,
        clients_connected: typeof s.clients_connected === "number" ? s.clients_connected : null,
        tools_available: typeof s.tools_available === "number" ? s.tools_available : null,
        servers: Array.isArray(s.servers) ? s.servers.map((x) => ({
          id: String(x.id || "?"),
          alive: !!x.alive,
          ready: !!x.ready,
          tools: typeof x.tools === "number" ? x.tools : 0,
          pid: typeof x.pid === "number" ? x.pid : null,
          error: typeof x.error === "string" ? x.error.slice(0, 120) : null,
          restart_attempts: typeof x.restart_attempts === "number" ? x.restart_attempts : 0,
          next_restart_in_seconds: typeof x.next_restart_in_seconds === "number" ? x.next_restart_in_seconds : 0,
          rss_kib: typeof x.rss_kib === "number" ? x.rss_kib : null,
        })) : [],
        log_tail_lines: Array.isArray(s.log_tail) ? s.log_tail.length : 0,
      };
      bridgeStatusAt = Date.now();
      broadcastStatus();
    }
  } finally {
    bridgeStatusProbing = false;
  }
}

function handleBridgeMessage(msg) {
  if ("studio" in msg && (typeof msg.studio === "boolean" || msg.studio === null)) {
    studioConnected = msg.studio;
  }
  if ("studio_app" in msg && (typeof msg.studio_app === "boolean" || msg.studio_app === null)) {
    studioApp = msg.studio_app;
  }
  if ("studio_proc" in msg && (typeof msg.studio_proc === "boolean" || msg.studio_proc === null)) {
    studioProc = msg.studio_proc;
  }
  if (msg.type === "studio_status") {
    resolvePending(msg.id, { ok: true, studio: studioConnected });
    broadcastStatus();
    return;
  }
  if (msg.type === "connected") {
    bridgeVersion = typeof msg.bridge_version === "string" ? msg.bridge_version : null;
    mcpAlive = !!msg.mcp_alive;
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    broadcastStatus();
    return;
  }
  if (msg.type === "bridge_status") {
    // send() resolves via resolvePending below; refreshBridgeStatus stores
    // the sanitized copy. This arm only feeds the pending request.
    resolvePending(msg.id, { ok: true, status: msg.status });
    return;
  }
  if (msg.type === "pong") {
    resolvePending(msg.id, { ok: true });
    return;
  }
  if (msg.type === "tools") {
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    mcpAlive = !!msg.mcp_alive;
    resolvePending(msg.id, { ok: true, tools: toolsCache });
    broadcastStatus();
    return;
  }
  if (msg.type === "tool_result") {
    resolvePending(msg.id, msg.ok
      ? { ok: true, text: msg.text, images: msg.images || [] }
      : { ok: false, kind: msg.kind, error: msg.error });
    return;
  }
  if (msg.type === "mcp_status") {
    mcpAlive = !!msg.alive;
    if (Array.isArray(msg.tools)) toolsCache = msg.tools;
    if (Array.isArray(msg.servers)) serversCache = msg.servers;
    resolvePending(msg.id, { ok: !!msg.ok, alive: msg.alive, error: msg.error });
    broadcastStatus();
    return;
  }
  if (msg.type === "token_rotated") {
    // The bridge answers with the new secret and only then drops the socket
    // (1012). Resolving here - before onclose can fail every pending request -
    // is what lets the worker store the token and reconnect with it instead of
    // retrying the old one into a 1008.
    resolvePending(msg.id, { ok: !!msg.ok, token: msg.token, error: msg.error });
    return;
  }
  if (msg.type === "server_changed") {
    // The bridge acks, then restarts itself to reload config.json. The socket
    // will drop right after this - the content script shows a spinner until the
    // reconnect lands and a fresh status arrives.
    resolvePending(msg.id, { ok: !!msg.ok, error: msg.error, restarting: !!msg.restarting });
    return;
  }
  if (msg.type === "error") {
    resolvePending(msg.id, { ok: false, error: msg.error });
    return;
  }
}

function resolvePending(id, value) {
  const p = pending.get(id);
  if (!p) return;
  clearTimeout(p.timer);
  pending.delete(id);
  p.resolve(value);
}

function failAllPending(reason) {
  for (const [, p] of pending) {
    clearTimeout(p.timer);
    p.resolve({ ok: false, kind: "disconnected", error: reason });
  }
  pending.clear();
}

// ── status push to any open DeepSeek tab + popup ─────────────────────────
function statusObj() {
  return { type: "zs-status", connected, authRequired, rotatingToken, savedToken: !!bridgeToken, mcpAlive, bridgeVersion, bridgeStatus: bridgeStatusCache, studio: studioConnected, studioApp, studioProc, tools: toolsCache.length, servers: serversCache };
}

function broadcastStatus() {
  chrome.runtime.sendMessage(statusObj()).catch(() => {});
  chrome.tabs.query({ url: PROVIDER_URLS }, (tabs) => {
    for (const t of tabs) chrome.tabs.sendMessage(t.id, statusObj()).catch(() => {});
  });
}

// ── messages from content.js / popup.js ─────────────────────────────────
chrome.runtime.onMessage.addListener((msg, _sender, sendResponse) => {
  (async () => {
    switch (msg.type) {
      case "status":
        if (!connected) connect(); // self-heal after a worker wake-up
        else refreshBridgeStatus(); // stale-guarded (5 s), fire-and-forget
        if (bridgeToken === null) bridgeToken = await readBridgeToken();
        sendResponse(statusObj());
        break;
      case "bridge_status":
        await refreshBridgeStatus(true);
        break;
      case "list_tools": {
        // Prefer a live refresh; fall back to cache so the loop never stalls.
        // 10s, not 25s: a catalogue request only blocks this long when one of the
        // MCP servers is dead (typically Roblox in a degraded, Blender-only
        // session), and in that exact case we already hold a perfectly good cached
        // catalogue. Waiting the full 25s just froze the boot for no new data.
        const r = await send({ type: "list_tools" }, 10000);
        if (r.ok) sendResponse({ ok: true, tools: r.tools });
        else sendResponse({ ok: toolsCache.length > 0, tools: toolsCache, error: r.error });
        break;
      }
      case "call_tool": {
        const timeout = (msg.timeout || 120000) + 10000;
        const r = await send(
          { type: "call_tool", name: msg.name, arguments: msg.arguments, timeout: msg.timeout, server: msg.server },
          timeout
        );
        sendResponse(r);
        break;
      }
      case "rotate_token": {
        // The bridge replies with the new token and then closes every authenticated
        // socket (1012), so it must be saved here before onclose fires: the
        // reconnect then dials in with the new value instead of asking to pair.
        const r = await send({ type: "rotate_token" }, 15000);
        if (r.ok && r.token) {
          bridgeToken = r.token;
          await new Promise((resolve) =>
            chrome.storage.local.set({ [TOKEN_KEY]: r.token }, resolve)
          );
          broadcastStatus();
        }
        sendResponse(r);
        break;
      }
      case "restart_mcp": {
        const r = await send({ type: "restart_mcp" }, 30000);
        sendResponse(r);
        break;
      }
      case "add_server": {
        const r = await send({
          type: "add_server", server_id: msg.server_id,
          command: msg.command, args: msg.args, env: msg.env,
        }, 15000);
        sendResponse(r);
        break;
      }
      case "remove_server": {
        const r = await send({ type: "remove_server", server_id: msg.server_id }, 15000);
        sendResponse(r);
        break;
      }
      case "reconnect":
        reconnectDelay = RECONNECT_MIN;
        bridgeToken = null; // re-read: the popup may have just saved a token
        connect();
        sendResponse({ ok: true });
        break;
      default:
        sendResponse({ ok: false, error: "unknown message" });
    }
  })();
  return true; // async sendResponse
});

// Wake/keepalive hooks.
chrome.runtime.onStartup.addListener(connect);
chrome.runtime.onInstalled.addListener(connect);

connect();
