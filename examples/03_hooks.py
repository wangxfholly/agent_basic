"""
03_hooks.py — wire in audit / metering / compliance via hooks.

Hook events emitted by the kernel:
    tool.before  — payload: {name, input, intent}
    tool.after   — payload: {name, intent, ok}
    tool.error   — payload: {name, intent, error}
    loop.iter    — payload: {iter, backend}

Run:
    python examples/03_hooks.py
"""
import time

from mega_agent import agent_loop, hooks

# crude in-memory metrics
metrics = {"tool_calls": 0, "errors": 0, "total_ms": 0}
_starts: dict[str, float] = {}


def on_before(payload):
    metrics["tool_calls"] += 1
    _starts[payload["name"]] = time.perf_counter()
    print(f"  [audit] BEFORE {payload['name']} risk={payload['intent']['risk']}")


def on_after(payload):
    name = payload["name"]
    if name in _starts:
        metrics["total_ms"] += (time.perf_counter() - _starts.pop(name)) * 1000


def on_error(payload):
    metrics["errors"] += 1
    print(f"  [audit] ERROR  {payload['name']}: {payload['error']}")


hooks.on("tool.before", on_before)
hooks.on("tool.after",  on_after)
hooks.on("tool.error",  on_error)


def main():
    msgs = agent_loop("Read README.md and tell me its first heading.")
    last = msgs[-1]["content"]
    for b in last if isinstance(last, list) else []:
        if getattr(b, "type", None) == "text":
            print("answer:", b.text)
    print(f"\nmetrics: {metrics}")


if __name__ == "__main__":
    main()
