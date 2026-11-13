// Quick Node regression test for providers/rudder.js (run: node test-rudder.js).
// Not shipped.
//
// Why this exists: Rudder renders messages as div.group/user and
// div.group/asst with NO data-message-* attributes. The generic provider
// selectors matched ZERO turns on the live site, so the loop never saw a reply.
// These tests pin the real structure, body-only reading, the 4,000-char send
// cap, send confirmation, and the transient-missing-composer guard.
const fs = require("fs");
const assert = require("assert");

// ── Minimal DOM ─────────────────────────────────────────────────────────────
const TEXT = 3;
const ELEM = 1;

function splitOutside(input, space) {
  const out = [];
  let buf = "";
  let depth = 0;
  let quote = null;
  for (let i = 0; i < input.length; i += 1) {
    const ch = input[i];
    if (quote) {
      buf += ch;
      if (ch === quote && input[i - 1] !== "\\") quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") { quote = ch; buf += ch; continue; }
    if (ch === "[") depth += 1;
    if (ch === "]") depth = Math.max(0, depth - 1);
    const boundary = space ? /\s/.test(ch) : ch === ",";
    if (!depth && boundary) {
      if (buf.trim()) out.push(buf.trim());
      buf = "";
      continue;
    }
    buf += ch;
  }
  if (buf.trim()) out.push(buf.trim());
  return out;
}

function bracketEnd(input) {
  let depth = 0;
  let quote = null;
  for (let i = 0; i < input.length; i += 1) {
    const ch = input[i];
    if (quote) {
      if (ch === quote && input[i - 1] !== "\\") quote = null;
      continue;
    }
    if (ch === '"' || ch === "'") quote = ch;
    else if (ch === "[") depth += 1;
    else if (ch === "]" && --depth === 0) return i;
  }
  return -1;
}

function attrMatches(el, inner) {
  const m = /^([\w-]+)\s*(?:([~*^$|]?=)\s*(?:"([^"]*)"|'([^']*)'|([^\s]+)))?(?:\s+([is]))?$/.exec(inner);
  if (!m) return false;
  const name = m[1];
  const op = m[2];
  const expected = m[3] ?? m[4] ?? m[5];
  const insensitive = (m[6] || "").toLowerCase() === "i";
  let actual = name === "class" ? el.className : el.getAttribute(name);
  if (actual == null) return false;
  if (!op) return true;
  let want = expected == null ? "" : expected;
  if (insensitive) { actual = actual.toLowerCase(); want = want.toLowerCase(); }
  switch (op) {
    case "=": return actual === want;
    case "~=": return actual.split(/\s+/).includes(want);
    case "*=": return actual.includes(want);
    case "^=": return actual.startsWith(want);
    case "$=": return actual.endsWith(want);
    case "|=": return actual === want || actual.startsWith(`${want}-`);
    default: return false;
  }
}

function matchesCompound(el, selector) {
  if (!el || el.nodeType !== ELEM) return false;
  let rest = selector;
  const tag = /^[a-zA-Z][\w-]*/.exec(rest);
  if (tag) {
    if (el.tagName !== tag[0].toUpperCase()) return false;
    rest = rest.slice(tag[0].length);
  }
  while (rest.length) {
    if (rest[0] === ".") {
      const m = /^\.([\w-]+)/.exec(rest);
      if (!m || !el.classList.contains(m[1])) return false;
      rest = rest.slice(m[0].length);
      continue;
    }
    if (rest[0] === "#") {
      const m = /^#([\w-]+)/.exec(rest);
      if (!m || el.id !== m[1]) return false;
      rest = rest.slice(m[0].length);
      continue;
    }
    if (rest[0] === "[") {
      const end = bracketEnd(rest);
      if (end < 0 || !attrMatches(el, rest.slice(1, end))) return false;
      rest = rest.slice(end + 1);
      continue;
    }
    return false;
  }
  return true;
}

function matches(el, selector) {
  return splitOutside(selector, false).some((part) => {
    const tokens = splitOutside(part, true);
    if (!tokens.length || !matchesCompound(el, tokens[tokens.length - 1])) return false;
    let node = el.parentElement;
    for (let i = tokens.length - 2; i >= 0; i -= 1) {
      while (node && !matchesCompound(node, tokens[i])) node = node.parentElement;
      if (!node) return false;
      node = node.parentElement;
    }
    return true;
  });
}

function textNode(value) {
  return { nodeType: TEXT, nodeValue: String(value), childNodes: [], parentElement: null };
}

class StubElement {
  constructor(tag, opts = {}) {
    this.nodeType = ELEM;
    this.tagName = tag.toUpperCase();
    this.childNodes = [];
    this.parentElement = null;
    this.attrs = { ...(opts.attrs || {}) };
    this._classes = new Set(String(opts.class || "").split(/\s+/).filter(Boolean));
    this._visible = opts.visible !== false;
    this._connected = true;
    this._listeners = {};
    this._click = null;
    this.disabled = !!opts.disabled;
    this.id = opts.id || "";
    this.oninput = null;
  }
  get className() { return [...this._classes].join(" "); }
  get textContent() {
    return this.childNodes.map((child) =>
      child.nodeType === TEXT ? child.nodeValue : child.textContent || ""
    ).join("");
  }
  get innerText() { return this.textContent; }
  get isConnected() { return this._connected; }
  get classList() {
    const self = this;
    return {
      add: (...names) => names.forEach((n) => self._classes.add(n)),
      remove: (...names) => names.forEach((n) => self._classes.delete(n)),
      contains: (name) => self._classes.has(name),
    };
  }
  getAttribute(name) {
    if (name === "class") return this.className;
    return Object.prototype.hasOwnProperty.call(this.attrs, name) ? this.attrs[name] : null;
  }
  setAttribute(name, value) { this.attrs[name] = String(value); }
  removeAttribute(name) { delete this.attrs[name]; }
  getClientRects() { return this._connected && this._visible ? [{ width: 10, height: 10 }] : []; }
  matches(selector) { return matches(this, selector); }
  closest(selector) {
    let node = this;
    while (node) {
      if (node.nodeType === ELEM && matches(node, selector)) return node;
      node = node.parentElement;
    }
    return null;
  }
  contains(node) {
    for (let n = node; n; n = n.parentElement) if (n === this) return true;
    return false;
  }
  querySelectorAll(selector) {
    const hits = [];
    const walk = (node) => {
      for (const child of node.childNodes) {
        if (child.nodeType !== ELEM) continue;
        if (matches(child, selector)) hits.push(child);
        walk(child);
      }
    };
    walk(this);
    return hits;
  }
  querySelector(selector) { return this.querySelectorAll(selector)[0] || null; }
  append(...children) {
    for (const child of children) {
      child.parentElement = this;
      this.childNodes.push(child);
    }
  }
  focus() {}
  setSelectionRange() {}
  dispatchEvent(event) {
    for (const fn of this._listeners[event.type] || []) fn(event);
    if (event.type === "input" && this.oninput) this.oninput(event);
    return true;
  }
  addEventListener(type, fn) {
    (this._listeners[type] ||= []).push(fn);
  }
  click() {
    if (this._click) this._click();
    else if (this.onclick) this.onclick();
  }
  remove() { this._connected = false; }
}

class StubTextArea extends StubElement {
  get value() { return this._value || ""; }
  set value(value) { this._value = String(value); }
}
class StubInput extends StubElement {
  get value() { return this._value || ""; }
  set value(value) { this._value = String(value); }
}

function el(tag, opts = {}, children = []) {
  const Cls = tag === "textarea" ? StubTextArea : tag === "input" ? StubInput : StubElement;
  const node = new Cls(tag, opts);
  for (const child of children) {
    node.append(typeof child === "string" ? textNode(child) : child);
  }
  return node;
}

global.Node = { TEXT_NODE: TEXT, ELEMENT_NODE: ELEM };
global.HTMLTextAreaElement = StubTextArea;
global.HTMLInputElement = StubInput;
global.Event = class { constructor(type, options) { this.type = type; Object.assign(this, options); } };
global.InputEvent = class extends global.Event {};
global.window = { HTMLTextAreaElement: StubTextArea };
global.location = { pathname: "/chat" };

const body = el("div", { class: "body" });
const documentElement = el("html");
documentElement.append(body);
global.document = {
  documentElement,
  body,
  querySelector: (selector) => body.querySelector(selector),
  querySelectorAll: (selector) => body.querySelectorAll(selector),
  addEventListener: () => {},
  hidden: false,
};
global.window.location = global.location;

// ── Fixture matching the live authenticated chat markup ────────────────────
const column = el("div", { class: "mx-auto flex w-full max-w-[768px] flex-col gap-5" });
body.append(column);

const userTurn = el("div", { class: "group/user flex w-full flex-col items-end" }, [
  el("span", { class: "sr-only" }, ["You said"]),
  el("div", { class: "user-text max-w-[80%]" }, ["hello"]),
  el("div", { class: "mt-1" }, ["Copy", "Edit"]),
]);
const firstAssistant = el("div", { class: "group/asst w-full" }, [
  el("div", { class: "mb-1.5" }, ["Thinking 2s"]),
  el("article", { class: "mx-auto w-full" }, [
    el("div", { class: "prose-th user-text" }, [
      el("p", {}, ['{"command":"list_commands"}']),
    ]),
    el("div", { class: "mt-2" }, ["Copy", "Regenerate"]),
  ]),
]);
column.append(userTurn, firstAssistant);

// A decoy textarea outside the form must never win over the real composer.
body.append(el("textarea", { attrs: { placeholder: "Search" } }));

const form = el("form", { class: "relative mx-auto flex max-w-[768px] flex-col" });
const editor = el("textarea", {
  attrs: { "aria-label": "Your message", placeholder: "Ask anything…", maxlength: "4000" },
  class: "block max-h-[160px] w-full",
});
const sendButton = el("button", {
  attrs: { type: "submit", "aria-label": "Send" },
  disabled: true,
});
const stopButton = el("button", {
  attrs: { type: "button", "aria-label": "Stop" },
  visible: false,
});
form.append(editor, sendButton, stopButton);
body.append(form);
editor.oninput = () => { sendButton.disabled = !editor.value.trim(); };

let lastSent = null;
let sends = 0;
sendButton._click = () => {
  if (sendButton.disabled) return;
  lastSent = editor.value;
  sends += 1;
  const sentText = editor.value;
  editor.value = "";
  sendButton.disabled = true;
  column.append(el("div", { class: "group/user flex w-full" }, [
    el("div", { class: "user-text" }, [sentText]),
  ]));
  column.append(el("div", { class: "group/asst w-full" }, [
    el("article", { class: "mx-auto w-full" }, [
      el("div", { class: "prose-th user-text" }, [""]),
    ]),
  ]));
  stopButton._visible = true;
};

const P = new Function(
  fs.readFileSync(__dirname + "/providers/rudder.js", "utf8") + "; return ZSProvider;"
)();

const results = [];
function ok(name, condition, detail = "") {
  results.push([name, !!condition]);
  console.log((condition ? "PASS  " : "FAIL  ") + name + (detail ? "  " + detail : ""));
}

(async () => {
  ok("real Rudder user turn is counted", P.userCount() === 1);
  ok("real Rudder assistant turn is counted", P.assistantCount() === 1);
  ok("real Rudder composer is found", P.getEditor() === editor);
  ok("real Rudder composer is anchored to its form", P.barAnchor() === form);
  ok("existing conversation is not fresh", P.isFreshChat() === false);

  const reply = P.readAssistant().reply;
  ok("assistant body is read without Thinking/action chrome",
    reply.includes('"command":"list_commands"') &&
    !reply.includes("Thinking") && !reply.includes("Regenerate"),
    JSON.stringify(reply));
  ok("itemText reads only the message body",
    P.itemText(firstAssistant).includes('"command":"list_commands"') &&
    !P.itemText(firstAssistant).includes("Thinking"));
  ok("Rudder stream length ignores header/action chrome",
    P.streamLen(firstAssistant) === reply.length + 1,
    String(P.streamLen(firstAssistant)));

  const firstId = P.lastAssistantId();
  const secondAssistant = el("div", { class: "group/asst w-full" }, [
    el("div", { class: "mb-1.5" }, ["Thinking 1s"]),
    el("article", { class: "mx-auto w-full" }, [
      el("div", { class: "prose-th user-text" }, [el("p", {}, ["second reply"])]),
      el("div", { class: "mt-2" }, ["Copy"]),
    ]),
  ]);
  column.append(secondAssistant);
  ok("assistant identity advances for a new turn",
    P.lastAssistantId() !== firstId && P.assistantCount() === 2);
  ok("new reply is read without old turn content",
    P.readAssistant().reply.includes("second reply") &&
    !P.readAssistant().reply.includes("list_commands"));

  // The real Rudder error panel has no role/error class name; it is a rounded
  // div carrying the rgba(194,74,74,.08) background token.
  const errorPanel = el("div", { class: "mt-4 rounded bg-[rgba(194,74,74,.08)] px-4 py-3" }, [
    "Conversation too long. Start a new chat.",
  ]);
  column.append(errorPanel);
  ok("real Rudder error panel is detected",
    (P.scanError() || "").includes("Conversation too long"),
    P.scanError() || "");
  errorPanel.remove();

  // A composer swap during a live reply must not be reported as a fatal error.
  stopButton._visible = true;
  editor._connected = false;
  ok("missing composer during generation is not fatal", P.scanError() === null, P.scanError() || "");

  // The startup gate waits for a lazy-mounted composer instead of failing.
  const readyPromise = P.ensureComposerReady("startup");
  setTimeout(() => { editor._connected = true; }, 300);
  const ready = await readyPromise;
  ok("startup waits for a late composer", ready.ready === true);

  // Long tool results are trimmed to Rudder's real 4,000-char limit, keeping
  // both ends and a visible omission marker.
  await P.typeAndSend("x".repeat(5000));
  ok("long message is capped below 4000 chars", lastSent.length <= 4000, String(lastSent.length));
  ok("long message keeps a truncation marker", lastSent.includes("truncated"));
  ok("send button actually submitted", sends === 1 && editor.value === "");
  ok("send confirmed by a new user turn", P.userCount() === 2, String(P.userCount()));
  ok("send confirmed by a new assistant turn", P.assistantCount() === 3, String(P.assistantCount()));

  // React can replace the textarea during a run. The lock must follow the new
  // node and the unlock must clear it, or the next turn is typed into a dead
  // editor / the replacement stays locked.
  P.setInputLock(true);
  const replacement = el("textarea", {
    attrs: { "aria-label": "Your message", placeholder: "Ask anything…" },
    class: "block max-h-[160px] w-full",
  });
  form.append(replacement);
  editor._connected = false;
  P.enforceComposer();
  ok("input lock follows a replaced composer", replacement.getAttribute("readonly") === "");
  P.setInputLock(false);
  ok("replacement composer unlocks", replacement.getAttribute("readonly") === null);

  ok("server character-limit text is classified as too long",
    P.isTooLongMsg("This message is 5,001 characters; the limit is 4,000. Shorten it, or send it in parts."));

  // Compact prompt must actually fit Rudder's cap.
  const ZS = new Function(
    fs.readFileSync(__dirname + "/core/config.js", "utf8") + "; return ZS;"
  )();
  const compact = ZS.buildSystemPrompt({
    siteName: "Rudder", targetServer: "roblox", targetName: "Roblox Studio", compact: true,
  });
  ok("compact Roblox prompt fits 4000 chars", compact.length <= 4000, String(compact.length));
  ok("compact prompt keeps the essential protocol",
    compact.includes("###LUA###") && compact.includes("list_commands") &&
    compact.includes("ONE command"));

  const failed = results.filter(([, passed]) => !passed);
  console.log(`\n${results.length - failed.length}/${results.length} PASS`);
  if (failed.length) process.exitCode = 1;
})().catch((error) => {
  console.error(error);
  process.exitCode = 1;
});
