"""Adapter for OpenCode CLI (`opencode`)."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
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
from sisters_mascot.attachments import AttachmentItem, collect_file_paths

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_VALID_OPENCODE_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_DEFAULT_OPENCODE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def format_model_with_effort(model: str, effort: str) -> str:
    """Attach `#variant` to `provider/model`, replacing any prior variant when effort is set."""
    cleaned_model = (model or "").strip()
    cleaned_effort = (effort or "").strip().lower()
    if not cleaned_model:
        return ""
    if not cleaned_effort or cleaned_effort not in _VALID_OPENCODE_EFFORTS or "/" not in cleaned_model:
        return cleaned_model
    base_model = cleaned_model.split("#", 1)[0].strip()
    if not base_model or "/" not in base_model:
        return cleaned_model
    return f"{base_model}#{cleaned_effort}"


class OpenCodeAdapter(BaseAgentAdapter):
    """Adapter for `opencode run --format json`."""

    def __init__(self) -> None:
        self.spec = AgentSpec(
            id="opencode",
            display_name="OpenCode",
            binary=shutil.which("opencode") or "opencode",
            default_models=[
                "opencode/big-pickle",
                "opencode/deepseek-v4.1-flash-free",
                "opencode/glm-5.3-flash-free",
                "opencode/beam-free",
            ],
            supports_system_prompt=False,
        )
        self._last_part_text: dict[str, str] = {}
        self._emitted_part_ids: list[str] = []
        self._seen_tool_states: dict[str, str] = {}
        self._seen_reasoning: set[str] = set()

    def reset_state(self) -> None:
        self._last_part_text.clear()
        self._emitted_part_ids.clear()
        self._seen_tool_states.clear()
        self._seen_reasoning.clear()

    def discover_model_choices(self) -> list[ModelChoice]:
        fallback = [ModelChoice(id=m, label=m) for m in self.spec.default_models]
        if not self.is_available():
            return fallback

        try:
            proc = subprocess.run(
                [self.resolved_binary(), "models"],
                capture_output=True,
                text=True,
                timeout=12,
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
            if not clean or "/" not in clean:
                continue
            token = clean.split()[0].strip().rstrip(",")
            if "/" not in token or token.startswith(("/", "http://", "https://")):
                continue
            provider, model_name = token.split("/", 1)
            if not provider or not model_name or token in seen:
                continue
            seen.add(token)
            discovered.append(ModelChoice(id=token, label=token))

        return discovered if discovered else fallback

    def discover_models(self) -> list[str]:
        return [choice.id for choice in self.discover_model_choices()]

    def supported_efforts(self, model: str = "") -> list[str]:
        cleaned = (model or "").strip()
        db_path = Path.home() / ".local" / "share" / "opencode" / "opencode.db"
        if cleaned and "/" in cleaned and db_path.is_file():
            provider_id, rest = cleaned.split("/", 1)
            model_id = rest.split("#", 1)[0].strip()
            if provider_id and model_id:
                try:
                    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=0.5)
                    try:
                        row = conn.execute(
                            "SELECT value FROM kv WHERE key='models-dev:v3:catalog'"
                        ).fetchone()
                    finally:
                        conn.close()
                    if row and row[0]:
                        outer = json.loads(row[0])
                        body_raw = outer.get("body") if isinstance(outer, dict) else None
                        cat = json.loads(body_raw) if isinstance(body_raw, str) else outer
                        if isinstance(cat, dict):
                            m_info = (
                                cat.get(provider_id, {})
                                .get("models", {})
                                .get(model_id, {})
                            )
                            reasoning = (
                                m_info.get("capabilities", {}).get("reasoning", {})
                                if isinstance(m_info, dict)
                                else {}
                            )
                            efforts = reasoning.get("effort") if isinstance(reasoning, dict) else None
                            if isinstance(efforts, list):
                                found = [
                                    str(e).strip().lower()
                                    for e in efforts
                                    if str(e).strip().lower() in _VALID_OPENCODE_EFFORTS
                                ]
                                if found:
                                    return found
                except (OSError, sqlite3.Error, json.JSONDecodeError, TypeError, ValueError):
                    pass
        return list(_DEFAULT_OPENCODE_EFFORTS)

    def build_command(
        self,
        prompt: str,
        *,
        model: str = "",
        effort: str = "",
        session_id: str | None = None,
        cwd: str = "",
        system_prompt: str = "",
        auto_approve: bool = True,
        attachments: Sequence[AttachmentItem] = (),
    ) -> tuple[list[str], str | None]:
        _ = cwd, system_prompt
        cmd = [self.resolved_binary(), "run", "--format", "json"]
        formatted_model = format_model_with_effort(model, effort)
        if formatted_model:
            cmd.extend(["-m", formatted_model])
        if session_id:
            cmd.extend(["-s", session_id])
        if auto_approve:
            cmd.append("--auto")
        for file_path in collect_file_paths(attachments):
            cmd.extend(["--file", file_path])
        cmd.append(prompt)
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
        sid = data.get("sessionID") or data.get("session_id")
        if isinstance(sid, str) and sid:
            events.append(StreamEvent(kind="session", session_id=sid))

        ev_type = str(data.get("type") or "")
        part = data.get("part") if isinstance(data.get("part"), dict) else {}
        part_type = str(part.get("type") or "")

        if ev_type == "text" or part_type == "text":
            text = str(part.get("text") if part else data.get("text") or "")
            part_id = str(part.get("id") or "__default__") if part else "__default__"
            if text:
                is_new_part = part_id not in self._last_part_text
                prev = self._last_part_text.get(part_id, "")
                if text != prev:
                    if prev and text.startswith(prev):
                        delta = text[len(prev):]
                        self._last_part_text[part_id] = text
                        if delta:
                            events.append(StreamEvent(kind="delta", text=delta))
                    else:
                        self._last_part_text[part_id] = text
                        emit_text = (
                            f"\n\n{text}"
                            if (is_new_part and self._emitted_part_ids and not text.startswith("\n"))
                            else text
                        )
                        events.append(StreamEvent(kind="delta", text=emit_text))
                if part_id not in self._emitted_part_ids:
                    self._emitted_part_ids.append(part_id)
            return events

        if ev_type in ("tool_use", "tool") or part_type == "tool":
            payload: Any = part if part else data
            raw_tool = str(part.get("tool") or part.get("name") or data.get("tool") or data.get("name") or "tool")
            resolved_tool = extract_effective_tool_name(raw_tool, payload)
            state = (
                part.get("state")
                if isinstance(part.get("state"), dict)
                else (data.get("state") if isinstance(data.get("state"), dict) else {})
            )
            status = str(state.get("status") or "").lower()
            part_id = str(part.get("id") or data.get("id") or "")
            if part_id:
                state_key = status or "pending"
                if self._seen_tool_states.get(part_id) == state_key:
                    return events
                self._seen_tool_states[part_id] = state_key

            if status == "error":
                events.append(
                    make_tool_result_event(resolved_tool, is_error=True, detail=state.get("error"))
                )
            elif status == "completed":
                events.append(make_tool_result_event(resolved_tool, is_error=False))
            else:
                events.append(make_tool_event(resolved_tool, payload))
            return events

        if ev_type == "reasoning" or part_type == "reasoning":
            r_text = str(part.get("text") if part else data.get("text") or "").strip()
            r_id = str(part.get("id") or data.get("id") or r_text[:40])
            if r_text and r_id not in self._seen_reasoning:
                self._seen_reasoning.add(r_id)
                snippet = " ".join(r_text.split())
                if len(snippet) > 90:
                    snippet = snippet[:89] + "…"
                events.append(StreamEvent(kind="log", level="info", text=f"💭 思考: {snippet}"))
            return events

        if ev_type == "error":
            err = data.get("error")
            if isinstance(err, dict):
                msg = str(err.get("message") or err.get("data", {}).get("message") or err)
            else:
                msg = str(err or data.get("message") or "OpenCode CLI error")
            events.append(StreamEvent(kind="error", text=msg))
            return events

        if ev_type == "step_finish":
            reason = str(part.get("reason") or data.get("reason") or "")
            if reason == "stop":
                events.append(StreamEvent(kind="done"))
        elif ev_type in ("done", "session.idle"):
            events.append(StreamEvent(kind="done"))

        return events
