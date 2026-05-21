"""
09_streaming_cancel.py — exercise the kernel's streaming + cancel hooks.

The mock adapter chunks text into ~12-char pseudo-tokens, so this demo
runs offline and is fully deterministic. Two scenarios:

    1. Plain streaming — every chunk lands in `on_text`.
    2. Cancellation   — flip the cancel flag mid-stream; the kernel
                        returns immediately with a synthetic "[cancelled
                        by user]" final block (and the API contract for
                        any in-flight tool_use → tool_result pairing is
                        still preserved).

Run:
    PYTHONPATH=. python examples/09_streaming_cancel.py
"""
import threading

from mega_agent.kernel import agent_loop
from mega_agent.llm import LLMResponse, MockAdapter, TextBlock, ToolUseBlock


def demo_stream():
    print("# 1. streaming — `on_text` receives each chunk live")
    MockAdapter.queue_response(LLMResponse(
        content=[TextBlock(text=(
            "Streaming demo: this sentence is split into ~12-char chunks "
            "and the kernel hands each one to your callback as it lands."
        ))],
        stop_reason="end_turn",
    ))
    chunks: list[str] = []
    msgs = agent_loop("hi", llm=MockAdapter(),
                      on_text=lambda c: chunks.append(c))
    print(f"  received {len(chunks)} chunks, total {sum(len(c) for c in chunks)} chars")
    print(f"  reassembled: {''.join(chunks)!r}")
    print(f"  loop messages: {len(msgs)}\n")


def demo_cancel():
    print("# 2. cancellation — flip the flag after 3 chunks")
    MockAdapter.queue_response(LLMResponse(
        content=[TextBlock(text="A" * 200)],  # ~17 chunks worth
        stop_reason="end_turn",
    ))
    cancel = threading.Event()
    chunks: list[str] = []

    def on_text(c: str) -> None:
        chunks.append(c)
        if len(chunks) >= 3:
            cancel.set()

    msgs = agent_loop("long", llm=MockAdapter(),
                      on_text=on_text, cancel_event=cancel)
    print(f"  delivered {len(chunks)} chunks before cancel")
    last_text = next(
        (getattr(b, "text", "") for b in msgs[-1]["content"]
         if getattr(b, "type", None) == "text"),
        "",
    )
    print(f"  final block: {last_text!r}\n")


def demo_cancel_during_tool():
    print("# 3. cancel between tool_use and the next stream — tool_result")
    print("     is synthesised so the LLM contract stays consistent")
    # First turn: model wants to call a tool.
    MockAdapter.queue_response(LLMResponse(
        content=[ToolUseBlock(id="t1", name="bash",
                              input={"command": "echo hi"})],
        stop_reason="tool_use",
    ))
    # Second turn would normally happen — but we cancel before it does.
    MockAdapter.queue_response(LLMResponse(
        content=[TextBlock(text="never reached")],
        stop_reason="end_turn",
    ))
    cancel = threading.Event()
    cancel.set()  # pre-set: the kernel bails on the very first iter check
    msgs = agent_loop("run a tool", llm=MockAdapter(), cancel_event=cancel)
    print(f"  loop returned {len(msgs)} messages")
    last_text = next(
        (getattr(b, "text", "") for b in msgs[-1]["content"]
         if getattr(b, "type", None) == "text"),
        "",
    )
    print(f"  final block: {last_text!r}\n")


def main():
    demo_stream()
    demo_cancel()
    demo_cancel_during_tool()
    print("OK ✓")


if __name__ == "__main__":
    main()
