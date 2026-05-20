"""
mega_agent.py — s0~s19 全功能整合 Agent
=========================================

分层架构:
    L0   Agent Loop Kernel              (s00~s06)   主循环 + 工具调度
    L0.5 LLM Gateway Adapter            (NEW)       anthropic / gateway / mock 三选一
    L1   Permissions                    (s07)       三态权限门 (allow/ask/deny)
    L2   Hooks + Memory + System Prompt (s08~s10)   钩子事件 + CLAUDE.md + 系统提示
    L3   Error Recovery                 (s11)       错误即观察 + 重试预算
    L4   Task Graph + Worktree Isolation(s12 + s18) 控制面任务图 + 执行面 git worktree
    L5   Background Tasks + Cron        (s13~s14)   异步通知队列 + 定时调度器
    L6   Teams + Protocols + Autonomous (s15~s17)   团队协作 + 协议 FSM + 自主认领
    L7   MCP / Plugin                   (s19)       插件加载 + 权限网关 + 工具路由

设计原则:
    1. 所有外部信号 → 统一收敛为 user message 中的 <tag>...</tag>
    2. 工具失败 → 转 observation,LLM 自决,绝不内部 retry
    3. 主循环 50 轮安全熔断,所有新机制都通过工具/事件/注入扩展
    4. 状态持久化文件粒度 = 单条记录,JSON/JSONL,git-friendly
    5. 配置全走环境变量,无 yaml 无 toml,零依赖外部解析器
"""

from __future__ import annotations
import json
import os
import re
import subprocess
import threading
import time
import uuid
import hashlib
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from queue import Queue, Empty
from typing import Any, Callable

# ---- 加密(可选,缺失时降级为明文存储 + 警告)----
try:
    from cryptography.fernet import Fernet, InvalidToken
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC
    import base64
    _HAS_CRYPTO = True
except ImportError:
    _HAS_CRYPTO = False

# ---- LLM 后端可选 SDK(全部 try-import,缺失不致命) ----
try:
    from anthropic import Anthropic
except ImportError:
    Anthropic = None
try:
    from openai import OpenAI  # 用于 OpenAI 协议兼容的 Gateway 适配
except ImportError:
    OpenAI = None

# ============================================================
# 全局配置 / 路径布局
# ============================================================
WORKDIR = Path(os.environ.get("AGENT_WORKDIR", ".")).resolve()
MODEL = os.environ.get("AGENT_MODEL", "claude-sonnet-4-20250514")
MAX_LOOP_ITERS = int(os.environ.get("AGENT_MAX_ITERS", "50"))


def detect_repo_root() -> Path:
    """优先 git rev-parse;失败降级为 WORKDIR。"""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=WORKDIR, capture_output=True, text=True, timeout=5,
        )
        if out.returncode == 0 and out.stdout.strip():
            return Path(out.stdout.strip()).resolve()
    except Exception:
        pass
    return WORKDIR


REPO_ROOT = detect_repo_root()

# 各层数据目录(全部位于 WORKDIR 下,git-friendly)
TASKS_DIR        = WORKDIR / ".tasks"
WORKTREE_ROOT    = WORKDIR / ".worktrees"
WORKTREE_INDEX   = WORKTREE_ROOT / "index.json"
EVENTS_LOG       = WORKTREE_ROOT / "events.jsonl"
RUNTIME_TASKS    = WORKDIR / ".runtime-tasks"
CRON_DIR         = WORKDIR / ".cron"
TEAM_DIR         = WORKDIR / ".team"
INBOX_DIR        = TEAM_DIR / "inbox"
REQUESTS_DIR     = TEAM_DIR / "requests"
TEAM_CONFIG      = TEAM_DIR / "config.json"
CLAIM_EVENTS     = TEAM_DIR / "claim_events.jsonl"
MEMORY_FILE      = WORKDIR / "CLAUDE.md"
HOOKS_FILE       = WORKDIR / ".hooks.json"
MODELS_FILE      = WORKDIR / "models.json"          # LLM profile 配置(含 api_key,勿入 git)
MODELS_ENC_FILE  = WORKDIR / "models.json.enc"      # 加密版本(优先级高于明文)
MASTER_SALT_FILE = WORKDIR / ".master-key.salt"     # PBKDF2 salt(可入 git,不含密钥)

NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")

for d in [TASKS_DIR, WORKTREE_ROOT, RUNTIME_TASKS, CRON_DIR,
          TEAM_DIR, INBOX_DIR, REQUESTS_DIR]:
    d.mkdir(parents=True, exist_ok=True)
if not WORKTREE_INDEX.exists():
    WORKTREE_INDEX.write_text("{}", encoding="utf-8")


# ============================================================
# L2: EventBus  —— 跨层共用的事件总线 (s18 起也覆盖任务/团队)
# ============================================================
class EventBus:
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


# ============================================================
# L1: Permissions  —— 三态权限门 (s07 + s19 升级版)
# ============================================================
READ_PREFIXES      = ("read", "list", "get", "show", "search", "query", "inspect")
HIGH_RISK_PREFIXES = ("delete", "remove", "drop", "shutdown", "kill", "rm")


class CapabilityPermissionGate:
    """
    决策表:
        denylist  → deny
        allowlist → allow
        mode=auto + 非 high → allow
        其它 → ask
    """
    def __init__(self, mode: str = "auto"):
        self.mode = mode  # auto | strict
        self.allowlist: set[str] = set()
        self.denylist: set[str] = set()

    def normalize(self, name: str, tool_input: dict) -> dict:
        # MCP 工具:mcp__server__tool
        if name.startswith("mcp__"):
            _, server, tool = name.split("__", 2)
            source = "mcp"
        else:
            server, tool, source = "native", name, "native"
        risk = self._assess_risk(name, tool, tool_input)
        return {"source": source, "server": server, "tool": tool, "risk": risk}

    def _assess_risk(self, full_name: str, tool: str, tool_input: dict) -> str:
        if full_name == "bash":
            cmd = (tool_input or {}).get("command", "").lower()
            if any(p in cmd for p in ("rm -rf", "shutdown", "mkfs", "dd if=")):
                return "high"
            return "write"
        lower = tool.lower()
        if any(lower.startswith(p) for p in HIGH_RISK_PREFIXES):
            return "high"
        if any(lower.startswith(p) for p in READ_PREFIXES):
            return "read"
        return "write"

    def check(self, name: str, tool_input: dict) -> tuple[str, dict]:
        intent = self.normalize(name, tool_input)
        if name in self.denylist:
            return "deny", intent
        if name in self.allowlist:
            return "allow", intent
        if intent["risk"] == "read":
            return "allow", intent
        if self.mode == "auto" and intent["risk"] != "high":
            return "allow", intent
        return "ask", intent

    def remember(self, name: str, decision: str):
        if decision == "allow":
            self.allowlist.add(name)
        elif decision == "deny":
            self.denylist.add(name)


