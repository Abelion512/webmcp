"""Live smoke test: the real bridge plus the unpacked extension in headless Chrome.

Unit tests cover the bridge in isolation and the popup helpers in Node, but the
handshake also depends on browser behaviour (extension Origin, WebSocket close
ordering) that only a real browser shows. Run it by hand after touching auth:

    python3 scripts/smoke_extension_live.py

Needs Google Chrome, a free port 17613 and a free port 9333 (the CDP endpoint);
uses a temp token file and profile, so the developer's own bridge token is
untouched. It refuses to start if either port is taken, because it would then
talk to whatever is already listening there. Exits non-zero on any failure.
"""
import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import websockets

REPO = Path(__file__).resolve().parent.parent  # repo root, not scripts/
EXT = REPO / "snapgent-extension"
PORT = 17613
CDP = 9333
results = []


def ok(name, cond, detail=""):
    results.append(bool(cond))
    print(("PASS  " if cond else "FAIL  ") + name + (f"  [{detail}]" if detail and not cond else ""))


def http(path, method="GET"):
    """CDP's HTTP endpoints are only up once Chrome has finished starting."""
    try:
        req = urllib.request.Request(f"http://127.0.0.1:{CDP}{path}", method=method)
        with urllib.request.urlopen(req, timeout=10) as response:
            body = response.read().decode()
        return json.loads(body) if body.strip() else {}
    except (urllib.error.URLError, OSError, json.JSONDecodeError):
        return None


async def evaluate(ws_url, expression, await_promise=False, timeout=20):
    async with websockets.connect(ws_url, max_size=None) as socket:
        await socket.send(json.dumps({
            "id": 1,
            "method": "Runtime.evaluate",
            "params": {"expression": expression, "awaitPromise": await_promise, "returnByValue": True},
        }))
        deadline = time.time() + timeout
        while time.time() < deadline:
            message = json.loads(await socket.recv())
            if message.get("id") == 1:
                return message.get("result", {}).get("result", {}).get("value")
    return None


async def load_unpacked():
    """Chrome 137+ ignores --load-extension, so drive the CDP Extensions domain."""
    version = http("/json/version") or {}
    async with websockets.connect(version["webSocketDebuggerUrl"], max_size=None) as socket:
        await socket.send(json.dumps({
            "id": 1, "method": "Extensions.loadUnpacked", "params": {"path": str(EXT)},
        }))
        deadline = time.time() + 30
        while time.time() < deadline:
            message = json.loads(await socket.recv())
            if message.get("id") == 1:
                return message.get("result")
    return None


def port_in_use(port):
    with socket.socket() as probe:
        probe.settimeout(0.5)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def find_target(kind, suffix=None):
    for target in http("/json/list") or []:
        if target.get("type") == kind and (suffix is None or target.get("url", "").endswith(suffix)):
            return target
    return None


async def wait_until(check, timeout, interval=0.2):
    deadline = time.time() + timeout
    while time.time() < deadline:
        value = check()
        if value:
            return value
        await asyncio.sleep(interval)
    return None


