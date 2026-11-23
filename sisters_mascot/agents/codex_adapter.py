"""Adapter for OpenAI Codex CLI (`codex`)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path

from sisters_mascot.agents.base import (
    AgentSpec,
    BaseAgentAdapter,
    ModelChoice,
    StreamEvent,
    make_tool_event,
)

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

    def discover_model_choices(self) -> list[ModelChoice]:
        configured_models: list[str] = []
        cfg_path = Path.home() / ".codex" / "config.toml"
        if cfg_path.is_file():
            try:
                for line in cfg_path.read_text(encoding="utf-8", errors="replace").splitlines():
                    match = _CONFIG_MODEL_RE.match(line)
                    if match:
                        val = match.group(1).strip()
                        if val:
                            configured_models.append(val)
            except OSError:
                pass

        visible_entries: list[tuple[int, int, str, str]] = []
        by_slug: dict[str, str] = {}
        cache_path = Path.home() / ".codex" / "models_cache.json"
        if cache_path.is_file():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                data = None
            raw_models = data.get("models") if isinstance(data, dict) else None
            if isinstance(raw_models, list):
                for idx, entry in enumerate(raw_models):
                    if not isinstance(entry, dict):
                        continue
                    slug = str(entry.get("slug") or "").strip()
                    if not slug:
                        continue
                    display_name = str(entry.get("display_name") or "").strip()
                    by_slug[slug] = display_name
                    visibility = str(entry.get("visibility") or "list").strip().lower()
                    if visibility == "hide":
                        continue
                    raw_prio = entry.get("priority", 9999)
                    try:
                        prio = int(raw_prio)
                    except (TypeError, ValueError):
                        prio = 9999
                    visible_entries.append((prio, idx, slug, display_name))

        visible_entries.sort(key=lambda item: (item[0], item[1]))

        choices: list[ModelChoice] = []
        seen: set[str] = set()

        def _fmt_label(slug: str, display_name: str) -> str:
            if display_name and display_name != slug:
                return f"{display_name} ({slug})"
            return slug

        for model_id in configured_models:
            if model_id in seen:
                continue
            seen.add(model_id)
            choices.append(ModelChoice(id=model_id, label=_fmt_label(model_id, by_slug.get(model_id, ""))))

        for _prio, _idx, slug, display_name in visible_entries:
            if slug in seen:
                continue
            seen.add(slug)
            choices.append(ModelChoice(id=slug, label=_fmt_label(slug, display_name)))

        if choices:
            return choices
        return [ModelChoice(id=m, label=m) for m in self.spec.default_models]

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
