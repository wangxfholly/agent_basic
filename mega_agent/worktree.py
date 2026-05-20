"""
mega_agent.worktree
===================

Git-worktree wrapper. Each subtask runs in its own worktree (own branch,
own working copy) so concurrent agents cannot stomp on each other's files.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
from pathlib import Path

from .config import NAME_RE, REPO_ROOT, WORKTREE_INDEX, WORKTREE_ROOT
from .events import EventBus, events
from .tasks import TaskManager, tasks as _tasks


class WorktreeManager:
    def __init__(self, root: Path, index: Path, ev: EventBus, tm: TaskManager):
        self.root, self.index, self.ev, self.tm = root, index, ev, tm
        self._lock = threading.Lock()

    @staticmethod
    def _validate_name(name: str):
        if not name or not NAME_RE.fullmatch(name):
            raise ValueError(f"invalid worktree name: {name!r}")

    def _load(self) -> dict:
        return json.loads(self.index.read_text(encoding="utf-8") or "{}")

    def _save(self, data: dict):
        with self._lock:
            self.index.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")

    def create(self, name: str, *, task_id: str | None = None, base: str = "HEAD") -> dict:
        self._validate_name(name)
        data = self._load()
        if name in data:
            raise ValueError(f"worktree {name!r} already exists")
        path = self.root / name
        branch = f"wt/{name}"
        self.ev.emit("worktree.create.before", name=name, task_id=task_id, base=base)
        try:
            subprocess.run(["git", "worktree", "add", "-b", branch, str(path), base],
                           cwd=REPO_ROOT, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            self.ev.emit("worktree.create.failed", name=name, error=e.stderr)
            raise
        record = {"name": name, "path": str(path), "branch": branch,
                  "base": base, "task_id": task_id,
                  "status": "active", "created_at": time.time()}
        data[name] = record
        self._save(data)
        if task_id:
            self.tm.bind_worktree(task_id, name)
        self.ev.emit("worktree.create.after", **record)
        return record

    def list(self) -> list[dict]:
        return list(self._load().values())

    def status(self, name: str) -> dict:
        data = self._load()
        if name not in data:
            raise KeyError(name)
        rec = dict(data[name])
        try:
            out = subprocess.run(["git", "status", "--short"],
                                 cwd=rec["path"], capture_output=True, text=True, timeout=10)
            rec["dirty"] = bool(out.stdout.strip())
            rec["status_text"] = out.stdout.strip()
        except Exception as e:
            rec["dirty"] = None
            rec["status_error"] = str(e)
        return rec

    def run(self, name: str, command: str, timeout: int = 120) -> dict:
        data = self._load()
        if name not in data:
            raise KeyError(name)
        path = data[name]["path"]
        self.ev.emit("worktree.run.before", name=name, command=command)
        proc = subprocess.run(command, shell=True, cwd=path,
                              capture_output=True, text=True, timeout=timeout)
        self.ev.emit("worktree.run.after", name=name, returncode=proc.returncode,
                     stdout_tail=proc.stdout[-500:], stderr_tail=proc.stderr[-500:])
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    def keep(self, name: str, *, note: str | None = None) -> dict:
        data = self._load()
        if name not in data:
            raise KeyError(name)
        data[name]["status"] = "kept"
        data[name]["kept_at"] = time.time()
        if note:
            data[name]["note"] = note
        self._save(data)
        tid = data[name].get("task_id")
        if tid:
            self.tm.unbind_worktree(tid, state="detached")
            self.tm.record_closeout(tid, {"action": "keep", "name": name,
                                          "ts": time.time(), "note": note})
        self.ev.emit("worktree.keep", name=name, task_id=tid, note=note)
        return data[name]

    def remove(self, name: str, *, force: bool = False, complete_task: bool = False) -> dict:
        data = self._load()
        if name not in data:
            raise KeyError(name)
        rec = data[name]
        path = rec["path"]
        self.ev.emit("worktree.remove.before", name=name, force=force)
        cmd = ["git", "worktree", "remove", path] + (["--force"] if force else [])
        try:
            subprocess.run(cmd, cwd=REPO_ROOT, check=True, capture_output=True, text=True)
        except subprocess.CalledProcessError as e:
            self.ev.emit("worktree.remove.failed", name=name, error=e.stderr)
            raise
        del data[name]
        self._save(data)
        tid = rec.get("task_id")
        if tid:
            state = "closed" if complete_task else "detached"
            self.tm.unbind_worktree(tid, state=state)
            if complete_task:
                self.tm.update_status(tid, "done")
            self.tm.record_closeout(tid, {"action": "remove", "name": name,
                                          "ts": time.time(),
                                          "complete_task": complete_task, "force": force})
        self.ev.emit("worktree.remove.after", name=name, task_id=tid, complete_task=complete_task)
        return {"removed": name, "task_id": tid}

    def closeout(self, name: str, action: str, **kwargs):
        if action == "keep":
            return self.keep(name, **kwargs)
        if action == "remove":
            return self.remove(name, **kwargs)
        raise ValueError(f"unknown closeout action: {action}")


worktrees = WorktreeManager(WORKTREE_ROOT, WORKTREE_INDEX, events, _tasks)
