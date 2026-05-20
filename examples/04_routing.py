"""
04_routing.py — programmatic profile + routing setup.

Run:
    export AGENT_MASTER_PASSWORD='choose your passphrase'   # optional but recommended
    python examples/04_routing.py
"""
from mega_agent import agent_loop, profiles, secrets


def setup_profiles():
    print("encryption:", "ENABLED" if secrets.enabled else "DISABLED (plaintext)")

    # Add three profiles. In production the api_key would come from a vault.
    profiles.upsert(
        name="claude",
        protocol="anthropic",
        model="claude-sonnet-4-20250514",
        api_key="sk-ant-REPLACE-ME",
    )
    profiles.upsert(
        name="gpt4o",
        protocol="openai",
        model="gpt-4o",
        base_url="https://api.openai.com/v1",
        api_key="sk-REPLACE-ME",
    )
    profiles.upsert(
        name="self-hosted",
        protocol="openai",
        model="claude-3-7-sonnet",
        base_url="http://litellm.internal:4000",
        api_key="sk-litellm",
    )

    # Semantic routing: which profile handles which kind of work.
    profiles.set_route("code",     "claude")
    profiles.set_route("write",    "gpt4o")
    profiles.set_route("default",  "self-hosted")


def main():
    setup_profiles()

    # @<route> prefix selects the profile for THIS call only.
    msgs = agent_loop("Write a Python decorator that times a function.", route="code")
    last = msgs[-1]["content"]
    for b in last if isinstance(last, list) else []:
        if getattr(b, "type", None) == "text":
            print("answer:", b.text)


if __name__ == "__main__":
    main()
