"""
05_mock_test.py — write deterministic tests with MockAdapter.

The mock backend never makes network calls; you queue scripted LLMResponses
in order and the kernel pops them turn by turn. This lets you unit-test:
  - tool registration
  - permission gating
  - hook side effects
  - retry budget logic

Run:
    python examples/05_mock_test.py
"""
from mega_agent import (
    LLMResponse,
    MockAdapter,
    TextBlock,
    ToolUseBlock,
    agent_loop,
)


def test_two_turn_conversation():
    MockAdapter.reset()

    # Turn 1: LLM decides to call bash.
    MockAdapter.queue_response(LLMResponse(
        content=[ToolUseBlock(id="t1", name="bash",
                              input={"command": "echo hello"})],
        stop_reason="tool_use",
    ))
    # Turn 2: after seeing tool result, LLM finishes with a text answer.
    MockAdapter.queue_response(LLMResponse(
        content=[TextBlock(text="done: hello")],
        stop_reason="end_turn",
    ))

    msgs = agent_loop("say hello via bash", llm=MockAdapter())
    final_text = next(
        b.text for b in msgs[-1]["content"]
        if getattr(b, "type", None) == "text"
    )
    assert final_text == "done: hello", final_text
    print(f"PASS: {len(msgs)} messages, final = {final_text!r}")


if __name__ == "__main__":
    test_two_turn_conversation()
