"""
mega_agent.kernel
=================

The agent main loop. Pure orchestration — no vendor SDKs and no
file IO beyond what tools / hooks already do.
"""
from __future__ import annotations

import json
from typing import Any

from .background import notify_q
from .config import MAX_LOOP_ITERS
from .hooks import build_system_prompt, hooks
from .llm import LLMClient, make_llm_client
from .mcp import mcp
from .permissions import permissions
from .retry import retry_budget
from .teams import bus
from .tools import TOOL_HANDLERS, build_tool_schemas, normalize_tool_result


def _drain_pending_messages() -> list[str]:
    """Collect every external signal and inject as the next user turn."""
    msgs: list[str] = []
    msgs.extend(retry_budget.drain_messages())

    bg = notify_q.drain()
    if bg:
        msgs.append("<background-results>" +
                    json.dumps(bg, ensure_ascii=False)[:3000] +
                    "</background-results>")

    inbox = bus.read_inbox("lead")
    if inbox:
        msgs.append("<inbox>" +
                    json.dumps(inbox, ensure_ascii=False)[:3000] +
                    "</inbox>")
    return msgs


def _exec_tool(name: str, tool_input: dict) -> tuple[bool, Any, dict]:
    decision, intent = permissions.check(name, tool_input)
    if decision == "deny":
        return False, f"DENIED by policy: {name}", intent
    if decision == "ask":
        # In a non-interactive kernel we conservatively deny on `ask`.
        # Real apps wrap this with a UI prompt and call permissions.remember().
        return False, f"ASK required for {name}; auto-denied in non-interactive run", intent

    hooks.emit("tool.before", name=name, input=tool_input, intent=intent)
    try:
        if name.startswith("mcp__"):
            raw = mcp.call(name, tool_input)
        else:
            raw = TOOL_HANDLERS[name](tool_input)
        hooks.emit("tool.after", name=name, intent=intent, ok=True)
        retry_budget.record(name, ok=True)
        return True, raw, intent
    except Exception as e:
        hooks.emit("tool.error", name=name, intent=intent, error=str(e))
        retry_budget.record(name, ok=False)
        return False, f"ERROR: {e}", intent


def agent_loop(user_prompt: str, *, role: str = "lead",
               llm: LLMClient | None = None,
               route: str | None = None,
               recall_k: int = 5) -> list[dict]:
    """
    Main loop. Talks to LLM strictly through `LLMClient.complete()`.
    Returns the full message history (last item contains the final answer).
    """
    llm = llm or make_llm_client(route=route)
    tools = build_tool_schemas()
    sys_prompt = build_system_prompt(
        role=role, recall_query=user_prompt, recall_k=recall_k,
    )
    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    for i in range(MAX_LOOP_ITERS):
        hooks.emit("loop.iter", iter=i, backend=llm.backend)

        pending = _drain_pending_messages()
        if pending:
            messages.append({"role": "user", "content": "\n".join(pending)})

        resp = llm.complete(
            system=sys_prompt, tools=tools,
            messages=messages, max_tokens=4096,
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            break

        results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            ok, raw, intent = _exec_tool(block.name, block.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": normalize_tool_result(intent, ok, raw)[:8000],
                "is_error": not ok,
            })
        messages.append({"role": "user", "content": results})

    return messages