permissions = CapabilityPermissionGate(mode=os.environ.get("AGENT_PERM_MODE", "auto"))


# ============================================================
# L2: Hooks + Memory + System Prompt
# ============================================================
class HookManager:
    """钩子事件:tool.before / tool.after / tool.error / loop.iter"""
    def __init__(self):
        self._handlers: dict[str, list[Callable]] = {}

    def on(self, event: str, handler: Callable):
        self._handlers.setdefault(event, []).append(handler)

    def emit(self, event: str, **payload):
        for h in self._handlers.get(event, []):
            try:
                h(payload)
            except Exception as e:
                events.emit("hook.failed", event=event, error=str(e))


hooks = HookManager()


def load_memory() -> str:
    if MEMORY_FILE.exists():
        return MEMORY_FILE.read_text(encoding="utf-8")
    return ""


def build_system_prompt(role: str = "lead") -> str:
    memory = load_memory()
    base = f"""You are mega_agent, an integrated agent combining s0-s19 capabilities.
Role: {role}
Workdir: {WORKDIR}
Repo:    {REPO_ROOT}

Capabilities layered as L0~L7:
  L0 loop, L1 permissions, L2 hooks/memory, L3 retry,
  L4 task-graph + worktree, L5 background+cron,
  L6 team+protocol+autonomous, L7 mcp/plugin.

Rules:
  - Errors are observations: read tool_result `is_error` and decide next step.
  - Use create_task BEFORE multi-step work; bind worktree for isolated execution.
  - Long-running work → background_run; recurring → cron_register.
  - Inbox/auto-claim messages arrive as <inbox>/<auto-claimed> tags — handle them.
"""
    if memory:
        base += f"\n# Project Memory (CLAUDE.md)\n{memory}\n"
    return base


# ============================================================
# L3: Error Recovery  —— 错误即观察 + 重试预算
# ============================================================
class RetryBudget:
    """
    每个工具最近 N 次连续失败计数,超过阈值则从工具池暂时摘除,
    主循环看到工具不可用时把这条信息以 <retry-budget> 注入下一轮 user 消息。
    """
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
        msg = "<retry-budget>tools temporarily disabled: " + ",".join(sorted(self._disabled)) + "</retry-budget>"
        return [msg]


retry_budget = RetryBudget(threshold=int(os.environ.get("AGENT_RETRY_THRESHOLD", "3")))


# ============================================================
# L4: TaskManager  —— s12 任务图 + s18 worktree 绑定字段
# ============================================================
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
        return [json.loads(p.read_text(encoding="utf-8")) for p in sorted(self.root.glob("*.json"))]

    def update_status(self, tid: str, status: str) -> dict | None:
        t = self.get(tid)
        if not t: return None
        t["status"] = status
        self._save(t)
        events.emit("task.status", task_id=tid, status=status)
        return t

    def link(self, tid: str, blocked_by: list[str]) -> dict | None:
        t = self.get(tid)
        if not t: return None
        t["blockedBy"] = list(set((t.get("blockedBy") or []) + blocked_by))
        self._save(t)
        return t

    # —— s18 worktree 绑定 ——
    def bind_worktree(self, tid: str, name: str) -> dict | None:
        t = self.get(tid)
        if not t: return None
        t["worktree"] = name
        t["worktree_state"] = "active"
        t["last_worktree"] = name
        self._save(t)
        return t

    def unbind_worktree(self, tid: str, *, state: str = "detached") -> dict | None:
        t = self.get(tid)
        if not t: return None
        t["worktree"] = None
        t["worktree_state"] = state
        self._save(t)
        return t

    def record_closeout(self, tid: str, info: dict) -> dict | None:
        t = self.get(tid)
        if not t: return None
        t["closeout"] = info
        self._save(t)
        return t

    def claimable(self, role: str) -> list[dict]:
        out = []
        for t in self.list():
            if t["status"] != "pending": continue
            if t.get("owner"): continue
            if t.get("blockedBy"): continue
            if t.get("role") and t["role"] != role: continue
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


# ============================================================
# L4: WorktreeManager  —— s18 git worktree 包装层
# ============================================================
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
        if name not in data: raise KeyError(name)
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
        if name not in data: raise KeyError(name)
        path = data[name]["path"]
        self.ev.emit("worktree.run.before", name=name, command=command)
        proc = subprocess.run(command, shell=True, cwd=path,
                              capture_output=True, text=True, timeout=timeout)
        self.ev.emit("worktree.run.after", name=name, returncode=proc.returncode,
                     stdout_tail=proc.stdout[-500:], stderr_tail=proc.stderr[-500:])
        return {"returncode": proc.returncode, "stdout": proc.stdout, "stderr": proc.stderr}

    def keep(self, name: str, *, note: str | None = None) -> dict:
        data = self._load()
        if name not in data: raise KeyError(name)
        data[name]["status"] = "kept"
        data[name]["kept_at"] = time.time()
        if note: data[name]["note"] = note
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
        if name not in data: raise KeyError(name)
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
        if action == "keep":   return self.keep(name, **kwargs)
        if action == "remove": return self.remove(name, **kwargs)
        raise ValueError(f"unknown closeout action: {action}")


worktrees = WorktreeManager(WORKTREE_ROOT, WORKTREE_INDEX, events, tasks)


# ============================================================
# L5: Background Tasks  —— s13 通知队列 + 异步执行
# ============================================================
PRIORITIES = {"immediate": 0, "high": 1, "medium": 2, "low": 3}


class NotificationQueue:
    """优先级 + 同 key 折叠的通知队列。"""
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


