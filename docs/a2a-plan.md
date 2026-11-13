# Plan: A2A server facade over the Snapgent browser bridge

Spec: `docs/a2a-design.md` (binding authority)
Branch: `a2a` (base `linux-stability` @ `106efab`)

## Global Constraints

- Python 3.9+ standard library only. No new dependencies. `websockets` is the
  only third-party import and it is already required by `bridge.py`.
- A2A target version: 1.0. PascalCase JSON-RPC methods, `TASK_STATE_*` enum,
  unified `Part` (no `kind` field), `statusUpdate` / `artifactUpdate` SSE events.
- Accept the A2A v0.3 method aliases `message/send`, `message/stream`,
  `tasks/get` as routing aliases only. Responses are always 1.0 shapes.
- Bind `127.0.0.1` only. Every POST requires `Authorization: Bearer <token>`.
- A2A really means A2A: all three methods funnel through one bridge
  `submit_prompt`. No parallel browser work.
- Never work on `main`. Never merge to `main` in this plan.
- Run `python3 -m unittest` after every task. All tests green before commit.
- Node tests run as `node <file>` from the repo root; no npm, no dependencies.
- Commit format: `<type>: <summary>`, type in feat/fix/docs/refactor/test/chore,
  summary max 72 chars.
- Do not touch `snapgent-app/`, the provider adapters, or the Roblox strings in
  the extension. Out of scope.
- Delete, do not deprecate.

---

## Task 1: `a2a_server.py` — Agent Card, JSON-RPC, task store, SSE, auth

Files: `a2a_server.py` (new), `test_a2a_server.py` (new).

Write the tests first, then the module.

Module surface (exact names):

```python
A2A_VERSION = "1.0"
DEFAULT_PORT = 17614
DEFAULT_TIMEOUT_MS = 600_000

def agent_card(base_url: str) -> dict
class TaskStore:                      # thread-safe
    def add(self, task: dict) -> None
    def get(self, task_id: str) -> Optional[dict]
    def update(self, task_id: str, **fields) -> Optional[dict]
class A2AServer:
    def __init__(self, submit, host="127.0.0.1", port=DEFAULT_PORT,
                 token="", base_url="", timeout_ms=DEFAULT_TIMEOUT_MS) -> None
    def start(self) -> None           # binds; stores self.port with the real port
    def stop(self) -> None
```

`submit` is `Callable[[str, float], dict]` returning
`{"ok": True, "text": "..."}` or `{"ok": False, "error": "..."}`. It blocks.

`agent_card` returns a 1.0 card plus legacy fields:

```json
{
  "name": "Snapgent Web Chat",
  "description": "Delegates a prompt to a free web AI chat tab driven by the Snapgent browser extension.",
  "version": "1.0.0",
  "supportedInterfaces": [
    {"url": "<base_url>", "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
  ],
  "capabilities": {"streaming": true, "pushNotifications": false},
  "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
  "securityRequirements": [{"bearer": []}],
  "defaultInputModes": ["text/plain"],
  "defaultOutputModes": ["text/plain"],
  "skills": [{
    "id": "web-chat-delegation",
    "name": "Delegate a prompt to a web AI chat",
    "description": "Send a prompt to a signed-in web AI chat tab and return its reply. The model may read files through its Snapgent MCP tools while it works.",
    "tags": ["web-chat", "browser", "delegation"],
    "examples": ["Summarize docs/a2a-design.md"]
  }],
  "url": "<base_url>",
  "protocolVersion": "1.0",
  "preferredTransport": "JSONRPC"
}
```

Methods: `SendMessage` (`message/send`), `SendStreamingMessage`
(`message/stream`), `GetTask` (`tasks/get`). Unknown method -> `-32601`.
Message without a text part -> `-32602`. Unknown task id in `GetTask` ->
`-32001`.

Prompt text = concatenation of every `Part` that has a string `text` member.

Blocking `SendMessage`: create the task, set it `TASK_STATE_WORKING`, call
`submit`, then set `TASK_STATE_COMPLETED` with one `Artifact`
(`artifactId`, `name: "reply"`, `parts: [{"text": <reply>}]`) plus an A2A
`Message` with `role: "ROLE_AGENT"` appended to `history`, or
`TASK_STATE_FAILED` with a status `message` carrying the error.

`returnImmediately: true`: return the task after CREATING it, in
`TASK_STATE_SUBMITTED`, and run `submit` on a daemon thread.

