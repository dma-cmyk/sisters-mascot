"""Base types and shared helpers for AI agent CLI adapters."""

from __future__ import annotations

import os
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

PLAYWRIGHT_TOOL_KEYWORDS = ("playwright", "browser_")


@dataclass
class StreamEvent:
    kind: str  # "delta" | "replace" | "tool" | "status" | "session" | "done" | "error"
    text: str = ""
    session_id: str | None = None
    tool_name: str | None = None


@dataclass
class AgentSpec:
    id: str
    display_name: str
    binary: str
    default_models: list[str] = field(default_factory=list)
    supports_system_prompt: bool = False


@dataclass
class ModelChoice:
    id: str
    label: str = ""


def is_playwright_tool(tool_name: str, payload: Any = None) -> bool:
    """Return True if a tool call targets Playwright / Chrome browser automation."""
    haystack = tool_name.lower()
    if any(kw in haystack for kw in PLAYWRIGHT_TOOL_KEYWORDS):
        return True
    if isinstance(payload, dict):
        for key in ("server", "mcp_server", "server_name", "tool", "name"):
            val = payload.get(key)
            if isinstance(val, str) and any(kw in val.lower() for kw in PLAYWRIGHT_TOOL_KEYWORDS):
                return True
    return False


def make_tool_event(tool_name: str, payload: Any = None) -> StreamEvent:
    """Create a StreamEvent for a tool invocation, highlighting Playwright Chrome actions."""
    clean_name = tool_name.strip() or "tool"
    if is_playwright_tool(clean_name, payload):
        return StreamEvent(
            kind="tool",
            tool_name=clean_name,
            text="🌐 Chrome (Playwright) 操作中...",
        )
    return StreamEvent(
        kind="tool",
        tool_name=clean_name,
        text=f"🛠️ {clean_name} 実行中...",
    )


# Common install dirs searched when a CLI is not found on PATH (GUI launches).
_COMMON_BIN_DIRS = (".opencode/bin", ".local/bin", "bin")
_SYSTEM_BIN_DIRS = ("/usr/local/bin", "/usr/bin", "/bin")


def _fallback_binary(name: str) -> str | None:
    """Return an executable `name` found in common install dirs, else None."""
    home = Path.home()
    candidates = [home / rel / name for rel in _COMMON_BIN_DIRS]
    candidates += [Path(d) / name for d in _SYSTEM_BIN_DIRS]
    for cand in candidates:
        if cand.is_file() and os.access(cand, os.X_OK):
            return str(cand)
    return None


class BaseAgentAdapter:
    """Common interface implemented by all CLI agent adapters."""

    spec: AgentSpec

    def reset_state(self) -> None:
        """Reset per-turn stream deduplication state before starting a new turn."""

    def resolved_binary(self) -> str:
        """Resolve the configured CLI binary via absolute path or PATH.

        Falls back to common user/system install directories when PATH lookup
        fails: GUI launchers (e.g. the KDE menu) often start apps with a minimal
        PATH that omits dirs like ``~/.opencode/bin``, which would otherwise make
        an installed CLI look unavailable and hide its real model list.
        """
        configured = self.spec.binary
        if os.sep in configured or "/" in configured:
            p = Path(configured)
            if p.is_file():
                return str(p)
        name = Path(configured).name
        found = shutil.which(name)
        if found:
            return found
        return _fallback_binary(name) or configured

    def is_available(self) -> bool:
        """Return True if the underlying CLI binary is installed and executable."""
        resolved = self.resolved_binary()
        p = Path(resolved)
        if p.is_file():
            return True
        return shutil.which(resolved) is not None

    def discover_models(self) -> list[str]:
        """Return available model names for this agent."""
        return list(self.spec.default_models)

    def discover_model_choices(self) -> list[ModelChoice]:
        """Return available model choices (id + display label) for this agent."""
        return [ModelChoice(id=m, label=m) for m in self.discover_models()]

    def build_command(
        self,
        prompt: str,
        *,
        model: str = "",
        session_id: str | None = None,
        cwd: str = "",
        system_prompt: str = "",
        auto_approve: bool = True,
    ) -> tuple[list[str], str | None]:
        """Build (argv, stdin_text) for invoking the CLI non-interactively."""
        raise NotImplementedError

    def parse_line(self, line: str) -> list[StreamEvent]:
        """Parse one line of stdout from the CLI into zero or more StreamEvents."""
        raise NotImplementedError
