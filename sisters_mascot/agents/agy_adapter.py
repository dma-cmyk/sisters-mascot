"""Adapter for the Antigravity CLI (`agy`)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Sequence

from sisters_mascot.agents.base import (
    AgentSpec,
    BaseAgentAdapter,
    ModelChoice,
    StreamEvent,
    make_tool_event,
    make_tool_result_event,
)
from sisters_mascot.attachments import AttachmentItem, collect_attachment_dirs

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_MODEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,}$")
_SKIP_PREFIXES = ("fetching", "loading", "available", "models", "usage", "#", "-", "=")


def _default_agy_binary() -> str:
    found = shutil.which("agy")
    if found:
        return found
    preferred = Path.home() / ".local" / "bin" / "agy"
    return str(preferred)


class AgyAdapter(BaseAgentAdapter):
    """Adapter for Antigravity CLI (`agy`) stream-json mode."""

    def __init__(self) -> None:
        self.spec = AgentSpec(
            id="agy",
            display_name="Antigravity (agy)",
            binary=_default_agy_binary(),
            default_models=[
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "claude-sonnet-4-6",
                "claude-opus-4-6",
            ],
            supports_system_prompt=False,
        )
        self._saw_stream_delta = False
        self._emitted_any_text = False

    def reset_state(self) -> None:
        self._saw_stream_delta = False
        self._emitted_any_text = False

    def discover_model_choices(self) -> list[ModelChoice]:
        fallback = [ModelChoice(id=m, label=m) for m in self.spec.default_models]
        if not self.is_available():
            return fallback

        try:
            proc = subprocess.run(
                [self.resolved_binary(), "models"],
                capture_output=True,
                text=True,
                timeout=15,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return fallback

        if proc.returncode != 0 or not proc.stdout.strip():
            return fallback

        discovered: list[ModelChoice] = []
        seen: set[str] = set()
        for raw_line in proc.stdout.splitlines():
            clean = _ANSI_RE.sub("", raw_line).strip()
            if not clean:
                continue
            lowered = clean.lower()
            if lowered.startswith(_SKIP_PREFIXES) or clean.endswith("..."):
                continue

            if "\t" in clean:
                parts = [p.strip() for p in clean.split("\t") if p.strip()]
                model_id = parts[0]
                display = parts[1] if len(parts) >= 2 else ""
            else:
                parts = clean.lstrip("*•- ").split()
                if len(parts) != 1:
                    continue
                model_id = parts[0].rstrip(",")
                display = ""

            if (
                not _MODEL_TOKEN_RE.match(model_id)
                or model_id.lower() in {"name", "model", "id", "alias"}
                or model_id in seen
            ):
                continue

            seen.add(model_id)
            label = f"{display} ({model_id})" if display and display != model_id else model_id
            discovered.append(ModelChoice(id=model_id, label=label))

        return discovered if discovered else fallback

    def discover_models(self) -> list[str]:
        return [choice.id for choice in self.discover_model_choices()]

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
        _ = system_prompt
        cmd = [self.resolved_binary(), "-p", prompt, "--output-format", "stream-json"]
        if model.strip():
            cmd.extend(["--model", model.strip()])
        if session_id:
            cmd.extend(["--conversation", session_id])
        if auto_approve:
            cmd.extend(["--mode", "auto"])
        for extra_dir in collect_attachment_dirs(attachments, cwd):
            cmd.extend(["--add-dir", extra_dir])
        return cmd, None

    def parse_line(self, line: str) -> list[StreamEvent]:
        stripped = line.strip()
        if not stripped or not stripped.startswith("{"):
            return []
        try:
            data = json.loads(stripped)
        except json.JSONDecodeError:
            return []
        if not isinstance(data, dict):
            return []

        events: list[StreamEvent] = []
        session_id = data.get("session_id") or data.get("conversation_id") or data.get("conversationId")
        if isinstance(session_id, str) and session_id:
            events.append(StreamEvent(kind="session", session_id=session_id))

        ev_type = data.get("type", "")
        if ev_type == "stream_event":
            inner = data.get("event")
            if isinstance(inner, dict):
                events.extend(self._parse_stream_event(inner))
            return events

        if ev_type == "assistant":
            msg = data.get("message")
            content = msg.get("content") if isinstance(msg, dict) else data.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict):
                        continue
                    b_type = block.get("type")
                    if b_type == "tool_use":
                        events.append(make_tool_event(str(block.get("name", "tool")), block))
                    elif b_type == "text" and not self._saw_stream_delta:
                        text = str(block.get("text", ""))
                        if text:
                            self._emitted_any_text = True
                            events.append(StreamEvent(kind="delta", text=text))
            elif isinstance(content, str) and content and not self._saw_stream_delta:
                self._emitted_any_text = True
                events.append(StreamEvent(kind="delta", text=content))
            return events

        if ev_type == "user":
            msg = data.get("message")
            content = msg.get("content") if isinstance(msg, dict) else data.get("content")
            if isinstance(content, list):
                for block in content:
                    if not isinstance(block, dict) or block.get("type") != "tool_result":
                        continue
                    t_name = str(block.get("tool_name") or block.get("name") or "tool")
                    is_err = bool(block.get("is_error"))
                    detail = block.get("content") if is_err else ""
                    events.append(make_tool_result_event(t_name, is_error=is_err, detail=detail))
            return events

        if ev_type == "tool_result":
            t_name = str(data.get("tool_name") or data.get("name") or "tool")
            is_err = bool(data.get("is_error"))
            detail = (data.get("content") or data.get("error") or "") if is_err else ""
            events.append(make_tool_result_event(t_name, is_error=is_err, detail=detail))
            return events

        if ev_type in ("tool_use", "tool", "tool_call"):
            name = str(data.get("name") or data.get("tool") or "tool")
            events.append(make_tool_event(name, data))
            return events

        if ev_type == "delta" and isinstance(data.get("text"), str):
            self._saw_stream_delta = True
            self._emitted_any_text = True
            events.append(StreamEvent(kind="delta", text=data["text"]))
            return events

        if ev_type == "result":
            is_err = bool(data.get("is_error")) or data.get("subtype") == "error"
            res_text = data.get("result")
            if is_err:
                err_msg = str(res_text or data.get("error") or "Antigravity CLI がエラーを返しました。")
                events.append(StreamEvent(kind="error", text=err_msg))
            else:
                if isinstance(res_text, str) and res_text and not self._emitted_any_text:
                    self._emitted_any_text = True
                    events.append(StreamEvent(kind="delta", text=res_text))
                events.append(StreamEvent(kind="done"))
            return events

        if ev_type == "error":
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else (err or data.get("message") or "Unknown error")
            events.append(StreamEvent(kind="error", text=str(msg)))
        return events

    def _parse_stream_event(self, inner: dict[str, Any]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        inner_type = inner.get("type", "")
        if inner_type == "content_block_delta":
            delta = inner.get("delta")
            if isinstance(delta, dict) and delta.get("type") == "text_delta":
                text = str(delta.get("text", ""))
                if text:
                    self._saw_stream_delta = True
                    self._emitted_any_text = True
                    events.append(StreamEvent(kind="delta", text=text))
        elif inner_type == "content_block_start":
            cb = inner.get("content_block")
            if isinstance(cb, dict) and cb.get("type") == "tool_use":
                events.append(make_tool_event(str(cb.get("name", "tool")), cb))
        return events
