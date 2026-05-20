"""
06_memory.py — cross-session memory: remember, recall, forget.

This example exercises the L2.5 memory layer end-to-end without ever
touching an LLM. It demonstrates:

  1. `remember(...)` — append-only fact log + vector index
  2. `recall(query)` — top-k retrieval (NaiveBackend by default)
  3. `set_pref / get_pref` — KV-style single-value preferences
  4. `forget(fact_id)` and `forget_user(user_id)` — GDPR-style deletion
  5. Auto-recall via `agent_loop` — system prompt is enriched with the
     top hits for the current user turn (works with MockAdapter too).

Run:
    python examples/06_memory.py

Switch backend (optional, requires `pip install chromadb`):
    AGENT_MEMORY_BACKEND=chroma python examples/06_memory.py
"""
from mega_agent import (
    LLMResponse,
    MockAdapter,
    TextBlock,
    agent_loop,
    memory,
)


def demo_remember_and_recall():
    print("\n# 1. remember + recall")
    f1 = memory.remember("user prefers dark mode", kind="preference",
                         user_id="alice", topic="ui")
    f2 = memory.remember("user is allergic to peanuts", kind="fact",
                         user_id="alice", topic="health")
    f3 = memory.remember("project uses Python 3.11", kind="fact",
                         topic="stack")

    hits = memory.recall("what theme does the user like?", k=3)
    print("  recall(theme) →", [h["text"] for h in hits])

    hits = memory.recall("python version", k=3)
    print("  recall(python) →", [h["text"] for h in hits])

    return f1, f2, f3


def demo_kv_prefs():
    print("\n# 2. set_pref / get_pref")
    memory.set("alice.lang", "zh-CN")
    memory.set("alice.timezone", "Asia/Singapore")
    print("  alice.lang     =", memory.get("alice.lang"))
    print("  alice.timezone =", memory.get("alice.timezone"))


def demo_forget(facts):
    print("\n# 3. forget single fact")
    f1 = facts[0]
    print(f"  forgetting fact {f1['id']!r}: {memory.forget(f1['id'])}")
    print(f"  recall again →", memory.recall("dark mode", k=3))


def demo_forget_user():
    print("\n# 4. GDPR-style forget_user")
    purged = memory.forget_user("alice")
    print(f"  purged {purged} records for user 'alice'")
    print("  remaining facts:", [
        f["content"] for f in memory.list_facts(limit=10)
    ])


def demo_auto_recall_in_kernel():
    print("\n# 5. agent_loop auto-injects recalled memories into system prompt")
    # Seed a fact the LLM will need.
    memory.remember("the production region is ap-southeast-1", kind="fact")

    MockAdapter.reset()
    MockAdapter.queue_response(LLMResponse(
        content=[TextBlock(text="we deploy to ap-southeast-1")],
        stop_reason="end_turn",
    ))
    msgs = agent_loop(
        "which region do we deploy to?",
        llm=MockAdapter(),
        recall_k=3,
    )
    final = next(
        b.text for b in msgs[-1]["content"]
        if getattr(b, "type", None) == "text"
    )
    print("  final answer:", final)


if __name__ == "__main__":
    facts = demo_remember_and_recall()
    demo_kv_prefs()
    demo_forget(facts)
    demo_forget_user()
    demo_auto_recall_in_kernel()
    print("\nOK ✓")
