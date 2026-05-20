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


def build_system_prompt(role: str = "lead",
                        recall_query: str | None = None,
                        recall_k: int = 5) -> str:
    memory_text = load_memory()
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
  - Memory: use `remember` to persist learnings, `recall` to look them up,
    `set_pref/get_pref` for stable preferences. Forget on user request.
  - Skills: scan the catalog below; call `load_skill(name)` to activate one
    before tackling a matching task. Loaded bodies stick for the rest of the run.
"""
    if memory_text:
        base += f"\n# Project Memory (CLAUDE.md)\n{memory_text}\n"

    # Auto-recall is opt-in (kernel passes recall_query for the user turn)
    if recall_query:
        try:
            from .memory import memory as _mem
            hits = _mem.recall(recall_query, k=recall_k)
            if hits:
                lines = ["\n# Recalled memories (top-k by relevance)"]
                for h in hits:
                    lines.append(
                        f"- [{h.get('score', '?')}] {h.get('text', '')[:200]}"
                    )
                base += "\n".join(lines) + "\n"
        except Exception:
            # Memory failure must never block the loop.
            pass

    # Skills: cheap catalog every turn + bodies of currently-loaded skills.
    try:
        from .skills import skills as _skills, render_active_skills_block, render_skill_catalog_block
        catalog = render_skill_catalog_block(_skills)
        if catalog:
            base += "\n" + catalog + "\n"
        active = render_active_skills_block(_skills)
        if active:
            base += "\n" + active + "\n"
    except Exception:
        pass
    return base
