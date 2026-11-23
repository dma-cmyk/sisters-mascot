"""Adapter for OpenAI Codex CLI (`codex`)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from sisters_mascot.agents.base import AgentSpec, BaseAgentAdapter, StreamEvent, make_tool_event

_CONFIG_MODEL_RE = re.compile(r'^\s*model\s*=\s*["\']([^"\']+)["\']')


class CodexAdapter(BaseAgentAdapter):
    """Adapter for `codex exec --json` and `codex exec resume --json`."""

    def __init__(self) -> None:
        self.spec = AgentSpec(
            id="codex",
            display_name="Codex CLI",
            binary=shutil.which("codex") or "/usr/bin/codex",
            default_models=[
                "gpt-5.4",
                "gpt-5.3-codex",
                "gpt-5.2-codex",
                "gpt-5.1-codex-mini",
                "o3",
                "o4-mini",
            ],
            supports_system_prompt=False,
        )
        self._item_texts: dict[str, str] = {}

    def reset_state(self) -> None:
        self._item_texts.clear()

    def discover_models(self) -> list[str]:
        curated = [
            "gpt-5.4",
            "gpt-5.3-codex",
            "gpt-5.2-codex",
            "gpt-5.1-codex-mini",
            "o3",
            "o4-mini",
        ]
        found: list[str] = []
        cfg_path = Path.home() / ".codex" / "config.toml"
        if cfg_path.is_file():
            try:
                for line in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    match = _CONFIG_MODEL_RE.match(line)
                    if match:
                        found.append(match.group(1).strip())
            except OSError:
                pass

        seen: set[str] = set()
        merged: list[str] = []
        for model in [*found, *curated]:
            if model and model not in seen:
                seen.add(model)
                merged.append(model)
        return merged

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
        _ = system_prompt
        binary = self.resolved_binary()
        cleaned_model = model.strip()

        if session_id:
            cmd = [binary, "exec", "resume", "--json", "--skip-git-repo-check"]
            if cleaned_model:
                cmd.extend(["-m", cleaned_model])
            cmd.extend([session_id, "-"])
            return cmd, prompt

        cmd = [binary, "exec", "--json", "--skip-git-repo-check", "-C", cwd]
        if cleaned_model:
            cmd.extend(["-m", cleaned_model])
        if auto_approve:
            cmd.append("--approve-for-me")
        cmd.append("-")
        return cmd, prompt

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

        if ev_type == "thread.started":
            tid = data.get("thread_id")
            if isinstance(tid, str) and tid:
                events.append(StreamEvent(kind="session", session_id=tid))
            return events

        if ev_type in ("item.started", "item.updated", "item.completed"):
            item = data.get("item")
            if not isinstance(item, dict):
                return events
            item_type = item.get("type", "")
            item_id = str(item.get("id") or "__default__")

            if item_type == "agent_message":
                text = str(item.get("text") or "")
                prev = self._item_texts.get(item_id, "")
                if text and text != prev:
                    if text.startswith(prev):
                        delta = text[len(prev):]
                        self._item_texts[item_id] = text
                        if delta:
                            events.append(StreamEvent(kind="delta", text=delta))
                    else:
                        self._item_texts[item_id] = text
                        events.append(StreamEvent(kind="replace", text=text))
            elif item_type == "mcp_tool_call":
                if ev_type in ("item.started", "item.updated"):
                    tool_name = str(item.get("tool") or item.get("name") or "mcp_tool")
                    events.append(make_tool_event(tool_name, item))
            elif item_type == "command_execution":
                if ev_type == "item.started":
                    events.append(
                        StreamEvent(
                            kind="tool",
                            tool_name="command_execution",
                            text="💻 コマンド実行中...",
                        )
                    )
            return events

        if ev_type == "error":
            msg = str(data.get("message") or data.get("error") or "Codex CLI error")
            events.append(StreamEvent(kind="error", text=msg))
            return events

        if ev_type == "turn.failed":
            err = data.get("error")
            msg = err.get("message") if isinstance(err, dict) else (err or "Codex turn failed")
            events.append(StreamEvent(kind="error", text=str(msg)))
            return events

        if ev_type == "turn.completed":
            events.append(StreamEvent(kind="done"))
        return events
