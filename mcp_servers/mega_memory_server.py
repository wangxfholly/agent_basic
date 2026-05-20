#!/usr/bin/env python3
"""
mega-mcp-memory
===============

A minimal, dependency-light MCP (Model Context Protocol) server that
exposes the `mega_agent.memory.MemoryStore` over JSON-RPC 2.0 / stdio.

Why bother?
-----------
Native tools (registered inside the kernel) are convenient but bound to
this process. Wrapping the same MemoryStore in an MCP server means:

  * Any MCP-compatible host (Claude Desktop, Cursor, your own kernel)
    can attach to it without importing mega_agent.
  * Memory survives across kernel restarts AND across hosts — one
    persistent vector store, many readers.
  * Drop-in replacement: switch `AGENT_MEMORY_BACKEND=chroma` and you
    have a real vector DB behind the same JSON-RPC surface.

Run::

    python -m mcp_servers.mega_memory_server

or wire it via mega_agent's MCP registry::

    {
      "mcpServers": {
        "memory": {
          "command": ["python", "-m", "mcp_servers.mega_memory_server"]
        }
      }
    }

Protocol: JSON-RPC 2.0 framed by newlines on stdin/stdout. Implements
the subset of MCP needed by mega_agent:

    initialize · notifications/initialized · tools/list · tools/call
    · shutdown

Tools exposed (all delegate straight to MemoryStore):

    remember · recall · forget · list_memory
    set_pref · get_pref · forget_user
"""
from __future__ import annotations

import json
import sys
import traceback

# Allow running as `python mcp_servers/mega_memory_server.py` from repo root.
import os as _os
sys.path.insert(0, _os.path.dirname(_os.path.dirname(_os.path.abspath(__file__))))

from mega_agent.memory import memory  # noqa: E402


PROTOCOL_VERSION = "2024-11-05"
SERVER_NAME = "mega-mcp-memory"
SERVER_VERSION = "0.1.0"


# ─────────────────────────────────────────────────────────────────────────────
# Tool surface (name → (description, JSON schema, handler)).
# ─────────────────────────────────────────────────────────────────────────────
def _tools() -> list[dict]:
    return [
        {
            "name": "remember",
            "description": (
                "Persist a fact / event / preference / note into long-term memory. "
                "Returns the stored record (id, ts, kind, ...)."
            ),
            "inputSchema": {
                "type": "object",
                "properties": {
                    "content": {"type": "string"},
                    "kind": {
                        "type": "string",
                        "enum": ["fact", "preference", "event", "task", "note"],
                        "default": "fact",
                    },
                    "user_id": {"type": ["string", "null"]},
                    "tags": {"type": "object", "additionalProperties": True},
                },
                "required": ["content"],
            },
        },
        {
            "name": "recall",
            "description": "Top-k retrieval over stored facts. Returns hits with score+text+tags.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "k": {"type": "integer", "default": 5},
                    "user_id": {"type": ["string", "null"]},
                },
                "required": ["query"],
            },
        },
        {
            "name": "forget",
            "description": "Remove a single fact by id (tombstone-style: rewrites the JSONL).",
            "inputSchema": {
                "type": "object",
                "properties": {"fact_id": {"type": "string"}},
                "required": ["fact_id"],
            },
        },
        {
            "name": "list_memory",
            "description": "Dump up to `limit` most-recent facts, optionally filtered by user_id.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "user_id": {"type": ["string", "null"]},
                    "limit": {"type": "integer", "default": 100},
                },
            },
        },
        {
            "name": "set_pref",
            "description": "Upsert a single-value preference into the KV store.",
            "inputSchema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "value": {"type": "string"},
                },
                "required": ["key", "value"],
            },
        },
        {
            "name": "get_pref",
            "description": "Look up a single-value preference; returns null if missing.",
            "inputSchema": {
                "type": "object",
                "properties": {"key": {"type": "string"}},
                "required": ["key"],
            },
        },
        {
            "name": "forget_user",
            "description": "GDPR-style purge: drop every fact and KV entry tagged with user_id.",
            "inputSchema": {
                "type": "object",
                "properties": {"user_id": {"type": "string"}},
                "required": ["user_id"],
            },
        },
    ]


def _dispatch(name: str, args: dict) -> dict:
    if name == "remember":
        tags = args.get("tags") or {}
        return memory.remember(
            content=args["content"],
            kind=args.get("kind", "fact"),
            user_id=args.get("user_id"),
            **tags,
        )
    if name == "recall":
        return {
            "hits": memory.recall(
                query=args["query"],
                k=int(args.get("k", 5)),
                user_id=args.get("user_id"),
            )
        }
    if name == "forget":
        return {"removed": memory.forget(args["fact_id"])}
    if name == "list_memory":
        return {
            "facts": memory.list_facts(
                user_id=args.get("user_id"),
                limit=int(args.get("limit", 100)),
            )
        }
    if name == "set_pref":
        memory.set(args["key"], args["value"])
        return {"ok": True}
    if name == "get_pref":
        return {"value": memory.get(args["key"])}
    if name == "forget_user":
        return {"purged": memory.forget_user(args["user_id"])}
    raise ValueError(f"unknown tool: {name}")


# ─────────────────────────────────────────────────────────────────────────────
# JSON-RPC framing (newline-delimited).
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

    # Notifications (no id): just absorb.
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
            args = params.get("arguments") or {}
            result = _dispatch(name, args)
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
