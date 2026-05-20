"""
mega_agent.skills
=================

Skills = on-demand capability bundles, compatible with the Claude Code /
Anthropic Skills file layout.

Each skill is a directory:

    skills/<name>/
        SKILL.md           required — YAML frontmatter + markdown body
        scripts/           optional — companion scripts (python/bash/...)
        resources/         optional — templates, datasets, prompts

SKILL.md frontmatter (YAML, between two `---` lines)::

    ---
    name: csv-analyst
    description: One-line summary the LLM sees in the catalog.
    allowed_tools:        # optional — auto-allowlisted while the skill is active
      - bash
      - read_file
    auto_load: false      # optional — eagerly inject body into system prompt
    ---

The body is plain markdown — instructions, examples, anything that should
land in the system prompt **only when the skill is loaded**.

Loading strategy
----------------
By default we follow the "catalog + on-demand" pattern: the system prompt
gets a one-line index (name + description), and the LLM calls
`load_skill(name)` to pull the body. This keeps token usage flat as the
skill library grows. Skills with `auto_load: true` skip the dance.

This module is stdlib-only. YAML frontmatter is parsed by a tiny
hand-rolled scanner (we only support `key: value` and flat lists, which
covers every skill we ship).
"""
from __future__ import annotations

import re
import subprocess
import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

from .config import WORKDIR
from .events import events


# ─────────────────────────────────────────────────────────────────────────────
# Search path — first match wins.
# ─────────────────────────────────────────────────────────────────────────────
DEFAULT_SKILL_DIRS: list[Path] = [
    WORKDIR / "skills",                 # project-local
    Path.home() / ".mega" / "skills",   # user-level
]

NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")


# ─────────────────────────────────────────────────────────────────────────────
# Manifest dataclass.
# ─────────────────────────────────────────────────────────────────────────────
@dataclass
class SkillManifest:
    name: str
    description: str
    body: str
    dir: Path
    allowed_tools: list[str] = field(default_factory=list)
    auto_load: bool = False

    def to_catalog_entry(self) -> dict:
        return {
            "name": self.name,
            "description": self.description,
            "auto_load": self.auto_load,
            "allowed_tools": list(self.allowed_tools),
            "dir": str(self.dir),
        }

    def list_resources(self) -> dict:
        out: dict[str, list[str]] = {"scripts": [], "resources": []}
        for sub in ("scripts", "resources"):
            d = self.dir / sub
            if d.is_dir():
                out[sub] = sorted(
                    str(p.relative_to(self.dir))
                    for p in d.rglob("*") if p.is_file()
                )
        return out


# ─────────────────────────────────────────────────────────────────────────────
# Tiny YAML frontmatter parser. Supports `key: value` and `key:\n  - item`.
# ─────────────────────────────────────────────────────────────────────────────
def _parse_frontmatter(text: str) -> tuple[dict, str]:
    """Return (meta, body). If no frontmatter, returns ({}, text)."""
    if not text.startswith("---"):
        return {}, text
    end = text.find("\n---", 3)
    if end == -1:
        return {}, text
    raw = text[3:end].strip("\n")
    body_start = text.find("\n", end + 4)
    body = text[body_start + 1:] if body_start != -1 else ""

    meta: dict = {}
    current_key: str | None = None
    for raw_line in raw.splitlines():
        line = raw_line.rstrip()
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line.startswith(("  - ", "\t- ")):
            if current_key is None:
                continue
            meta.setdefault(current_key, [])
            if isinstance(meta[current_key], list):
                meta[current_key].append(line.split("- ", 1)[1].strip())
            continue
        if ":" in line:
            key, _, val = line.partition(":")
            key = key.strip()
            val = val.strip()
            if val == "":
                meta[key] = []
                current_key = key
            else:
                # strip optional surrounding quotes
                if (val.startswith('"') and val.endswith('"')) or \
                   (val.startswith("'") and val.endswith("'")):
                    val = val[1:-1]
                # primitive coerce for booleans / ints
                low = val.lower()
                if low in ("true", "false"):
                    meta[key] = (low == "true")
                else:
                    try:
                        meta[key] = int(val)
                    except ValueError:
                        meta[key] = val
                current_key = key
    return meta, body


