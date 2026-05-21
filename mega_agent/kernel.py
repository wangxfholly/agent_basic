"""
mega_agent.kernel
=================

The agent main loop. Pure orchestration — no vendor SDKs and no
file IO beyond what tools / hooks already do.

Streaming + cancellation
------------------------
Pass ``on_text=callback`` to receive incremental text chunks (the kernel
will use ``llm.stream()`` instead of ``llm.complete()``). Pass
``cancel_event=threading.Event()`` to abort cleanly: the kernel checks
the flag between iterations, between stream chunks, and after each
tool call. On cancellation it appends a synthetic
``CancelledError: cancelled by user`` text block and returns.
"""
from __future__ import annotations

import json
from typing import Any, Callable

from .background import notify_q
from .config import MAX_LOOP_ITERS
from .hooks import build_system_prompt, hooks
from .llm import LLMClient, LLMResponse, TextBlock, make_llm_client
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


def _is_cancelled(cancel_event) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _consume_stream(llm: LLMClient, *, system: str, tools: list[dict],
                    messages: list[dict], on_text: Callable[[str], None] | None,
                    cancel_event) -> tuple[LLMResponse, bool]:
    """Drive llm.stream(); deliver text chunks; honor cancellation.

    Returns (response, cancelled). If cancelled mid-stream the response
    contains whatever text was accumulated so far and stop_reason="end_turn".
    """
    text_buf: list[str] = []
    final: LLMResponse | None = None
    for kind, payload in llm.stream(system=system, tools=tools,
                                    messages=messages):
        if _is_cancelled(cancel_event):
            return (LLMResponse(
                content=[TextBlock(text="".join(text_buf))],
                stop_reason="end_turn",
            ), True)
        if kind == "text":
            text_buf.append(payload)
            if on_text:
                on_text(payload)
        elif kind == "done":
            final = payload
            break
    if final is None:
        # Adapter ended without a "done" frame; synthesize one.
        final = LLMResponse(
            content=[TextBlock(text="".join(text_buf))],
            stop_reason="end_turn",
        )
    return final, False


def agent_loop(user_prompt: str, *, role: str = "lead",
               llm: LLMClient | None = None,
               route: str | None = None,
               recall_k: int = 5,
               on_text: Callable[[str], None] | None = None,
               cancel_event=None) -> list[dict]:
    """
    Main loop. Talks to LLM strictly through `LLMClient.complete()`
    (or `.stream()` when ``on_text`` is supplied).
    Returns the full message history (last item contains the final answer).
    """
    llm = llm or make_llm_client(route=route)
    tools = build_tool_schemas()
    sys_prompt = build_system_prompt(
        role=role, recall_query=user_prompt, recall_k=recall_k,
    )
    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    cancelled = False
    for i in range(MAX_LOOP_ITERS):
        if _is_cancelled(cancel_event):
            cancelled = True
            break
        hooks.emit("loop.iter", iter=i, backend=llm.backend)

        pending = _drain_pending_messages()
        if pending:
            messages.append({"role": "user", "content": "\n".join(pending)})

        if on_text is not None:
            resp, cancelled = _consume_stream(
                llm, system=sys_prompt, tools=tools, messages=messages,
                on_text=on_text, cancel_event=cancel_event,
            )
        else:
            resp = llm.complete(
                system=sys_prompt, tools=tools,
                messages=messages, max_tokens=4096,
            )
        messages.append({"role": "assistant", "content": resp.content})
        if cancelled:
            break

        if resp.stop_reason != "tool_use":
            break

        results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            if _is_cancelled(cancel_event):
                cancelled = True
                # Mark remaining tool calls as cancelled so the API
                # contract (every tool_use needs a tool_result) holds.
                results.append({
                    "type": "tool_result",
                    "tool_use_id": block.id,
                    "content": "CANCELLED by user",
                    "is_error": True,
                })
                continue
            ok, raw, intent = _exec_tool(block.name, block.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": normalize_tool_result(intent, ok, raw)[:8000],
                "is_error": not ok,
            })
        messages.append({"role": "user", "content": results})
        if cancelled:
            break

    if cancelled:
        messages.append({
            "role": "assistant",
            "content": [TextBlock(text="[cancelled by user]")],
        })
    return messages
