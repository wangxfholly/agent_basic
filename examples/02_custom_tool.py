"""
02_custom_tool.py — register your own business tool.

Pattern:
    1. Write a handler that takes a single dict and returns JSON-serialisable data.
    2. Register it on TOOL_HANDLERS BEFORE calling agent_loop.
    3. The LLM sees the tool automatically via build_tool_schemas().

Run:
    python examples/02_custom_tool.py
"""
from mega_agent import TOOL_HANDLERS, agent_loop


def query_user(inp: dict) -> dict:
    """Pretend to look up a user from your internal service."""
    user_id = inp["user_id"]
    fake_db = {
        "u001": {"name": "Alice", "level": "VIP", "balance": 1234.5},
        "u002": {"name": "Bob",   "level": "free", "balance": 0.0},
    }
    return fake_db.get(user_id) or {"error": f"user {user_id!r} not found"}


def grant_coupon(inp: dict) -> dict:
    """Pretend to issue a coupon. High-risk → permission gate will gate it."""
    return {"granted": True, "user_id": inp["user_id"], "amount": inp["amount"]}


# Register before calling agent_loop. Multiple registrations are idempotent.
TOOL_HANDLERS["query_user"]   = query_user
TOOL_HANDLERS["grant_coupon"] = grant_coupon


def main():
    prompt = (
        "User u001 wants a refund. Look up their profile, "
        "and if they are a VIP, grant them a coupon of 50."
    )
    msgs = agent_loop(prompt)
    last = msgs[-1]["content"]
    for b in last if isinstance(last, list) else []:
        if getattr(b, "type", None) == "text":
            print("answer:", b.text)


if __name__ == "__main__":
    main()
