"""
08_skill_market.py — install / verify / pin / remove skills end-to-end.

This demo does NOT require network access. We seed two fake "remote"
skills as local directories, install them through the marketplace
pipeline, exercise multi-version + pin, run integrity verification, and
sync from the lockfile to a fresh MEGA_HOME to prove reproducibility.

Run:
    python examples/08_skill_market.py

What you should see:
    - Both skills land in ~/.mega/skills/<name>@<version>/
    - The lockfile records sha256 + source + installed_at
    - Pin selects an older version even though a newer one is installed
    - Verify reports OK; corrupting a file flips it to mismatch
    - Removing a single version leaves others intact
"""
import json
import os
import shutil
import sys
import tempfile
from pathlib import Path

# Force a throw-away ~/.mega for this demo so it doesn't pollute the user's.
SANDBOX_HOME = Path(tempfile.mkdtemp(prefix="mega-demo-home-"))
os.environ["HOME"] = str(SANDBOX_HOME)
Path(SANDBOX_HOME, ".mega").mkdir(parents=True, exist_ok=True)

# Force the agent's WORKDIR to a clean dir as well.
os.environ["AGENT_WORKDIR"] = tempfile.mkdtemp(prefix="mega-demo-work-")

# Reload Path.home to pick up the new HOME inside this process.
Path.home.cache_clear() if hasattr(Path.home, "cache_clear") else None

import importlib  # noqa: E402

# `from .skills import skills` in mega_agent/__init__.py shadows the
# submodule with the singleton; grab the actual module objects directly.
_skills_mod  = importlib.import_module("mega_agent.skills")
_market_mod  = importlib.import_module("mega_agent.skill_market")
importlib.reload(_skills_mod)
importlib.reload(_market_mod)
skills = _skills_mod.skills
skill_market = _market_mod


def make_fake_remote(version: str, body: str) -> Path:
    """Create a temp dir that looks like a published skill source."""
    d = Path(tempfile.mkdtemp(prefix=f"fake-skill-{version}-"))
    (d / "SKILL.md").write_text(
        f"---\n"
        f"name: greeter\n"
        f"description: Says hi (v{version}).\n"
        f"version: {version}\n"
        f"allowed_tools:\n"
        f"  - bash\n"
        f"---\n\n"
        f"{body}\n",
        encoding="utf-8",
    )
    (d / "scripts").mkdir()
    (d / "scripts" / "hello.sh").write_text(
        f'#!/bin/sh\necho "hello from greeter v{version}"\n', encoding="utf-8")
    return d


def main():
    print(f"sandbox HOME = {SANDBOX_HOME}")
    print(f"install root = {skill_market.INSTALL_ROOT}\n")

    src_v1 = make_fake_remote("0.1.0", "v1 body — used as fallback")
    src_v2 = make_fake_remote("0.2.0", "v2 body — newer, default")

    print("# 1. install two versions of the same skill")
    rec1 = skill_market.install(str(src_v1))
    rec2 = skill_market.install(str(src_v2))
    print("  v1:", rec1["name"], rec1["version"], "→", rec1.get("tree_sha256")[:10])
    print("  v2:", rec2["name"], rec2["version"], "→", rec2.get("tree_sha256")[:10])

    print("\n# 2. registry picks the highest SemVer by default")
    skills.refresh()
    cat = {s["name"]: s for s in skills.catalog()}
    print("  active 'greeter' →", cat["greeter"]["version"])
    print("  all installed   →", skills.all_versions("greeter"))

    print("\n# 3. pin to v0.1.0")
    skills.pin("greeter", "0.1.0")
    cat = {s["name"]: s for s in skills.catalog()}
    print("  active 'greeter' (after pin) →", cat["greeter"]["version"])
    skills.pin("greeter", None)  # unpin

    print("\n# 4. lockfile contents")
    lock = skill_market.list_installed()
    print(" ", json.dumps(lock["skills"], indent=2, ensure_ascii=False)[:400], "...")

    print("\n# 5. verify integrity (clean)")
    print(" ", skill_market.verify_installed("greeter"))

    print("\n# 6. tamper with v2 then verify again")
    target = skill_market.INSTALL_ROOT / "greeter@0.2.0" / "scripts" / "hello.sh"
    target.write_text("# tampered\n", encoding="utf-8")
    print(" ", skill_market.verify_installed("greeter"))

    print("\n# 7. remove v0.1.0 only — v0.2.0 stays")
    print(" ", skill_market.remove("greeter", version="0.1.0"))
    print("  remaining versions:", skills.all_versions("greeter"))

    print("\n# 8. sync from lockfile into a fresh HOME (reproducibility)")
    fresh_home = Path(tempfile.mkdtemp(prefix="mega-fresh-"))
    fresh_lock = fresh_home / ".mega" / "skills.lock.json"
    fresh_lock.parent.mkdir(parents=True)
    shutil.copy(skill_market.LOCK_FILE, fresh_lock)
    # swap HOME briefly
    saved = os.environ["HOME"]
    os.environ["HOME"] = str(fresh_home)
    importlib.reload(_market_mod)
    print(" ", _market_mod.sync())
    os.environ["HOME"] = saved
    importlib.reload(_market_mod)

    print("\nOK ✓")


if __name__ == "__main__":
    main()
