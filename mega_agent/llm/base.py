"""
mega_agent.llm.base
===================

LLM-vendor-agnostic types and the `LLMClient` protocol. The kernel only
ever talks to `LLMClient.complete()`; concrete adapters live in sibling
modules (anthropic_adapter / gateway_adapter / mock_adapter).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass
class TextBlock:
    text: str
    type: str = "text"


@dataclass
class ToolUseBlock:
    id: str
    name: str
    input: dict
    type: str = "tool_use"


@dataclass
class LLMResponse:
    content: list  # list[TextBlock | ToolUseBlock]
    stop_reason: str  # "tool_use" | "end_turn" | "max_tokens"


class LLMClient:
    """Common LLM interface. Each backend implements `complete()`."""

    backend: str = "abstract"

    def complete(self, *, system: str, tools: list[dict],
                 messages: list[dict], max_tokens: int = 4096) -> LLMResponse:
        raise NotImplementedError