# ─────────────────────────────────────────────────────────────────────────────
# Registry.
# ─────────────────────────────────────────────────────────────────────────────
class SkillRegistry:
    """Discovers, validates, and serves skill manifests."""

    def __init__(self, search_dirs: Iterable[Path] | None = None):
        self.search_dirs = [Path(p) for p in (search_dirs or DEFAULT_SKILL_DIRS)]
        self._lock = threading.Lock()
        self._skills: dict[str, SkillManifest] = {}
        self._loaded: dict[str, str] = {}   # name → body (active in this run)
        self.refresh()

    # ---- discovery ----
    def refresh(self) -> int:
        """Re-scan all search dirs. Later dirs do NOT override earlier names."""
        found: dict[str, SkillManifest] = {}
        for root in self.search_dirs:
            if not root.is_dir():
                continue
            for child in sorted(root.iterdir()):
                if not child.is_dir():
                    continue
                manifest = self._load_manifest(child)
                if manifest and manifest.name not in found:
                    found[manifest.name] = manifest
        with self._lock:
            self._skills = found
        events.emit("skills.refresh", count=len(found),
                    dirs=[str(p) for p in self.search_dirs])
        return len(found)

    def _load_manifest(self, skill_dir: Path) -> SkillManifest | None:
        md = skill_dir / "SKILL.md"
        if not md.is_file():
            return None
        try:
            text = md.read_text(encoding="utf-8")
            meta, body = _parse_frontmatter(text)
            name = str(meta.get("name") or skill_dir.name)
            if not NAME_RE.match(name):
                events.emit("skills.skip", reason="bad_name",
                            dir=str(skill_dir), name=name)
                return None
            desc = str(meta.get("description") or "").strip()
            allowed = meta.get("allowed_tools") or []
            if isinstance(allowed, str):
                allowed = [allowed]
            return SkillManifest(
                name=name,
                description=desc,
                body=body.strip(),
                dir=skill_dir.resolve(),
                allowed_tools=list(allowed),
                auto_load=bool(meta.get("auto_load", False)),
            )
        except Exception as e:
            events.emit("skills.skip", reason="parse_error",
                        dir=str(skill_dir), error=str(e))
            return None

    # ---- catalog (cheap, for system prompt) ----
    def catalog(self) -> list[dict]:
        with self._lock:
            return [m.to_catalog_entry() for m in self._skills.values()]

    def names(self) -> list[str]:
        with self._lock:
            return sorted(self._skills)

    def get(self, name: str) -> SkillManifest | None:
        with self._lock:
            return self._skills.get(name)

    # ---- on-demand loading ----
    def load(self, name: str) -> dict:
        """Mark skill body as 'active for this run' and return it."""
        m = self.get(name)
        if not m:
            raise KeyError(f"skill not found: {name}")
        with self._lock:
            self._loaded[name] = m.body
        events.emit("skills.load", name=name)
        # auto-allowlist declared tools at load time
        if m.allowed_tools:
            try:
                from .permissions import permissions
                for t in m.allowed_tools:
                    permissions.remember(t, "allow")
            except Exception:
                pass
        return {
            "name": m.name,
            "description": m.description,
            "body": m.body,
            "allowed_tools": list(m.allowed_tools),
            "resources": m.list_resources(),
        }

    def loaded_bodies(self) -> dict[str, str]:
        with self._lock:
            return dict(self._loaded)

    def auto_load_bodies(self) -> dict[str, str]:
        """Bodies for every skill marked auto_load: true (idempotent)."""
        out: dict[str, str] = {}
        with self._lock:
            for n, m in self._skills.items():
                if m.auto_load:
                    out[n] = m.body
                    self._loaded.setdefault(n, m.body)
        return out

    def unload(self, name: str) -> bool:
        with self._lock:
            existed = name in self._loaded
            self._loaded.pop(name, None)
        if existed:
            events.emit("skills.unload", name=name)
        return existed

    # ---- script execution ----
    def run_script(self, name: str, script: str,
                   args: list[str] | None = None,
                   timeout: int = 60) -> dict:
        m = self.get(name)
        if not m:
            raise KeyError(f"skill not found: {name}")
        # path traversal guard — script must live under <skill>/scripts/
        scripts_dir = (m.dir / "scripts").resolve()
        target = (scripts_dir / script).resolve()
        if not str(target).startswith(str(scripts_dir) + "/") and target != scripts_dir:
            raise PermissionError(f"script escapes skill dir: {script}")
        if not target.is_file():
            raise FileNotFoundError(str(target))

        # interpret by extension
        suffix = target.suffix.lower()
        if suffix == ".py":
            cmd = ["python3", str(target), *(args or [])]
        elif suffix in (".sh", ".bash", ""):
            cmd = ["bash", str(target), *(args or [])]
        else:
            cmd = [str(target), *(args or [])]

        proc = subprocess.run(
            cmd, capture_output=True, text=True,
            cwd=m.dir, timeout=timeout,
        )
        events.emit("skills.run_script", name=name, script=script,
                    rc=proc.returncode)
        return {
            "rc": proc.returncode,
            "stdout": proc.stdout[-4000:],
            "stderr": proc.stderr[-1000:],
        }


# ─────────────────────────────────────────────────────────────────────────────
# System-prompt rendering.
# ─────────────────────────────────────────────────────────────────────────────
def render_skill_catalog_block(reg: "SkillRegistry") -> str:
    """One-line-per-skill catalog. Cheap; lives in every system prompt."""
    cat = reg.catalog()
    if not cat:
        return ""
    lines = ["# Available skills (call `load_skill(name)` to activate)"]
    for s in cat:
        marker = " *(auto-loaded)*" if s["auto_load"] else ""
        lines.append(f"- `{s['name']}` — {s['description']}{marker}")
    return "\n".join(lines)


def render_active_skills_block(reg: "SkillRegistry") -> str:
    """Bodies of every currently-loaded skill (auto + on-demand)."""
    reg.auto_load_bodies()  # ensure auto-load are in the active set
    bodies = reg.loaded_bodies()
    if not bodies:
        return ""
    chunks = []
    for name, body in bodies.items():
        chunks.append(f"# Skill: {name}\n{body}")
    return "\n\n".join(chunks)


# Default singleton.
skills = SkillRegistry()
