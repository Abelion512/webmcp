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

# None means "stay quiet": the bridge passes its own logger in, tests do not.
LogFn = Optional[Callable[[str], None]]


def token_path() -> Path:
    override = (os.environ.get(TOKEN_PATH_ENV) or "").strip()
    if override:
        return Path(override).expanduser()
    return Path.home() / ".snapgent" / "bridge_token"


def auth_disabled() -> bool:
    return (os.environ.get(AUTH_DISABLE_ENV) or "").strip().lower() in AUTH_DISABLED_VALUES


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


def load_auth_token(log_fn: LogFn = None) -> Tuple[Optional[str], bool]:
    """Return (token, created). A token of None means auth is turned off."""
    if auth_disabled():
        return None, False
    env_token = (os.environ.get(TOKEN_ENV) or "").strip()
    if env_token:
        return env_token, False

    path = token_path()
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except FileNotFoundError:
        existing = ""
    except OSError as error:
        _notify(log_fn, f"WARNING: could not read the bridge token ({error}); token auth is off.")
        return None, False
    if existing:
        return existing, False

    token = secrets.token_urlsafe(32)
    try:
        _create_private_file(path, token)
    except FileExistsError:
        # Another bridge won the first-run race; use the token it wrote.
        try:
            return path.read_text(encoding="utf-8").strip() or None, False
        except OSError:
            return None, False
    except OSError as error:
        _notify(log_fn, f"WARNING: could not create the bridge token ({error}); token auth is off.")
        return None, False
    return token, True


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
