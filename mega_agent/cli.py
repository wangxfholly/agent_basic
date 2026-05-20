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

from .background import cron
from .config import MODELS_ENC_FILE, MODELS_FILE, REPO_ROOT, WORKDIR
from .kernel import agent_loop
from .llm import LLMClient, make_llm_client
from .mcp import mcp
from .permissions import permissions
from .profiles import profiles, secrets
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