# ============================================================
# L5: Cron Scheduler  —— s14 零依赖 cron
# ============================================================
def _field_match(field: str, value: int) -> bool:
    if field == "*": return True
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
            if v == value: return True
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
        if self._thread: return
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


# ============================================================
# L6: MessageBus + TeammateManager  —— s15+s16+s17 三合一
# ============================================================
VALID_MSG_TYPES = {"chat", "result", "shutdown_request", "shutdown_response",
                   "plan_approval", "plan_approval_response"}


class MessageBus:
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
        if not p.exists(): return []
        with self._lock:
            lines = p.read_text(encoding="utf-8").splitlines()
            p.write_text("", encoding="utf-8")
        return [json.loads(l) for l in lines if l.strip()]


bus = MessageBus(INBOX_DIR)


class RequestStore:
    """s16 协议请求记账。"""
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
        if not p.exists(): return None
        with self._lock:
            rec = json.loads(p.read_text(encoding="utf-8"))
            rec.update(fields)
            p.write_text(json.dumps(rec, indent=2), encoding="utf-8")
        return rec


requests_store = RequestStore(REQUESTS_DIR)


class TeammateManager:
    """s15+s16+s17 团队 + 协议 + 自治。"""
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
        # 启动后台 loop(简化版:仅占位,实际工作通过 inbox 轮询触发)
        if name not in self.threads or not self.threads[name].is_alive():
            t = threading.Thread(target=self._teammate_loop, args=(name, role, autonomous), daemon=True)
            self.threads[name] = t
            t.start()
        return members[name]

    def shutdown(self, name: str):
        cfg = self._load()
        if name in cfg.get("members", {}):
            cfg["members"][name]["status"] = "shutdown"
            self._save(cfg)

    def _teammate_loop(self, name: str, role: str, autonomous: bool):
        """简化的 teammate 后台 loop:轮询 inbox,自治模式下尝试认领任务。"""
        for _ in range(50):
            cfg = self._load()
            if cfg.get("members", {}).get(name, {}).get("status") != "working":
                return
            inbox = bus.read_inbox(name)
            for msg in inbox:
                events.emit("teammate.inbox", to=name, msg_type=msg["type"], from_=msg.get("from"))
            if autonomous:
                cands = tasks.claimable(role)
                for t in cands:
                    with self._claim_lock:
                        claimed = tasks.claim(t["id"], owner=name)
                    if claimed:
                        with CLAIM_EVENTS.open("a", encoding="utf-8") as f:
                            f.write(json.dumps({"ts": time.time(), "by": name, "task": t["id"]}) + "\n")
                        break
            time.sleep(5)


team = TeammateManager(TEAM_CONFIG)


# ============================================================
# L7: MCP Plugin Layer  —— s19 简化集成
# ============================================================
class MCPClient:
    """stdio + JSON-RPC 2.0 极简实现。生产可换 mcp 官方 SDK。"""
    def __init__(self, name: str, command: list[str], env: dict | None = None):
        self.name, self.command = name, command
        self.env = {**os.environ, **(env or {})}
        self.proc: subprocess.Popen | None = None
        self._id = 0
        self._lock = threading.Lock()

    def connect(self):
        self.proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, env=self.env, bufsize=1)
        self._call("initialize", {"protocolVersion": "2024-11-05",
                                  "capabilities": {}, "clientInfo": {"name": "mega_agent"}})
        self._notify("notifications/initialized", {})

    def _next_id(self) -> int:
        with self._lock:
            self._id += 1
            return self._id

    def _send(self, payload: dict):
        if not self.proc or not self.proc.stdin:
            raise RuntimeError("not connected")
        self.proc.stdin.write(json.dumps(payload) + "\n")
        self.proc.stdin.flush()

    def _recv(self) -> dict:
        line = self.proc.stdout.readline() if self.proc and self.proc.stdout else ""
        if not line: raise RuntimeError("empty response")
        return json.loads(line)

    def _call(self, method: str, params: dict) -> dict:
        rid = self._next_id()
        self._send({"jsonrpc": "2.0", "id": rid, "method": method, "params": params})
        resp = self._recv()
        if "error" in resp: raise RuntimeError(resp["error"])
        return resp.get("result", {})

    def _notify(self, method: str, params: dict):
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def list_tools(self) -> list[dict]:
        return self._call("tools/list", {}).get("tools", [])

    def call_tool(self, name: str, arguments: dict) -> dict:
        return self._call("tools/call", {"name": name, "arguments": arguments})

    def disconnect(self):
        if not self.proc: return
        try: self._call("shutdown", {})
        except Exception: pass
        try: self.proc.terminate(); self.proc.wait(timeout=5)
        except Exception:
            try: self.proc.kill()
            except Exception: pass


class MCPRegistry:
    def __init__(self):
        self.clients: dict[str, MCPClient] = {}

    def register(self, server: str, command: list[str], env: dict | None = None):
        c = MCPClient(server, command, env)
        c.connect()
        self.clients[server] = c
        events.emit("mcp.registered", server=server)

    def get_agent_tools(self) -> list[dict]:
        out = []
        for server, c in self.clients.items():
            for t in c.list_tools():
                out.append({
                    "name": f"mcp__{server}__{t['name']}",
                    "description": t.get("description", ""),
                    "input_schema": t.get("inputSchema", {"type": "object"}),
                })
        return out

    def call(self, full_name: str, arguments: dict) -> dict:
        _, server, tool = full_name.split("__", 2)
        if server not in self.clients:
            raise KeyError(f"mcp server {server} not registered")
        return self.clients[server].call_tool(tool, arguments)


mcp = MCPRegistry()


def normalize_tool_result(intent: dict, ok: bool, raw: Any) -> str:
    preview = json.dumps(raw, ensure_ascii=False)[:1500] if not isinstance(raw, str) else raw[:1500]
    return json.dumps({
        "source": intent["source"], "server": intent["server"],
        "tool": intent["tool"], "risk": intent["risk"],
        "status": "ok" if ok else "error", "preview": preview,
    }, ensure_ascii=False)


# ============================================================
# 工具实现层  —— 4 base + 5 task + 9 worktree + 5 bg/cron + 6 team + 1 events = 30 个原生工具
# ============================================================
def t_bash(inp):
    proc = subprocess.run(inp["command"], shell=True, cwd=WORKDIR,
                          capture_output=True, text=True, timeout=inp.get("timeout", 60))
    return {"rc": proc.returncode, "stdout": proc.stdout[-4000:], "stderr": proc.stderr[-1000:]}


