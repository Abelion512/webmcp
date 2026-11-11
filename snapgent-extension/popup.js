// SPDX-License-Identifier: GPL-3.0-or-later
const SUPPORTED_HOSTS = [
  "chat.deepseek.com", "deepseek.com", "chatgpt.com", "chat.openai.com", "claude.ai",
  "gemini.google.com", "www.kimi.ai", "kimi.ai",
  "chat.z.ai", "chat.qwen.ai", "arena.ai", "www.meta.ai", "meta.ai", "ptero.pro", "grok.com", "chat.mistral.ai", "rudder.chat",
];
// const DEFAULT_AI_URL = "https://chat.deepseek.com/"; // DeepSeek hidden (disabled, not deleted)
const DEFAULT_AI_URL = "https://chatgpt.com/";



function fmtUptime(seconds) {
  if (typeof seconds !== "number" || seconds < 0) return "";
  if (seconds < 60) return `${Math.floor(seconds)}s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)}m`;
  return `${Math.floor(seconds / 3600)}h${Math.floor((seconds % 3600) / 60)}m`;
}

function fmtRss(kib) {
  if (typeof kib !== "number" || kib < 0) return "";
  return kib >= 1024 * 1024
    ? `${(kib / 1024 / 1024).toFixed(1)} GB`
    : `${(kib / 1024).toFixed(1)} MB`;
}

// One line per server from the bridge diagnostic snapshot. Falls back to
// the plain servers list when the bridge is old (no bridgeStatus).
// textContent only - bridge error strings are never injected as HTML.
function serverLines(s, list) {
  const diag = s.bridgeStatus;
  if (diag && Array.isArray(diag.servers) && diag.servers.length) {
    return diag.servers.map((x) => {
      const parts = [x.alive ? "●" : "○", x.id];
      if (x.pid) parts.push(`pid ${x.pid}`);
      parts.push(x.alive ? `${x.tools} tools` : "down");
      const rss = fmtRss(x.rss_kib);
      if (rss && x.alive) parts.push(rss);
      if (x.restart_attempts > 0) {
        parts.push(x.next_restart_in_seconds > 0
          ? `retry ${x.restart_attempts} in ${Math.round(x.next_restart_in_seconds)}s`
          : `retries ${x.restart_attempts}`);
      }
      let line = parts.join(" · ");
      if (!x.alive && x.error) line += ` — ${String(x.error).slice(0, 60)}`;
      return line;
    });
  }
  return list.map((x) => `${x.alive ? "●" : "○"} ${x.id} (${x.alive ? x.tools + " tools" : "down"})`);
}

function render(s) {
  const state = document.getElementById("state");
  const tools = document.getElementById("tools");
  const servers = document.getElementById("servers");
  const list = s.servers || [];
  const up = list.filter((x) => x.alive).length;
  const mcpOk = s.connected && (s.mcpAlive || up > 0 || s.tools > 0);
  const studioOff = mcpOk && s.studio === false; // MCP up but no Studio attached
  const ok = mcpOk && !studioOff;
  const dot = document.getElementById("dot");
  dot.className = "dot " + (!s.connected ? "bad" : ok ? "ok" : "warn");
  state.textContent = s.connected
    ? (ok ? "Connected"
        : studioOff ? "No Studio"
        : "Open Studio")
    : "Bridge offline";
  const ver = document.getElementById("ver");
  const extVer = chrome.runtime.getManifest().version;
  ver.textContent = !s.connected ? `ext v${extVer}`
    : s.bridgeVersion ? `ext v${extVer} · bridge v${s.bridgeVersion}`
    : `ext v${extVer} · bridge old`;
  tools.textContent = s.connected
    ? `${s.tools || 0} tools`
      + (s.bridgeStatus && s.bridgeStatus.uptime_seconds != null
          ? ` · bridge up ${fmtUptime(s.bridgeStatus.uptime_seconds)}`
          : "")
    : "Run start.bat";
  servers.textContent = s.connected ? serverLines(s, list).join("\n") : "";
}

function refresh() {
  chrome.runtime.sendMessage({ type: "status" }, (s) => s && render(s));
}

document.getElementById("reconnect").addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "reconnect" }, () => setTimeout(refresh, 600));
});
document.getElementById("restart").addEventListener("click", (e) => {
  e.target.textContent = "Restarting…";
  chrome.runtime.sendMessage({ type: "restart_mcp" }, () => {
    e.target.textContent = "⟳ Restart";
    setTimeout(refresh, 600);
  });
});
document.getElementById("settings").addEventListener("click", () => {
  // Tries the in-page panel on an already-open supported AI tab first, so
  // opening it doesn't require a conversation to already be started there.
  chrome.tabs.query({}, (tabs) => {
    const active = tabs.find((t) => t.active && t.url && SUPPORTED_HOSTS.some((h) => t.url.includes(h)));
    const anySupported = active || tabs.find((t) => t.url && SUPPORTED_HOSTS.some((h) => t.url.includes(h)));
    if (anySupported) {
      chrome.tabs.sendMessage(anySupported.id, { type: "zs-open-menu" });
      chrome.tabs.update(anySupported.id, { active: true });
    } else {
      chrome.tabs.create({ url: DEFAULT_AI_URL });
    }
  });
});

chrome.runtime.onMessage.addListener((msg) => {
  if (msg && msg.type === "zs-status") render(msg);
});
refresh();
setInterval(refresh, 2000);
