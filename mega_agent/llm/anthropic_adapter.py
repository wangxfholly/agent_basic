"""
mega_agent.llm.anthropic_adapter
================================

Direct Anthropic SDK adapter. base_url / api_key may be overridden via
profile (otherwise SDK falls back to ANTHROPIC_API_KEY).
"""
from __future__ import annotations

try:
    from anthropic import Anthropic
except ImportError:  # SDK is optional — only required when this backend is used
    Anthropic = None  # type: ignore

from .base import LLMClient, LLMResponse, TextBlock, ToolUseBlock


class AnthropicAdapter(LLMClient):
    backend = "anthropic"

    def __init__(self, model: str, *, base_url: str | None = None,
                 api_key: str | None = None):
        if Anthropic is None:
            raise RuntimeError("anthropic SDK not installed; pip install anthropic")
        kwargs = {}
        if api_key:
            kwargs["api_key"] = api_key
        if base_url:
            kwargs["base_url"] = base_url
        self.client = Anthropic(**kwargs)
        self.model = model

    def complete(self, *, system, tools, messages, max_tokens=4096):
        resp = self.client.messages.create(
            model=self.model, system=system, tools=tools,
            messages=messages, max_tokens=max_tokens,
        )
        blocks = []
        for b in resp.content:
            t = getattr(b, "type", None)
            if t == "text":
                blocks.append(TextBlock(text=b.text))
            elif t == "tool_use":
                blocks.append(ToolUseBlock(id=b.id, name=b.name, input=b.input))
        return LLMResponse(content=blocks, stop_reason=resp.stop_reason)
