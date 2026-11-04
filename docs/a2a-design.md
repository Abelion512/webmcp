# Design: A2A server facade over the Snapgent browser bridge

Date: 2026-11-04
Branch: `a2a`
Base: `linux-stability` @ `106efab`

## Problem

The Snapgent bridge (`bridge.py`) is a WebSocket RPC server that only the bundled
browser extension speaks. A local CLI agent (Hermes, Claude Code, OpenCode,
another machine) cannot delegate a prompt to a free web AI chat without speaking
that private protocol.

## Goal

Expose the existing browser-driven web AI chat as an **A2A (Agent2Agent) agent**.
Any A2A client can discover it, send a prompt, poll the task, or stream the
result. The browser extension keeps doing all DOM work; Python only adds a thin
HTTP facade.

## Scope

In scope:

- `a2a_server.py` (new): Agent Card, JSON-RPC 2.0 dispatch, task store, SSE
  streaming, bearer-token auth, single-flight queue.
- `bridge.py`: a new outbound `submit_prompt` message and its inbound
  `prompt_result` counterpart; start/stop the A2A server in the same process.
- `snapgent-extension/background.js`: relay a bridge `submit_prompt` to the
  active provider tab, and relay the tab's result back to the bridge.
- `snapgent-extension/core/main.js`: a `zs-a2a-prompt` content-script handler
  that starts the session if needed, submits the prompt, runs the existing
  agentic loop, and returns the final assistant text.
- Tests, docs, and a `.snapgent/skills/` entry.

Out of scope:

- A2A push notifications, authenticated extended card, task subscription.
- `tasks/cancel` (the extension cannot interrupt a generation cleanly).
- Concurrency above 1. One tab, one generation.
- Driving the browser via CDP. The extension is the only browser driver.
- Provider adapter changes. All 12 providers keep working unchanged.
- The Linux VS Code/Roblox cleanup. That is a separate branch.

## A2A version

Target **A2A 1.0**: JSON-RPC 2.0 binding, PascalCase method names
(`SendMessage`, `SendStreamingMessage`, `GetTask`), unified `Part` with
member-based discrimination, `TASK_STATE_*` enum, `supportedInterfaces[]` in the
Agent Card, SSE event objects wrapped as `statusUpdate` / `artifactUpdate`.

Compatibility shim: the same methods are also routed under their A2A v0.3 names
(`message/send`, `message/stream`, `tasks/get`). Aliases change routing only;
responses are always 1.0 shapes. This costs a few dict entries and lets
pre-1.0 SDK clients connect.

## Architecture

```
A2A client ──HTTP/JSON-RPC──> a2a_server.py ──asyncio callback──> bridge.py
                                                                    │ WebSocket
                                                                    ▼
                                                    background.js (service worker)
                                                                    │ tabs.sendMessage
                                                                    ▼
                                                            core/main.js (content script)
                                                                    │ DOM
                                                                    ▼
                                                       ChatGPT / Claude / Gemini / ...
```

`a2a_server.py` runs a `ThreadingHTTPServer` on `127.0.0.1:17614` in a daemon
thread. It never touches the browser. To run a prompt it calls an injected
callable that schedules `Bridge.submit_prompt` on the bridge's asyncio loop with
`asyncio.run_coroutine_threadsafe`.

`Bridge.submit_prompt(prompt, timeout)` holds an `asyncio.Lock` (single-flight),
creates a future, broadcasts `{"type":"submit_prompt","id","prompt"}` to the
extension, and awaits the matching `prompt_result`.

## Components

### 1. `a2a_server.py`

Public surface:

- `agent_card(base_url) -> dict`
- `TaskStore` (thread-safe `dict` of task id to task)
- `A2AServer(submit, host, port, token, base_url)` with `start()` / `stop()`

HTTP routes:

| Method | Path | Auth | Response |
| --- | --- | --- | --- |
| GET | `/.well-known/agent-card.json` | no | Agent Card (JSON) |
| GET | `/.well-known/agent.json` | no | Agent Card (v0.3 path alias) |
| POST | `/` | Bearer | JSON-RPC 2.0 response |

The Agent Card is public: a client needs it to learn the endpoint and the
security scheme. Everything else requires `Authorization: Bearer <token>`.
Missing or wrong token returns HTTP 401 with `WWW-Authenticate: Bearer`.

Content type: `application/json` for responses, `text/event-stream` for
`SendStreamingMessage`.

### 2. JSON-RPC methods

| Method (aliases) | Params | Result |
| --- | --- | --- |
| `SendMessage` (`message/send`) | `SendMessageRequest` | `Task` |
| `SendStreamingMessage` (`message/stream`) | `SendMessageRequest` | SSE stream |
| `GetTask` (`tasks/get`) | `{id, historyLength}` | `Task` |

`SendMessage` is blocking by default: it returns when the task reaches
`TASK_STATE_COMPLETED` or `TASK_STATE_FAILED`. With
`configuration.returnImmediately = true` it returns the task in
`TASK_STATE_SUBMITTED` or `TASK_STATE_WORKING` immediately, and the client polls
`GetTask`. `historyLength` trims the returned `history` array.

`SendStreamingMessage` emits, in order:

```
data: {"task": { ...status working... }}
data: {"artifactUpdate": {"taskId","contextId","artifact":{...},"index":0}}
data: {"statusUpdate": {"taskId","contextId","status":{"state":"TASK_STATE_COMPLETED"}}}
```

