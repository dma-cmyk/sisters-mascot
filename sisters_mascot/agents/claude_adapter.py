"""Adapter for Claude Code CLI (`claude`)."""

from __future__ import annotations

import json
import os
import shutil
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

_DYNAMIC_ALIASES: tuple[tuple[str, str], ...] = (
    ("sonnet", "Sonnet (sonnet)"),
    ("opus", "Opus (opus)"),
    ("haiku", "Haiku (haiku)"),
    ("sonnet[1m]", "Sonnet 1M (sonnet[1m])"),
    ("opus[1m]", "Opus 1M (opus[1m])"),
)
_SENSITIVE_KEY_PARTS = ("KEY", "TOKEN", "SECRET", "PASS", "AUTH", "CREDENTIAL")


def _is_safe_model_key(key: str) -> bool:
    upper = key.upper()
    return "MODEL" in upper and not any(part in upper for part in _SENSITIVE_KEY_PARTS)


class ClaudeAdapter(BaseAgentAdapter):
    """Adapter for `claude -p --output-format stream-json --verbose`."""

    def __init__(self) -> None:
        self.spec = AgentSpec(
            id="claude",
            display_name="Claude Code",
            binary=shutil.which("claude") or "claude",
            default_models=[
                "sonnet",
                "opus",
                "haiku",
                "sonnet[1m]",
                "opus[1m]",
            ],
            supports_system_prompt=True,
        )
        self._saw_stream_delta = False
        self._emitted_any_text = False

    def reset_state(self) -> None:
        self._saw_stream_delta = False
        self._emitted_any_text = False

    def discover_model_choices(self) -> list[ModelChoice]:
        configured: list[str] = []

        for filename in ("settings.json", "settings.local.json"):
            cfg_path = Path.home() / ".claude" / filename
            if not cfg_path.is_file():
                continue
            try:
                data = json.loads(cfg_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(data, dict):
                continue

            model_val = data.get("model")
            if isinstance(model_val, str) and model_val.strip():
                configured.append(model_val.strip())

            avail = data.get("availableModels")
            if isinstance(avail, list):
                for entry in avail:
                    if isinstance(entry, str) and entry.strip():
                        configured.append(entry.strip())

            env_map = data.get("env")
            if isinstance(env_map, dict):
                for key, val in env_map.items():
                    if (
                        isinstance(key, str)
                        and _is_safe_model_key(key)
                        and isinstance(val, str)
                        and val.strip()
                    ):
                        configured.append(val.strip())

        for key, val in os.environ.items():
            if (
                key.upper().startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
                and _is_safe_model_key(key)
                and val.strip()
            ):
                configured.append(val.strip())

        alias_labels = dict(_DYNAMIC_ALIASES)
        choices: list[ModelChoice] = []
        seen: set[str] = set()

        for model_id in configured:
            if not model_id or model_id == "default" or model_id in seen:
                continue
            seen.add(model_id)
            choices.append(ModelChoice(id=model_id, label=alias_labels.get(model_id, model_id)))

        for alias_id, alias_label in _DYNAMIC_ALIASES:
            if alias_id in seen:
                continue
            seen.add(alias_id)
            choices.append(ModelChoice(id=alias_id, label=alias_label))

        return choices

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
        cmd = [
            self.resolved_binary(),
            "-p",
            prompt,
            "--output-format",
            "stream-json",
            "--verbose",
        ]
        cleaned_model = model.strip()
        if cleaned_model and cleaned_model != "default":
            cmd.extend(["--model", cleaned_model])
        if session_id:
            cmd.extend(["--resume", session_id])
        if system_prompt.strip():
            cmd.extend(["--append-system-prompt", system_prompt])
        if auto_approve:
            cmd.extend(["--permission-mode", "acceptEdits"])
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
        ev_type = data.get("type", "")

        if ev_type == "system" and data.get("subtype") == "init":
            sid = data.get("session_id")
            if isinstance(sid, str) and sid:
                events.append(StreamEvent(kind="session", session_id=sid))
            return events

        if ev_type == "stream_event":
            inner = data.get("event")
            if isinstance(inner, dict):
                events.extend(self._parse_stream_event(inner))
            return events

        if ev_type == "assistant":
            msg = data.get("message")
            content = msg.get("content") if isinstance(msg, dict) else None
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

        if ev_type == "result":
            sid = data.get("session_id")
            if isinstance(sid, str) and sid:
                events.append(StreamEvent(kind="session", session_id=sid))
            is_err = bool(data.get("is_error")) or data.get("subtype") == "error"
            res = data.get("result")
            if is_err:
                events.append(StreamEvent(kind="error", text=str(res or "Claude CLI returned an error.")))
            else:
                if isinstance(res, str) and res and not self._emitted_any_text:
                    self._emitted_any_text = True
                    events.append(StreamEvent(kind="delta", text=res))
                events.append(StreamEvent(kind="done"))
            return events

        if ev_type == "error":
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else (err or "Claude CLI error")
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
