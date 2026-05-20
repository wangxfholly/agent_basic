"""
mega_agent.teams
================

Multi-agent collaboration: a JSONL inbox (MessageBus), an open-request
ledger (RequestStore), and a TeammateManager that runs each teammate
on a background thread polling its inbox.
"""
from __future__ import annotations

import json
import threading
import time
import uuid
from pathlib import Path

from .config import CLAIM_EVENTS, INBOX_DIR, REQUESTS_DIR, TEAM_CONFIG
from .events import events
from .tasks import tasks

VALID_MSG_TYPES = {
    "chat", "result", "shutdown_request", "shutdown_response",
    "plan_approval", "plan_approval_response",
}


class MessageBus:
    """Per-recipient JSONL inbox. read_inbox is destructive (drains)."""

    def __init__(self, inbox_dir: Path):
        self.inbox = inbox_dir
        self.inbox.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _path(self, name: str) -> Path:
        return self.inbox / f"{name}.jsonl"

    def send(self, sender: str, recipient: str, msg_type: str, body: dict):
        if msg_type not in VALID_MSG_TYPES:
            raise ValueError(f"unknown msg_type: {msg_type}")
        rec = {"id": uuid.uuid4().hex[:8], "ts": time.time(),
               "from": sender, "to": recipient, "type": msg_type, "body": body}
        with self._lock:
            with self._path(recipient).open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        return rec

    def read_inbox(self, name: str) -> list[dict]:
        p = self._path(name)
        if not p.exists():
            return []
        with self._lock:
            lines = p.read_text(encoding="utf-8").splitlines()
            p.write_text("", encoding="utf-8")
        return [json.loads(l) for l in lines if l.strip()]


bus = MessageBus(INBOX_DIR)


class RequestStore:
    """Open request ledger for cross-agent protocols (approve/result/etc)."""

    def __init__(self, root: Path):
        self.root = root
        self.root.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def create(self, kind: str, **payload) -> dict:
        rid = uuid.uuid4().hex[:8]
        rec = {"id": rid, "kind": kind, "status": "open",
               "created_at": time.time(), **payload}
        with self._lock:
            (self.root / f"{rid}.json").write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return rec

    def update(self, rid: str, **fields) -> dict | None:
        p = self.root / f"{rid}.json"
        if not p.exists():
            return None
        with self._lock:
            rec = json.loads(p.read_text(encoding="utf-8"))
            rec.update(fields)
            p.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return rec


requests_store = RequestStore(REQUESTS_DIR)


class TeammateManager:
    """Spawn / shutdown teammates; each runs a 50-tick background loop."""

    def __init__(self, config_path: Path):
        self.config_path = config_path
        self.threads: dict[str, threading.Thread] = {}
        self._claim_lock = threading.Lock()
        if not config_path.exists():
            config_path.write_text(json.dumps({"members": {}}, indent=2), encoding="utf-8")

    def _load(self) -> dict:
        return json.loads(self.config_path.read_text(encoding="utf-8"))

    def _save(self, data: dict):
        self.config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def list(self) -> list[dict]:
        return list(self._load().get("members", {}).values())

    def spawn(self, name: str, role: str, *, autonomous: bool = False) -> dict:
        cfg = self._load()
        members = cfg.setdefault("members", {})
        if name in members and members[name].get("status") == "working":
            raise ValueError(f"{name} is already working")
        members[name] = {"name": name, "role": role,
                         "status": "working", "autonomous": autonomous,
                         "spawned_at": time.time()}
        self._save(cfg)
        if name not in self.threads or not self.threads[name].is_alive():
            t = threading.Thread(
                target=self._teammate_loop, args=(name, role, autonomous), daemon=True)
            self.threads[name] = t
            t.start()
        return members[name]

    def shutdown(self, name: str):
        cfg = self._load()
        if name in cfg.get("members", {}):
            cfg["members"][name]["status"] = "shutdown"
            self._save(cfg)

    def _teammate_loop(self, name: str, role: str, autonomous: bool):
        for _ in range(50):
            cfg = self._load()
            if cfg.get("members", {}).get(name, {}).get("status") != "working":
                return
            inbox = bus.read_inbox(name)
            for msg in inbox:
                events.emit("teammate.inbox", to=name, msg_type=msg["type"],
                            from_=msg.get("from"))
            if autonomous:
                cands = tasks.claimable(role)
                for t in cands:
                    with self._claim_lock:
                        claimed = tasks.claim(t["id"], owner=name)
                    if claimed:
                        with CLAIM_EVENTS.open("a", encoding="utf-8") as f:
                            f.write(json.dumps({"ts": time.time(), "by": name,
                                                "task": t["id"]}) + "\n")
                        break
            time.sleep(5)


team = TeammateManager(TEAM_CONFIG)
