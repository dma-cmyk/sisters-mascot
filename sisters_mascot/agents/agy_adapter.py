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
    extract_effective_tool_name,
    make_tool_event,
    make_tool_result_event,
)
from sisters_mascot.attachments import AttachmentItem, collect_attachment_dirs

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_MODEL_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{2,}$")
_SKIP_PREFIXES = ("fetching", "loading", "available", "models", "usage", "#", "-", "=")
_DONE_STATES = frozenset({"DONE", "COMPLETED", "SUCCESS"})
_ERROR_STATES = frozenset({"ERROR", "FAILED", "FAILURE", "CANCELED", "CANCELLED", "DENIED"})


def _default_agy_binary() -> str:
    found = shutil.which("agy")
    if found:
        return found
    preferred = Path.home() / ".local" / "bin" / "agy"
    return str(preferred)


def _extract_session_id(data: dict[str, Any]) -> str | None:
    """Find session/conversation ID across top-level and nested agy event objects."""
    containers: list[dict[str, Any]] = [data]
    for nested_key in ("init", "step_update", "result"):
        nested = data.get(nested_key)
        if isinstance(nested, dict):
            containers.append(nested)
    for obj in containers:
        for key in ("session_id", "conversation_id", "conversationId"):
            val = obj.get(key)
            if isinstance(val, str) and val:
                return val
    return None


