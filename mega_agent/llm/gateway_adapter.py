"""
mega_agent.llm.gateway_adapter
==============================

OpenAI-protocol-compatible adapter. Use this for OpenAI itself,
LiteLLM, OpenRouter, vLLM, or any in-house LLM gateway that speaks
the OpenAI Chat Completions API.

base_url / api_key precedence:
  1. explicit kwargs (from profile)
  2. AGENT_GATEWAY_BASE_URL / AGENT_GATEWAY_API_KEY
  3. OPENAI_BASE_URL        / OPENAI_API_KEY
"""
from __future__ import annotations

import json
import os

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None  # type: ignore

from .base import LLMClient, LLMResponse, TextBlock, ToolUseBlock


class GatewayAdapter(LLMClient):
    backend = "gateway"

    def __init__(self, model: str, *, base_url: str | None = None,
                 api_key: str | None = None):
        if OpenAI is None:
            raise RuntimeError("openai SDK not installed; pip install openai")
        base_url = (base_url
                    or os.environ.get("AGENT_GATEWAY_BASE_URL")
                    or os.environ.get("OPENAI_BASE_URL"))
        api_key = (api_key
                   or os.environ.get("AGENT_GATEWAY_API_KEY")
                   or os.environ.get("OPENAI_API_KEY"))
        if not api_key:
            raise RuntimeError(
                "api_key required (profile.api_key or AGENT_GATEWAY_API_KEY)")
        kwargs = {"api_key": api_key}
        if base_url:
            kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model

    # -------- protocol shims --------
    @staticmethod
    def _to_openai_tools(tools: list[dict]) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object"},
            },
        } for t in tools]

    @staticmethod
    def _to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            role, content = m["role"], m["content"]
            # tool_result list → multiple role=tool messages
            if (isinstance(content, list) and content
                    and isinstance(content[0], dict)
                    and content[0].get("type") == "tool_result"):
                for r in content:
                    out.append({
                        "role": "tool",
                        "tool_call_id": r["tool_use_id"],
                        "content": r["content"],
                    })
                continue
            # assistant content may be a list of TextBlock/ToolUseBlock
            if role == "assistant" and isinstance(content, list):
                texts, tool_calls = [], []
                for b in content:
                    if isinstance(b, TextBlock) or getattr(b, "type", None) == "text":
                        texts.append(getattr(b, "text", ""))
                    elif (isinstance(b, ToolUseBlock)
                          or getattr(b, "type", None) == "tool_use"):
                        tool_calls.append({
                            "id": b.id, "type": "function",
                            "function": {
                                "name": b.name,
                                "arguments": json.dumps(b.input, ensure_ascii=False),
                            },
                        })
                msg = {"role": "assistant", "content": "\n".join(texts) or None}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
                continue
            out.append({"role": role, "content": content})
        return out

    def complete(self, *, system, tools, messages, max_tokens=4096):
        oai_msgs = self._to_openai_messages(system, messages)
        oai_tools = self._to_openai_tools(tools) if tools else None
        resp = self.client.chat.completions.create(
            model=self.model, messages=oai_msgs, tools=oai_tools,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        msg = choice.message
        blocks = []
        if msg.content:
            blocks.append(TextBlock(text=msg.content))
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            blocks.append(ToolUseBlock(id=tc.id, name=tc.function.name, input=args))
        finish = choice.finish_reason
        stop_reason = (
            "tool_use" if finish == "tool_calls"
            else "max_tokens" if finish == "length"
            else "end_turn"
        )
        return LLMResponse(content=blocks, stop_reason=stop_reason)
