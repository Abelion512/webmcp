#!/usr/bin/env python3
"""Configure the Linux scoped MCP workspace server."""

import json
import os
import sys
from pathlib import Path
from typing import Dict, List, Optional


ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config.json"
WORKSPACE_MCP = ROOT / "workspace_mcp.py"


def _load_config() -> Dict[str, object]:
    if not CONFIG_PATH.exists():
        return {"mcpServers": {}}
    try:
        configuration = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise RuntimeError(f"Could not read {CONFIG_PATH.name}: {error}") from error
    if not isinstance(configuration, dict):
        raise RuntimeError(f"{CONFIG_PATH.name} must contain a JSON object.")
    servers = configuration.setdefault("mcpServers", {})
    if not isinstance(servers, dict):
        raise RuntimeError(f"{CONFIG_PATH.name} must contain an object named mcpServers.")
    return configuration


def _set_server(
    servers: Dict[str, object], server_id: str, command: str, args: List[str],
    env: Optional[Dict[str, str]] = None,
) -> None:
    servers[server_id] = {"command": command, "args": args, "env": env or {}}


def main(root_override: Optional[str] = None) -> int:
    if sys.platform != "linux":
        print("ERROR: setup-config.py supports Linux only.", file=sys.stderr)
        return 1
    try:
        selected_root = (
            root_override
            if root_override is not None
            else os.environ.get("ZS_WORKSPACE_ROOT") or os.getcwd()
        )
        if not str(selected_root).strip():
            print("ERROR: workspace root cannot be empty.", file=sys.stderr)
            return 1
        root = Path(selected_root).expanduser().resolve()
        if not root.is_dir():
            print(f"ERROR: workspace root does not exist: {root}", file=sys.stderr)
            return 1

        configuration = _load_config()
        servers = configuration["mcpServers"]
        assert isinstance(servers, dict)

        _set_server(
            servers, "repo", sys.executable, [str(WORKSPACE_MCP)],
            {"ZS_WORKSPACE_ROOT": str(root)},
        )
        for stale in ("vscode", "roblox", "effectcraft"):
            servers.pop(stale, None)

        # Write via a same-directory temp file + atomic replace: the running
        # bridge may reload config.json at any moment (switch-project.sh), so
        # it must never observe a half-written file.
        payload = json.dumps(configuration, indent=2, ensure_ascii=False) + "\n"
        temp_path = CONFIG_PATH.with_name(CONFIG_PATH.name + ".tmp")
        try:
            temp_path.write_text(payload, encoding="utf-8")
            temp_path.replace(CONFIG_PATH)
        finally:
            try:
                temp_path.unlink()
            except OSError:
                pass
        print(f"workspace MCP configured: {WORKSPACE_MCP} (root: {root})")
        return 0
    except (OSError, RuntimeError) as error:
        print(f"ERROR: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    override = sys.argv[1] if len(sys.argv) > 1 else None
    raise SystemExit(main(override))