Errors follow standard JSON-RPC codes (-32700, -32600, -32601, -32602, -32603)
plus `-32001` Task not found for an unknown `GetTask` id.

### 3. Task model

`Task` = `{id, contextId, status, artifacts, history, metadata}`.

`TaskStatus` = `{state, message?, timestamp?}` where `message` is an A2A
`Message` for the terminal state.

States used: `TASK_STATE_SUBMITTED`, `TASK_STATE_WORKING`,
`TASK_STATE_COMPLETED`, `TASK_STATE_FAILED`.

`SendMessageRequest.params` = `{message, configuration?, metadata?}` where
`message` is an A2A `Message` = `{messageId, role, parts, contextId?, taskId?}`.
The prompt text is the concatenation of every `text` part. A message with no
text part is rejected with `-32602`.

The reply is one `Artifact` (`artifactId`, `name`, `parts: [{text: <reply>}]`).

### 4. Single-flight

The extension drives exactly one browser tab and one generation. All three
methods funnel through `Bridge.submit_prompt`, which serialises on an
`asyncio.Lock`. A second A2A task waits for the first to finish. There is no
worker pool, by design: `ponytail: single-flight; add a worker pool when a
second browser host exists`.

### 5. Completion condition

`Bridge.submit_prompt` resolves only when the extension reports that the model
stopped generating **and** no snapgent tool call is pending. The extension
achieves this by awaiting the existing `agentLoop`, which returns only after a
final non-command reply. One A2A task may therefore contain many
`workspace_mcp` tool round-trips.

Default task timeout: 600 s (`ZS_A2A_TIMEOUT_MS`). On timeout the task ends
`TASK_STATE_FAILED`.

### 6. Authentication

`ZS_A2A_TOKEN` sets the bearer token. If unset, the bridge generates one with
`secrets.token_urlsafe(32)` at start and prints it. The server binds
`127.0.0.1` only.

Rationale, non-negotiable: this endpoint writes filesystem policy and drives a
logged-in browser session. A local port with no token is reachable by any page
in that browser via a simple `fetch`.

### 7. Extension relay

`background.js`:

- bridge `submit_prompt` -> find the active provider tab, send
  `{type:"zs-a2a-prompt", prompt, promptId}`, do not await the reply.
- content script -> `{type:"a2a_result", promptId, ok, text, error}` via
  `chrome.runtime.sendMessage`; `background.js` forwards it to the bridge as
  `{"type":"prompt_result","prompt_id","ok","text","error"}`.

Fire-and-forget across the MV3 hop, not a long-lived `sendResponse`: an MV3
service worker can be evicted mid-task, and each hop re-wakes it. The bridge
fails pending prompts if the extension disconnects.

`core/main.js` handler:

```
if (A.running || A.starting || A.injecting) -> error "agent busy"
if (!A.started) { await startSession(); if (!A.started) -> error }
const base = await submitAndGetBase(prompt);
await agentLoop(base);            // returns when no tool call is pending
return P.itemText(P.lastAssistant()) || "";
```

## Error handling

| Condition | Result |
| --- | --- |
| No token / wrong token | HTTP 401 |
| No extension connected | task `FAILED`, message `extension not connected` |
| Agent busy / session cannot start | task `FAILED`, message from the content script |
| Task timeout | task `FAILED`, message `timed out after Ns` |
| Unknown `GetTask` id | JSON-RPC `-32001` |
| Message with no text part | JSON-RPC `-32602` |
| Unknown method | JSON-RPC `-32601` |

## Configuration

| Env var | Default | Meaning |
| --- | --- | --- |
| `ZS_A2A_PORT` | `17614` | HTTP listen port |
| `ZS_A2A_TOKEN` | random | Bearer token |
| `ZS_A2A_TIMEOUT_MS` | `600000` | Task timeout (10 min) |
| `ZS_A2A_DISABLE` | unset | `1` skips starting the A2A server |

## Testing

- `test_a2a_server.py`: agent card shape and version; 401 without token; unknown
  method (`-32601`); no-text message (`-32602`); `GetTask` unknown id
  (`-32001`); `SendMessage` blocking completes with an artifact; blocking
  failure sets `FAILED`; `returnImmediately` returns early; SSE emits the three
  events.
- `test_bridge.py`: `submit_prompt` round-trip resolves on a `prompt_result` for
  the matching id; no extension fails fast; disconnect fails pending prompts.
- `snapgent-extension/test-a2a.js`: `background.js` relays `submit_prompt` to the
  chosen tab and relays `a2a_result` back to the bridge (stubbed `chrome` and
  `WebSocket`, no npm).

Run: `python3 -m unittest` and `node snapgent-extension/test-a2a.js`.

## Risks

- The extension websocket on `ws://127.0.0.1:17613` is **not authenticated**.
  Any local process can connect to it. The A2A facade mitigates this by sending
  `submit_prompt` to a single socket and resolving a task only from the socket
  that received it, but it cannot stop a local process from connecting at all.
  This is pre-existing bridge behaviour; authenticating the extension socket is
  the real fix and is out of scope here.
- The browser tab must be the foreground tab of its window. A backgrounded tab
  pauses the loop. Same constraint as the existing bridge, now exposed to A2A
  clients.
- An MV3 service-worker eviction mid-task is tolerated by the fire-and-forget
  relay, but the content script's own loop still dies with the tab. Documented
  limit.
- The A2A server adds a second local port. It is loopback-only and token-gated.
