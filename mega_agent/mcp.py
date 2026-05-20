"""
mega_agent.mcp
==============

Minimal MCP (Model Context Protocol) integration: stdio + JSON-RPC 2.0.
For production deployments swap in the official `mcp` SDK; the surface
area used by the kernel is `MCPRegistry.get_agent_tools()` and `.call()`.
"""
from __future__ import annotations

import json
import os
import subprocess
import threading

from .events import events


class MCPClient:
    """stdio + JSON-RPC 2.0 minimalist implementation."""

    def __init__(self, name: str, command: list[str], env: dict | None = None):
        self.name, self.command = name, command
        self.env = {**os.environ, **(env or {})}
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()

    def connect(self):
        self.proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=self.env, bufsize=1)
        self._call("initialize", {
            "protocolVersion": "2024-11-05",
            "capabilities": {},
            "clientInfo": {"name": "mega_agent"},
        })
        self._notify("notifications/initialized", {})

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _send(self, payload: dict):
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("not connected")
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        line = self.proc.stdout.readline() if self.proc and self.proc.stdout else ""
        if not line:
            raise RuntimeError("empty response")
        return json.loads(line)

    def _call(self, method: str, params: dict) -> dict:
        rid = self._next_id()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        resp = self._recv()
        if "error" in resp:
            raise RuntimeError(resp["error"])
        return resp.get("result", {})

    def _notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def list_tools(self) -> list[dict]:
        return self._call("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._call("tools/call", {"name": name, "arguments": arguments})

    def disconnect(self):
        if not self.proc:
            return
        try:
            self._call("shutdown", {})
        except Exception:
            pass
        try:
            self.proc.terminate()
            self.proc.wait(timeout=5)
        except Exception:
            try:
                self.proc.kill()
            except Exception:
                pass


class MCPRegistry:
    """Holds all connected MCP servers; exposes a flat tool namespace."""

    def __init__(self):
        self.clients: dict[str, MCPClient] = {}

    def register(self, server: str, command: list[str], env: dict | None = None):
        c = MCPClient(server, command, env)
        c.connect()
        self.clients[server] = c
        events.emit("mcp.registered", server=server)

    def get_agent_tools(self) -> list[dict]:
        out = []
        for server, c in self.clients.items():
            for t in c.list_tools():
                out.append({
                    "name": f"mcp__{server}__{t['name']}",
                    "description": t.get("description", ""),
                    "input_schema": t.get("inputSchema", {"type": "object"}),
                })
        return out

    def call(self, full_name: str, arguments: dict) -> dict:
        _, server, tool = full_name.split("__", 2)
        if server not in self.clients:
            raise KeyError(f"mcp server {server} not registered")
        return self.clients[server].call_tool(tool, arguments)


mcp = MCPRegistry()
