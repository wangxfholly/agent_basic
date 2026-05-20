"""
mega_agent.events
=================

JSONL event bus. Single global instance `events` is consumed by every layer
(permissions, hooks, tasks, worktree, background, cron, teams, mcp, profiles).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .config import EVENTS_LOG


class EventBus:
    """Append-only JSONL event log with thread-safe writes."""

    def __init__(self, path: Path):
        self.path = path
        self._lock = threading.Lock()

    def emit(self, kind: str, **payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        record = {"id": uuid.uuid4().hex[:8], "ts": time.time(), "kind": kind, **payload}
        with self._lock:
            with self.path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
        return record

    def list_recent(self, limit: int = 20):
        if not self.path.exists():
            return []
        lines = self.path.read_text(encoding="utf-8").splitlines()[-limit:]
        return [json.loads(l) for l in lines if l.strip()]


events = EventBus(EVENTS_LOG)