def t_read(inp):
    return (WORKDIR / inp["path"]).read_text(encoding="utf-8")[: inp.get("limit", 8000)]


def t_write(inp):
    p = WORKDIR / inp["path"]
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(inp["content"], encoding="utf-8")
    return {"written": str(p), "bytes": len(inp["content"])}


def t_edit(inp):
    p = WORKDIR / inp["path"]
    src = p.read_text(encoding="utf-8")
    if inp["old"] not in src:
        raise ValueError("old string not found")
    p.write_text(src.replace(inp["old"], inp["new"], 1), encoding="utf-8")
    return {"edited": str(p)}


# Task & worktree tools
def t_create_task(inp):       return tasks.create(inp["title"], role=inp.get("role"),
                                                   blocked_by=inp.get("blockedBy"),
                                                   owner=inp.get("owner"))
def t_list_tasks(_):          return {"tasks": tasks.list()}
def t_update_task_status(inp):return tasks.update_status(inp["id"], inp["status"])
def t_get_task(inp):          return tasks.get(inp["id"])
def t_link_tasks(inp):        return tasks.link(inp["id"], inp["blockedBy"])
def t_create_worktree(inp):   return worktrees.create(inp["name"], task_id=inp.get("task_id"), base=inp.get("base", "HEAD"))
def t_list_worktrees(_):      return {"worktrees": worktrees.list()}
def t_worktree_status(inp):   return worktrees.status(inp["name"])
def t_enter_worktree(inp):
    rec = worktrees._load().get(inp["name"])
    if not rec: raise KeyError(inp["name"])
    return {"path": rec["path"], "branch": rec["branch"]}
def t_run_in_worktree(inp):   return worktrees.run(inp["name"], inp["command"], timeout=inp.get("timeout", 120))
def t_keep_worktree(inp):     return worktrees.keep(inp["name"], note=inp.get("note"))
def t_remove_worktree(inp):   return worktrees.remove(inp["name"], force=inp.get("force", False),
                                                       complete_task=inp.get("complete_task", False))
def t_worktree_closeout(inp): return worktrees.closeout(inp["name"], inp["action"],
                                                         **{k: v for k, v in inp.items() if k not in ("name", "action")})
def t_recent_events(inp):     return {"events": events.list_recent(limit=inp.get("limit", 20))}

# Background & cron tools
def t_background_run(inp):    return background.run(inp["command"], label=inp.get("label"), timeout=inp.get("timeout", 300))
def t_background_status(inp): return background.status(inp["id"]) or {"error": "not found"}
def t_cron_register(inp):     return cron.register(inp["name"], inp["expr"], inp["command"])
def t_cron_list(_):           return {"jobs": cron.list()}
def t_cron_start(_):          cron.start(); return {"status": "started"}

# Team tools
def t_spawn(inp):             return team.spawn(inp["name"], inp["role"], autonomous=inp.get("autonomous", False))
def t_list_teammates(_):      return {"members": team.list()}
def t_send_message(inp):      return bus.send(inp.get("sender", "lead"), inp["to"], inp["type"], inp.get("body", {}))
def t_read_my_inbox(inp):     return {"messages": bus.read_inbox(inp.get("who", "lead"))}
def t_shutdown_teammate(inp):
    team.shutdown(inp["name"])
    bus.send("lead", inp["name"], "shutdown_request", {"reason": inp.get("reason", "")})
    return {"shutdown": inp["name"]}
def t_idle(_):                return {"status": "idle"}


TOOL_HANDLERS: dict[str, Callable] = {
    # base
    "bash": t_bash, "read_file": t_read, "write_file": t_write, "edit_file": t_edit,
    # tasks
    "create_task": t_create_task, "list_tasks": t_list_tasks,
    "update_task_status": t_update_task_status, "get_task": t_get_task, "link_tasks": t_link_tasks,
    # worktree
    "create_worktree": t_create_worktree, "list_worktrees": t_list_worktrees,
    "worktree_status": t_worktree_status, "enter_worktree": t_enter_worktree,
    "run_in_worktree": t_run_in_worktree, "keep_worktree": t_keep_worktree,
    "remove_worktree": t_remove_worktree, "worktree_closeout": t_worktree_closeout,
    "recent_events": t_recent_events,
    # bg / cron
    "background_run": t_background_run, "background_status": t_background_status,
    "cron_register": t_cron_register, "cron_list": t_cron_list, "cron_start": t_cron_start,
    # team
    "spawn": t_spawn, "list_teammates": t_list_teammates,
    "send_message": t_send_message, "read_my_inbox": t_read_my_inbox,
    "shutdown_teammate": t_shutdown_teammate, "idle": t_idle,
}


# ============================================================
# 工具 schema 列表(简化:仅给主要工具示例 schema,其它走宽松 object)
# ============================================================
def build_tool_schemas() -> list[dict]:
    base = []
    for name in TOOL_HANDLERS:
        base.append({
            "name": name,
            "description": f"native tool: {name}",
            "input_schema": {"type": "object", "additionalProperties": True},
        })
    # 合并 MCP 工具,避免覆盖原生
    native_names = {t["name"] for t in base}
    for t in mcp.get_agent_tools():
        if t["name"] not in native_names:
            base.append(t)
    return base


# ============================================================
# L0.5: LLM Gateway 适配层
# ============================================================
# 设计目标:
#   1. 主循环只跟 LLMClient.complete() 一个统一接口对话,跟具体后端解耦
#   2. 三个内置后端:
#        - "anthropic":官方 Anthropic SDK 直连(开发/测试)
#        - "gateway"  :OpenAI 协议兼容的企业 LLM Gateway(生产)
#        - "mock"     :离线测试用,固定返回 stop_reason="end_turn",不发任何请求
#   3. 抽象出统一返回结构 LLMResponse:
#        - .content        : List[ContentBlock]   (text / tool_use)
#        - .stop_reason    : "tool_use" | "end_turn" | ...
#   4. 所有 endpoint / api_key / model 走环境变量,代码内绝不硬编码
#
# 切换方式:
#   export AGENT_LLM_BACKEND=anthropic|gateway|mock
#   export AGENT_GATEWAY_BASE_URL=https://your-gateway/v1     # gateway only
#   export AGENT_GATEWAY_API_KEY=xxx                          # gateway only
# ============================================================
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
    """统一 LLM 接口。各后端只需实现 complete()。"""
    backend: str = "abstract"

    def complete(self, *, system: str, tools: list[dict],
                 messages: list[dict], max_tokens: int = 4096) -> LLMResponse:
        raise NotImplementedError


