#!/usr/bin/env python3
"""Handshake authentication for the local bridge.

Browsers do not apply CORS to WebSockets, so the bridge can only tell a
browser-extension client from a web page by its Origin header, and it cannot
tell two local processes apart at all. The Origin allowlist covers the first
case and a per-install token the second. Both checks, and the token file
handling they need, live here so the policy can be read and tested without
starting MCP servers.
"""

import hmac
import os
import secrets
import urllib.parse
from pathlib import Path
from typing import Any, Callable, Optional, Tuple

# Only browser-extension origins and non-browser clients (no Origin header at
# all) are accepted; any website could otherwise open ws://127.0.0.1:PORT.
ALLOWED_ORIGIN_PREFIXES = (
    "chrome-extension://",
    "moz-extension://",
    "safari-web-extension://",
)
# A per-install shared secret, sent by the extension as ?token=... on connect (or
# by native clients as the X-Bridge-Token header). Auto-generated on first run.
TOKEN_ENV = "ZS_BRIDGE_TOKEN"
TOKEN_PATH_ENV = "ZS_BRIDGE_TOKEN_FILE"
AUTH_DISABLE_ENV = "ZS_BRIDGE_AUTH"  # set to 0/off/false/no to run without a token
AUTH_DISABLED_VALUES = {"0", "off", "false", "no"}
# The same switch doubles as a mode selector: `strict` keeps the old rule
# where every client presents the token, extensions included. See
# token_required_for.
STRICT_VALUE = "strict"

# None means "stay quiet": the bridge passes its own logger in, tests do not.
LogFn = Optional[Callable[[str], None]]


