"""Base types and shared helpers for AI agent CLI adapters."""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from sisters_mascot.attachments import AttachmentItem

PLAYWRIGHT_TOOL_KEYWORDS = ("playwright", "browser_")
_TOOL_CALL_RE = re.compile(r"tools\.([A-Za-z0-9_]+\.[A-Za-z0-9_]+)")


@dataclass
class StreamEvent:
    kind: str  # "delta" | "replace" | "tool" | "status" | "log" | "session" | "done" | "error"
    text: str = ""
    session_id: str | None = None
    tool_name: str | None = None
    detail: str = ""
    level: str = "info"


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


def _clip_single_line(text: Any, max_chars: int = 90) -> str:
    """Collapse whitespace and extract a concise single-line preview."""
    if isinstance(text, list):
        parts: list[str] = []
        for item in text:
            if isinstance(item, dict) and item.get("text"):
                parts.append(str(item["text"]))
            elif isinstance(item, str):
                parts.append(item)
        raw = " ".join(parts)
    elif isinstance(text, dict):
        raw = str(text.get("message") or text.get("text") or text.get("error") or text)
    else:
        raw = str(text or "")
    collapsed = " ".join(raw.split()).strip()
    if len(collapsed) <= max_chars:
        return collapsed
    return collapsed[: max(1, max_chars - 1)] + "…"


def _iter_param_dicts(payload: Any) -> list[dict[str, Any]]:
    """Collect parameter dicts from nested `parameters` or `tool_info.parameters`."""
    if not isinstance(payload, dict):
        return []
    params_list: list[dict[str, Any]] = []
    direct = payload.get("parameters")
    if isinstance(direct, dict):
        params_list.append(direct)
    tool_info = payload.get("tool_info")
    if isinstance(tool_info, dict):
        nested = tool_info.get("parameters")
        if isinstance(nested, dict):
            params_list.append(nested)
    return params_list


def extract_effective_tool_name(tool_name: str, payload: Any = None) -> str:
    """Resolve wrapped tool names (e.g. OpenCode `execute` or `agy` `call_mcp_tool`)."""
    clean = (tool_name or "").strip() or "tool"
    if not isinstance(payload, dict):
        return clean

    for params in _iter_param_dicts(payload):
        inner = str(params.get("ToolName") or params.get("tool_name") or "").strip()
        if inner:
            server = str(params.get("ServerName") or params.get("server_name") or "").strip()
            if (
                server
                and "." not in inner
                and not inner.startswith(f"{server}.")
                and not inner.startswith("mcp")
            ):
                return f"{server}.{inner}"
            return inner

    state = payload.get("state")
    if isinstance(state, dict):
        meta = state.get("metadata")
        if isinstance(meta, dict):
            tool_calls = meta.get("toolCalls")
            if isinstance(tool_calls, list) and tool_calls:
                last_call = tool_calls[-1]
                if isinstance(last_call, dict):
                    inner = str(last_call.get("tool") or last_call.get("name") or "").strip()
                    if inner:
                        return inner
        st_input = state.get("input")
        if isinstance(st_input, dict):
            code = st_input.get("code")
            if isinstance(code, str):
                match = _TOOL_CALL_RE.search(code)
                if match:
                    return match.group(1)

    return clean


def is_playwright_tool(tool_name: str, payload: Any = None) -> bool:
    """Return True if a tool call targets Playwright / Chrome browser automation."""
    effective = extract_effective_tool_name(tool_name, payload)
    for candidate in (tool_name, effective):
        haystack = (candidate or "").lower()
        if any(kw in haystack for kw in PLAYWRIGHT_TOOL_KEYWORDS):
            return True
    if isinstance(payload, dict):
        for key in ("server", "mcp_server", "server_name", "tool", "name"):
            val = payload.get(key)
            if isinstance(val, str) and any(kw in val.lower() for kw in PLAYWRIGHT_TOOL_KEYWORDS):
                return True
        for params in _iter_param_dicts(payload):
            for key in ("ServerName", "server_name", "ToolName", "tool_name"):
                val = params.get(key)
                if isinstance(val, str) and any(kw in val.lower() for kw in PLAYWRIGHT_TOOL_KEYWORDS):
                    return True
        state = payload.get("state")
        if isinstance(state, dict):
            st_input = state.get("input")
            if isinstance(st_input, dict):
                code = st_input.get("code")
                if isinstance(code, str) and any(kw in code.lower() for kw in PLAYWRIGHT_TOOL_KEYWORDS):
                    return True
    return False