async def main():
    for port, what in ((PORT, "bridge"), (CDP, "Chrome debugging")):
        free = not port_in_use(port)
        ok(f"port {port} is free for the {what}", free,
           "something is already listening - stop it first")
        if not free:
            return

    tmp = Path(tempfile.mkdtemp(prefix="zs-smoke-"))
    token_path = tmp / "bridge_token"
    log_path = tmp / "bridge.log"
    env = {k: v for k, v in os.environ.items()
           if k not in ("ZS_BRIDGE_TOKEN", "ZS_BRIDGE_AUTH", "ZS_BRIDGE_TOKEN_FILE")}
    env.update(ZS_BRIDGE_TOKEN_FILE=str(token_path), ZS_BRIDGE_PORT=str(PORT))

    log_handle = open(log_path, "wb")
    bridge = subprocess.Popen([sys.executable, "bridge.py"], cwd=REPO, env=env,
                              stdout=log_handle, stderr=subprocess.STDOUT)
    chrome = None
    try:
        ok("bridge listens", await wait_until(lambda: "listening on ws://" in tail(log_path), 25))
        token = await wait_until(
            lambda: token_path.read_text(encoding="utf-8").strip() if token_path.exists() else "", 10)
        ok("bridge created a token file", token)

        chrome = subprocess.Popen([
            "google-chrome", "--headless=new", "--disable-gpu", "--no-first-run",
            "--no-default-browser-check", "--disable-background-networking",
            f"--user-data-dir={tmp / 'profile'}", f"--remote-debugging-port={CDP}",
            "--enable-unsafe-extension-debugging", "about:blank",
        ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

        await wait_until(lambda: http("/json/version"), 20)
        ok("extension loads unpacked", await load_unpacked())
        worker = await wait_until(lambda: find_target("service_worker", "/background.js"), 30)
        ok("extension service worker started", worker)
        if not worker:
            return
        extension_id = worker["url"].split("/")[2]

        # 1. Unpaired: the worker dials without a token and must be refused (1008).
        ok("unpaired extension is refused", await wait_until(
            lambda: "missing or invalid bridge token" in tail(log_path), 20))

        # 2. Pair: store the token the bridge generated, then dial again.
        await evaluate(worker["webSocketDebuggerUrl"],
                       "chrome.storage.local.set({bridgeToken: %s}).then(() => { bridgeToken = null; return connect(); })"
                       % json.dumps(token), await_promise=True)
        ok("paired extension connects", await wait_until(
            lambda: "extension connected" in tail(log_path), 20))

        # 3. Popup: open it as a tab and read what the user actually sees.
        popup = http(f"/json/new?chrome-extension://{extension_id}/popup.html", method="PUT")
        await asyncio.sleep(1.5)
        state_js = 'document.getElementById("state").textContent + "|" + document.getElementById("tools").textContent'
        before = await evaluate(popup["webSocketDebuggerUrl"], state_js)
        ok("popup does not ask to pair while connected",
           before and "Bridge token required" not in before, before)

        # 4. Rotate from the popup button and watch the status text through the drop.
        seen, old_token = [], token

        async def read_state():
            text = await evaluate(popup["webSocketDebuggerUrl"], state_js)
            if text and (not seen or seen[-1] != text):
                seen.append(text)
            return text

        await evaluate(popup["webSocketDebuggerUrl"], 'document.getElementById("rotate-token").click()')
        deadline = time.time() + 12
        while time.time() < deadline:
            text = await read_state()
            if text and ("reconnecting" in text or text.startswith("Bridge offline")):
                break
            await asyncio.sleep(0.15)
        ok("popup never says Bridge offline during rotation",
           not any(t.startswith("Bridge offline") for t in seen), " | ".join(seen))
        ok("popup says Token rotated · reconnecting",
           any("Token rotated · reconnecting" in t for t in seen), " | ".join(seen))

        def new_token():
            if not token_path.exists():
                return ""
            value = token_path.read_text(encoding="utf-8").strip()
            return value if value != old_token else ""

        rotated = await wait_until(new_token, 15)
        ok("bridge wrote a new token", rotated)
        ok("bridge revoked the live session", await wait_until(
            lambda: "revoked 1 authenticated connection(s)" in tail(log_path), 10))

        # 5. The worker stores the reply, reconnects with it and the popup settles.
        stored = await evaluate(worker["webSocketDebuggerUrl"],
                                "chrome.storage.local.get('bridgeToken').then((v) => v.bridgeToken)",
                                await_promise=True)
        ok("worker stored the new token", stored == rotated)
        ok("worker reconnected with the new token", await wait_until(
            lambda: tail(log_path).count("extension connected") >= 2, 15))
        await asyncio.sleep(2)
        ok("worker reports itself connected again",
           await evaluate(worker["webSocketDebuggerUrl"], "connected") is True)
        after = await read_state()
        # A connected popup reads Connected / No Studio / Open Studio, depending on
        # which MCP servers exist here, so judge by the failure texts instead.
        ok("popup settles on a live state",
           bool(after) and not any(word in after for word in
                                   ("Bridge offline", "reconnecting", "Bridge token required")),
           after)
    finally:
        for process in (chrome, bridge):
            if process and process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    process.kill()
        log_handle.close()
        print("\n--- bridge log ---")
        print(tail(log_path, 2000))
        shutil.rmtree(tmp, ignore_errors=True)


def tail(path, size=4000):
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")[-size:]
    except FileNotFoundError:
        return ""


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0 if results and all(results) else 1)
