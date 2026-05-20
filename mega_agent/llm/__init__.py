"""
LLM adapter sub-package. Public API:

    from mega_agent.llm import (
        LLMClient, LLMResponse, TextBlock, ToolUseBlock,
        AnthropicAdapter, GatewayAdapter, MockAdapter,
        make_llm_client,
    )
"""
from .anthropic_adapter import AnthropicAdapter
from .base import LLMClient, LLMResponse, TextBlock, ToolUseBlock
from .factory import make_llm_client
from .gateway_adapter import GatewayAdapter
from .mock_adapter import MockAdapter

__all__ = [
    "LLMClient", "LLMResponse", "TextBlock", "ToolUseBlock",
    "AnthropicAdapter", "GatewayAdapter", "MockAdapter",
    "make_llm_client",
]