def token_path() -> Path:
    override = (os.environ.get(TOKEN_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".snapgent" / "bridge_token"


def a2a_token_path() -> Path:
    """Where the A2A bearer token is kept.

    A local agent reads it from here instead of copying it out of the bridge
    window. 0600 in a 0700 directory, like the bridge token.
    """
    override = (os.environ.get("ZS_A2A_TOKEN_FILE") or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".snapgent" / "a2a_token"


def auth_disabled() -> bool:
    return (os.environ.get(AUTH_DISABLE_ENV) or "").strip().lower() in AUTH_DISABLED_VALUES


def strict_auth() -> bool:
    return (os.environ.get(AUTH_DISABLE_ENV) or "").strip().lower() == STRICT_VALUE


def token_required_for(origin: Optional[str]) -> bool:
    """Whether this client has to present the bridge token.

    A browser extension cannot exist without the user installing it, and a
    web page cannot forge a `chrome-extension://` Origin, so the allowlist
    above already covers that case - which is what makes pairing
    copy-paste-free. A native client sends no Origin, so the bridge cannot
    tell it apart from any other local process and it must prove it holds
    the token.

    `ZS_BRIDGE_AUTH=strict` requires the token from every client again.
    """
    if strict_auth():
        return True
    return not (origin or "").startswith(ALLOWED_ORIGIN_PREFIXES)


def _notify(log_fn: LogFn, message: str) -> None:
    if log_fn is not None:
        log_fn(message)


def _create_private_file(path: Path, token: str) -> None:
    """Create `path` holding the token, mode 0600 in a 0700 directory.

    O_EXCL keeps two bridges racing on first run from overwriting a token the
    other one already handed out, and it is what makes an interrupted write
    visible instead of silently clobbering an existing secret.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.parent.chmod(0o700)
    except OSError:
        pass
    handle = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(handle, "w", encoding="utf-8") as stream:
        stream.write(token + "\n")


def load_or_create_token(
    path: Path, label: str, log_fn: LogFn = None
) -> Tuple[Optional[str], bool]:
    """Read `path`, or create it with a fresh secret. Returns (token, created).

    A token of None means the file could be neither read nor written; the
    caller decides what that means for the auth mode it is serving.
    """
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as error:
        _notify(log_fn, f"WARNING: could not read the {label} ({error}).")
        return None, False
    if existing:
        return existing, False

    token = secrets.token_urlsafe(32)
    try:
        _create_private_file(path, token)
    except FileExistsError:
        # Another process won the first-run race; use the secret it wrote.
        try:
            return path.read_text(encoding="utf-8").strip() or None, False
        except OSError:
            return None, False
    except OSError as error:
        _notify(log_fn, f"WARNING: could not create the {label} ({error}).")
        return None, False
    return token, True


def load_auth_token(log_fn: LogFn = None) -> Tuple[Optional[str], bool]:
    """Return (token, created). A token of None means token auth is off."""
    if auth_disabled():
        return None, False
    env_token = (os.environ.get(TOKEN_ENV) or "").strip()
    if env_token:
        return env_token, False
    token, created = load_or_create_token(token_path(), "bridge token", log_fn)
    if token is None:
        _notify(
            log_fn,
            "token auth is off (the origin allowlist stays active).",
        )
    return token, created


def write_token_file(path: Path, token: str) -> None:
    """Write the token atomically with mode 0600, replacing any previous value."""
    temp = path.with_name(f"{path.name}.tmp-{os.getpid()}")
    try:
        _create_private_file(temp, token)
    except BaseException:
        try:
            temp.unlink()
        except OSError:
            pass
        raise
    os.replace(temp, path)


def origin_allowed(origin: Optional[str]) -> bool:
    if not origin:
        return True  # native clients (tests, curl, desktop app) send no Origin
    return origin.startswith(ALLOWED_ORIGIN_PREFIXES)


def request_headers(websocket: Any) -> Any:
    headers = getattr(getattr(websocket, "request", None), "headers", None)
    if headers is not None:
        return headers
    return getattr(websocket, "request_headers", None)  # websockets < 14


def request_path(websocket: Any) -> str:
    path = getattr(getattr(websocket, "request", None), "path", None)
    if isinstance(path, str):
        return path
    legacy = getattr(websocket, "path", None)  # websockets < 14
    return legacy if isinstance(legacy, str) else ""


def header_value(websocket: Any, name: str) -> Optional[str]:
    headers = request_headers(websocket)
    if headers is None:
        return None
    try:
        value = headers.get(name)
    except AttributeError:
        return None
    return value if isinstance(value, str) else None


def host_allowed(host: Optional[str]) -> bool:
    """Whether a WebSocket Host header may reach the loopback-only bridge.

    Browser clients always send Host (HTTP/1.1) and Origin; native clients send
    neither. A missing Host is therefore accepted here and left to the Origin
    allowlist, which is the check that actually separates a web page from a
    local process. When Host IS present it must be a loopback literal.
    """
    if not host:
        return True
    name = host.strip().lower()
    if not name:
        return True
    if name.startswith("["):
        # IPv6 literal, e.g. "[::1]:17613".
        end = name.find("]")
        if end == -1:
            return False
        name = name[1:end]
    else:
        name = name.split(":", 1)[0]
    return name in ("127.0.0.1", "localhost", "::1")


def presented_token(websocket: Any) -> Optional[str]:
    header_token = (header_value(websocket, "X-Bridge-Token") or "").strip()
    if header_token:
        return header_token
    query = urllib.parse.parse_qs(urllib.parse.urlparse(request_path(websocket)).query)
    values = query.get("token") or []
    return values[0].strip() if values else None


def token_matches(presented: Optional[str], expected: Optional[str]) -> bool:
    """Compare in constant time, tolerating any bytes the client sends.

    hmac.compare_digest() raises TypeError when a str argument holds non-ASCII
    characters, which crashed the connection handler (close code 1011 and a
    traceback) instead of rejecting the client. Comparing UTF-8 bytes keeps the
    comparison constant-time and turns every mismatch into a plain False.
    """
    if not presented or not expected:
        return False
    try:
        presented_bytes = presented.encode("utf-8")
    except UnicodeEncodeError:
        # Lone surrogates from a malformed percent-escape cannot be a token.
        return False
    return hmac.compare_digest(presented_bytes, expected.encode("utf-8"))
