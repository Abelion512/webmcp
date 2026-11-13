// SPDX-License-Identifier: GPL-3.0-or-later
// providers/rudder.js - the Rudder.chat (rudder.chat) provider.
// Live-validated 2026-11 against rudder.chat/chat:
//  - The composer is a real <textarea aria-label="Your message" maxlength="4000">
//    inside a <form>, with a submit button aria-label="Send". The 4,000-char
//    cap is enforced by the site, so every outgoing turn is trimmed to fit.
//  - Turns do NOT carry data-message-* attributes. A user turn is
//    div.group/user with the text in div.user-text; an assistant turn is
//    div.group/asst > article with the markdown in div.prose-th.user-text.
//    Reading the whole turn would also pick up "Thinking 2s" and the
//    Copy/Regenerate action bar, which makes stream length jitter forever.
// eslint-disable-next-line no-unused-vars
const ZSProvider = (() => {
  "use strict";
  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
  let diag = () => {};
  let lockedEditor = null;
  let sampledItem = null;
  let sampledLen = 0;
  let grewAt = 0;
  const ids = new WeakMap();
  let nextId = 0;

  // Rudder's stable class tokens. The slash is part of the Tailwind class, so
  // [class~="group/user"] is used instead of an escaped CSS class selector.
  const RUDDER_USER = 'div[class~="group/user"]';
  const RUDDER_ASSISTANT = 'div[class~="group/asst"]';
  const S = {
    user: `${RUDDER_USER},[data-message-author-role="user"],[data-role="user"],[data-testid*="user-message"],[data-testid*="user-turn"],div[class*="user-message"],div[class*="userMessage"]`,
    assistant: `${RUDDER_ASSISTANT},[data-message-author-role="assistant"],[data-role="assistant"],[data-testid*="assistant-message"],[data-testid*="assistant-turn"],div[class*="assistant-message"],div[class*="assistantMessage"],div[class*="bot-message"]`,
    // Prefer Rudder's real form textarea. The bare `textarea` fallback from the
    // generic provider is deliberately gone: it can grab a search box on a
    // login/help route and then type the system prompt into the wrong field.
    editor: 'form textarea[aria-label="Your message"],form textarea[placeholder*="Ask" i],textarea[aria-label="Your message"],textarea[placeholder*="Ask" i],form textarea,textarea[placeholder*="Message" i],[contenteditable="true"][role="textbox"]',
    content: '.prose-th.user-text,div.user-text',
    errors: '[role="alert"],[data-testid*="error"],[class*="error-message"],[class*="error"],div[class*="194,74,74"]',
  };
  // Rudder rejects messages over 4,000 characters. Keep a little headroom for
  // the truncation marker and preserve both ends of long tool output.
  const SEND_CAP = 4000;
  const SEND_MAX = 3950;
  const timings = {
    GEN_IDLE_MS: 2000,
    REASON_IDLE_MS: 15000,
    WARMUP_MS: 60000,
    REASON_NOREPLY_MS: 120000,
    STABLE_MS: 10000,
    RESPONSE_TIMEOUT_MS: 300000,
  };
  const visible = (el) => !!el && el.isConnected && el.getClientRects().length > 0;
  const excluded = (el) => !el.closest("#zs-root");
  const isUserItem = (item) => !!item && item.matches(S.user);
  const isAssistantItem = (item) => !!item && item.matches(S.assistant);
  const allItems = () => [...document.querySelectorAll(`${S.user},${S.assistant}`)]
    .filter(excluded);
  const assistantItems = () => allItems().filter(isAssistantItem);
  const assistantCount = () => assistantItems().length;
  const userCount = () => allItems().filter(isUserItem).length;
  const lastAssistant = () => {
    const items = assistantItems();
    return items.length ? items[items.length - 1] : null;
  };

  function getEditor() {
    if (visible(lockedEditor) && excluded(lockedEditor)) return lockedEditor;
    for (const editor of document.querySelectorAll(S.editor)) {
      if (excluded(editor) && visible(editor)) return editor;
    }
    return null;
  }
  const editorText = () => {
    const editor = getEditor();
    return editor ? ("value" in editor ? editor.value : editor.innerText || editor.textContent || "") : "";
  };
  const chatIsEmpty = () => allItems().length === 0;
  const isFreshChat = () => chatIsEmpty() && !!getEditor();
  const composerFrame = () => {
    const editor = getEditor();
    return editor && (editor.closest("form") || editor.parentElement);
  };
  const barAnchor = () => composerFrame();

  function textWithout(root, excludeSel) {
    if (!root) return "";
    const skip = [".zs-chip", excludeSel].filter(Boolean).join(",");
    let text = "";
    const walk = (node) => {
      if (node.nodeType === Node.TEXT_NODE) {
        text += node.nodeValue || "";
        return;
      }
      if (node.nodeType !== Node.ELEMENT_NODE || (skip && node.matches(skip))) return;
      if (node.tagName === "BR") {
        text += "\n";
        return;
      }
      const block = /^(ADDRESS|ARTICLE|BLOCKQUOTE|DIV|LI|P|PRE|SECTION)$/.test(node.tagName);
      if (block && text && !text.endsWith("\n")) text += "\n";
      for (const child of node.childNodes) walk(child);
      if (block && !text.endsWith("\n")) text += "\n";
    };
    walk(root);
    return text;
  }
  // Read ONLY the message body. On Rudder the turn root also contains the
  // "Thinking Ns" header and a Copy/Regenerate action bar; including those
  // makes the stream look like it is still growing after the answer ended.
  const bodyOf = (item) => {
    if (!item) return null;
    return item.querySelector(S.content) || item;
  };
  const itemText = (item) => textWithout(bodyOf(item));
  const classifyText = (item, excludeSel) => textWithout(bodyOf(item), excludeSel);
  const streamText = (item) => textWithout(bodyOf(item));
  const streamLen = (item) => streamText(item === undefined ? lastAssistant() : item).length;
  const lastAssistantId = () => {
    const item = lastAssistant();
    if (!item) return null;
    const id = item.getAttribute("data-message-id") || item.getAttribute("data-messageid") || item.id;
    if (id) return id;
    if (!ids.has(item)) ids.set(item, ++nextId);
    return ids.get(item);
  };
  function sampleStream() {
    const item = lastAssistant();
    const length = streamLen(item);
    const now = Date.now();
    if (item !== sampledItem || length < sampledLen - 400) {
      sampledItem = item;
      sampledLen = length;
      grewAt = now;
    } else if (length > sampledLen) {
      sampledLen = length;
      grewAt = now;
    }
  }

  const form = () => {
    const editor = getEditor();
    return editor && editor.closest("form");
  };
  const buttons = () => {
    const root = form() || document;
    return [...root.querySelectorAll("button,[role='button']")]
      .filter((button) => excluded(button) && visible(button));
  };
  const buttonLabel = (button) => [
    button.getAttribute("aria-label"),
    button.getAttribute("title"),
    button.getAttribute("data-testid"),
    button.textContent,
    ...[...button.querySelectorAll("[aria-label]")].map((el) => el.getAttribute("aria-label")),
  ].filter(Boolean).join(" ");
  const stopButton = () => buttons().find((button) =>
    /\b(stop|cancel|halt)\b/i.test(buttonLabel(button))
  ) || null;
  const sendButton = () => {
    const candidates = buttons();
    return candidates.find((button) =>
      button.matches('button[type="submit"],button[aria-label="Send" i],button[aria-label="Submit" i]') ||
      /\b(send|submit)\b/i.test(buttonLabel(button))
    ) || null;
  };
  const isHardGenerating = () => !!stopButton();
  const isGenerating = () => {
    sampleStream();
    return isHardGenerating() || (sampledLen > 0 && Date.now() - grewAt < timings.GEN_IDLE_MS);
  };
  const isBusyNow = isGenerating;

  function snapshot() {
    return { th: 0, rp: streamLen() };
  }
  function readAssistant() {
    const item = lastAssistant();
    return {
      present: !!item,
      reply: item ? streamText(item).trim() : "",
      thinking: "",
      item,
    };
  }
  async function waitFor(predicate, timeout) {
    const start = Date.now();
    while (Date.now() - start < timeout) {
      if (predicate()) return true;
      await sleep(120);
    }
    return false;
  }

  function applyInputLock(editor, on) {
    if (!editor) return;
    if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {
      if (on) editor.setAttribute("readonly", "");
      else editor.removeAttribute("readonly");
    } else {
      editor.setAttribute("contenteditable", on ? "false" : "true");
    }
  }
  function setInputLock(on) {
    const previous = lockedEditor;
    if (on) {
      const editor = getEditor();
      lockedEditor = editor;
      applyInputLock(editor, true);
      return;
    }
    lockedEditor = null;
    // Unlock BOTH the editor we locked and the current one. React can replace
    // the textarea while the agent runs; if we only touched the old detached
    // node, the replacement would stay writable/locked forever.
    const current = getEditor();
    applyInputLock(previous, false);
    if (current !== previous) applyInputLock(current, false);
  }
  function writeText(editor, text) {
    if (editor instanceof HTMLTextAreaElement || editor instanceof HTMLInputElement) {
      editor.removeAttribute("readonly");
      const prototype = editor instanceof HTMLTextAreaElement
        ? HTMLTextAreaElement.prototype
        : HTMLInputElement.prototype;
      const setter = Object.getOwnPropertyDescriptor(prototype, "value").set;
      setter.call(editor, text);
      editor.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: text,
      }));
      if (lockedEditor === editor) editor.setAttribute("readonly", "");
      return;
    }
    editor.focus();
    editor.setAttribute("contenteditable", "true");
    const selection = window.getSelection();
    if (selection) {
      const range = document.createRange();
      range.selectNodeContents(editor);
      selection.removeAllRanges();
      selection.addRange(range);
    }
    const inserted = document.execCommand("insertText", false, text);
    if (!inserted || editorText().trim() !== text.trim()) {
      editor.textContent = text;
      editor.dispatchEvent(new InputEvent("input", {
        bubbles: true,
        inputType: "insertText",
        data: text,
      }));
    }
    if (lockedEditor === editor) editor.setAttribute("contenteditable", "false");
  }
  function truncateForSend(text) {
    const raw = String(text == null ? "" : text);
    if (raw.length <= SEND_CAP) return raw;
    const omitted = raw.length - SEND_MAX;
    const marker =
      `\n\n[…snapgentV3: message truncated to fit Rudder's 4,000-character limit - ` +
      `${omitted} of ${raw.length} characters omitted…]\n\n`;
    const budget = Math.max(0, SEND_MAX - marker.length);
    const headLen = Math.floor(budget * 0.8);
    const tailLen = budget - headLen;
    const tail = tailLen > 0 ? raw.slice(raw.length - tailLen) : "";
    return raw.slice(0, headLen) + marker + tail;
  }

  async function typeAndSend(text) {
    const editor = getEditor();
    if (!editor) throw new Error("Rudder input box not found");
    const payload = truncateForSend(text);
    if (payload.length !== String(text == null ? "" : text).length) {
      diag("send.truncated", { from: String(text).length, to: payload.length, cap: SEND_CAP });
    }
    const beforeUsers = userCount();
    const beforeAssistants = assistantCount();
    const wasHard = isHardGenerating();
    writeText(editor, payload);
    const ready = await waitFor(() => {
      const button = sendButton();
      return !!button && !button.disabled && button.getAttribute("aria-disabled") !== "true";
    }, 12000);
    if (!ready) {
      if (authWall()) throw new Error("Sign in to Rudder to send messages.");
      throw new Error("Rudder send button did not become available");
    }
    // Click and CONFIRM. A single swallowed click used to throw even though the
    // core re-checks the editor, aborting the turn; retry once ONLY when the
    // composer still holds our exact text and no auth wall appeared.
    let sent = false;
    for (let attempt = 0; attempt < 2 && !sent; attempt++) {
      if (authWall()) break;
      const button = sendButton();
      if (!button || button.disabled || button.getAttribute("aria-disabled") === "true") break;
      button.click();
      sent = await waitFor(() =>
        editorText().trim() === "" || userCount() > beforeUsers ||
        assistantCount() > beforeAssistants || (!wasHard && isHardGenerating()),
      attempt === 0 ? 5000 : 4000);
      if (!sent && attempt === 0) {
        if (authWall() || editorText().trim() !== payload.trim()) break;
        await sleep(200);
      }
    }
    if (!sent) {
      if (authWall()) throw new Error("Sign in to Rudder to send messages.");
      // The site clears the textarea as soon as it accepts the message. If that
      // happened just after the poll expired, this is a success, not a failure.
      if (editorText().trim() === "") return;
      throw new Error("Rudder did not confirm the message was sent");
    }
  }
  let stopPoll = null;
  function stopGeneration() {
    if (stopPoll) { clearInterval(stopPoll); stopPoll = null; }
    const clickIfPresent = () => {
      const button = stopButton();
      if (!button) return false;
      try { button.click(); } catch {}
      return true;
    };
    if (clickIfPresent()) return;
    // Rudder mounts the native stop control a beat after "Generating" appears;
    // polling briefly prevents the stop from being swallowed in that gap.
    const startedAt = Date.now();
    stopPoll = setInterval(() => {
      if (clickIfPresent() || Date.now() - startedAt > 8000) {
        clearInterval(stopPoll);
        stopPoll = null;
      }
    }, 120);
  }
  const enforceComposer = () => {
    const editor = getEditor();
    // A locked session must also cover a composer React swapped in mid-run.
    if (lockedEditor && editor && editor !== lockedEditor) {
      lockedEditor = editor;
      applyInputLock(editor, true);
    }
    return { ready: !!editor };
  };
  async function ensureComposerReady(reason) {
    let ready = !!getEditor();
    if (!ready) {
      // The Rudder shell paints before the composer mounts. Waiting here avoids
      // a spurious "mode not ready" when the user clicks Start on first load.
      diag("mode_ready.wait", { reason, provider: "rudder" });
      ready = await waitFor(() => !!getEditor(), 10000);
    }
    // startSession called setInputLock(true) before the composer existed; now
    // that it mounted, make the lock real for the bootstrap send.
    if (ready && reason === "startup" && !lockedEditor) setInputLock(true);
    diag("mode_ready", { reason, provider: "rudder", ready });
    return { ready };
  }

  const authLinkVisible = (root) =>
    [...root.querySelectorAll('a[href*="/login"],a[href*="signin"],a[href*="sign-in"]')].some(visible);
  const authWall = () => {
    if (/^\/login(?:[/?]|$)/.test(location.pathname)) return true;
    return [...document.querySelectorAll('[role="dialog"]')]
      .some((dialog) => visible(dialog) && authLinkVisible(dialog));
  };
  // The composer can disappear for a frame while Next.js swaps a route or the
  // sign-in modal mounts. A single missing read is NOT a fatal context-limit -
  // only persist it for a few seconds, and never while a reply is streaming.
  let missingEditorSince = 0;
  function scanError() {
    for (const el of document.querySelectorAll(S.errors)) {
      if (!visible(el) || !excluded(el) || el.closest(`${S.user},${S.assistant}`)) continue;
      const text = (el.innerText || el.textContent || "").trim();
      if (text.length > 8 && text.length < 600) return text.slice(0, 240);
    }
    const editor = getEditor();
    if (!editor) {
      if (isGenerating()) {
        missingEditorSince = 0;
        return null;
      }
      if (authWall()) return "Sign in to Rudder to use the chat.";
      if (!missingEditorSince) missingEditorSince = Date.now();
      if (Date.now() - missingEditorSince < 4000) return null;
      return "The Rudder input box is not available.";
    }
    missingEditorSince = 0;
    if (authWall()) return "Sign in to Rudder to use the chat.";
    return null;
  }
  const isTooLongMsg = (text) => /conversation.{0,30}(too long|limit)|context.{0,20}limit|message is .{0,40}characters; the limit is|shorten it, or send it in parts/i.test(text);
  const isBusyMsg = (text) => /try again later|temporarily unavailable|rate limit|too many requests/i.test(text);
  const turnHalted = () => false;
  const findContinueBtn = () => null;
  const clickContinueBtn = () => false;
  const conversationKey = () => {
    if (location.pathname === "/" || location.pathname === "/chat" || location.pathname === "/chat/") return "";
    return `rudder:${location.pathname}${location.search}`;
  };

  function installSendHooks(handlers) {
    document.addEventListener("keydown", (event) => {
      if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
      const editor = getEditor();
      if (!editor || !(editor === event.target || editor.contains(event.target)) ||
          !editorText().trim() || handlers.isBlocked()) return;
      if (!handlers.isStarted()) {
        if (chatIsEmpty()) handlers.onBlockedAttempt();
        return;
      }
      handlers.onUserMessage(assistantCount());
    }, true);
    document.addEventListener("click", (event) => {
      const target = event.target;
      const button = target && target.closest ? target.closest("button,[role='button']") : null;
      if (!button || !getEditor()) return;
      if (stopButton() === button) {
        handlers.onNativeStop();
        return;
      }
      if (sendButton() !== button || button.disabled ||
          button.getAttribute("aria-disabled") === "true" || handlers.isBlocked()) return;
      if (!handlers.isStarted()) {
        if (chatIsEmpty()) handlers.onBlockedAttempt();
        return;
      }
      handlers.onUserMessage(assistantCount());
    }, true);
  }

  const commandShape = /"(?:command|tool)"\s*:\s*"|###\s*(?:lua|mcp_tool)/i;
  function findToolBlockSpot(item) {
    let found = null;
    const body = bodyOf(item);
    if (!body) return null;
    body.querySelectorAll("pre,code,p,li").forEach((el) => {
      if (el.closest(".zs-chip, .zs-tool-hide")) return;
      const text = (el.textContent || "").trim();
      if (text && commandShape.test(text)) {
        el.classList.add("zs-tool-hide");
        found = found || { parent: el.parentElement, ref: el };
      }
    });
    return found;
  }

  return {
    id: "rudder",
    displayName: "Rudder",
    sendRetryLimit: 1,
    // Rudder caps a single message at 4,000 chars. The core uses this budget
    // before appending a system reminder, and the send path truncates long
    // tool results instead of letting the site reject the whole turn.
    sendCharBudget: SEND_MAX,
    compactPrompt: true,
    compactToolList: true,
    supportsVision: false,
    timings,
    chipAtItemLevel: true,
    reliableCounts: true,
    init({ diag: d } = {}) { if (d) diag = d; },
    allItems, isUserItem, isAssistantItem, itemText, classifyText,
    assistantCount, userCount, lastAssistant, lastAssistantId, readAssistant,
    streamLen, snapshot,
    getEditor, editorText, chatIsEmpty, isFreshChat, composerFrame, barAnchor,
    setInputLock, typeAndSend, stopGeneration,
    isGenerating, isBusyNow, isHardGenerating,
    enforceComposer, ensureComposerReady,
    turnHalted, findContinueBtn, clickContinueBtn,
    scanError, isTooLongMsg, isBusyMsg,
    attachImages: () => false,
    clearAttachments: () => {},
    conversationKey, installSendHooks, findToolBlockSpot,
  };
})();