`SendStreamingMessage`: `Content-Type: text/event-stream`, then in order
`data: {"task": {...}}` (working), `data: {"artifactUpdate": {...}}`,
`data: {"statusUpdate": {... completed}}`, then close. On failure emit the
`statusUpdate` with `TASK_STATE_FAILED`.

`GetTask` honours `historyLength` (0 -> empty history; unset -> full).
`SendMessage` also passes its `configuration.historyLength` through.

HTTP: `GET /.well-known/agent-card.json` and `GET /.well-known/agent.json`
return the card, no auth. `POST /` requires the bearer token; on failure
HTTP 401 with `WWW-Authenticate: Bearer`. Wrong path -> 404.

Tests (`test_a2a_server.py`, `urllib.request`, `A2AServer(port=0)`):

1. `test_agent_card_is_served_without_auth` — GET the well-known path, assert
   `supportedInterfaces[0].protocolVersion == "1.0"` and a `skills` entry.
2. `test_post_without_token_is_unauthorized` — 401 and `WWW-Authenticate`.
3. `test_unknown_method_is_method_not_found` — `-32601`.
4. `test_message_without_text_part_is_invalid_params` — `-32602`.
5. `test_send_message_completes_with_artifact` — stub submit returns text; the
   result task is `TASK_STATE_COMPLETED` with the text in `artifacts[0].parts`.
6. `test_send_message_failure_sets_failed` — stub returns `ok: False`.
7. `test_return_immediately_returns_working_task` — the response state is not
   `TASK_STATE_COMPLETED`; then poll `GetTask` until completed.
8. `test_get_task_unknown_id` — `-32001`.
9. `test_get_task_history_length_zero` — `history == []`.
10. `test_stream_emits_task_then_artifact_then_status` — read the SSE body,
    assert the three JSON payloads in order.

Verification: `python3 -m unittest test_a2a_server -v`.

Commit: `feat: add A2A JSON-RPC server facade`

---

## Task 2: Wire the A2A server into `bridge.py`

Files: `bridge.py`, `test_bridge.py`.

Add to `Bridge`:

- `self._pending_prompts: Dict[str, asyncio.Future] = {}`
- `self._prompt_lock = asyncio.Lock()`

New method:

```python
async def submit_prompt(self, prompt: str, timeout: float) -> Dict[str, Any]:
    """Single-flight: one browser tab, one generation."""
    if not isinstance(prompt, str) or not prompt.strip():
        return {"ok": False, "error": "Empty prompt."}
    async with self._prompt_lock:
        if not self.clients:
            return {"ok": False, "error": "No Snapgent extension is connected."}
        if not any(server.alive for server in self.servers.values()) and not self._catalog:
            # Not fatal: the model may not need MCP tools. Only log.
            log("submit_prompt: no MCP tools available")
        prompt_id = f"p{uuid.uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._pending_prompts[prompt_id] = future
        await self.broadcast({"type": "submit_prompt", "id": prompt_id, "prompt": prompt})
        try:
            result = await asyncio.wait_for(future, timeout)
        except asyncio.TimeoutError:
            return {"ok": False, "error": f"The web AI did not answer within {int(timeout)}s."}
        finally:
            self._pending_prompts.pop(prompt_id, None)
        return result
```

In `_handle_request`, add:

```python
        elif kind == "prompt_result":
            await self._resolve_prompt(websocket, request)
```

```python
    async def _resolve_prompt(self, websocket: Any, request: Dict[str, Any]) -> None:
        prompt_id = request.get("prompt_id")
        future = self._pending_prompts.pop(prompt_id, None) if isinstance(prompt_id, str) else None
        if future is not None and not future.done():
            if request.get("ok"):
                future.set_result({"ok": True, "text": str(request.get("text") or "")})
            else:
                future.set_result({"ok": False, "error": str(request.get("error") or "The web AI failed.")})
        await self._send_client(websocket, {"type": "prompt_ack", "id": request.get("id")})
```

In `handle_client`'s `finally`, after discarding the socket: if no clients
remain, fail every pending prompt with
`{"ok": False, "error": "The extension disconnected."}`.

Add a module-level builder:

```python
def build_a2a_server(bridge: Bridge, loop: asyncio.AbstractEventLoop) -> Any:
    """Return an A2AServer whose submit() runs bridge.submit_prompt on `loop`."""
```

