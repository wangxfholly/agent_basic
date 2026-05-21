"""
mega_agent.llm.mock_adapter
===========================

Deterministic offline adapter for tests. Queue scripted LLMResponse
instances via `MockAdapter.queue_response(...)`; the kernel pops them
in order and never makes network calls.
"""
from __future__ import annotations

from typing import Iterator

from .base import LLMClient, LLMResponse, TextBlock


class MockAdapter(LLMClient):
    backend = "mock"
    _queue: list[LLMResponse] = []

    def __init__(self, model: str = "mock-1"):
        self.model = model

    @classmethod
    def queue_response(cls, resp: LLMResponse):
        cls._queue.append(resp)

    @classmethod
    def reset(cls):
        cls._queue.clear()

    def complete(self, *, system, tools, messages, max_tokens=4096):
        if self._queue:
            return self._queue.pop(0)
        return LLMResponse(
            content=[TextBlock(text="[mock] no scripted response; ending.")],
            stop_reason="end_turn",
        )

    def stream(self, *, system, tools, messages, max_tokens=4096
               ) -> Iterator[tuple[str, object]]:
        """Chunk-emulate streaming so kernel/CLI exercise the streaming path."""
        resp = self.complete(system=system, tools=tools,
                             messages=messages, max_tokens=max_tokens)
        for b in resp.content:
            if getattr(b, "type", None) == "text":
                txt = getattr(b, "text", "")
                # split into pseudo-token chunks of ~12 chars
                for i in range(0, len(txt), 12):
                    yield ("text", txt[i:i + 12])
        yield ("done", resp)
