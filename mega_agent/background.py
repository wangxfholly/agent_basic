"""
mega_agent.background
=====================

Background command runner + notification queue + zero-dependency cron.
The cron loop dispatches matched jobs through `BackgroundManager.run`.
"""
from __future__ import annotations

import json
import subprocess
import threading
import time
import uuid
from pathlib import Path

from .config import RUNTIME_TASKS, WORKDIR
from .events import events

# ------------------------------------------------------------------
# Notification queue (priority + key-collapsing)
# ------------------------------------------------------------------
PRIORITIES = {"immediate": 0, "high": 1, "medium": 2, "low": 3}


class NotificationQueue:
    def __init__(self):
        self._items: dict[str, dict] = {}
        self._lock = threading.Lock()

    def push(self, key: str, payload: dict, priority: str = "medium"):
        with self._lock:
            self._items[key] = {
                "key": key, "payload": payload,
                "priority": PRIORITIES.get(priority, 2),
                "ts": time.time(),
            }

    def drain(self) -> list[dict]:
        with self._lock:
            items = list(self._items.values())
            self._items.clear()
        items.sort(key=lambda x: (x["priority"], x["ts"]))
        return items


notify_q = NotificationQueue()


# ------------------------------------------------------------------
# Background command runner
# ------------------------------------------------------------------
class BackgroundManager:
    def __init__(self, archive_dir: Path):
        self.archive = archive_dir
        self.archive.mkdir(parents=True, exist_ok=True)
        self._tasks: dict[str, dict] = {}
        self._lock = threading.Lock()

    def run(self, command: str, *, label: str | None = None, timeout: int = 300) -> dict:
        bid = uuid.uuid4().hex[:8]
        rec = {"id": bid, "label": label or command[:40],
               "command": command, "status": "running",
               "started_at": time.time()}
        with self._lock:
            self._tasks[bid] = rec
        threading.Thread(target=self._exec, args=(bid, command, timeout), daemon=True).start()
        return rec

    def _exec(self, bid: str, command: str, timeout: int):
        try:
            proc = subprocess.run(command, shell=True, cwd=WORKDIR,
                                  capture_output=True, text=True, timeout=timeout)
            ok = proc.returncode == 0
            payload = {"id": bid, "ok": ok, "rc": proc.returncode,
                       "stdout_tail": proc.stdout[-500:], "stderr_tail": proc.stderr[-500:]}
            self._tasks[bid]["status"] = "done" if ok else "failed"
            self._tasks[bid]["finished_at"] = time.time()
            (self.archive / f"{bid}.json").write_text(
                json.dumps({**self._tasks[bid], **payload}, indent=2), encoding="utf-8")
            notify_q.push(bid, payload, priority="medium" if ok else "high")
        except Exception as e:
            notify_q.push(bid, {"id": bid, "ok": False, "error": str(e)}, priority="high")

    def status(self, bid: str) -> dict | None:
        return self._tasks.get(bid)


background = BackgroundManager(RUNTIME_TASKS)


# ------------------------------------------------------------------
# Cron scheduler (no external deps)
# ------------------------------------------------------------------
def _field_match(field: str, value: int) -> bool:
    if field == "*":
        return True
    for part in field.split(","):
        step = 1
        if "/" in part:
            part, s = part.split("/")
            step = int(s)
        if part in ("*", ""):
            lo, hi = 0, 59
        elif "-" in part:
            lo, hi = map(int, part.split("-"))
        else:
            lo = hi = int(part)
        for v in range(lo, hi + 1, step):
            if v == value:
                return True
    return False


def cron_matches(expr: str, dt: time.struct_time) -> bool:
    minute, hour, dom, mon, dow = expr.split()
    weekday = (dt.tm_wday + 1) % 7
    return (_field_match(minute, dt.tm_min) and _field_match(hour, dt.tm_hour) and
            _field_match(dom, dt.tm_mday) and _field_match(mon, dt.tm_mon) and
            _field_match(dow, weekday))


class CronScheduler:
    def __init__(self):
        self._jobs: dict[str, dict] = {}
        self._stop = threading.Event()
        self._last_minute = -1
        self._thread: threading.Thread | None = None

    def register(self, name: str, expr: str, command: str) -> dict:
        job = {"name": name, "expr": expr, "command": command,
               "registered_at": time.time(), "last_run": None}
        self._jobs[name] = job
        return job

    def list(self) -> list[dict]:
        return list(self._jobs.values())

    def start(self):
        if self._thread:
            return
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def _loop(self):
        while not self._stop.is_set():
            now = time.localtime()
            if now.tm_min != self._last_minute:
                self._last_minute = now.tm_min
                for job in list(self._jobs.values()):
                    try:
                        if cron_matches(job["expr"], now):
                            background.run(job["command"], label=f"cron:{job['name']}")
                            job["last_run"] = time.time()
                    except Exception as e:
                        events.emit("cron.failed", name=job["name"], error=str(e))
            time.sleep(1)


cron = CronScheduler()
