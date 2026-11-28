"""Adapter for OpenAI Codex CLI (`codex`)."""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Sequence

from sisters_mascot.agents.base import (
    AgentSpec,
    BaseAgentAdapter,
    ModelChoice,
    StreamEvent,
    make_tool_event,
    make_tool_result_event,
    summarize_tool_payload,
)
from sisters_mascot.attachments import (
    AttachmentItem,
    collect_attachment_dirs,
    collect_image_paths,
)

_CONFIG_MODEL_RE = re.compile(r'^\s*model\s*=\s*["\']([^"\']+)["\']')
_VALID_CODEX_EFFORTS = ("none", "minimal", "low", "medium", "high", "xhigh", "max", "ultra")
_DEFAULT_CODEX_EFFORTS = ["low", "medium", "high", "xhigh", "max", "ultra"]


class CodexAdapter(BaseAgentAdapter):
    """Adapter for `codex exec --json` and `codex exec resume --json`."""

    def __init__(self) -> None:
        self.spec = AgentSpec(
            id="codex",
            display_name="Codex CLI",
            binary=shutil.which("codex") or "codex",
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

    def supported_efforts(self, model: str = "") -> list[str]:
        target_slug = (model or "").strip()
        cache_path = Path.home() / ".codex" / "models_cache.json"
        if target_slug and cache_path.is_file():
            try:
                data = json.loads(cache_path.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError):
                data = None
            raw_models = data.get("models") if isinstance(data, dict) else None
            if isinstance(raw_models, list):
                for entry in raw_models:
                    if not isinstance(entry, dict):
                        continue
                    if str(entry.get("slug") or "").strip() != target_slug:
                        continue
                    levels = entry.get("supported_reasoning_levels")
                    if isinstance(levels, list):
                        found = [
                            str(lv.get("effort") or "").strip().lower()
                            for lv in levels
                            if isinstance(lv, dict)
                            and str(lv.get("effort") or "").strip().lower() in _VALID_CODEX_EFFORTS
                        ]
                        if found:
                            return found
        return list(_DEFAULT_CODEX_EFFORTS)

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
        _ = system_prompt
        binary = self.resolved_binary()
        cleaned_model = model.strip()
        cleaned_effort = (effort or "").strip().lower()
        if cleaned_effort not in _VALID_CODEX_EFFORTS:
            cleaned_effort = ""
        effective_cwd = cwd.strip() or str(Path.home())
        images = collect_image_paths(attachments)

        if session_id:
            cmd = [binary, "exec", "resume", "--json", "--skip-git-repo-check"]
            if cleaned_model:
                cmd.extend(["-m", cleaned_model])
            if cleaned_effort:
                cmd.extend(["-c", f'model_reasoning_effort="{cleaned_effort}"'])
            for img_path in images:
                cmd.append(f"--image={img_path}")
            cmd.extend([session_id, "-"])
            return cmd, prompt

        cmd = [binary, "exec", "--json", "--skip-git-repo-check", "-C", effective_cwd]
        if cleaned_model:
            cmd.extend(["-m", cleaned_model])
        if cleaned_effort:
            cmd.extend(["-c", f'model_reasoning_effort="{cleaned_effort}"'])
        if auto_approve:
            cmd.append("--approve-for-me")
        for extra_dir in collect_attachment_dirs(attachments, effective_cwd):
            cmd.extend(["--add-dir", extra_dir])
        for img_path in images:
            cmd.append(f"--image={img_path}")
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
                tool_name = str(item.get("tool") or item.get("name") or "mcp_tool")
                if ev_type in ("item.started", "item.updated"):
                    events.append(make_tool_event(tool_name, item))
                elif ev_type == "item.completed":
                    err_obj = item.get("error")
                    is_err = bool(err_obj) or str(item.get("status") or "").lower() == "failed"
                    events.append(
                        make_tool_result_event(tool_name, is_error=is_err, detail=err_obj or "")
                    )
            elif item_type == "command_execution":
                cmd_snip = summarize_tool_payload("command_execution", item)
                if ev_type == "item.started":
                    events.append(
                        StreamEvent(
                            kind="tool",
                            tool_name="command_execution",
                            text="💻 コマンド実行中...",
                            detail=cmd_snip,
                            level="tool",
                        )
                    )
                elif ev_type == "item.completed":
                    exit_code = item.get("exit_code")
                    is_err = exit_code is not None and exit_code != 0
                    detail = (
                        f"exit={exit_code} {cmd_snip}".strip()
                        if exit_code is not None
                        else cmd_snip
                    )
                    events.append(
                        make_tool_result_event("command_execution", is_error=is_err, detail=detail)
                    )
            elif item_type == "reasoning" and ev_type == "item.completed":
                r_text = " ".join(str(item.get("text") or "").split()).strip()
                if r_text:
                    if len(r_text) > 90:
                        r_text = r_text[:89] + "…"
                    events.append(StreamEvent(kind="log", level="info", text=f"💭 思考: {r_text}"))
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
