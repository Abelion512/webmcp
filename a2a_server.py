#!/usr/bin/env python3
"""A2A 1.0 HTTP facade over the Snapgent browser bridge.

The server never touches the browser. It calls an injected ``submit(prompt,
timeout)`` callable that blocks until the bridge reports the reply. See
``docs/a2a-design.md`` for the architecture.

stdlib only.
"""

import copy
import json
import secrets
import threading
import uuid
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Dict, Optional
from urllib.parse import urlsplit

A2A_VERSION = "1.0"
DEFAULT_PORT = 17614
DEFAULT_TIMEOUT_MS = 600_000
MAX_BODY_BYTES = 2 * 1024 * 1024
MAX_TASKS = 200

CARD_PATHS = ("/.well-known/agent-card.json", "/.well-known/agent.json")

_METHOD_ALIASES = {
    "SendMessage": "SendMessage",
    "message/send": "SendMessage",
    "SendStreamingMessage": "SendStreamingMessage",
    "message/stream": "SendStreamingMessage",
    "GetTask": "GetTask",
    "tasks/get": "GetTask",
}


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def agent_card(base_url: str) -> Dict[str, Any]:
    """Return the A2A 1.0 Agent Card, plus the legacy top-level fields."""
    return {
        "name": "Snapgent Web Chat",
        "description": (
            "Delegates a prompt to a free web AI chat tab driven by the "
            "Snapgent browser extension."
        ),
        "version": "1.0.0",
        "supportedInterfaces": [
            {"url": base_url, "protocolBinding": "JSONRPC", "protocolVersion": "1.0"}
        ],
        "capabilities": {"streaming": True, "pushNotifications": False},
        "securitySchemes": {"bearer": {"type": "http", "scheme": "bearer"}},
        "securityRequirements": [{"bearer": []}],
        "defaultInputModes": ["text/plain"],
        "defaultOutputModes": ["text/plain"],
        "skills": [
            {
                "id": "web-chat-delegation",
                "name": "Delegate a prompt to a web AI chat",
                "description": (
                    "Send a prompt to a signed-in web AI chat tab and return its "
                    "reply. The model may read files through its Snapgent MCP "
                    "tools while it works."
                ),
                "tags": ["web-chat", "browser", "delegation"],
                "examples": ["Summarize docs/a2a-design.md"],
            }
        ],
        "url": base_url,
        "protocolVersion": "1.0",
        "preferredTransport": "JSONRPC",
    }


def _concat_text(message: Any) -> Optional[str]:
    """Prompt text = concatenation of every Part with a string ``text`` member."""
    if not isinstance(message, dict):
        return None
    parts = message.get("parts")
    if not isinstance(parts, list):
        return None
    texts = [
        part["text"]
        for part in parts
        if isinstance(part, dict) and isinstance(part.get("text"), str)
    ]
    if not texts:
        return None
    return "".join(texts)


class TaskStore:
    """Thread-safe task store: id -> task dict."""

    def __init__(self) -> None:
        self._tasks: Dict[str, Dict[str, Any]] = {}
        self._lock = threading.Lock()

    def add(self, task: Dict[str, Any]) -> None:
        with self._lock:
            self._tasks[task["id"]] = task
            # ponytail: cap the store; add TTL eviction if tasks ever outlive a session.
            while len(self._tasks) > MAX_TASKS:
                self._tasks.pop(next(iter(self._tasks)))

    def get(self, task_id: str) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            return copy.deepcopy(task) if task is not None else None

    def update(self, task_id: str, **fields: Any) -> Optional[Dict[str, Any]]:
        with self._lock:
            task = self._tasks.get(task_id)
            if task is None:
                return None
            task.update(fields)
            return copy.deepcopy(task)


