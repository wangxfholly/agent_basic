"""
mega_agent.tools
================

Native tool implementations exposed to the LLM. Each handler takes a
single `dict` (already JSON-decoded by the kernel) and returns a JSON-
serialisable result. Errors are *raised*; the kernel catches them and
turns them into `is_error=True` tool_results.
"""
from __future__ import annotations

import json
import subprocess
from typing import Any, Callable

from .background import background, cron
from .config import WORKDIR
from .events import events
from .mcp import mcp
from .memory import memory
from .skills import skills
from .tasks import tasks
from .teams import bus, team
from .worktree import worktrees


# ---- normalised tool result envelope ----
def normalize_tool_result(intent: dict, ok: bool, raw: Any) -> str:
    preview = (json.dumps(raw, ensure_ascii=False)[:1500]
               if not isinstance(raw, str) else raw[:1500])
    return json.dumps({
        "source": intent["source"], "server": intent["server"],
        "tool": intent["tool"], "risk": intent["risk"],
        "status": "ok" if ok else "error", "preview": preview,
    }, ensure_ascii=False)


# ---- base ----
def t_bash(inp):
    proc = subprocess.run(inp["command"], shell=True, cwd=WORKDIR,
                          capture_output=True, text=True,
                          timeout=inp.get("timeout", 60))
    return {"rc": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-1000:]}


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


# ---- task graph ----
def t_create_task(inp):
    return tasks.create(inp["title"], role=inp.get("role"),
                        blocked_by=inp.get("blockedBy"),
                        owner=inp.get("owner"))


def t_list_tasks(_):           return {"tasks": tasks.list()}
def t_update_task_status(inp): return tasks.update_status(inp["id"], inp["status"])
def t_get_task(inp):           return tasks.get(inp["id"])
def t_link_tasks(inp):         return tasks.link(inp["id"], inp["blockedBy"])


# ---- worktree ----
def t_create_worktree(inp):
    return worktrees.create(inp["name"],
                            task_id=inp.get("task_id"),
                            base=inp.get("base", "HEAD"))


def t_list_worktrees(_):     return {"worktrees": worktrees.list()}
def t_worktree_status(inp):  return worktrees.status(inp["name"])


def t_enter_worktree(inp):
    rec = worktrees._load().get(inp["name"])
    if not rec:
        raise KeyError(inp["name"])
    return {"path": rec["path"], "branch": rec["branch"]}


def t_run_in_worktree(inp):
    return worktrees.run(inp["name"], inp["command"],
                         timeout=inp.get("timeout", 120))


def t_keep_worktree(inp):    return worktrees.keep(inp["name"], note=inp.get("note"))


def t_remove_worktree(inp):
    return worktrees.remove(inp["name"], force=inp.get("force", False),
                            complete_task=inp.get("complete_task", False))


def t_worktree_closeout(inp):
    return worktrees.closeout(
        inp["name"], inp["action"],
        **{k: v for k, v in inp.items() if k not in ("name", "action")})


def t_recent_events(inp):
    return {"events": events.list_recent(limit=inp.get("limit", 20))}


# ---- background / cron ----
def t_background_run(inp):
    return background.run(inp["command"], label=inp.get("label"),
                          timeout=inp.get("timeout", 300))


def t_background_status(inp):
    return background.status(inp["id"]) or {"error": "not found"}


def t_cron_register(inp):
    return cron.register(inp["name"], inp["expr"], inp["command"])


def t_cron_list(_):  return {"jobs": cron.list()}
def t_cron_start(_): cron.start(); return {"status": "started"}


# ---- team ----
def t_spawn(inp):
    return team.spawn(inp["name"], inp["role"],
                      autonomous=inp.get("autonomous", False))


def t_list_teammates(_):  return {"members": team.list()}


def t_send_message(inp):
    return bus.send(inp.get("sender", "lead"),
                    inp["to"], inp["type"], inp.get("body", {}))


def t_read_my_inbox(inp):
    return {"messages": bus.read_inbox(inp.get("who", "lead"))}


def t_shutdown_teammate(inp):
    team.shutdown(inp["name"])
    bus.send("lead", inp["name"], "shutdown_request",
             {"reason": inp.get("reason", "")})
    return {"shutdown": inp["name"]}


def t_idle(_):  return {"status": "idle"}


# ---- memory ----
def t_remember(inp):
    return memory.remember(
        content=inp["content"],
        kind=inp.get("kind", "fact"),
        user_id=inp.get("user_id"),
        **(inp.get("tags") or {}),
    )


def t_recall(inp):
    return {"hits": memory.recall(
        query=inp["query"], k=inp.get("k", 5),
        user_id=inp.get("user_id"))}


def t_forget(inp):
    return {"removed": memory.forget(inp["fact_id"])}


def t_list_memory(inp):
    return {"facts": memory.list_facts(
        user_id=inp.get("user_id"), limit=inp.get("limit", 100))}


def t_set_pref(inp):
    memory.set(inp["key"], inp["value"]); return {"ok": True}


def t_get_pref(inp):
    return {"value": memory.get(inp["key"])}


def t_forget_user(inp):
    return {"purged": memory.forget_user(inp["user_id"])}


# ---- skills ----
def t_list_skills(_):
    return {"skills": skills.catalog()}


def t_load_skill(inp):
    return skills.load(inp["name"])


def t_unload_skill(inp):
    return {"unloaded": skills.unload(inp["name"])}


def t_run_skill_script(inp):
    return skills.run_script(
        inp["name"], inp["script"],
        args=inp.get("args"),
        timeout=inp.get("timeout", 60),
    )


def t_refresh_skills(_):
    return {"count": skills.refresh()}


# ---- registry ----
TOOL_HANDLERS: dict[str, Callable] = {
    # base
    "bash": t_bash, "read_file": t_read, "write_file": t_write, "edit_file": t_edit,
    # tasks
    "create_task": t_create_task, "list_tasks": t_list_tasks,
    "update_task_status": t_update_task_status,
    "get_task": t_get_task, "link_tasks": t_link_tasks,
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
    # memory
    "remember": t_remember, "recall": t_recall, "forget": t_forget,
    "list_memory": t_list_memory,
    "set_pref": t_set_pref, "get_pref": t_get_pref,
    "forget_user": t_forget_user,
    # skills
    "list_skills": t_list_skills, "load_skill": t_load_skill,
    "unload_skill": t_unload_skill, "run_skill_script": t_run_skill_script,
    "refresh_skills": t_refresh_skills,
}


def build_tool_schemas() -> list[dict]:
    """Native tools first, then non-conflicting MCP tools."""
    base = []
    for name in TOOL_HANDLERS:
        base.append({
            "name": name,
            "description": f"native tool: {name}",
            "input_schema": {"type": "object", "additionalProperties": True},
        })
    native_names = {t["name"] for t in base}
    for t in mcp.get_agent_tools():
        if t["name"] not in native_names:
            base.append(t)
    return base
