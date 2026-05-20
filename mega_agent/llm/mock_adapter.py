"""
mega_agent.llm.mock_adapter
===========================

Deterministic offline adapter for tests. Queue scripted LLMResponse
instances via `MockAdapter.queue_response(...)`; the kernel pops them
in order and never makes network calls.
"""
from __future__ import annotations

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
