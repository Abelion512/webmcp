---
name: a2a-bridge
description: Use when building, running, authenticating, or debugging the A2A HTTP facade that exposes the Snapgent web AI chat to local A2A clients.
---

# A2A bridge skill

Exposes the browser-driven web AI chat as an A2A 1.0 agent over
`http://127.0.0.1:17614`. See `docs/a2a.md` for the full reference.

## Build

No build step. stdlib only.

```
python3 -m unittest
node snapgent-extension/test-a2a.js
```

## Run

```
bash Linux_Start.sh
```

`Linux_Start.sh` runs `python3 bridge.py` in the foreground. Startup prints the
endpoint and the token:

```
A2A agent card: http://127.0.0.1:17614/.well-known/agent-card.json
A2A bearer token: <token>
```

Keep the terminal open. `ZS_A2A_DISABLE=1` skips the A2A server.

## Authenticate

```
export ZS_A2A_TOKEN=<token from the bridge log>
```

Every `POST /` needs `Authorization: Bearer $ZS_A2A_TOKEN`. The Agent Card is
public:

```
curl -s http://127.0.0.1:17614/.well-known/agent-card.json
```

## Delegate a prompt

```
curl -s -X POST http://127.0.0.1:17614/ \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ZS_A2A_TOKEN" \
  -d '{
    "jsonrpc": "2.0",
    "id": 1,
    "method": "SendMessage",
    "params": {
      "message": {
        "messageId": "msg-1",
        "role": "ROLE_USER",
        "parts": [{"text": "Summarize docs/a2a-design.md"}]
      }
    }
  }'
```

Blocking: returns the task in `TASK_STATE_COMPLETED` with the reply in
`result.artifacts[0].parts[0].text`, or `TASK_STATE_FAILED` with the error in
`result.status.message.parts[0].text`.

## Poll

Add `"configuration": {"returnImmediately": true}` to the request, read
`result.id`, then:

```
curl -s -X POST http://127.0.0.1:17614/ \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ZS_A2A_TOKEN" \
  -d '{"jsonrpc":"2.0","id":2,"method":"GetTask","params":{"id":"<task id>"}}'
```

`historyLength` in the params trims `history`. Unknown id returns `-32001`.

## Stream

```
curl -sN -X POST http://127.0.0.1:17614/ \
  -H 'Content-Type: application/json' \
  -H "Authorization: Bearer $ZS_A2A_TOKEN" \
  -d '{
    "jsonrpc": "2.0",
    "id": 3,
    "method": "SendStreamingMessage",
    "params": {
      "message": {
        "messageId": "msg-2",
        "role": "ROLE_USER",
        "parts": [{"text": "Summarize docs/a2a-design.md"}]
      }
    }
  }'
```

Three SSE events in order: `{"task": ...}` (working), `{"artifactUpdate": ...}`,
`{"statusUpdate": ...}` (completed or failed).

## Troubleshoot

- HTTP 401: the `Authorization` header is missing or does not match the printed
  token. `ZS_A2A_TOKEN` only applies to the process that printed it.
- Task `FAILED` with `No Snapgent extension is connected.`: load the extension
  and open a supported AI chat tab.
- Task `FAILED` with `The web AI did not answer within <N>s.`: the tab is
  backgrounded, the model stalled, or the work exceeded `ZS_A2A_TIMEOUT_MS`.
- Task `FAILED` with `The web AI is already working on another task.`: one
  generation at a time; wait for the current task or cancel it in the chat.
- Port already in use: set `ZS_A2A_PORT`, or stop the process holding 17614.
- No A2A lines in the log: `ZS_A2A_DISABLE=1` is set, or the port bind failed
  (the bridge logs `A2A server disabled: ...`).