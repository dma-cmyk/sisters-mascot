"""Adapter for OpenCode CLI (`opencode`)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from typing import Any

from sisters_mascot.agents.base import (
    AgentSpec,
    BaseAgentAdapter,
    ModelChoice,
    StreamEvent,
    make_tool_event,
)

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


class OpenCodeAdapter(BaseAgentAdapter):
    """Adapter for `opencode run --format json`."""

    def __init__(self) -> None:
        self.spec = AgentSpec(
            id="opencode",
            display_name="OpenCode",
            binary=shutil.which("opencode") or "/usr/bin/opencode",
            default_models=[
                "opencode/big-pickle",
                "opencode/deepseek-v4.1-flash-free",
                "opencode/glm-5.3-flash-free",
                "opencode/beam-free",
            ],
            supports_system_prompt=False,
        )
        self._last_part_text: dict[str, str] = {}

    def reset_state(self) -> None:
        self._last_part_text.clear()

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

    def build_command(
        self,
        prompt: str,
        *,
        model: str = "",
        session_id: str | None = None,
        cwd: str = "/home/dma",
        system_prompt: str = "",
        auto_approve: bool = True,
    ) -> tuple[list[str], str | None]:
        _ = cwd, system_prompt
        cmd = [self.resolved_binary(), "run", "--format", "json"]
        if model.strip():
            cmd.extend(["-m", model.strip()])
        if session_id:
            cmd.extend(["-s", session_id])
        if auto_approve:
            cmd.append("--auto")
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
                prev = self._last_part_text.get(part_id, "")
                if text != prev:
                    if prev and text.startswith(prev):
                        delta = text[len(prev):]
                        self._last_part_text[part_id] = text
                        if delta:
                            events.append(StreamEvent(kind="delta", text=delta))
                    else:
                        self._last_part_text[part_id] = text
                        events.append(StreamEvent(kind="delta", text=text))
            return events

        if ev_type in ("tool_use", "tool") or part_type == "tool":
            tool_name = str(part.get("tool") or part.get("name") or data.get("tool") or data.get("name") or "tool")
            payload: Any = part if part else data
            events.append(make_tool_event(tool_name, payload))
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