class AnthropicAdapter(LLMClient):
    """官方 Anthropic SDK 直连。可通过 profile 覆盖 base_url/api_key。"""
    backend = "anthropic"

    def __init__(self, model: str, *, base_url: str | None = None,
                 api_key: str | None = None):
        if Anthropic is None:
            raise RuntimeError("anthropic SDK not installed; pip install anthropic")
        kwargs = {}
        if api_key:  kwargs["api_key"]  = api_key
        if base_url: kwargs["base_url"] = base_url
        self.client = Anthropic(**kwargs)  # 默认走 ANTHROPIC_API_KEY
        self.model = model

    def complete(self, *, system, tools, messages, max_tokens=4096):
        resp = self.client.messages.create(
            model=self.model, system=system, tools=tools,
            messages=messages, max_tokens=max_tokens,
        )
        # 把 SDK 对象转成统一 dataclass(保持下游纯数据)
        blocks = []
        for b in resp.content:
            t = getattr(b, "type", None)
            if t == "text":
                blocks.append(TextBlock(text=b.text))
            elif t == "tool_use":
                blocks.append(ToolUseBlock(id=b.id, name=b.name, input=b.input))
        return LLMResponse(content=blocks, stop_reason=resp.stop_reason)


class GatewayAdapter(LLMClient):
    """
    OpenAI 协议兼容的 LLM 适配器。可对接:
      - OpenAI 官方
      - OpenRouter / LiteLLM / 任意 OpenAI 兼容 gateway
      - 用户自有的企业网关
    base_url / api_key 可由 profile 显式传入,否则回退到环境变量。
    """
    backend = "gateway"

    def __init__(self, model: str, *, base_url: str | None = None,
                 api_key: str | None = None):
        if OpenAI is None:
            raise RuntimeError("openai SDK not installed; pip install openai")
        base_url = base_url or os.environ.get("AGENT_GATEWAY_BASE_URL") \
                   or os.environ.get("OPENAI_BASE_URL")
        api_key  = api_key  or os.environ.get("AGENT_GATEWAY_API_KEY") \
                   or os.environ.get("OPENAI_API_KEY")
        if not api_key:
            raise RuntimeError("api_key required (profile.api_key or AGENT_GATEWAY_API_KEY)")
        kwargs = {"api_key": api_key}
        if base_url: kwargs["base_url"] = base_url
        self.client = OpenAI(**kwargs)
        self.model = model

    # ---- 协议转换:Anthropic-shape ↔ OpenAI-shape ----
    @staticmethod
    def _to_openai_tools(tools: list[dict]) -> list[dict]:
        return [{
            "type": "function",
            "function": {
                "name": t["name"],
                "description": t.get("description", ""),
                "parameters": t.get("input_schema") or {"type": "object"},
            },
        } for t in tools]

    @staticmethod
    def _to_openai_messages(system: str, messages: list[dict]) -> list[dict]:
        out = [{"role": "system", "content": system}]
        for m in messages:
            role, content = m["role"], m["content"]
            # tool_result 数组 → 多条 role=tool 消息
            if isinstance(content, list) and content and isinstance(content[0], dict) \
                    and content[0].get("type") == "tool_result":
                for r in content:
                    out.append({
                        "role": "tool",
                        "tool_call_id": r["tool_use_id"],
                        "content": r["content"],
                    })
                continue
            # assistant 的 content 可能是 dataclass list(我们自己塞回去的)
            if role == "assistant" and isinstance(content, list):
                texts, tool_calls = [], []
                for b in content:
                    if isinstance(b, TextBlock) or getattr(b, "type", None) == "text":
                        texts.append(getattr(b, "text", ""))
                    elif isinstance(b, ToolUseBlock) or getattr(b, "type", None) == "tool_use":
                        tool_calls.append({
                            "id": b.id, "type": "function",
                            "function": {"name": b.name,
                                         "arguments": json.dumps(b.input, ensure_ascii=False)},
                        })
                msg = {"role": "assistant", "content": "\n".join(texts) or None}
                if tool_calls:
                    msg["tool_calls"] = tool_calls
                out.append(msg)
                continue
            # 其它(纯字符串 user / assistant)
            out.append({"role": role, "content": content})
        return out

    def complete(self, *, system, tools, messages, max_tokens=4096):
        oai_msgs = self._to_openai_messages(system, messages)
        oai_tools = self._to_openai_tools(tools) if tools else None
        # 注意:这里调用的是用户自有 Gateway,不是任何内部推理端点。
        resp = self.client.chat.completions.create(
            model=self.model, messages=oai_msgs, tools=oai_tools,
            max_tokens=max_tokens,
        )
        choice = resp.choices[0]
        msg = choice.message
        blocks = []
        if msg.content:
            blocks.append(TextBlock(text=msg.content))
        for tc in (msg.tool_calls or []):
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError:
                args = {"_raw": tc.function.arguments}
            blocks.append(ToolUseBlock(id=tc.id, name=tc.function.name, input=args))
        # OpenAI finish_reason → Anthropic stop_reason 归一化
        finish = choice.finish_reason
        stop_reason = "tool_use" if finish == "tool_calls" else (
            "max_tokens" if finish == "length" else "end_turn")
        return LLMResponse(content=blocks, stop_reason=stop_reason)


class MockAdapter(LLMClient):
    """
    离线/单测后端:不发任何网络请求,直接返回预设结果。
    可通过 MockAdapter.queue_response() 注入下一轮的 LLMResponse,
    主要用于跑 agent_loop 的端到端测试。
    """
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
        # 默认行为:回个 text "ok" 并立刻结束
        return LLMResponse(content=[TextBlock(text="[mock] no scripted response; ending.")],
                           stop_reason="end_turn")


