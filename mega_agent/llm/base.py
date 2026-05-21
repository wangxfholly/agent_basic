"""
mega_agent.llm.base
===================

LLM-vendor-agnostic types and the `LLMClient` protocol. The kernel only
ever talks to `LLMClient.complete()` (or `.stream()` when streaming);
concrete adapters live in sibling modules
(anthropic_adapter / gateway_adapter / mock_adapter).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator


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
    """Common LLM interface. Each backend implements `complete()`.

    Streaming is opt-in: adapters MAY override `stream()` to yield
    incremental ``("text", chunk)`` tuples plus a final
    ``("done", LLMResponse)``. The default falls back to a single
    `complete()` call so every adapter is at minimum stream-shaped.
    """

    backend: str = "abstract"

    def complete(self, *, system: str, tools: list[dict],
                 messages: list[dict], max_tokens: int = 4096) -> LLMResponse:
        raise NotImplementedError

    def stream(self, *, system: str, tools: list[dict],
               messages: list[dict], max_tokens: int = 4096
               ) -> Iterator[tuple[str, object]]:
        """Default: emulate streaming by yielding the final blocks once."""
        resp = self.complete(system=system, tools=tools,
                             messages=messages, max_tokens=max_tokens)
        for b in resp.content:
            if getattr(b, "type", None) == "text":
                yield ("text", getattr(b, "text", ""))
        yield ("done", resp)
