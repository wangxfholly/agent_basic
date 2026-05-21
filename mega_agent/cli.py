"""
mega_agent.cli
==============

Reference REPL. Shows how to wire all layers together. Customise or
replace this with your own driver (HTTP server, queue worker, etc.)
when integrating into a product — the rest of the package never imports
anything from here.
"""
from __future__ import annotations

import os
import signal
import sys
import threading

from .background import cron
from .config import MODELS_ENC_FILE, MODELS_FILE, REPO_ROOT, WORKDIR
from .kernel import agent_loop
from .llm import LLMClient, make_llm_client
from .mcp import mcp
from .permissions import permissions
from .profiles import profiles, secrets
from .skill_market import (
    install as install_skill,
    list_installed as list_installed_skills,
    remove as remove_skill,
    sync as sync_skills,
    verify_installed as verify_installed_skill,
)
from .skills import skills
from .tools import TOOL_HANDLERS


def main():
    print(f"== mega_agent ==")
    print(f"WORKDIR   = {WORKDIR}")
    print(f"REPO_ROOT = {REPO_ROOT}")
    print(f"tools     = {len(TOOL_HANDLERS)} native + {len(mcp.get_agent_tools())} mcp")
    print(f"secrets   = {'ENABLED (Fernet/PBKDF2)' if secrets.enabled else 'DISABLED (plaintext)'}")
    act = profiles.active()
    print(f"profile   = "
          + (f"{act['name']} ({act['model']})" if act else "(none — use /add-profile or env)"))
    rt = profiles.routing()
    if rt:
        print(f"routing   = {', '.join(f'{k}→{v}' for k, v in rt.items())}")
    print(f"models    = {MODELS_ENC_FILE if secrets.enabled else MODELS_FILE}")
    print("commands  : /profiles  /use <name>  /add-profile  /rm-profile <name>")
    print("            /routes  /route <name> <profile>  /rm-route <name>")
    print("            /perm <auto|strict>  /backend <anthropic|gateway|mock>  /quit")
    print("skills    : /skills  /install <src> [sha256]  /remove <name>[@version]")
    print("            /pin <name> [version]  /verify [name]  /sync")
    print("streaming : /stream <on|off>  (Ctrl-C during a turn cancels it)")
    print("syntax    : '@<route> <prompt>'  → run with routed model")

    cron.start()
    llm: LLMClient | None = None
    streaming = True   # default-on for interactive REPL

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

        # ---- profile management ----
        if line == "/profiles":
            for p in profiles.list():
                star = "*" if profiles.active() and profiles.active()["name"] == p["name"] else " "
                print(f" {star} {p['name']:20s} {p['protocol']:10s} "
                      f"{p['model']:30s} key={p['api_key']}")
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
                name = input("  name      : ").strip()
                protocol = input("  protocol  [anthropic|openai|mock]: ").strip() or "openai"
                model = input("  model     : ").strip()
                base_url = input("  base_url  (empty=default): ").strip() or None
                api_key = input("  api_key   : ").strip()
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

        # ---- routing ----
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

        # ---- runtime knobs ----
        if line.startswith("/perm "):
            permissions.mode = line.split(maxsplit=1)[1]
            print(f"perm mode = {permissions.mode}")
            continue
        if line.startswith("/backend "):
            os.environ["AGENT_LLM_BACKEND"] = line.split(maxsplit=1)[1]
            llm = None
            print(f"llm backend = {os.environ['AGENT_LLM_BACKEND']}")
            continue

        # ---- skills marketplace ----
        if line == "/skills":
            cat = skills.catalog()
            pins = skills._load_pins()
            if not cat:
                print("  (no skills discovered) — install one with /install <src>")
            for s in cat:
                pinned = f" [pinned→{pins[s['name']]}]" if s["name"] in pins else ""
                auto = " [auto]" if s.get("auto_load") else ""
                print(f"  {s['name']:24s} v{s.get('version','?'):10s} "
                      f"{s.get('description','')[:60]}{pinned}{auto}")
            continue
        if line.startswith("/install "):
            parts = line.split(maxsplit=2)
            src = parts[1]
            sha = parts[2].strip() if len(parts) > 2 else None
            try:
                rec = install_skill(src, sha256=sha)
                print(f"installed {rec['name']}@{rec['version']} "
                      f"(tree_sha={rec.get('tree_sha256','?')[:10]}…)")
            except Exception as e:
                print(f"[install failed] {e}")
            continue
        if line.startswith("/remove "):
            arg = line.split(maxsplit=1)[1].strip()
            name, _, version = arg.partition("@")
            try:
                res = remove_skill(name, version=version or None)
                print(f"removed: {res['removed'] or '(nothing matched)'}")
            except Exception as e:
                print(f"[remove failed] {e}")
            continue
        if line.startswith("/pin "):
            parts = line.split(maxsplit=2)
            name = parts[1]
            version = parts[2].strip() if len(parts) > 2 else None
            try:
                pins = skills.pin(name, version)
                if name in pins:
                    print(f"pin: {name} → v{pins[name]}")
                else:
                    print(f"pin: {name} → (unpinned)")
            except Exception as e:
                print(f"[pin failed] {e}")
            continue
        if line == "/verify" or line.startswith("/verify "):
            name = (line.split(maxsplit=1)[1].strip()
                    if line.startswith("/verify ") else None)
            try:
                res = verify_installed_skill(name)
                if not res:
                    print("  (nothing installed)")
                for n, info in res.items():
                    mark = "✓" if info.get("ok") else "✗"
                    print(f"  {mark} {n}: {info}")
            except Exception as e:
                print(f"[verify failed] {e}")
            continue
        if line == "/sync":
            try:
                res = sync_skills()
                print(f"  installed: {res['installed']}")
                print(f"  skipped  : {res['skipped']}")
                if res["errors"]:
                    print(f"  errors   : {res['errors']}")
            except Exception as e:
                print(f"[sync failed] {e}")
            continue
        if line.startswith("/stream"):
            arg = (line.split(maxsplit=1)[1].strip().lower()
                   if " " in line else "")
            if arg in ("on", "off"):
                streaming = (arg == "on")
            print(f"streaming = {'on' if streaming else 'off'}")
            continue

        # ---- inference (optional `@route` prefix) ----
        route = None
        prompt = line
        if line.startswith("@"):
            head, _, rest = line[1:].partition(" ")
            route, prompt = head.strip(), rest.strip()
        try:
            current = (make_llm_client(route=route) if route
                       else (llm or make_llm_client()))
            if not route:
                llm = current
            print(f"[llm] {current.backend} / {getattr(current, 'model', '?')}"
                  + (f" via route '{route}'" if route else "")
                  + (" [stream]" if streaming else ""))

            cancel_event = threading.Event()

            def _on_sigint(signum, frame):
                cancel_event.set()
                # Give the user immediate feedback; do NOT raise — let the
                # kernel finish its current step and unwind cleanly.
                sys.stderr.write("\n[Ctrl-C: cancelling current turn…]\n")
                sys.stderr.flush()

            prev_handler = signal.getsignal(signal.SIGINT)
            try:
                signal.signal(signal.SIGINT, _on_sigint)
                if streaming:
                    sys.stdout.write("agent> ")
                    sys.stdout.flush()
                    def _emit(chunk: str):
                        sys.stdout.write(chunk)
                        sys.stdout.flush()
                    msgs = agent_loop(prompt, llm=current,
                                      on_text=_emit,
                                      cancel_event=cancel_event)
                    sys.stdout.write("\n")
                else:
                    msgs = agent_loop(prompt, llm=current,
                                      cancel_event=cancel_event)
                    last = msgs[-1]["content"]
                    if isinstance(last, list):
                        for b in last:
                            if getattr(b, "type", None) == "text":
                                print("agent>", getattr(b, "text", ""))
                    else:
                        print("agent>", last)
            finally:
                signal.signal(signal.SIGINT, prev_handler)
        except Exception as e:
            print(f"[mega_agent error] {e}")
