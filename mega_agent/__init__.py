"""
mega_agent
==========

Layered agent runtime. Top-level public API; subpackages and
modules are also stable import targets.

Quick start::

    from mega_agent import agent_loop, make_llm_client

    # auto-resolves backend via active profile or AGENT_LLM_BACKEND
    msgs = agent_loop("List files and summarise README.md")

Layers:
    config / events / permissions / hooks / retry / tasks /
    worktree / background / teams / mcp / tools / kernel / cli

LLM adapters live under ``mega_agent.llm``; profiles + routing
live under ``mega_agent.profiles``.
"""
from .background import background, cron, notify_q
from .events import events
from .hooks import build_system_prompt, hooks, load_memory
from .kernel import agent_loop
from .llm import (
    AnthropicAdapter,
    GatewayAdapter,
    LLMClient,
    LLMResponse,
    MockAdapter,
    TextBlock,
    ToolUseBlock,
    make_llm_client,
)
from .mcp import MCPClient, MCPRegistry, mcp
from .permissions import CapabilityPermissionGate, permissions
from .profiles import ProfileStore, SecretBox, profiles, secrets
from .retry import RetryBudget, retry_budget
from .tasks import TaskManager, tasks
from .teams import MessageBus, RequestStore, TeammateManager, bus, requests_store, team
from .tools import TOOL_HANDLERS, build_tool_schemas, normalize_tool_result
from .worktree import WorktreeManager, worktrees

__version__ = "0.2.0"

__all__ = [
    # kernel
    "agent_loop",
    # llm
    "LLMClient", "LLMResponse", "TextBlock", "ToolUseBlock",
    "AnthropicAdapter", "GatewayAdapter", "MockAdapter", "make_llm_client",
    # state
    "events", "hooks", "permissions", "retry_budget",
    "tasks", "worktrees", "background", "cron", "notify_q",
    "bus", "team", "requests_store", "mcp", "profiles", "secrets",
    # types
    "EventBus" if False else "events",  # placeholder; classes re-exported below
    "CapabilityPermissionGate", "RetryBudget", "TaskManager", "WorktreeManager",
    "MessageBus", "RequestStore", "TeammateManager",
    "MCPClient", "MCPRegistry", "ProfileStore", "SecretBox",
    # tools
    "TOOL_HANDLERS", "build_tool_schemas", "normalize_tool_result",
    # prompt
    "build_system_prompt", "load_memory",
]
