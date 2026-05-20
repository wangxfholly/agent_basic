"""
mega_agent.llm.factory
======================

Resolves which adapter to instantiate based on, in order of priority:
    1. explicit profile dict
    2. profile_name (lookup in ProfileStore)
    3. route (resolved via routing table → default → active)
    4. AGENT_LLM_PROFILE env
    5. ProfileStore.active
    6. AGENT_LLM_BACKEND env (legacy mode, no profile)
"""
from __future__ import annotations

import os

from ..config import MODEL
from .anthropic_adapter import AnthropicAdapter
from .base import LLMClient
from .gateway_adapter import GatewayAdapter
from .mock_adapter import MockAdapter


def _client_from_profile(p: dict) -> LLMClient:
    proto = (p.get("protocol") or "openai").lower()
    model = p.get("model") or MODEL
    if proto == "openai":
        return GatewayAdapter(model=model,
                              base_url=p.get("base_url"),
                              api_key=p.get("api_key"))
    if proto == "anthropic":
        return AnthropicAdapter(model=model,
                                base_url=p.get("base_url"),
                                api_key=p.get("api_key"))
    if proto == "mock":
        return MockAdapter(model=model)
    raise ValueError(f"unknown protocol in profile {p.get('name')!r}: {proto}")


def make_llm_client(profile_name: str | None = None,
                    profile: dict | None = None,
                    route: str | None = None) -> LLMClient:
    # Late import — profiles depends on llm.base via SecretBox sibling
    from ..profiles import profiles as _profiles

    if profile is None:
        if profile_name:
            profile = _profiles.get(profile_name)
        elif route is not None:
            profile = _profiles.resolve_route(route)
        else:
            env_name = os.environ.get("AGENT_LLM_PROFILE")
            if env_name:
                profile = _profiles.get(env_name)
            else:
                profile = _profiles.active()
    if profile:
        return _client_from_profile(profile)

    # legacy fallback: pure-env mode
    backend = os.environ.get("AGENT_LLM_BACKEND", "anthropic").lower()
    if backend == "anthropic":
        return AnthropicAdapter(MODEL)
    if backend == "gateway":
        return GatewayAdapter(MODEL)
    if backend == "mock":
        return MockAdapter(MODEL)
    raise ValueError(f"unknown AGENT_LLM_BACKEND: {backend!r}")
