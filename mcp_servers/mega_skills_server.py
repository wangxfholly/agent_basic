#!/usr/bin/env python3
"""
mega-mcp-skills
===============

Standalone MCP server that exposes the `mega_agent.skills.SkillRegistry`
over JSON-RPC 2.0 / stdio. Same shape as `mega_memory_server` — drop into
any MCP host and you get the skill catalog + on-demand bodies + script
execution.

Run::

    python -m mcp_servers.mega_skills_server

Or wire via mega_agent's MCP registry::

    mcp.register("skills",
                 ["python", "-m", "mcp_servers.mega_skills_server"])

Tools exposed:

    list_skills · load_skill · unload_skill
    run_skill_script · refresh_skills
"""
from __future__ import annotations

import json
import os as _os
import sys
import traceback

sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from mega_agent.skills import skills  # noqa: E402


PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mega-mcp-skills"
SERVER_VERSION = "0.1.0"


def _tools() -> list[dict]:
    return [
        {
            "name": "list_skills",
            "description": "List all discoverable skills (name + description + auto_load flag).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "load_skill",
            "description": (
                "Activate a skill by name. Returns its body markdown and any "
                "bundled scripts/resources. Auto-allowlists declared tools."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "unload_skill",
            "description": "Deactivate a previously loaded skill (idempotent).",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
        },
        {
            "name": "run_skill_script",
            "description": (
                "Execute a script inside <skill>/scripts/. Path traversal is "
                "blocked. Returns rc/stdout/stderr."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "script": {"type": "string"},
                    "args": {"type": "array", "items": {"type": "string"}},
                    "timeout": {"type": "integer", "default": 60},
                },
                "required": ["name", "script"],
            },
        },
        {
            "name": "refresh_skills",
            "description": "Re-scan the skill search dirs. Returns the new count.",
            "inputSchema": {"type": "object", "properties": {}},
        },
    ]


def _dispatch(name: str, args: dict) -> dict:
    if name == "list_skills":
        return {"skills": skills.catalog()}
    if name == "load_skill":
        return skills.load(args["name"])
    if name == "unload_skill":
        return {"unloaded": skills.unload(args["name"])}
    if name == "run_skill_script":
        return skills.run_script(
            args["name"], args["script"],
            args=args.get("args"),
            timeout=int(args.get("timeout", 60)),
        )
    if name == "refresh_skills":
        return {"count": skills.refresh()}
    raise ValueError(f"unknown tool: {name}")


# ─────────────────────────────────────────────────────────────────────────────
# JSON-RPC plumbing (same as memory server).
# ─────────────────────────────────────────────────────────────────────────────
def _send(payload: dict) -> None:
    sys.stdout.write(json.dumps(payload, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _ok(rid, result):
    _send({"jsonrpc": "2.0", "id": rid, "result": result})


def _err(rid, code: int, message: str, data=None):
    err: dict = {"code": code, "message": message}
    if data is not None:
        err["data"] = data
    _send({"jsonrpc": "2.0", "id": rid, "error": err})


def _handle(req: dict) -> None:
    method = req.get("method")
    rid = req.get("id")
    params = req.get("params") or {}
    if rid is None:
        return
    try:
        if method == "initialize":
            _ok(rid, {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {"tools": {}},
                "serverInfo": {"name": SERVER_NAME, "version": SERVER_VERSION},
            })
        elif method == "tools/list":
            _ok(rid, {"tools": _tools()})
        elif method == "tools/call":
            name = params.get("name")
            arguments = params.get("arguments") or {}
            result = _dispatch(name, arguments)
            _ok(rid, {
                "content": [{
                    "type": "text",
                    "text": json.dumps(result, ensure_ascii=False),
                }],
                "isError": False,
            })
        elif method == "shutdown":
            _ok(rid, {})
        else:
            _err(rid, -32601, f"method not found: {method}")
    except Exception as e:  # noqa: BLE001
        _err(rid, -32000, str(e), data=traceback.format_exc(limit=3))


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            req = json.loads(line)
        except json.JSONDecodeError as e:
            _err(None, -32700, f"parse error: {e}")
            continue
        _handle(req)


if __name__ == "__main__":
    main()