# ============================================================
# Profile Store —— 多模型多 key 配置化
# ============================================================
# models.json 结构:
# {
#   "active": "claude-sonnet",          # 当前选中的 profile 名
#   "profiles": {
#     "claude-sonnet": {
#       "name": "claude-sonnet",
#       "protocol": "anthropic",        # anthropic | openai | mock
#       "base_url": null,               # 可选,默认走官方
#       "api_key":  "sk-ant-...",
#       "model":    "claude-sonnet-4-20250514"
#     },
#     "gpt4o": {
#       "name": "gpt4o",
#       "protocol": "openai",
#       "base_url": "https://api.openai.com/v1",
#       "api_key":  "sk-...",
#       "model":    "gpt-4o"
#     },
#     "openrouter-gemini": {
#       "name": "openrouter-gemini",
#       "protocol": "openai",
#       "base_url": "https://openrouter.ai/api/v1",
#       "api_key":  "sk-or-...",
#       "model":    "google/gemini-2.5-pro"
#     },
#     "litellm-local": {
#       "name": "litellm-local",
#       "protocol": "openai",
#       "base_url": "http://localhost:4000",
#       "api_key":  "sk-litellm",
#       "model":    "claude-3-7-sonnet"
#     }
#   }
# }
class SecretBox:
    """
    简单的对称加密包装。
    - 用户提供 master password → PBKDF2-HMAC-SHA256(200k 轮) 派生 Fernet key
    - salt 落盘 .master-key.salt(无密钥成分,可入 git;丢了仍能用,只是要重新派生)
    - master password 来源(按优先级):
        1. 显式传入构造函数
        2. AGENT_MASTER_PASSWORD 环境变量
        3. 主进程交互式 getpass
    - 缺 cryptography 库 → 退化为明文模式,首次会打印一次性警告
    """
    def __init__(self, salt_path: Path, password: str | None = None):
        self.salt_path = salt_path
        self._password = password or os.environ.get("AGENT_MASTER_PASSWORD")
        self._fernet: Fernet | None = None

    @property
    def enabled(self) -> bool:
        return _HAS_CRYPTO and self._password is not None

    def _ensure_salt(self) -> bytes:
        if self.salt_path.exists():
            return self.salt_path.read_bytes()
        salt = os.urandom(16)
        self.salt_path.write_bytes(salt)
        return salt

    def _build(self) -> Fernet:
        if self._fernet: return self._fernet
        salt = self._ensure_salt()
        kdf = PBKDF2HMAC(algorithm=hashes.SHA256(), length=32,
                         salt=salt, iterations=200_000)
        key = base64.urlsafe_b64encode(kdf.derive(self._password.encode("utf-8")))
        self._fernet = Fernet(key)
        return self._fernet

    def encrypt(self, plaintext: str) -> bytes:
        return self._build().encrypt(plaintext.encode("utf-8"))

    def decrypt(self, token: bytes) -> str:
        return self._build().decrypt(token).decode("utf-8")


_warned_plaintext = False


def _warn_plaintext_once():
    global _warned_plaintext
    if _warned_plaintext: return
    _warned_plaintext = True
    print("[mega_agent] WARNING: storing api keys as plaintext "
          "(set AGENT_MASTER_PASSWORD to enable encryption)")


class ProfileStore:
    """
    多 profile 配置 + 可选加密 + routing 表。
    落盘逻辑:
      - 启动时 SecretBox.enabled  → 优先读 models.json.enc;若不存在但有 models.json,
        自动迁移(读明文 → 写密文 → 删明文)
      - 落盘时 enabled 写 .enc,disabled 写明文(并打印一次性警告)
    """
    def __init__(self, path: Path, enc_path: Path, secrets: SecretBox):
        self.path, self.enc_path, self.secrets = path, enc_path, secrets
        self._lock = threading.Lock()
        # 首次启动:确保至少存在一份(明文骨架),后续迁移
        if not self.path.exists() and not self.enc_path.exists():
            skeleton = {"active": None, "profiles": {}, "routing": {}}
            self._raw_write_plain(json.dumps(skeleton, indent=2))
        # 启用加密但只有明文 → 自动迁移
        if self.secrets.enabled and self.path.exists() and not self.enc_path.exists():
            data = json.loads(self.path.read_text(encoding="utf-8") or "{}")
            self._raw_write_enc(json.dumps(data, indent=2, ensure_ascii=False))
            self.path.unlink()  # 移除明文

    # ---- 底层 IO ----
    def _raw_write_plain(self, text: str):
        self.path.write_text(text, encoding="utf-8")

    def _raw_write_enc(self, text: str):
        token = self.secrets.encrypt(text)
        self.enc_path.write_bytes(token)

    def _load(self) -> dict:
        if self.secrets.enabled and self.enc_path.exists():
            try:
                txt = self.secrets.decrypt(self.enc_path.read_bytes())
            except InvalidToken:
                raise RuntimeError("master password mismatch — cannot decrypt models.json.enc")
            return json.loads(txt or "{}")
        if self.path.exists():
            return json.loads(self.path.read_text(encoding="utf-8") or "{}")
        return {"active": None, "profiles": {}, "routing": {}}

    def _save(self, data: dict):
        text = json.dumps(data, indent=2, ensure_ascii=False)
        with self._lock:
            if self.secrets.enabled:
                self._raw_write_enc(text)
            else:
                _warn_plaintext_once()
                self._raw_write_plain(text)

    # ---- profile CRUD ----
    def list(self) -> list[dict]:
        """列出所有 profile,api_key 已脱敏。"""
        out = []
        data = self._load()
        for p in data.get("profiles", {}).values():
            redacted = dict(p)
            if redacted.get("api_key"):
                k = redacted["api_key"]
                redacted["api_key"] = (k[:6] + "***" + k[-4:]) if len(k) > 12 else "***"
            out.append(redacted)
        return out

    def get(self, name: str) -> dict | None:
        return self._load().get("profiles", {}).get(name)

    def active(self) -> dict | None:
        data = self._load()
        name = data.get("active")
        return data.get("profiles", {}).get(name) if name else None

    def upsert(self, *, name: str, protocol: str, model: str,
               api_key: str, base_url: str | None = None) -> dict:
        if not NAME_RE.fullmatch(name):
            raise ValueError(f"invalid profile name: {name!r}")
        if protocol not in ("anthropic", "openai", "mock"):
            raise ValueError(f"protocol must be anthropic|openai|mock, got {protocol!r}")
        data = self._load()
        data.setdefault("profiles", {})[name] = {
            "name": name, "protocol": protocol, "model": model,
            "api_key": api_key, "base_url": base_url,
        }
        if not data.get("active"):
            data["active"] = name
        self._save(data)
        events.emit("profile.upsert", name=name, protocol=protocol, model=model)
        return data["profiles"][name]

    def remove(self, name: str) -> bool:
        data = self._load()
        if name not in data.get("profiles", {}):
            return False
        del data["profiles"][name]
        if data.get("active") == name:
            data["active"] = next(iter(data["profiles"]), None)
        # 同步清理 routing 中指向该 profile 的条目
        routing = data.get("routing", {})
        for k in list(routing.keys()):
            if routing[k] == name:
                del routing[k]
        self._save(data)
        return True

    def use(self, name: str) -> dict:
        data = self._load()
        if name not in data.get("profiles", {}):
            raise KeyError(name)
        data["active"] = name
        self._save(data)
        events.emit("profile.use", name=name)
        return data["profiles"][name]

    # ---- routing CRUD ----
    def routing(self) -> dict:
        return self._load().get("routing", {}) or {}

    def set_route(self, route: str, profile_name: str) -> dict:
        data = self._load()
        if profile_name not in data.get("profiles", {}):
            raise KeyError(f"profile not found: {profile_name}")
        data.setdefault("routing", {})[route] = profile_name
        self._save(data)
        events.emit("profile.route", route=route, profile=profile_name)
        return data["routing"]

    def del_route(self, route: str) -> bool:
        data = self._load()
        if route not in data.get("routing", {}):
            return False
        del data["routing"][route]
        self._save(data)
        return True

    def resolve_route(self, route: str | None) -> dict | None:
        """
        路由解析:
          1. route 命中 routing 表 → 用对应 profile
          2. routing.default → 用其指定 profile
          3. fallback → active profile
        """
        data = self._load()
        if route:
            name = data.get("routing", {}).get(route)
            if name and name in data.get("profiles", {}):
                return data["profiles"][name]
        name = data.get("routing", {}).get("default")
        if name and name in data.get("profiles", {}):
            return data["profiles"][name]
        active = data.get("active")
        return data.get("profiles", {}).get(active) if active else None


