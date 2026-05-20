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

from mega_agent import skill_market  # noqa: E402
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
        {
            "name": "install_skill",
            "description": (
                "Install a skill from a git+ URL, http(s) tarball, or local "
                "directory. Pipeline: parse → policy → fetch → verify "
                "(SKILL.md / sha256 / optional Ed25519 sig) → atomic commit. "
                "Never executes skill scripts during install."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "source": {"type": "string"},
                    "sha256": {"type": ["string", "null"]},
                    "force": {"type": "boolean", "default": False},
                    "allow_unsigned": {"type": ["boolean", "null"]},
                    "require_signature": {"type": ["boolean", "null"]},
                    "insecure": {"type": "boolean", "default": False},
                },
                "required": ["source"],
            },
        },
        {
            "name": "remove_skill",
            "description": "Uninstall a skill (optionally pin a single version).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "version": {"type": ["string", "null"]},
                },
                "required": ["name"],
            },
        },
        {
            "name": "list_installed_skills",
            "description": "Return the lockfile contents (every installed skill).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "verify_installed_skill",
            "description": "Re-hash on-disk skill trees vs lockfile (integrity check).",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": ["string", "null"]}},
            },
        },
        {
            "name": "sync_skills",
            "description": "Reproduce installs from the lockfile (CI / new-machine).",
            "inputSchema": {"type": "object", "properties": {}},
        },
        {
            "name": "pin_skill",
            "description": "Pin a skill name to a specific installed version (or unpin with null).",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "version": {"type": ["string", "null"]},
                },
                "required": ["name"],
            },
        },
        {
            "name": "skill_versions",
            "description": "List all installed versions of a skill name.",
            "inputSchema": {
                "type": "object",
                "properties": {"name": {"type": "string"}},
                "required": ["name"],
            },
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
    if name == "install_skill":
        return skill_market.install(
            args["source"],
            sha256=args.get("sha256"),
            force=args.get("force", False),
            allow_unsigned=args.get("allow_unsigned"),
            require_signature=args.get("require_signature"),
            insecure=args.get("insecure", False),
        )
    if name == "remove_skill":
        return skill_market.remove(args["name"], version=args.get("version"))
    if name == "list_installed_skills":
        return skill_market.list_installed()
    if name == "verify_installed_skill":
        return skill_market.verify_installed(name=args.get("name"))
    if name == "sync_skills":
        return skill_market.sync()
    if name == "pin_skill":
        return {"pinned": skills.pin(args["name"], args.get("version"))}
    if name == "skill_versions":
        return {"versions": skills.all_versions(args["name"])}
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
