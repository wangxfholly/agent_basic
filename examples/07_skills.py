"""
07_skills.py — discover, load, and execute skills end-to-end.

Skills follow the Claude Code / Anthropic Skills file layout:

    skills/<name>/
        SKILL.md         (YAML frontmatter + markdown body)
        scripts/         (optional companion scripts)
        resources/       (optional templates/data)

By default the system prompt only sees a one-line catalog (cheap).
The LLM activates a skill on demand via `load_skill(name)`.

Run:
    python examples/07_skills.py
"""
import csv
import json
import tempfile
from pathlib import Path

from mega_agent import (
    LLMResponse,
    MockAdapter,
    TextBlock,
    ToolUseBlock,
    agent_loop,
    build_system_prompt,
    skills,
)


def demo_discovery():
    print("\n# 1. discovery")
    skills.refresh()
    print("  catalog:", json.dumps(skills.catalog(), ensure_ascii=False, indent=2))


def demo_catalog_in_prompt():
    print("\n# 2. system prompt carries a one-line catalog")
    sp = build_system_prompt(role="lead", recall_query="profile a csv")
    block = "\n".join(
        line for line in sp.splitlines()
        if "Available skills" in line or line.startswith("- `")
    )
    print(block)


def demo_load_and_run():
    print("\n# 3. load skill + run bundled script")
    info = skills.load("csv-analyst")
    print("  body preview:", info["body"].splitlines()[0])
    print("  resources:", info["resources"])

    # write a tiny CSV and profile it
    with tempfile.NamedTemporaryFile("w", suffix=".csv",
                                     delete=False, newline="") as f:
        w = csv.writer(f)
        w.writerow(["id", "city", "score"])
        w.writerow([1, "Beijing", 88])
        w.writerow([2, "Shanghai", 91])
        w.writerow([3, "Beijing", 77])
        w.writerow([4, "", 65])
        path = f.name

    res = skills.run_script("csv-analyst", "profile.py", args=[path])
    print("  rc:", res["rc"])
    print("  stdout:", res["stdout"].strip())
    Path(path).unlink()


def demo_kernel_loads_skill():
    print("\n# 4. kernel: LLM calls load_skill via tool_use")
    MockAdapter.reset()
    MockAdapter.queue_response(LLMResponse(
        content=[ToolUseBlock(id="s1", name="load_skill",
                              input={"name": "csv-analyst"})],
        stop_reason="tool_use",
    ))
    MockAdapter.queue_response(LLMResponse(
        content=[TextBlock(text="loaded csv-analyst, ready to profile.")],
        stop_reason="end_turn",
    ))
    msgs = agent_loop("help me profile a csv", llm=MockAdapter())
    final = next(b.text for b in msgs[-1]["content"]
                 if getattr(b, "type", None) == "text")
    print("  final:", final)


if __name__ == "__main__":
    demo_discovery()
    demo_catalog_in_prompt()
    demo_load_and_run()
    demo_kernel_loads_skill()
    print("\nOK ✓")