It reads `ZS_A2A_PORT`, `ZS_A2A_TOKEN`, `ZS_A2A_TIMEOUT_MS`,
`ZS_A2A_DISABLE`. Token: env value, else `secrets.token_urlsafe(32)`.
`base_url = f"http://{HOST}:{port}"`. `submit` uses
`asyncio.run_coroutine_threadsafe(...).result(timeout + 60)` and maps
`concurrent.futures.TimeoutError` to `{"ok": False, "error": "timed out"}`.

In `run()`: after `websockets.serve`, unless disabled, `a2a = build_a2a_server(bridge, loop); a2a.start()` and log the endpoint and token. In the `finally`
block: `a2a.stop()` if started.

Tests (`test_bridge.py`):

1. `test_submit_prompt_resolves_on_prompt_result` — start a `Bridge`, inject a
   fake client object with an async `send`, call `submit_prompt` as a task,
   await the broadcast, then feed `{"type":"prompt_result","id":7,
   "prompt_id":<id>,"ok":True,"text":"hi"}` through `_handle_request`, assert
   the result is `{"ok": True, "text": "hi"}`.
2. `test_submit_prompt_requires_extension` — no clients -> `ok False`.
3. `test_submit_prompt_fails_when_extension_disconnects` — start a submit,
   simulate the disconnect path, assert `ok False`.

Verification: `python3 -m unittest test_bridge -v`.

Commit: `feat: add submit_prompt plumbing to the bridge`

---

## Task 3: Extension relay (`background.js`) + node test

Files: `snapgent-extension/background.js`, `snapgent-extension/test-a2a.js`.

`background.js`:

- In `handleBridgeMessage`, before the `connected` branch:

```js
  if (msg.type === "submit_prompt") {
    handleSubmitPrompt(msg);
    return;
  }
  if (msg.type === "prompt_ack") {
    return; // fire-and-forget relay: nothing to resolve
  }
```

- New function:

```js
// Relay a bridge prompt to the active provider tab. Fire-and-forget: the
// content script answers later via an "a2a_result" runtime message. An MV3
// worker eviction between the two hops is expected and handled by reconnect.
async function handleSubmitPrompt(msg) {
  const promptId = msg.id;
  const reply = (payload) =>
    send({ type: "prompt_result", prompt_id: promptId, ok: !!payload.ok,
           text: payload.text || "", error: payload.error || "" }, 15000);
  try {
    let tabs = await chrome.tabs.query({ url: PROVIDER_URLS, active: true, lastFocusedWindow: true });
    if (!tabs.length) tabs = await chrome.tabs.query({ url: PROVIDER_URLS });
    const tab = tabs.find((t) => t.active) || tabs[0];
    if (!tab) { await reply({ ok: false, error: "No AI chat tab is open." }); return; }
    try {
      await chrome.tabs.sendMessage(tab.id, { type: "zs-a2a-prompt", prompt: msg.prompt, promptId });
    } catch (e) {
      await reply({ ok: false, error: "The AI chat tab did not accept the prompt." });
    }
  } catch (e) {
    await reply({ ok: false, error: String((e && e.message) || e) });
  }
}
```

- In the `chrome.runtime.onMessage` switch, add:

```js
      case "a2a_result": {
        await send({ type: "prompt_result", prompt_id: msg.promptId,
                     ok: !!msg.ok, text: msg.text || "", error: msg.error || "" }, 15000);
        sendResponse({ ok: true });
        break;
      }
```

`test-a2a.js`, standalone, same style as `test-chatgpt.js` (no npm):

- Stub `chrome` (`runtime.sendMessage` -> resolved promise,
  `runtime.onMessage.addListener` capturing the listener, `tabs.query` ->
  resolved provider tab, `tabs.sendMessage` recorder,
  `runtime.onStartup/onInstalled.addListener`, `tabs.sendMessage` rejects are
  never triggered).
- Stub `WebSocket` capturing the instance; call `onopen()` after load so
  `connected` becomes true; `send` records outgoing frames and calls the
  matching `pending` resolution? No: for `prompt_result` we only assert the
  frame was sent.
