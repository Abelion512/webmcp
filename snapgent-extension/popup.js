// SPDX-License-Identifier: GPL-3.0-or-later
const SUPPORTED_HOSTS = [
  "chat.deepseek.com", "deepseek.com", "chatgpt.com", "chat.openai.com", "claude.ai",
  "gemini.google.com", "www.kimi.ai", "kimi.ai",
  "chat.z.ai", "chat.qwen.ai", "arena.ai", "www.meta.ai", "meta.ai", "ptero.pro", "grok.com", "chat.mistral.ai",
];
// const DEFAULT_AI_URL = "https://chat.deepseek.com/"; // DeepSeek hidden (disabled, not deleted)
const DEFAULT_AI_URL = "https://chatgpt.com/";



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
  dot.className = "dot " + (!s.connected ? (s.rotatingToken ? "warn" : "bad") : ok ? "ok" : "warn");
  state.textContent = s.connected
    ? (ok ? "Connected"
        : studioOff ? "No Studio"
        : "Open Studio")
    : s.authRequired ? "Bridge token required"
    // 1012: the bridge rotated the token and dropped us on purpose; the new one
    // is already stored, so this is a reconnect, not a pairing problem.
    : s.rotatingToken ? "Token rotated · reconnecting"
    : "Bridge offline";
  // Only prompt for the token while the bridge is actively refusing us.
  document.getElementById("pair").hidden = !(s.authRequired && !s.connected);
  // Offer to re-pair whenever a token is stored, including while connected.
  document.getElementById("forget-token").hidden = !s.savedToken;
  // Rotation needs an authenticated socket; the bridge applies the new token live.
  document.getElementById("rotate-token").hidden = !(s.connected && s.savedToken);
  const ver = document.getElementById("ver");
  const extVer = chrome.runtime.getManifest().version;
  ver.textContent = !s.connected ? `ext v${extVer}`
    : s.bridgeVersion ? `ext v${extVer} · bridge v${s.bridgeVersion}`
    : `ext v${extVer} · bridge old`;
  tools.textContent = s.connected
    ? `${s.tools || 0} tools`
    : s.authRequired ? "Paste the token below"
    : s.rotatingToken ? "New token saved"
    : "Run start.bat";
  servers.textContent = s.connected
    ? list.map((x) => `${x.alive ? "●" : "○"} ${x.id} (${x.alive ? x.tools + " tools" : "down"})`).join("\n")
    : "";
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
document.getElementById("save-token").addEventListener("click", () => {
  const input = document.getElementById("token");
  const value = input.value.trim();
  if (!value) return;
  chrome.storage.local.set({ bridgeToken: value }, () => {
    input.value = "";
    chrome.runtime.sendMessage({ type: "reconnect" }, () => setTimeout(refresh, 800));
  });
});
document.getElementById("rotate-token").addEventListener("click", (e) => {
  const button = e.target;
  button.textContent = "Rotating…";
  chrome.runtime.sendMessage({ type: "rotate_token" }, (r) => {
    if (r && r.ok) {
      button.textContent = "🔑 Rotate token";
    } else {
      button.textContent = "✗ Rotation failed";
      setTimeout(() => { button.textContent = "🔑 Rotate token"; }, 3000);
      console.warn("token rotation failed:", r && r.error);
    }
    setTimeout(refresh, 600);
  });
});
document.getElementById("forget-token").addEventListener("click", () => {
  chrome.storage.local.remove("bridgeToken", () => {
    document.getElementById("token").value = "";
    // Reconnect without a token: the bridge answers 1008 and the pairing card
    // comes back, so a fresh token can be pasted.
    chrome.runtime.sendMessage({ type: "reconnect" }, () => setTimeout(refresh, 800));
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