def summarize_tool_payload(tool_name: str, payload: Any = None, *, max_chars: int = 90) -> str:
    """Extract a concise single-line preview of a tool's target or arguments."""
    _ = tool_name
    if not isinstance(payload, dict):
        return ""

    candidates: list[dict[str, Any]] = [payload]
    tool_info = payload.get("tool_info")
    if isinstance(tool_info, dict):
        candidates.append(tool_info)
    for params in _iter_param_dicts(payload):
        candidates.append(params)
        args_obj = params.get("Arguments")
        if isinstance(args_obj, dict):
            candidates.append(args_obj)

    state = payload.get("state")
    if isinstance(state, dict):
        meta = state.get("metadata")
        if isinstance(meta, dict):
            tool_calls = meta.get("toolCalls")
            if isinstance(tool_calls, list) and tool_calls and isinstance(tool_calls[-1], dict):
                last_in = tool_calls[-1].get("input")
                if isinstance(last_in, dict):
                    candidates.append(last_in)
        if isinstance(state.get("input"), dict):
            candidates.append(state["input"])
    for key in ("input", "arguments", "params"):
        sub = payload.get(key)
        if isinstance(sub, dict):
            candidates.append(sub)

    for src in candidates:
        for field_name in (
            "command",
            "CommandLine",
            "cmd",
            "url",
            "Url",
            "URL",
            "path",
            "Path",
            "file_path",
            "FilePath",
            "TargetFile",
            "AbsolutePath",
            "DirectoryPath",
            "SearchPath",
            "element",
            "query",
            "Query",
            "Prompt",
            "prompt",
            "text",
            "target",
            "code",
        ):
            val = src.get(field_name)
            if isinstance(val, str) and val.strip():
                return _clip_single_line(val, max_chars)
    return ""


def make_tool_event(tool_name: str, payload: Any = None) -> StreamEvent:
    """Create a StreamEvent for a tool invocation, highlighting Playwright Chrome actions."""
    effective = extract_effective_tool_name(tool_name, payload)
    detail = summarize_tool_payload(effective, payload)
    if is_playwright_tool(effective, payload):
        return StreamEvent(
            kind="tool",
            tool_name=effective,
            text="🌐 Chrome (Playwright) 操作中...",
            detail=detail,
            level="tool",
        )
    return StreamEvent(
        kind="tool",
        tool_name=effective,
        text=f"🛠️ {effective} 実行中...",
        detail=detail,
        level="tool",
    )


def make_tool_result_event(
    tool_name: str,
    *,
    is_error: bool = False,
    detail: Any = "",
) -> StreamEvent:
    """Create a structured execution log StreamEvent when a tool completes or fails."""
    clean_name = (tool_name or "").strip() or "tool"
    clean_detail = _clip_single_line(detail, 140) if detail else ""
    if is_error:
        msg = (
            f"❌ ツール失敗 ({clean_name}): {clean_detail}"
            if clean_detail
            else f"❌ ツール失敗 ({clean_name})"
        )
        return StreamEvent(
            kind="log",
            tool_name=clean_name,
            text=msg,
            detail=clean_detail,
            level="error",
        )
    msg = f"✅ 完了 ({clean_name}: {clean_detail})" if clean_detail else f"✅ 完了 ({clean_name})"
    return StreamEvent(
        kind="log",
        tool_name=clean_name,
        text=msg,
        detail=clean_detail,
        level="info",
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
        attachments: Sequence[AttachmentItem] = (),
    ) -> tuple[list[str], str | None]:
        """Build (argv, stdin_text) for invoking the CLI non-interactively."""
        raise NotImplementedError

    def parse_line(self, line: str) -> list[StreamEvent]:
        """Parse one line of stdout from the CLI into zero or more StreamEvents."""
        raise NotImplementedError
