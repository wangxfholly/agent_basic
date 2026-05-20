"""
mega_agent.hooks
================

In-process hook bus + project memory + system-prompt builder.

Events emitted by the kernel:
    tool.before / tool.after / tool.error / loop.iter
"""
from __future__ import annotations

from typing import Callable

from .config import MEMORY_FILE, REPO_ROOT, WORKDIR
from .events import events


class HookManager:
    """In-process hook bus. Handlers run synchronously in caller thread."""

    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {}

    def on(self, event: str, handler: Callable):
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, **payload):
        for h in self._handlers.get(event, []):
            try:
                h(payload)
            except Exception as e:
                events.emit("hook.failed", event=event, error=str(e))


hooks = HookManager()


def load_memory() -> str:
    if MEMORY_FILE.exists():
        return MEMORY_FILE.read_text(encoding="utf-8")
    return ""


def build_system_prompt(role: str = "lead") -> str:
    memory = load_memory()
    base = f"""You are mega_agent, a layered agent runtime.
Role: {role}
Workdir: {WORKDIR}
Repo:    {REPO_ROOT}

Capabilities (L0-L7):
  L0 loop, L1 permissions, L2 hooks/memory, L3 retry,
  L4 task-graph + worktree, L5 background+cron,
  L6 team+protocol+autonomous, L7 mcp/plugin.

Rules:
  - Errors are observations: read tool_result `is_error` and decide next step.
  - Use create_task BEFORE multi-step work; bind worktree for isolated execution.
  - Long-running work → background_run; recurring → cron_register.
  - Inbox/auto-claim messages arrive as <inbox>/<auto-claimed> tags — handle them.
"""
    if memory:
        base += f"\n# Project Memory (CLAUDE.md)\n{memory}\n"
    return base
