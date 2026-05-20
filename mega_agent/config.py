"""
mega_agent.config
=================

Global path layout, environment variables and shared regex.
All other modules import from here — never hard-code paths or env vars.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

# ---- Tunables (env-driven) ----
WORKDIR        = Path(os.environ.get("AGENT_WORKDIR", ".")).resolve()
MODEL          = os.environ.get("AGENT_MODEL", "claude-sonnet-4-20250514")
MAX_LOOP_ITERS = int(os.environ.get("AGENT_MAX_ITERS", "50"))


def detect_repo_root() -> Path:
    """git rev-parse first; fall back to WORKDIR."""
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

# ---- Per-layer state directories (all relative to WORKDIR, git-friendly) ----
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
MEMORY_DIR       = WORKDIR / ".memory"
MEMORY_FACTS     = MEMORY_DIR / "facts.jsonl"
MEMORY_KV        = MEMORY_DIR / "kv.json"
MEMORY_VECTORS   = MEMORY_DIR / "vectors"
HOOKS_FILE       = WORKDIR / ".hooks.json"
MODELS_FILE      = WORKDIR / "models.json"
MODELS_ENC_FILE  = WORKDIR / "models.json.enc"
MASTER_SALT_FILE = WORKDIR / ".master-key.salt"

NAME_RE = re.compile(r"[A-Za-z0-9._-]{1,40}")


def ensure_dirs() -> None:
    for d in [TASKS_DIR, WORKTREE_ROOT, RUNTIME_TASKS, CRON_DIR,
              TEAM_DIR, INBOX_DIR, REQUESTS_DIR,
              MEMORY_DIR, MEMORY_VECTORS]:
        d.mkdir(parents=True, exist_ok=True)
    if not WORKTREE_INDEX.exists():
        WORKTREE_INDEX.write_text("{}", encoding="utf-8")


ensure_dirs()
