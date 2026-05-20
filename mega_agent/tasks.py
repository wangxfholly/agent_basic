"""
mega_agent.tasks
================

Filesystem-backed task graph (one JSON file per task). Each task may be
bound to a git worktree (see mega_agent.worktree).
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .config import TASKS_DIR
from .events import events


class TaskManager:
    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, tid: str) -> Path:
        return self.root / f"{tid}.json"

    def _save(self, task: dict):
        with self._lock:
            self._path(task["id"]).write_text(
                json.dumps(task, indent=2, ensure_ascii=False), encoding="utf-8")

    def create(self, title: str, *, role: str | None = None,
               blocked_by: list[str] | None = None,
               owner: str | None = None) -> dict:
        tid = uuid.uuid4().hex[:8]
        task = {
            "id": tid, "title": title, "status": "pending",
            "role": role, "owner": owner,
            "blockedBy": blocked_by or [],
            "created_at": time.time(),
            "worktree": None, "worktree_state": None,
            "last_worktree": None, "closeout": None,
        }
        self._save(task)
        events.emit("task.created", task_id=tid, title=title, role=role)
        return task

    def get(self, tid: str) -> dict | None:
        p = self._path(tid)
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None

    def list(self) -> list[dict]:
        return [json.loads(p.read_text(encoding="utf-8"))
                for p in sorted(self.root.glob("*.json"))]

    def update_status(self, tid: str, status: str) -> dict | None:
        t = self.get(tid)
        if not t:
            return None
        t["status"] = status
        self._save(t)
        events.emit("task.status", task_id=tid, status=status)
        return t

    def link(self, tid: str, blocked_by: list[str]) -> dict | None:
        t = self.get(tid)
        if not t:
            return None
        t["blockedBy"] = list(set((t.get("blockedBy") or []) + blocked_by))
        self._save(t)
        return t

    # ---- worktree binding ----
    def bind_worktree(self, tid: str, name: str) -> dict | None:
        t = self.get(tid)
        if not t:
            return None
        t["worktree"] = name
        t["worktree_state"] = "active"
        t["last_worktree"] = name
        self._save(t)
        return t

    def unbind_worktree(self, tid: str, *, state: str = "detached") -> dict | None:
        t = self.get(tid)
        if not t:
            return None
        t["worktree"] = None
        t["worktree_state"] = state
        self._save(t)
        return t

    def record_closeout(self, tid: str, info: dict) -> dict | None:
        t = self.get(tid)
        if not t:
            return None
        t["closeout"] = info
        self._save(t)
        return t

    # ---- claim / autonomy ----
    def claimable(self, role: str) -> list[dict]:
        out = []
        for t in self.list():
            if t["status"] != "pending":
                continue
            if t.get("owner"):
                continue
            if t.get("blockedBy"):
                continue
            if t.get("role") and t["role"] != role:
                continue
            out.append(t)
        return out

    def claim(self, tid: str, owner: str) -> dict | None:
        with self._lock:
            t = self.get(tid)
            if not t or t.get("owner") or t["status"] != "pending":
                return None
            t["owner"] = owner
            t["status"] = "in_progress"
            self._save(t)
        events.emit("task.claimed", task_id=tid, owner=owner)
        return t


tasks = TaskManager(TASKS_DIR)
