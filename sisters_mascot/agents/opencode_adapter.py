"""Adapter for OpenCode CLI (`opencode`)."""

from __future__ import annotations

import base64
import http.client
import json
import re
import shutil
import subprocess
import urllib.parse
from pathlib import Path
from typing import Any, Sequence

from sisters_mascot.agents.base import (
    AgentSpec,
    BaseAgentAdapter,
    ModelChoice,
    StreamEvent,
    extract_effective_tool_name,
    lookup_catalog_reasoning,
    make_tool_event,
    make_tool_result_event,
)
from sisters_mascot.attachments import AttachmentItem, collect_file_paths

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")
_VALID_OPENCODE_EFFORTS = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
    "ultra",
    "thinking",
)
_DEFAULT_OPENCODE_EFFORTS = ["low", "medium", "high", "xhigh", "max"]


def _derive_opencode_variants_from_reasoning(
    reasoning: dict[str, Any] | None,
) -> tuple[list[str], str]:
    """Derive OpenCode variant IDs and default effort from a catalog `reasoning` block."""
    if not isinstance(reasoning, dict) or not reasoning.get("supported"):
        return [], ""

    default_val = ""
    raw_def = reasoning.get("default")
    if isinstance(raw_def, dict) and str(raw_def.get("type") or "").lower() == "effort":
        cand = str(raw_def.get("value") or "").strip().lower()
        if cand in _VALID_OPENCODE_EFFORTS:
            default_val = cand

    has_toggle = bool(reasoning.get("toggle"))
    raw_efforts = reasoning.get("effort")
    has_budget = isinstance(reasoning.get("budget"), dict)

    if isinstance(raw_efforts, list) and raw_efforts:
        effs: list[str] = []
        for item in raw_efforts:
            val = str(item or "").strip().lower()
            if val in _VALID_OPENCODE_EFFORTS and val not in effs:
                effs.append(val)
        if effs:
            if has_toggle and "none" not in effs:
                effs.insert(0, "none")
            return effs, (default_val if default_val in effs else "")

    if has_budget:
        effs = (["none"] if has_toggle else []) + ["high", "max"]
        return effs, (default_val if default_val in effs else "")

    if has_toggle:
        return ["none", "thinking"], (default_val if default_val in ("none", "thinking") else "")

    return [], ""


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
        self._server_variants: dict[str, list[str]] = {}
        self._server_defaults: dict[str, str] = {}

    def reset_state(self) -> None:
        self._last_part_text.clear()
        self._emitted_part_ids.clear()
        self._seen_tool_states.clear()
        self._seen_reasoning.clear()

    def _probe_server_models(
        self,
        service_path: Path | None = None,
    ) -> tuple[list[ModelChoice], dict[str, list[str]], dict[str, str]]:
        """Query local OpenCode HTTP `/api/model` if running to obtain exact per-model variants."""
        target = service_path or (Path.home() / ".local" / "state" / "opencode" / "service.json")
        if not target.is_file():
            return [], {}, {}
        try:
            svc = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return [], {}, {}
        if not isinstance(svc, dict):
            return [], {}, {}
        base_url = str(svc.get("url") or "").strip().rstrip("/")
        password = str(svc.get("password") or "")
        if not base_url:
            return [], {}, {}

        parsed = urllib.parse.urlsplit(base_url)
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or (443 if parsed.scheme == "https" else 80)
        conn: http.client.HTTPConnection | None = None
        try:
            conn = (
                http.client.HTTPSConnection(host, port, timeout=1.5)
                if parsed.scheme == "https"
                else http.client.HTTPConnection(host, port, timeout=1.5)
            )
            headers = {
                "Accept": "application/json",
                "x-opencode-directory": urllib.parse.quote(str(Path.home()), safe=""),
            }
            if password:
                tok = base64.b64encode(f"opencode:{password}".encode("utf-8")).decode("ascii")
                headers["Authorization"] = f"Basic {tok}"
            conn.request("GET", "/api/model", headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            if resp.status != 200 or not raw:
                return [], {}, {}
            payload = json.loads(raw.decode("utf-8", errors="replace"))
        except (OSError, http.client.HTTPException, json.JSONDecodeError, ValueError):
            return [], {}, {}
        finally:
            if conn is not None:
                try:
                    conn.close()
                except OSError:
                    pass

        entries: list[dict[str, Any]] = []
        if isinstance(payload, list):
            entries = [e for e in payload if isinstance(e, dict)]
        elif isinstance(payload, dict):
            if isinstance(payload.get("models"), list):
                entries = [e for e in payload["models"] if isinstance(e, dict)]
            elif isinstance(payload.get("data"), list):
                entries = [e for e in payload["data"] if isinstance(e, dict)]
            else:
                for prov_key, prov_val in payload.items():
                    if isinstance(prov_val, dict) and isinstance(prov_val.get("models"), dict):
                        for mid, mobj in prov_val["models"].items():
                            if isinstance(mobj, dict):
                                entries.append({"providerID": prov_key, "id": mid, **mobj})

        choices: list[ModelChoice] = []
        variants_map: dict[str, list[str]] = {}
        defaults_map: dict[str, str] = {}
        seen_ids: set[str] = set()

        for entry in entries:
            pid = str(entry.get("providerID") or "").strip()
            mid = str(entry.get("id") or entry.get("modelID") or "").strip()
            if not pid and "/" in mid:
                pid, mid = mid.split("/", 1)
            if not pid or not mid:
                continue
            full_id = f"{pid}/{mid}"
            raw_vars = entry.get("variants")
            parsed_vars: list[str] = []
            if isinstance(raw_vars, list):
                for v in raw_vars:
                    vid = str(v.get("id") if isinstance(v, dict) else v or "").strip().lower()
                    if vid in _VALID_OPENCODE_EFFORTS and vid not in parsed_vars:
                        parsed_vars.append(vid)
            elif isinstance(raw_vars, dict):
                for vk in raw_vars.keys():
                    vid = str(vk or "").strip().lower()
                    if vid in _VALID_OPENCODE_EFFORTS and vid not in parsed_vars:
                        parsed_vars.append(vid)

            def_hint = str(entry.get("defaultVariant") or "").strip().lower()
            if not def_hint:
                cap = entry.get("capabilities")
                r_cap = cap.get("reasoning") if isinstance(cap, dict) else None
                _, def_hint = _derive_opencode_variants_from_reasoning(r_cap)
            if def_hint not in parsed_vars:
                def_hint = ""

            variants_map[full_id] = parsed_vars
            if def_hint:
                defaults_map[full_id] = def_hint
            if full_id not in seen_ids:
                seen_ids.add(full_id)
                disp = str(entry.get("name") or "").strip()
                lbl = f"{disp} ({full_id})" if disp and disp != full_id else full_id
                choices.append(ModelChoice(id=full_id, label=lbl))

        return choices, variants_map, defaults_map

    def discover_model_choices(self) -> list[ModelChoice]:
        srv_choices, srv_vars, srv_defs = self._probe_server_models()
        if srv_vars:
            self._server_variants = srv_vars
            self._server_defaults = srv_defs

        fallback = [ModelChoice(id=m, label=m) for m in self.spec.default_models]
        if not self.is_available():
            return srv_choices if srv_choices else fallback

        try:
            proc = subprocess.run(
                [self.resolved_binary(), "models"],
                capture_output=True,
                text=True,
                timeout=12,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            return srv_choices if srv_choices else fallback

        if proc.returncode != 0 or not proc.stdout.strip():
            return srv_choices if srv_choices else fallback

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

        return discovered if discovered else (srv_choices if srv_choices else fallback)

    def discover_models(self) -> list[str]:
        return [choice.id for choice in self.discover_model_choices()]

    def _resolve_efforts_and_default(self, model: str) -> tuple[list[str], str]:
        cleaned = (model or "").strip()
        if not cleaned or "/" not in cleaned:
            return [], ""
        provider_id, rest = cleaned.split("/", 1)
        model_id = rest.split("#", 1)[0].strip()
        if not provider_id or not model_id:
            return [], ""
        base_key = f"{provider_id}/{model_id}"
        if base_key in self._server_variants:
            return list(self._server_variants[base_key]), self._server_defaults.get(base_key, "")
        found, reasoning = lookup_catalog_reasoning(provider_id, model_id)
        if found:
            return _derive_opencode_variants_from_reasoning(reasoning)
        return list(_DEFAULT_OPENCODE_EFFORTS), ""

    def supported_efforts(self, model: str = "") -> list[str]:
        efforts, _ = self._resolve_efforts_and_default(model)
        return efforts

    def default_effort_hint(self, model: str = "") -> str:
        _, default_hint = self._resolve_efforts_and_default(model)
        return default_hint

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
        safe_effort = self.sanitize_effort(model, effort)
        formatted_model = format_model_with_effort(model, safe_effort)
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