secrets = SecretBox(MASTER_SALT_FILE)
profiles = ProfileStore(MODELS_FILE, MODELS_ENC_FILE, secrets)


# ============================================================
# L0.5: LLM 工厂(profile-aware)
# ============================================================
def make_llm_client(profile_name: str | None = None,
                    profile: dict | None = None,
                    route: str | None = None) -> LLMClient:
    """
    工厂函数。优先级:
        1. 显式传入 profile dict
        2. profile_name → 从 ProfileStore 读取
        3. route → 走 routing 表(命中后会回退 default / active)
        4. AGENT_LLM_PROFILE 环境变量
        5. ProfileStore.active
        6. AGENT_LLM_BACKEND 环境变量(向后兼容,纯环境变量模式)
    """
    if profile is None:
        if profile_name:
            profile = profiles.get(profile_name)
        elif route is not None:
            profile = profiles.resolve_route(route)
        else:
            env_name = os.environ.get("AGENT_LLM_PROFILE")
            if env_name:
                profile = profiles.get(env_name)
            else:
                profile = profiles.active()
    if profile:
        return _client_from_profile(profile)

    # ---- 4:fallback 到旧的环境变量模式 ----
    backend = os.environ.get("AGENT_LLM_BACKEND", "anthropic").lower()
    if backend == "anthropic":
        return AnthropicAdapter(MODEL)
    if backend == "gateway":
        return GatewayAdapter(MODEL)
    if backend == "mock":
        return MockAdapter(MODEL)
    raise ValueError(f"unknown AGENT_LLM_BACKEND: {backend!r}")


def _client_from_profile(p: dict) -> LLMClient:
    """根据 profile 实例化对应适配器。"""
    proto = (p.get("protocol") or "openai").lower()
    model = p.get("model") or MODEL
    if proto == "openai":
        return GatewayAdapter(
            model=model,
            base_url=p.get("base_url"),
            api_key=p.get("api_key"),
        )
    if proto == "anthropic":
        return AnthropicAdapter(
            model=model,
            base_url=p.get("base_url"),
            api_key=p.get("api_key"),
        )
    if proto == "mock":
        return MockAdapter(model=model)
    raise ValueError(f"unknown protocol in profile {p.get('name')!r}: {proto}")


# ============================================================
# L0: Agent Loop Kernel
# ============================================================
def _drain_pending_messages() -> list[str]:
    """收集所有外部信号 → 注入下一轮 user message。"""
    msgs: list[str] = []
    msgs.extend(retry_budget.drain_messages())
    bg = notify_q.drain()
    if bg:
        msgs.append("<background-results>" +
                    json.dumps(bg, ensure_ascii=False)[:3000] + "</background-results>")
    inbox = bus.read_inbox("lead")
    if inbox:
        msgs.append("<inbox>" + json.dumps(inbox, ensure_ascii=False)[:3000] + "</inbox>")
    return msgs


def _exec_tool(name: str, tool_input: dict) -> tuple[bool, Any, dict]:
    decision, intent = permissions.check(name, tool_input)
    if decision == "deny":
        return False, f"DENIED by policy: {name}", intent
    if decision == "ask":
        # 简化:ask 模式下默认拒绝,生产里换成交互式弹窗
        return False, f"ASK required for {name}; auto-denied in non-interactive run", intent
    hooks.emit("tool.before", name=name, input=tool_input, intent=intent)
    try:
        if name.startswith("mcp__"):
            raw = mcp.call(name, tool_input)
        else:
            raw = TOOL_HANDLERS[name](tool_input)
        hooks.emit("tool.after", name=name, intent=intent, ok=True)
        retry_budget.record(name, ok=True)
        return True, raw, intent
    except Exception as e:
        hooks.emit("tool.error", name=name, intent=intent, error=str(e))
        retry_budget.record(name, ok=False)
        return False, f"ERROR: {e}", intent


