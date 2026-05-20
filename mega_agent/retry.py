"""
mega_agent.retry
================

Per-tool failure budget. Once a tool fails N times in a row it is
"disabled" — the kernel injects a <retry-budget> tag into the next
user message so the LLM can route around it.
"""
from __future__ import annotations

import os


class RetryBudget:
    def __init__(self, threshold: int = 3):
        self.threshold = threshold
        self._fail: dict[str, int] = {}
        self._disabled: set[str] = set()

    def record(self, name: str, ok: bool):
        if ok:
            self._fail[name] = 0
            self._disabled.discard(name)
            return
        self._fail[name] = self._fail.get(name, 0) + 1
        if self._fail[name] >= self.threshold:
            self._disabled.add(name)

    def is_disabled(self, name: str) -> bool:
        return name in self._disabled

    def drain_messages(self) -> list[str]:
        if not self._disabled:
            return []
        msg = ("<retry-budget>tools temporarily disabled: "
               + ",".join(sorted(self._disabled)) + "</retry-budget>")
        return [msg]


retry_budget = RetryBudget(threshold=int(os.environ.get("AGENT_RETRY_THRESHOLD", "3")))