class A2AServer:
    """ThreadingHTTPServer wrapping a blocking ``submit`` callable."""

    def __init__(
        self,
        submit: Callable[[str, float], Dict[str, Any]],
        host: str = "127.0.0.1",
        port: int = DEFAULT_PORT,
        token: str = "",
        base_url: str = "",
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        self.submit = submit
        self.host = host
        self.port = port
        self.token = token
        self.base_url = base_url
        self.timeout_ms = timeout_ms
        self.store = TaskStore()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        self._httpd = ThreadingHTTPServer((self.host, self.port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.a2a = self  # type: ignore[attr-defined]
        self.port = self._httpd.server_address[1]
        if not self.base_url or self.base_url.endswith(":0"):
            self.base_url = f"http://{self.host}:{self.port}"
        self._thread = threading.Thread(target=self._httpd.serve_forever, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        if self._httpd is not None:
            self._httpd.shutdown()
            self._httpd.server_close()
            self._httpd = None
        if self._thread is not None:
            self._thread.join(timeout=5)
            self._thread = None

    # -- HTTP surface ----------------------------------------------------

    def card(self) -> Dict[str, Any]:
        return agent_card(self.base_url)

    def authorized(self, header: Optional[str]) -> bool:
        if not self.token or not isinstance(header, str):
            return False
        # Bytes: a non-ASCII Authorization header must be rejected, not raise.
        return secrets.compare_digest(
            header.encode("utf-8"), f"Bearer {self.token}".encode("utf-8")
        )

    # -- JSON-RPC --------------------------------------------------------

    @staticmethod
    def error(rpc_id: Any, code: int, message: str, data: Any = None) -> Dict[str, Any]:
        error: Dict[str, Any] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return {"jsonrpc": "2.0", "id": rpc_id, "error": error}

    @staticmethod
    def _result(rpc_id: Any, result: Any) -> Dict[str, Any]:
        return {"jsonrpc": "2.0", "id": rpc_id, "result": result}

    def dispatch(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        rpc_id = payload.get("id")
        method = payload.get("method")
        if payload.get("jsonrpc") != "2.0" or not isinstance(method, str):
            return self.error(rpc_id, -32600, "Invalid Request")
        name = _METHOD_ALIASES.get(method)
        if name == "SendMessage":
            return self._send_message(payload)
        if name == "GetTask":
            return self._get_task(payload)
        return self.error(rpc_id, -32601, "Method not found")

    def _send_message(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        rpc_id = payload.get("id")
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}
        message = params.get("message")
        text = _concat_text(message)
        if text is None:
            return self.error(rpc_id, -32602, "Message must contain a text part.")

        configuration = params.get("configuration")
        configuration = configuration if isinstance(configuration, dict) else {}
        history_length = configuration.get("historyLength")

        task = self._new_task(message, params)
        self.store.add(task)
        if configuration.get("returnImmediately"):
            # Snapshot before the worker can flip the task to WORKING/COMPLETED.
            view = self._view(self.store.get(task["id"]), history_length)
            threading.Thread(
                target=self._run_submit, args=(task["id"], text), daemon=True
            ).start()
            return self._result(rpc_id, view)

        self.store.update(
            task["id"], status={"state": "TASK_STATE_WORKING", "timestamp": _now_iso()}
        )
        self._run_submit(task["id"], text)
        return self._result(rpc_id, self._view(self.store.get(task["id"]), history_length))

    def _get_task(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        rpc_id = payload.get("id")
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}
        task_id = params.get("id")
        task = self.store.get(task_id) if isinstance(task_id, str) else None
        if task is None:
            return self.error(rpc_id, -32001, "Task not found")
        return self._result(rpc_id, self._view(task, params.get("historyLength")))

    # -- streaming -------------------------------------------------------

    def stream(self, handler: "_Handler", payload: Dict[str, Any]) -> None:
        rpc_id = payload.get("id")
        params = payload.get("params")
        params = params if isinstance(params, dict) else {}
        message = params.get("message")
        text = _concat_text(message)
        if text is None:
            handler.send_json(self.error(rpc_id, -32602, "Message must contain a text part."))
            return

        task = self._new_task(message, params)
        task["status"] = {"state": "TASK_STATE_WORKING", "timestamp": _now_iso()}
        self.store.add(task)

        handler.start_sse()
        handler.send_sse({"task": self._view(task, None)})

        result = self._call_submit(text)
        if result.get("ok"):
            text_out = str(result.get("text") or "")
            artifact = self._artifact(text_out)
            completed = self._complete(task["id"], text_out, artifact=artifact)
            handler.send_sse(
                {
                    "artifactUpdate": {
                        "taskId": task["id"],
                        "contextId": task["contextId"],
                        "artifact": artifact,
                        "index": 0,
                    }
                }
            )
            handler.send_sse(
                {
                    "statusUpdate": {
                        "taskId": task["id"],
                        "contextId": task["contextId"],
                        "status": completed["status"],
                    }
                }
            )
        else:
            failed = self._fail(
                task["id"], str(result.get("error") or "The web AI failed.")
            )
            handler.send_sse(
                {
                    "statusUpdate": {
                        "taskId": task["id"],
                        "contextId": task["contextId"],
                        "status": failed["status"],
                    }
                }
            )

    # -- task helpers ----------------------------------------------------

    def _new_task(self, message: Any, params: Dict[str, Any]) -> Dict[str, Any]:
        context_id = f"ctx-{uuid.uuid4().hex}"
        if isinstance(message, dict) and isinstance(message.get("contextId"), str):
            context_id = message["contextId"] or context_id
        metadata = params.get("metadata")
        if not isinstance(metadata, dict):
            metadata = {}
        return {
            "id": f"task-{uuid.uuid4().hex}",
            "contextId": context_id,
            "status": {"state": "TASK_STATE_SUBMITTED", "timestamp": _now_iso()},
            "artifacts": [],
            "history": [message],
            "metadata": metadata,
        }

    @staticmethod
    def _artifact(text: str) -> Dict[str, Any]:
        return {
            "artifactId": f"artifact-{uuid.uuid4().hex}",
            "name": "reply",
            "parts": [{"text": text}],
        }

    @staticmethod
    def _agent_message(task: Dict[str, Any], text: str) -> Dict[str, Any]:
        return {
            "messageId": f"msg-{uuid.uuid4().hex}",
            "role": "ROLE_AGENT",
            "parts": [{"text": text}],
            "taskId": task.get("id", ""),
            "contextId": task.get("contextId", ""),
        }

    def _call_submit(self, text: str) -> Dict[str, Any]:
        try:
            result = self.submit(text, self.timeout_ms / 1000.0)
        except Exception as error:  # noqa: BLE001 - report any submit failure
            return {"ok": False, "error": str(error)}
        if not isinstance(result, dict):
            return {"ok": False, "error": "submit returned an invalid result"}
        return result

    def _run_submit(self, task_id: str, text: str) -> None:
        result = self._call_submit(text)
        if result.get("ok"):
            self._complete(task_id, str(result.get("text") or ""))
        else:
            self._fail(task_id, str(result.get("error") or "The web AI failed."))

    def _complete(
        self, task_id: str, text: str, artifact: Optional[Dict[str, Any]] = None
    ) -> Optional[Dict[str, Any]]:
        task = self.store.get(task_id) or {}
        reply = self._agent_message(task, text)
        status = {
            "state": "TASK_STATE_COMPLETED",
            "timestamp": _now_iso(),
            "message": reply,
        }
        history = list(task.get("history", [])) + [reply]
        return self.store.update(
            task_id,
            status=status,
            artifacts=[artifact or self._artifact(text)],
            history=history,
        )

    def _fail(self, task_id: str, error: str) -> Optional[Dict[str, Any]]:
        task = self.store.get(task_id) or {}
        reply = self._agent_message(task, error)
        status = {"state": "TASK_STATE_FAILED", "timestamp": _now_iso(), "message": reply}
        history = list(task.get("history", [])) + [reply]
        return self.store.update(task_id, status=status, history=history)

    @staticmethod
    def _view(task: Optional[Dict[str, Any]], history_length: Any) -> Optional[Dict[str, Any]]:
        if task is None:
            return None
        view = copy.deepcopy(task)
        if (
            isinstance(history_length, int)
            and not isinstance(history_length, bool)
            and history_length >= 0
        ):
            view["history"] = view.get("history", [])[:history_length]
        return view


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.0"

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002
        pass

    # -- response writers -------------------------------------------------

    def send_json(
        self,
        payload: Dict[str, Any],
        status: int = 200,
        headers: Optional[Dict[str, str]] = None,
    ) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        for name, value in (headers or {}).items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def start_sse(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()

    def send_sse(self, payload: Dict[str, Any]) -> None:
        line = f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
        self.wfile.write(line.encode("utf-8"))
        self.wfile.flush()

    # -- routing ----------------------------------------------------------

    def do_GET(self) -> None:  # noqa: N802
        a2a: A2AServer = self.server.a2a  # type: ignore[attr-defined]
        path = urlsplit(self.path).path
        if path in CARD_PATHS:
            self.send_json(a2a.card())
        else:
            self.send_json({"error": "not found"}, status=404)

    def do_POST(self) -> None:  # noqa: N802
        a2a: A2AServer = self.server.a2a  # type: ignore[attr-defined]
        if urlsplit(self.path).path != "/":
            self.send_json({"error": "not found"}, status=404)
            return
        if not a2a.authorized(self.headers.get("Authorization")):
            self.send_json(
                {"error": "unauthorized"},
                status=401,
                headers={"WWW-Authenticate": "Bearer"},
            )
            return

        raw = self._read_body()
        if raw is None:
            self.send_json(a2a.error(None, -32600, "Invalid Request"))
            return
        try:
            payload = json.loads(raw.decode("utf-8")) if raw else None
        except ValueError:
            self.send_json(a2a.error(None, -32700, "Parse error"))
            return
        if (
            not isinstance(payload, dict)
            or payload.get("jsonrpc") != "2.0"
            or not isinstance(payload.get("method"), str)
        ):
            self.send_json(a2a.error(None, -32600, "Invalid Request"))
            return

        if _METHOD_ALIASES.get(payload["method"]) == "SendStreamingMessage":
            a2a.stream(self, payload)
            return
        self.send_json(a2a.dispatch(payload))

    def _read_body(self) -> Optional[bytes]:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            return None
        if length < 0 or length > MAX_BODY_BYTES:
            return None
        return self.rfile.read(length) if length else b""


if __name__ == "__main__":  # pragma: no cover - manual smoke entry point
    import os

    _port = int(os.environ.get("ZS_A2A_PORT", str(DEFAULT_PORT)))
    _token = os.environ.get("ZS_A2A_TOKEN") or secrets.token_urlsafe(32)
    _base = f"http://127.0.0.1:{_port}"

    def _echo(prompt: str, timeout: float) -> Dict[str, Any]:
        return {"ok": True, "text": f"echo: {prompt}"}

    server = A2AServer(_echo, port=_port, token=_token, base_url=_base)
    server.start()
    print(f"A2A endpoint: {_base}  token: {_token}", flush=True)
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
