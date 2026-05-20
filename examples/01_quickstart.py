"""
01_quickstart.py — minimal embed.

Drop this into any Python service. Requires:

    export ANTHROPIC_API_KEY=sk-ant-xxx
    # or set up a profile via the REPL

Run:
    python examples/01_quickstart.py
"""
from mega_agent import agent_loop


def main():
    msgs = agent_loop("List the files in the current directory and tell me how many there are.")
    last = msgs[-1]["content"]
    if isinstance(last, list):
        for b in last:
            if getattr(b, "type", None) == "text":
                print("answer:", b.text)
    else:
        print("answer:", last)


if __name__ == "__main__":
    main()