class AgyAdapter(BaseAgentAdapter):
    """Adapter for Antigravity CLI (`agy`) stream-json mode."""

    def __init__(self) -> None:
        self.spec = AgentSpec(
            id="agy",
            display_name="Antigravity (agy)",
            binary=_default_agy_binary(),
            default_models=[
                "gemini-4-hydrogen-low",
                "gemini-4-hydrogen-high",
                "gemini-4-neon-high",
                "gemini-3.8-flash-low",
                "claude-sonnet-5-5-high",
            ],
            supports_system_prompt=False,
        )
        self._saw_stream_delta = False
        self._emitted_any_text = False
        self._last_text_step_index: int | None = None
        self._last_text_ended_newline: bool = True
        self._seen_tool_steps: dict[int, str] = {}

    def reset_state(self) -> None:
        self._saw_stream_delta = False
        self._emitted_any_text = False
        self._last_text_step_index = None
        self._last_text_ended_newline = True
        self._seen_tool_steps.clear()

    def discover_model_choices(self) -> list[ModelChoice]:
        fallback = [ModelChoice(id=m, label=m) for m in self.spec.default_models]
        if not self.is_available():
            return fallback

        try:
            proc = subprocess.run(
                [self.resolved_binary(), "models"],
                capture_output=True,
                text=True,
                timeout=25,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return fallback

        if proc.returncode != 0 or not proc.stdout.strip():
            return fallback

        discovered: list[ModelChoice] = []
        seen: set[str] = set()
        for raw_line in proc.stdout.splitlines():
            clean = _ANSI_RE.sub("", raw_line.split("\r")[-1]).strip()
            if not clean:
                continue
            lowered = clean.lower()
            if lowered.startswith(_SKIP_PREFIXES) or clean.endswith("..."):
                continue

            cols = [p.strip() for p in re.split(r"\t+|\s{2,}", clean, maxsplit=1) if p.strip()]
            if len(cols) >= 2:
                model_id = cols[0].rstrip(",")
                display = cols[1]
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
            cmd.append("--dangerously-skip-permissions")
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
        session_id = _extract_session_id(data)
        if session_id:
            events.append(StreamEvent(kind="session", session_id=session_id))

        # Current Antigravity CLI stream-json schema (`"event": "init" | "step_update" | "result"`)
        ev_name = data.get("event", "")
        if ev_name == "init":
            return events

        if ev_name == "step_update":
            step = data.get("step_update")
            if isinstance(step, dict):
                events.extend(self._parse_step_update(step))
            return events

        if ev_name == "result":
            res = data.get("result") if isinstance(data.get("result"), dict) else data
            status = str(res.get("status") or "").upper()
            finished_reason = str(res.get("finished_reason") or "").lower()
            err_text = str(res.get("error") or "").strip()
            resp_text = res.get("response")
            is_err = (
                status in ("ERROR", "FAILED", "FAILURE")
                or finished_reason == "error"
                or bool(err_text)
            )
            if is_err:
                events.append(
                    StreamEvent(
                        kind="error",
                        text=err_text or str(resp_text or "Antigravity CLI がエラーを返しました。"),
                    )
                )
            else:
                if isinstance(resp_text, str) and resp_text and not self._emitted_any_text:
                    self._emitted_any_text = True
                    events.append(StreamEvent(kind="delta", text=resp_text))
                if finished_reason == "print_timeout":
                    events.append(
                        StreamEvent(
                            kind="log",
                            level="warn",
                            text="⚠️ タイムアウトにより途中までの応答で終了しました。",
                        )
                    )
                events.append(StreamEvent(kind="done"))
            return events

        if ev_name == "error":
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else (err or data.get("message") or "Unknown error")
            events.append(StreamEvent(kind="error", text=str(msg)))
            return events

        # Legacy Claude-compatible stream-json schema fallback (`"type": ...`)
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

    def _parse_step_update(self, step: dict[str, Any]) -> list[StreamEvent]:
        events: list[StreamEvent] = []
        step_type = str(step.get("step_type") or "")
        state_upper = str(step.get("state") or "").upper()
        step_index = step.get("step_index")
        is_int_step = isinstance(step_index, int) and not isinstance(step_index, bool)

        text_delta = step.get("text_delta")
        if isinstance(text_delta, str) and text_delta:
            emit_text = text_delta
            if (
                self._emitted_any_text
                and is_int_step
                and self._last_text_step_index is not None
                and step_index != self._last_text_step_index
                and not self._last_text_ended_newline
                and not text_delta.startswith("\n")
            ):
                emit_text = f"\n\n{text_delta}"
            self._saw_stream_delta = True
            self._emitted_any_text = True
            self._last_text_ended_newline = text_delta.endswith("\n")
            if is_int_step:
                self._last_text_step_index = step_index
            events.append(StreamEvent(kind="delta", text=emit_text))

        if step_type in ("tool", "subagent"):
            if state_upper in _DONE_STATES:
                phase = "done"
            elif state_upper in _ERROR_STATES:
                phase = "error"
            else:
                phase = "start"

            if is_int_step:
                if self._seen_tool_steps.get(step_index) == phase:
                    return events
                self._seen_tool_steps[step_index] = phase

            if step_type == "tool":
                tool_info = step.get("tool_info") if isinstance(step.get("tool_info"), dict) else {}
                raw_tool = str(step.get("tool_name") or tool_info.get("name") or "tool")
                payload = tool_info if tool_info else step
                resolved_tool = extract_effective_tool_name(raw_tool, payload)
                if phase == "start":
                    events.append(make_tool_event(resolved_tool, payload))
                elif phase == "error":
                    detail = (
                        step.get("error")
                        or step.get("error_message")
                        or tool_info.get("error")
                        or tool_info.get("output")
                        or state_upper
                    )
                    events.append(make_tool_result_event(resolved_tool, is_error=True, detail=detail))
                else:
                    tool_err = step.get("error") or step.get("error_message") or tool_info.get("error")
                    is_err = bool(tool_info.get("is_error")) or bool(tool_err)
                    detail = tool_err if is_err else (tool_info.get("output") or "")
                    events.append(make_tool_result_event(resolved_tool, is_error=is_err, detail=detail))
                return events

            sub = step.get("subagent_info") if isinstance(step.get("subagent_info"), dict) else {}
            label = str(sub.get("role") or sub.get("type_name") or step.get("tool_name") or "subagent")
            if phase == "start":
                events.append(StreamEvent(kind="status", text=f"🤖 サブエージェント実行中 ({label})..."))
            else:
                detail = step.get("error") or step.get("error_message") or ""
                events.append(
                    make_tool_result_event(f"subagent:{label}", is_error=(phase == "error"), detail=detail)
                )
            return events

        if step_type == "error" or (
            state_upper in ("ERROR", "FAILED") and (step.get("error") or step.get("error_message"))
        ):
            err_msg = step.get("error") or step.get("error_message") or "Antigravity CLI でエラーが発生しました。"
            events.append(StreamEvent(kind="error", text=str(err_msg)))

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
