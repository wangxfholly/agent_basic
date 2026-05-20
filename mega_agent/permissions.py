"""
mega_agent.permissions
======================

Capability-based permission gate. Classifies a tool call into
(read | write | high) and returns a decision (allow | ask | deny).

Production code typically wraps this with a UI prompt to handle `ask`.
"""
from __future__ import annotations

import os

READ_PREFIXES      = ("read", "list", "get", "show", "search", "query", "inspect")
HIGH_RISK_PREFIXES = ("delete", "remove", "drop", "shutdown", "kill", "rm")


class CapabilityPermissionGate:
    """
    Decision matrix:
        denylist          → deny
        allowlist         → allow
        risk == read      → allow
        mode==auto && !high → allow
        otherwise         → ask
    """

    def __init__(self, mode: str = "auto"):
        self.mode = mode  # "auto" | "strict"
        self.allowlist: set[str] = set()
        self.denylist:  set[str] = set()

    def normalize(self, name: str, tool_input: dict) -> dict:
        if name.startswith("mcp__"):
            _, server, tool = name.split("__", 2)
            source = "mcp"
        else:
            server, tool, source = "native", name, "native"
        risk = self._assess_risk(name, tool, tool_input)
        return {"source": source, "server": server, "tool": tool, "risk": risk}

    def _assess_risk(self, full_name: str, tool: str, tool_input: dict) -> str:
        if full_name == "bash":
            cmd = (tool_input or {}).get("command", "").lower()
            if any(p in cmd for p in ("rm -rf", "shutdown", "mkfs", "dd if=")):
                return "high"
            return "write"
        lower = tool.lower()
        if any(lower.startswith(p) for p in HIGH_RISK_PREFIXES):
            return "high"
        if any(lower.startswith(p) for p in READ_PREFIXES):
            return "read"
        return "write"

    def check(self, name: str, tool_input: dict) -> tuple[str, dict]:
        intent = self.normalize(name, tool_input)
        if name in self.denylist:
            return "deny", intent
        if name in self.allowlist:
            return "allow", intent
        if intent["risk"] == "read":
            return "allow", intent
        if self.mode == "auto" and intent["risk"] != "high":
            return "allow", intent
        return "ask", intent

    def remember(self, name: str, decision: str):
        if decision == "allow":
            self.allowlist.add(name)
        elif decision == "deny":
            self.denylist.add(name)


permissions = CapabilityPermissionGate(mode=os.environ.get("AGENT_PERM_MODE", "auto"))