def agent_loop(user_prompt: str, *, role: str = "lead",
               llm: LLMClient | None = None,
               route: str | None = None) -> list[dict]:
    """主循环:不再直接耦合 Anthropic,统一通过 LLMClient.complete()。
    可选 route 参数 → 经 routing 表自动选 profile(代码任务用 sonnet,文案用 4o,等)。
    """
    llm = llm or make_llm_client(route=route)
    tools = build_tool_schemas()
    sys_prompt = build_system_prompt(role=role)
    messages: list[dict] = [{"role": "user", "content": user_prompt}]

    for i in range(MAX_LOOP_ITERS):
        hooks.emit("loop.iter", iter=i, backend=llm.backend)
        # 在每轮 LLM 调用前注入外部信号
        pending = _drain_pending_messages()
        if pending:
            messages.append({"role": "user", "content": "\n".join(pending)})

        resp = llm.complete(
            system=sys_prompt, tools=tools,
            messages=messages, max_tokens=4096,
        )
        messages.append({"role": "assistant", "content": resp.content})

        if resp.stop_reason != "tool_use":
            break

        results = []
        for block in resp.content:
            if getattr(block, "type", None) != "tool_use":
                continue
            ok, raw, intent = _exec_tool(block.name, block.input)
            results.append({
                "type": "tool_result",
                "tool_use_id": block.id,
                "content": normalize_tool_result(intent, ok, raw)[:8000],
                "is_error": not ok,
            })
        messages.append({"role": "user", "content": results})
    return messages


# ============================================================
# REPL 入口
# ============================================================
def main():
    print(f"== mega_agent ==")
    print(f"WORKDIR   = {WORKDIR}")
    print(f"REPO_ROOT = {REPO_ROOT}")
    print(f"tools     = {len(TOOL_HANDLERS)} native + {len(mcp.get_agent_tools())} mcp")
    print(f"secrets   = {'ENABLED (Fernet/PBKDF2)' if secrets.enabled else 'DISABLED (plaintext)'}")
    act = profiles.active()
    print(f"profile   = {act['name'] + ' (' + act['model'] + ')' if act else '(none — use /add-profile or env)'}")
    rt = profiles.routing()
    if rt:
        print(f"routing   = {', '.join(f'{k}→{v}' for k,v in rt.items())}")
    print(f"models    = {MODELS_ENC_FILE if secrets.enabled else MODELS_FILE}")
    print("commands  : /profiles  /use <name>  /add-profile  /rm-profile <name>")
    print("            /routes  /route <name> <profile>  /rm-route <name>")
    print("            /perm <auto|strict>  /quit")
    print("syntax    : '@<route> <prompt>'  → run with routed model")
    cron.start()
    llm: LLMClient | None = None
    while True:
        try:
            line = input("you> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break
        if not line:
            continue
        if line in ("/quit", "/exit"):
            break

        # ---- profile 管理 ----
        if line == "/profiles":
            for p in profiles.list():
                star = "*" if profiles.active() and profiles.active()["name"] == p["name"] else " "
                print(f" {star} {p['name']:20s} {p['protocol']:10s} {p['model']:30s} key={p['api_key']}")
            if not profiles.list():
                print("  (empty) — use /add-profile to create one")
            continue
        if line.startswith("/use "):
            name = line.split(maxsplit=1)[1].strip()
            try:
                p = profiles.use(name)
                llm = None
                print(f"active profile = {p['name']} ({p['protocol']} / {p['model']})")
            except KeyError:
                print(f"no such profile: {name!r}")
            continue
        if line == "/add-profile":
            try:
                name     = input("  name      : ").strip()
                protocol = input("  protocol  [anthropic|openai|mock]: ").strip() or "openai"
                model    = input("  model     : ").strip()
                base_url = input("  base_url  (empty=default): ").strip() or None
                api_key  = input("  api_key   : ").strip()
                profiles.upsert(name=name, protocol=protocol, model=model,
                                base_url=base_url, api_key=api_key)
                llm = None
                print(f"saved → {MODELS_ENC_FILE if secrets.enabled else MODELS_FILE}")
            except Exception as e:
                print(f"[add-profile failed] {e}")
            continue
        if line.startswith("/rm-profile "):
            name = line.split(maxsplit=1)[1].strip()
            print("removed" if profiles.remove(name) else "not found")
            llm = None
            continue

        # ---- routing 管理 ----
        if line == "/routes":
            rt = profiles.routing()
            if not rt:
                print("  (empty) — e.g. /route code claude   /route default gpt4o")
            for k, v in rt.items():
                print(f"  {k:15s} → {v}")
            continue
        if line.startswith("/route "):
            try:
                _, k, v = line.split(maxsplit=2)
                profiles.set_route(k, v)
                print(f"route {k} → {v}")
            except (ValueError, KeyError) as e:
                print(f"[route failed] {e}")
            continue
        if line.startswith("/rm-route "):
            k = line.split(maxsplit=1)[1].strip()
            print("removed" if profiles.del_route(k) else "not found")
            continue

        # ---- 旧命令 ----
        if line.startswith("/perm "):
            permissions.mode = line.split(maxsplit=1)[1]
            print(f"perm mode = {permissions.mode}")
            continue
        if line.startswith("/backend "):
            os.environ["AGENT_LLM_BACKEND"] = line.split(maxsplit=1)[1]
            llm = None
            print(f"llm backend = {os.environ['AGENT_LLM_BACKEND']}")
            continue

        # ---- 推理(支持 @route 前缀) ----
        route = None
        prompt = line
        if line.startswith("@"):
            head, _, rest = line[1:].partition(" ")
            route, prompt = head.strip(), rest.strip()
        try:
            # 走 route 时每轮都重建 client(不同 route 对应不同 profile)
            current = make_llm_client(route=route) if route else (llm or make_llm_client())
            if not route:
                llm = current
            print(f"[llm] {current.backend} / {getattr(current,'model','?')}"
                  + (f" via route '{route}'" if route else ""))
            msgs = agent_loop(prompt, llm=current)
            last = msgs[-1]["content"]
            if isinstance(last, list):
                for b in last:
                    if getattr(b, "type", None) == "text":
                        print("agent>", getattr(b, "text", ""))
            else:
                print("agent>", last)
        except Exception as e:
            print(f"[mega_agent error] {e}")


if __name__ == "__main__":
    main()
