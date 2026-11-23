"""AI CLI agent adapters and router."""

from sisters_mascot.agents.agy_adapter import AgyAdapter
from sisters_mascot.agents.base import AgentSpec, BaseAgentAdapter, StreamEvent
from sisters_mascot.agents.claude_adapter import ClaudeAdapter
from sisters_mascot.agents.codex_adapter import CodexAdapter
from sisters_mascot.agents.opencode_adapter import OpenCodeAdapter
from sisters_mascot.agents.router import AGENT_IDS, AgentRouter

__all__ = [
    "AGENT_IDS",
    "AgentRouter",
    "AgentSpec",
    "AgyAdapter",
    "BaseAgentAdapter",
    "ClaudeAdapter",
    "CodexAdapter",
    "OpenCodeAdapter",
    "StreamEvent",
]