- Load `background.js` with `new Function("chrome", "WebSocket", src + "; return { handleBridgeMessage, handleSubmitPrompt };")`.
- Assertions: `handleBridgeMessage({type:"submit_prompt", id:"p1", prompt:"hi"})`
  leads to one `tabs.sendMessage` with `type === "zs-a2a-prompt"` and
  `promptId === "p1"`. Then invoke the captured runtime listener with
  `{type:"a2a_result", promptId:"p1", ok:true, text:"hello"}` and assert a
  frame with `type:"prompt_result"`, `prompt_id:"p1"`, `text:"hello"` was sent
  on the socket.
- Print a PASS/FAIL summary; exit non-zero on failure.

Verification: `node snapgent-extension/test-a2a.js`.

Commit: `feat: relay A2A prompts through the extension background`

---

## Task 4: Content-script handler (`core/main.js`)

Files: `snapgent-extension/core/main.js`.

Add inside the main IIFE, next to the existing wiring, and register it in the
`chrome.runtime.onMessage` listener at the WIRING section:

```js
  // A2A delegation: run one prompt to completion and report the final text
  // back to the background worker. The background relays it to the bridge.
  async function runA2APrompt(prompt, promptId) {
    const reply = (payload) => {
      try {
        chrome.runtime.sendMessage({ type: "a2a_result", promptId, ...payload }).catch(() => {});
      } catch {}
    };
    try {
      if (A.running || A.starting || A.injecting) throw new Error("The web AI is already working on another task.");
      if (!A.started) {
        await startSession();
        if (!A.started) throw new Error("Could not start a Snapgent session in this chat.");
      }
      const base = await submitAndGetBase(prompt);
      await agentLoop(base);            // returns when no tool call is pending
      const item = P.lastAssistant();
      const text = item ? P.itemText(item) : "";
      reply({ ok: true, text: text || "" });
    } catch (e) {
      reply({ ok: false, error: String((e && e.message) || e) });
    }
  }
```

Register:

```js
    if (msg && msg.type === "zs-a2a-prompt") {
      runA2APrompt(msg.prompt, msg.promptId); // async; result goes via runtime message
    }
```

Constraints: keep the handler inside the existing IIFE so it can close over
`A`, `P`, `startSession`, `submitAndGetBase`, `agentLoop`. Do not return `true`
for this branch. `agentLoop` already no-ops when `A.running`.

Verification: `node -e "new Function(require('fs').readFileSync('snapgent-extension/core/main.js','utf8'))"`
parses the file (syntax check), and
`python3 -m unittest` stays green. Also run the existing node tests:
`node snapgent-extension/test-parser.js` and
`node snapgent-extension/test-chatgpt.js`.

Commit: `feat: add A2A prompt handler to the content script`

---

## Task 5: Docs and skill

Files: `docs/a2a.md` (new), `.snapgent/skills/a2a-bridge/SKILL.md` (new),
`CHANGELOG.md`, `README.md` (pointer only).

1. `docs/a2a.md` — how to build and how to use:
   - What it is, one paragraph, and the data-flow diagram from the design.
   - Run: `bash Linux_Start.sh`; the bridge prints the A2A URL and token.
   - Auth: `Authorization: Bearer $ZS_A2A_TOKEN`.
   - Complete `curl` examples for each method: `SendMessage`, `GetTask`,
     `SendMessage` with `returnImmediately`, `SendStreamingMessage`.
   - Env vars table from the design.
   - Limits: foreground tab, concurrency 1, 10 min timeout, no cancel, no push.
2. `.snapgent/skills/a2a-bridge/SKILL.md` — front matter shape copied from
   `.snapgent/skills/linux-bridge/SKILL.md`; sections Build, Run, Authenticate,
   Delegate a prompt, Poll, Stream, Troubleshoot.
3. `CHANGELOG.md` — one entry under `## Unreleased` (create if absent) naming:
   A2A server, bridge `submit_prompt`, extension relay, docs, skill.
4. `README.md` — one pointer sentence to `docs/a2a.md`. Do not rewrite the
   README.

Verification: every command in `docs/a2a.md` is copy-pasteable and matches the
implementation; `python3 -m unittest` green.

Commit: `docs: document the A2A bridge server and add the skill`

---

## Task 6: Final review

Files: none.

1. Whole-branch diff review over `106efab`.
2. Confirm: only stdlib imports added; `127.0.0.1` binding; token enforced on
   POST; three methods plus aliases; no `kind` field in emitted A2A objects;
   `TASK_STATE_*` values only; no new dependency.
3. `python3 -m unittest` green; `node snapgent-extension/test-a2a.js` passes.
4. Report findings; fix in one dispatch if any.
