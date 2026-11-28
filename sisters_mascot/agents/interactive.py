"""Unified interactive approval, form, and question runners across all 4 CLIs."""

from __future__ import annotations

import base64
import http.client
import json
import queue
import ssl
import subprocess
import threading
import time
import urllib.parse
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Sequence

from sisters_mascot.agents.agy_adapter import AgyAdapter, resolve_model_and_effort
from sisters_mascot.agents.base import (
    StreamEvent,
    extract_effective_tool_name,
    make_tool_event,
    make_tool_result_event,
    summarize_tool_payload,
)
from sisters_mascot.agents.claude_adapter import ClaudeAdapter
from sisters_mascot.agents.codex_adapter import CodexAdapter
from sisters_mascot.agents.interaction import (
    InteractionAction,
    InteractionCallback,
    InteractionField,
    InteractionOption,
    InteractionRequest,
    InteractionResponse,
)
from sisters_mascot.attachments import AttachmentItem, collect_attachment_dirs, collect_image_paths


@dataclass
class InteractiveTurnOutcome:
    """Result returned by an interactive runner to `AgentRouter`."""

    handled: bool
    text: str = ""
    session_id: str | None = None
    returncode: int = 0
    cancelled: bool = False
    error: str = ""
    fallback_needed: bool = False


SteerHandler = Callable[[str, Sequence[AttachmentItem]], bool]


def _format_detail_json(obj: Any, max_chars: int = 800) -> str:
    if obj is None or obj == {} or obj == []:
        return ""
    if isinstance(obj, str):
        text = obj.strip()
    else:
        try:
            text = json.dumps(obj, ensure_ascii=False, indent=2)
        except (TypeError, ValueError):
            text = str(obj)
    if len(text) > max_chars:
        return text[: max_chars - 1] + "…"
    return text


def _build_elicitation_fields(schema: Any) -> list[InteractionField]:
    """Convert JSON Schema `requestedSchema` (MCP/ACP elicitation) to `InteractionField`s."""
    if not isinstance(schema, dict):
        return []
    props = schema.get("properties")
    if not isinstance(props, dict):
        return []
    req_set = set(str(k) for k in (schema.get("required") or []) if isinstance(k, str))
    fields: list[InteractionField] = []

    for key, raw_prop in props.items():
        if not isinstance(raw_prop, dict):
            continue
        p_type = str(raw_prop.get("type") or "string").lower()
        title = str(raw_prop.get("title") or key)
        desc = str(raw_prop.get("description") or "")
        default = raw_prop.get("default")
        required = key in req_set

        enum_vals = raw_prop.get("enum")
        one_of = raw_prop.get("oneOf")
        if isinstance(enum_vals, list) and enum_vals:
            names = raw_prop.get("enumNames") if isinstance(raw_prop.get("enumNames"), list) else []
            opts = [
                InteractionOption(
                    value=str(val),
                    label=str(names[idx]) if idx < len(names) else str(val),
                )
                for idx, val in enumerate(enum_vals)
            ]
            fields.append(
                InteractionField(
                    key=key,
                    title=title,
                    description=desc,
                    field_type="choice",
                    required=required,
                    options=opts,
                    default=default,
                )
            )
            continue

        if isinstance(one_of, list) and one_of:
            opts = []
            for item in one_of:
                if not isinstance(item, dict):
                    continue
                val = item.get("const")
                if val is None and isinstance(item.get("enum"), list) and item["enum"]:
                    val = item["enum"][0]
                if val is not None:
                    opts.append(
                        InteractionOption(
                            value=str(val),
                            label=str(item.get("title") or val),
                            description=str(item.get("description") or ""),
                        )
                    )
            if opts:
                fields.append(
                    InteractionField(
                        key=key,
                        title=title,
                        description=desc,
                        field_type="choice",
                        required=required,
                        options=opts,
                        default=default,
                    )
                )
                continue

        if p_type == "array":
            items_obj = raw_prop.get("items") if isinstance(raw_prop.get("items"), dict) else {}
            item_enum = items_obj.get("enum") if isinstance(items_obj.get("enum"), list) else []
            any_of = items_obj.get("anyOf") if isinstance(items_obj.get("anyOf"), list) else []
            opts = [InteractionOption(value=str(v), label=str(v)) for v in item_enum]
            if not opts and any_of:
                for entry in any_of:
                    if isinstance(entry, dict) and entry.get("const") is not None:
                        opts.append(
                            InteractionOption(
                                value=str(entry["const"]),
                                label=str(entry.get("title") or entry["const"]),
                            )
                        )
            fields.append(
                InteractionField(
                    key=key,
                    title=title,
                    description=desc,
                    field_type="multiselect" if opts else "text",
                    required=required,
                    options=opts,
                    default=default,
                )
            )
        elif p_type == "boolean":
            fields.append(
                InteractionField(
                    key=key,
                    title=title,
                    description=desc,
                    field_type="boolean",
                    required=required,
                    default=bool(default) if default is not None else False,
                )
            )
        elif p_type in ("number", "integer"):
            fields.append(
                InteractionField(
                    key=key,
                    title=title,
                    description=desc,
                    field_type="number",
                    required=required,
                    default=default,
                )
            )
        else:
            fields.append(
                InteractionField(
                    key=key,
                    title=title,
                    description=desc,
                    field_type="text",
                    required=required,
                    default=default,
                )
            )
    return fields


def _coerce_elicitation_answers(
    schema: Any, raw_answers: dict[str, Any]
) -> dict[str, Any]:
    """Ensure boolean/number types match JSON Schema for elicitation replies."""
    props = schema.get("properties") if isinstance(schema, dict) else {}
    if not isinstance(props, dict):
        return dict(raw_answers)
    coerced: dict[str, Any] = {}
    for key, val in raw_answers.items():
        p_type = str((props.get(key) or {}).get("type") or "string").lower()
        if p_type == "integer":
            try:
                coerced[key] = int(val)
            except (TypeError, ValueError):
                coerced[key] = val
        elif p_type == "number":
            try:
                coerced[key] = float(val)
            except (TypeError, ValueError):
                coerced[key] = val
        elif p_type == "boolean":
            coerced[key] = bool(val)
        else:
            coerced[key] = val
    return coerced


def _write_json_line(proc: subprocess.Popen[str], payload: dict[str, Any]) -> bool:
    """Serialize `payload` as a compact JSON line to `proc.stdin`."""
    if proc.stdin is None:
        return False
    try:
        proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
        proc.stdin.flush()
        return True
    except OSError:
        return False


class _LockedJsonStdin:
    """Thread-safe JSONL stdin writer supporting concurrent mid-turn steering."""

    def __init__(self, proc: subprocess.Popen[str]) -> None:
        self._proc = proc
        self._lock = threading.Lock()
        self._closed = False

    def write_json(self, payload: dict[str, Any]) -> bool:
        with self._lock:
            if self._closed or self._proc.stdin is None:
                return False
            try:
                self._proc.stdin.write(json.dumps(payload, ensure_ascii=False) + "\n")
                self._proc.stdin.flush()
                return True
            except (OSError, ValueError):
                return False

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._proc.stdin is not None:
                try:
                    self._proc.stdin.close()
                except OSError:
                    pass


def _build_jsonrpc_reader(
    proc: subprocess.Popen[str],
    msg_queue: queue.Queue[dict[str, Any] | None],
    on_raw_line: Callable[[str], None] | None = None,
) -> threading.Thread:
    """Start a daemon thread parsing JSONL messages from `proc.stdout`."""

    def _reader() -> None:
        try:
            if proc.stdout is None:
                return
            for line in proc.stdout:
                stripped = line.strip()
                if not stripped:
                    continue
                if stripped.startswith("{"):
                    try:
                        parsed = json.loads(stripped)
                        if isinstance(parsed, dict):
                            msg_queue.put(parsed)
                            continue
                    except json.JSONDecodeError:
                        pass
                if on_raw_line is not None:
                    on_raw_line(stripped)
        finally:
            msg_queue.put(None)

    t = threading.Thread(target=_reader, daemon=True)
    t.start()
    return t


def _start_stderr_reader(
    proc: subprocess.Popen[str],
    stderr_lines: list[str],
    on_stderr_line: Callable[[str], None],
) -> threading.Thread:
    """Drain `proc.stderr` in a daemon thread."""

    def _read() -> None:
        if proc.stderr is None:
            return
        for raw in proc.stderr:
            cleaned = raw.strip()
            if cleaned:
                stderr_lines.append(cleaned)
                if len(stderr_lines) > 40:
                    stderr_lines.pop(0)
                on_stderr_line(cleaned)

    t = threading.Thread(target=_read, daemon=True)
    t.start()
    return t


# ---------------------------------------------------------------------------
# 1. OpenCode HTTP + SSE Runner
# ---------------------------------------------------------------------------


def _load_opencode_service_info(
    service_path: Path | None = None,
) -> tuple[str, str] | None:
    target = service_path or (Path.home() / ".local" / "state" / "opencode" / "service.json")
    if not target.is_file():
        return None
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    url = str(data.get("url") or "").strip().rstrip("/")
    password = str(data.get("password") or "")
    if not url:
        return None
    return url, password


class OpenCodeInteractiveRunner:
    """Runs OpenCode turns via its local HTTP + SSE API so forms & permissions work live."""

    def __init__(self, service_path: Path | None = None) -> None:
        self._service_path = service_path

    @staticmethod
    def _make_connection(
        parsed_url: urllib.parse.SplitResult, timeout: float | None
    ) -> http.client.HTTPConnection:
        host = parsed_url.hostname or "127.0.0.1"
        if parsed_url.scheme == "https":
            port = parsed_url.port or 443
            return http.client.HTTPSConnection(
                host,
                port,
                timeout=timeout,
                context=ssl.create_default_context(),
            )
        port = parsed_url.port or 80
        return http.client.HTTPConnection(host, port, timeout=timeout)

    @staticmethod
    def _build_headers(password: str, cwd: str, *, accept: str = "application/json") -> dict[str, str]:
        headers = {
            "Accept": accept,
            "x-opencode-directory": urllib.parse.quote(cwd, safe=""),
        }
        if password:
            token = base64.b64encode(f"opencode:{password}".encode("utf-8")).decode("ascii")
            headers["Authorization"] = f"Basic {token}"
        return headers

    def _request_json(
        self,
        parsed_url: urllib.parse.SplitResult,
        password: str,
        cwd: str,
        method: str,
        path: str,
        payload: dict[str, Any] | None = None,
        *,
        timeout: float = 10.0,
    ) -> tuple[int, Any]:
        conn = self._make_connection(parsed_url, timeout=timeout)
        headers = self._build_headers(password, cwd)
        body_bytes: bytes | None = None
        if payload is not None:
            headers["Content-Type"] = "application/json"
            body_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        try:
            conn.request(method, path, body=body_bytes, headers=headers)
            resp = conn.getresponse()
            raw = resp.read()
            status = resp.status
            if not raw:
                return status, None
            text = raw.decode("utf-8", errors="replace")
            try:
                return status, json.loads(text)
            except json.JSONDecodeError:
                return status, text
        finally:
            conn.close()

    @staticmethod
    def _extract_error_message(err_obj: Any) -> str:
        if isinstance(err_obj, dict):
            data = err_obj.get("data")
            if isinstance(data, dict) and data.get("message"):
                return str(data["message"])
            if err_obj.get("message"):
                return str(err_obj["message"])
            return json.dumps(err_obj, ensure_ascii=False)
        return str(err_obj or "OpenCode CLI error")

    @staticmethod
    def _build_form_fields(raw_fields: Any) -> list[InteractionField]:
        fields: list[InteractionField] = []
        if not isinstance(raw_fields, list):
            return fields
        for raw_f in raw_fields:
            if not isinstance(raw_f, dict):
                continue
            key = str(raw_f.get("key") or "").strip()
            if not key:
                continue
            ftype_raw = str(raw_f.get("type") or "string").lower()
            title = str(raw_f.get("title") or key)
            desc = str(raw_f.get("description") or "")
            required = bool(raw_f.get("required", True))
            allow_custom = bool(raw_f.get("custom", False))
            default = raw_f.get("default")
            placeholder = str(raw_f.get("placeholder") or "")
            url = str(raw_f.get("url") or "")
            raw_opts = raw_f.get("options")
            opts: list[InteractionOption] = []
            if isinstance(raw_opts, list):
                for opt in raw_opts:
                    if isinstance(opt, dict):
                        opts.append(
                            InteractionOption(
                                value=str(opt.get("value") or ""),
                                label=str(opt.get("label") or opt.get("value") or ""),
                                description=str(opt.get("description") or ""),
                            )
                        )
            if ftype_raw == "external":
                mapped_type = "external"
            elif ftype_raw == "multiselect":
                mapped_type = "multiselect"
            elif ftype_raw == "boolean":
                mapped_type = "boolean"
            elif ftype_raw in ("number", "integer"):
                mapped_type = "number"
            elif opts:
                mapped_type = "choice"
            else:
                mapped_type = "text"

            fields.append(
                InteractionField(
                    key=key,
                    title=title,
                    description=desc,
                    field_type=mapped_type,
                    required=required,
                    options=opts,
                    allow_custom=allow_custom,
                    default=default,
                    url=url,
                    placeholder=placeholder,
                )
            )
        return fields

    def _handle_form(
        self,
        form_obj: dict[str, Any],
        *,
        parsed_url: urllib.parse.SplitResult,
        password: str,
        cwd: str,
        sister_name: str,
        on_interaction: InteractionCallback,
        handled_forms: set[str],
    ) -> None:
        form_id = str(form_obj.get("id") or "").strip()
        fsid = str(form_obj.get("sessionID") or "").strip()
        if not form_id or not fsid or form_id in handled_forms:
            return
        handled_forms.add(form_id)

        meta = form_obj.get("metadata") if isinstance(form_obj.get("metadata"), dict) else {}
        kind = "question" if meta.get("kind") == "question" else "form"
        title = str(form_obj.get("title") or "OpenCode からの確認")
        desc = str(meta.get("message") or "")
        fields = self._build_form_fields(form_obj.get("fields"))

        req = InteractionRequest(
            agent_id="opencode",
            sister_name=sister_name,
            kind=kind,
            title=title,
            description=desc,
            fields=fields,
        )
        resp = on_interaction(req)
        enc_sid = urllib.parse.quote(fsid, safe="")
        enc_fid = urllib.parse.quote(form_id, safe="")
        try:
            if resp.cancelled:
                self._request_json(
                    parsed_url,
                    password,
                    cwd,
                    "DELETE",
                    f"/api/session/{enc_sid}/form/{enc_fid}",
                    timeout=5.0,
                )
            else:
                self._request_json(
                    parsed_url,
                    password,
                    cwd,
                    "POST",
                    f"/api/session/{enc_sid}/form/{enc_fid}/reply",
                    {"answer": resp.answers},
                    timeout=5.0,
                )
        except OSError:
            pass

    def _handle_permission(
        self,
        perm_obj: dict[str, Any],
        *,
        parsed_url: urllib.parse.SplitResult,
        password: str,
        cwd: str,
        auto_approve: bool,
        sister_name: str,
        on_interaction: InteractionCallback,
        handled_perms: set[str],
    ) -> None:
        req_id = str(perm_obj.get("id") or "").strip()
        psid = str(perm_obj.get("sessionID") or "").strip()
        if not req_id or not psid or req_id in handled_perms:
            return
        handled_perms.add(req_id)

        enc_sid = urllib.parse.quote(psid, safe="")
        enc_rid = urllib.parse.quote(req_id, safe="")
        if auto_approve:
            try:
                self._request_json(
                    parsed_url,
                    password,
                    cwd,
                    "POST",
                    f"/api/session/{enc_sid}/permission/{enc_rid}/reply",
                    {"decision": "once"},
                    timeout=5.0,
                )
            except OSError:
                pass
            return

        action = str(perm_obj.get("action") or "permission")
        resources = perm_obj.get("resources")
        res_list = [str(r) for r in resources] if isinstance(resources, list) else []
        msg_hint = str(perm_obj.get("message") or "").strip()
        detail = "\n".join(res_list) if res_list else _format_detail_json(perm_obj.get("metadata"))

        req = InteractionRequest(
            agent_id="opencode",
            sister_name=sister_name,
            kind="permission",
            title=f"実行許可: {action}",
            description=msg_hint or f"OpenCode が `{action}` の実行許可を求めています。",
            tool_name=action,
            detail=detail,
            actions=[
                InteractionAction("once", "✅ 1回許可", "success"),
                InteractionAction("always", "🔓 常に許可", "primary"),
                InteractionAction("reject", "❌ 拒否", "danger"),
            ],
        )
        resp = on_interaction(req)
        decision = "reject" if resp.cancelled else (resp.action_id or "reject")
        if decision not in ("once", "always", "reject"):
            decision = "reject"
        body: dict[str, Any] = {"decision": decision}
        if decision == "reject" and resp.message:
            body["message"] = resp.message
        try:
            self._request_json(
                parsed_url,
                password,
                cwd,
                "POST",
                f"/api/session/{enc_sid}/permission/{enc_rid}/reply",
                body,
                timeout=5.0,
            )
        except OSError:
            pass

    def run(
        self,
        prompt: str,
        *,
        model: str = "",
        effort: str = "",
        session_id: str | None = None,
        cwd: str = "",
        auto_approve: bool = True,
        sister_name: str = "",
        cancel_event: threading.Event | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_event: Callable[[StreamEvent], None],
        on_interaction: InteractionCallback,
        on_log: Callable[[str, str], None],
        set_steer_handler: Callable[[SteerHandler | None], None] | None = None,
    ) -> InteractiveTurnOutcome:
        svc = _load_opencode_service_info(self._service_path)
        if svc is None:
            return InteractiveTurnOutcome(handled=False, fallback_needed=True)

        base_url, password = svc
        parsed_url = urllib.parse.urlsplit(base_url)
        effective_cwd = cwd.strip() or str(Path.home())

        # Quick health probe before committing to interactive server mode.
        try:
            status, _info = self._request_json(
                parsed_url, password, effective_cwd, "GET", "/api/info", timeout=1.5
            )
            if status not in (200, 204):
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)
        except OSError:
            return InteractiveTurnOutcome(handled=False, fallback_needed=True)

        sse_conn = self._make_connection(parsed_url, timeout=None)
        event_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        sse_stop = threading.Event()
        try:
            sse_headers = self._build_headers(password, effective_cwd, accept="text/event-stream")
            sse_conn.request("GET", "/api/event", headers=sse_headers)
            sse_resp = sse_conn.getresponse()
            if sse_resp.status != 200:
                sse_conn.close()
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)
        except OSError:
            try:
                sse_conn.close()
            except OSError:
                pass
            return InteractiveTurnOutcome(handled=False, fallback_needed=True)

        def _sse_reader() -> None:
            data_lines: list[str] = []
            try:
                while not sse_stop.is_set():
                    raw_line = sse_resp.readline()
                    if not raw_line:
                        break
                    line = raw_line.decode("utf-8", errors="replace").rstrip("\r\n")
                    if not line:
                        if data_lines:
                            payload_str = "\n".join(data_lines)
                            data_lines.clear()
                            try:
                                parsed = json.loads(payload_str)
                                if isinstance(parsed, dict):
                                    event_queue.put(parsed)
                            except json.JSONDecodeError:
                                pass
                        continue
                    if line.startswith(":"):
                        continue
                    if line.startswith("data:"):
                        data_lines.append(line[5:].lstrip())
            except OSError:
                pass
            finally:
                event_queue.put(None)

        sse_thread = threading.Thread(target=_sse_reader, daemon=True)
        sse_thread.start()

        def _check_cancel() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            return bool(is_cancelled is not None and is_cancelled())

        sid = session_id or ""
        try:
            if sid:
                st_get, _ = self._request_json(
                    parsed_url,
                    password,
                    effective_cwd,
                    "GET",
                    f"/api/session/{urllib.parse.quote(sid, safe='')}",
                    timeout=5.0,
                )
                if st_get != 200:
                    sid = ""

            if not sid:
                st_create, sess_data = self._request_json(
                    parsed_url, password, effective_cwd, "POST", "/api/session", {}, timeout=5.0
                )
                if st_create != 200 or not isinstance(sess_data, dict):
                    sse_stop.set()
                    sse_conn.close()
                    return InteractiveTurnOutcome(handled=False, fallback_needed=True)
                sid = str(sess_data.get("id") or sess_data.get("sessionID") or "").strip()
                if not sid:
                    sse_stop.set()
                    sse_conn.close()
                    return InteractiveTurnOutcome(handled=False, fallback_needed=True)

            on_event(StreamEvent(kind="session", session_id=sid))
            enc_sid = urllib.parse.quote(sid, safe="")

            cleaned_model = model.strip()
            if cleaned_model and "/" in cleaned_model:
                provider_id, rest = cleaned_model.split("/", 1)
                base_model = rest.split("#", 1)[0].strip()
                inline_variant = rest.split("#", 1)[1].strip() if "#" in rest else ""
                variant = (effort or "").strip().lower() or inline_variant
                model_obj: dict[str, Any] = {"providerID": provider_id, "id": base_model}
                if variant:
                    model_obj["variant"] = variant
                st_mod, _ = self._request_json(
                    parsed_url,
                    password,
                    effective_cwd,
                    "POST",
                    f"/api/session/{enc_sid}/model",
                    {"model": model_obj},
                    timeout=5.0,
                )
                if st_mod >= 400 and variant:
                    # Retry without variant if the selected model rejects that effort level.
                    self._request_json(
                        parsed_url,
                        password,
                        effective_cwd,
                        "POST",
                        f"/api/session/{enc_sid}/model",
                        {"model": {"providerID": provider_id, "id": base_model}},
                        timeout=5.0,
                    )

            st_prompt, prompt_data = self._request_json(
                parsed_url,
                password,
                effective_cwd,
                "POST",
                f"/api/session/{enc_sid}/prompt",
                {"text": prompt, "delivery": "steer"},
                timeout=10.0,
            )
            if st_prompt >= 400:
                err_txt = self._extract_error_message(prompt_data)
                on_event(StreamEvent(kind="error", text=err_txt))
                return InteractiveTurnOutcome(
                    handled=True, session_id=sid, returncode=1, error=err_txt
                )

            def _steer(text: str, attachments: Sequence[AttachmentItem] = ()) -> bool:
                _ = attachments
                if sse_stop.is_set() or _check_cancel():
                    return False
                try:
                    st_s, _ = self._request_json(
                        parsed_url,
                        password,
                        effective_cwd,
                        "POST",
                        f"/api/session/{enc_sid}/prompt",
                        {"text": text, "delivery": "steer"},
                        timeout=8.0,
                    )
                    return 200 <= st_s < 300
                except OSError:
                    return False

            if set_steer_handler is not None:
                set_steer_handler(_steer)

            tracked_sessions: set[str] = {sid}
            handled_perms: set[str] = set()
            handled_forms: set[str] = set()
            text_buffers: dict[tuple[str, int], str] = {}
            emitted_parts: list[tuple[str, int]] = []
            seen_reasoning: set[tuple[str, int]] = set()
            tool_names: dict[tuple[str, str], str] = {}
            tool_started: set[tuple[str, str]] = set()
            tool_finished: set[tuple[str, str]] = set()
            full_text = ""
            saw_error = False
            error_msg = ""

            # Drain any permissions/forms created immediately upon prompt admission.
            for endpoint, handler in (
                (
                    f"/api/session/{enc_sid}/permission",
                    lambda item: self._handle_permission(
                        item,
                        parsed_url=parsed_url,
                        password=password,
                        cwd=effective_cwd,
                        auto_approve=auto_approve,
                        sister_name=sister_name,
                        on_interaction=on_interaction,
                        handled_perms=handled_perms,
                    ),
                ),
                (
                    f"/api/session/{enc_sid}/form",
                    lambda item: self._handle_form(
                        item,
                        parsed_url=parsed_url,
                        password=password,
                        cwd=effective_cwd,
                        sister_name=sister_name,
                        on_interaction=on_interaction,
                        handled_forms=handled_forms,
                    ),
                ),
            ):
                try:
                    st_chk, chk_data = self._request_json(
                        parsed_url, password, effective_cwd, "GET", endpoint, timeout=3.0
                    )
                    if st_chk == 200 and isinstance(chk_data, list):
                        for entry in chk_data:
                            if isinstance(entry, dict):
                                handler(entry)
                except OSError:
                    pass

            def _is_tracked(ev_sid: str) -> bool:
                if not ev_sid:
                    return False
                if ev_sid in tracked_sessions:
                    return True
                try:
                    st_s, s_info = self._request_json(
                        parsed_url,
                        password,
                        effective_cwd,
                        "GET",
                        f"/api/session/{urllib.parse.quote(ev_sid, safe='')}",
                        timeout=3.0,
                    )
                    if st_s == 200 and isinstance(s_info, dict):
                        pid = str(s_info.get("parentID") or "")
                        if pid in tracked_sessions:
                            tracked_sessions.add(ev_sid)
                            return True
                except OSError:
                    pass
                return False

            def _emit_delta_piece(msg_id: str, ordinal: int, delta_chunk: str) -> None:
                nonlocal full_text
                if not delta_chunk:
                    return
                key = (msg_id, ordinal)
                is_new = key not in text_buffers
                text_buffers[key] = text_buffers.get(key, "") + delta_chunk
                emit = (
                    f"\n\n{delta_chunk}"
                    if (is_new and emitted_parts and not delta_chunk.startswith("\n"))
                    else delta_chunk
                )
                if key not in emitted_parts:
                    emitted_parts.append(key)
                full_text += emit
                on_event(StreamEvent(kind="delta", text=emit))

            while True:
                if _check_cancel():
                    try:
                        self._request_json(
                            parsed_url,
                            password,
                            effective_cwd,
                            "POST",
                            f"/api/session/{enc_sid}/interrupt",
                            {},
                            timeout=3.0,
                        )
                    except OSError:
                        pass
                    return InteractiveTurnOutcome(
                        handled=True,
                        text=full_text.strip(),
                        session_id=sid,
                        returncode=0,
                        cancelled=True,
                    )

                try:
                    ev = event_queue.get(timeout=0.12)
                except queue.Empty:
                    continue

                if ev is None:
                    break

                ev_type = str(ev.get("type") or "")
                data = ev.get("data") if isinstance(ev.get("data"), dict) else {}

                if ev_type == "session.created":
                    info = data.get("info") if isinstance(data.get("info"), dict) else data
                    child_id = str(info.get("id") or data.get("sessionID") or "")
                    parent_id = str(info.get("parentID") or "")
                    if child_id and parent_id in tracked_sessions:
                        tracked_sessions.add(child_id)
                    continue

                if ev_type == "permission.asked":
                    psid = str(data.get("sessionID") or "")
                    if _is_tracked(psid):
                        self._handle_permission(
                            data,
                            parsed_url=parsed_url,
                            password=password,
                            cwd=effective_cwd,
                            auto_approve=auto_approve,
                            sister_name=sister_name,
                            on_interaction=on_interaction,
                            handled_perms=handled_perms,
                        )
                    continue

                if ev_type == "form.created":
                    form_obj = data.get("form") if isinstance(data.get("form"), dict) else data
                    fsid = str(form_obj.get("sessionID") or "")
                    loc = ev.get("location") if isinstance(ev.get("location"), dict) else {}
                    loc_dir = str(loc.get("directory") or "")
                    if _is_tracked(fsid) or (fsid == "global" and loc_dir == effective_cwd):
                        self._handle_form(
                            form_obj,
                            parsed_url=parsed_url,
                            password=password,
                            cwd=effective_cwd,
                            sister_name=sister_name,
                            on_interaction=on_interaction,
                            handled_forms=handled_forms,
                        )
                    continue

                ev_sid = str(data.get("sessionID") or "")
                if ev_sid != sid:
                    continue

                if ev_type == "session.text.started":
                    msg_id = str(data.get("assistantMessageID") or "")
                    ordinal = int(data.get("ordinal") or 0)
                    text_buffers.setdefault((msg_id, ordinal), "")
                elif ev_type == "session.text.delta":
                    msg_id = str(data.get("assistantMessageID") or "")
                    ordinal = int(data.get("ordinal") or 0)
                    chunk = str(data.get("delta") or "")
                    _emit_delta_piece(msg_id, ordinal, chunk)
                elif ev_type == "session.text.ended":
                    msg_id = str(data.get("assistantMessageID") or "")
                    ordinal = int(data.get("ordinal") or 0)
                    final_part = str(data.get("text") or "")
                    prev_buf = text_buffers.get((msg_id, ordinal), "")
                    if final_part:
                        if prev_buf and final_part.startswith(prev_buf):
                            rem = final_part[len(prev_buf) :]
                            _emit_delta_piece(msg_id, ordinal, rem)
                        elif not prev_buf:
                            _emit_delta_piece(msg_id, ordinal, final_part)
                elif ev_type == "session.reasoning.ended":
                    msg_id = str(data.get("assistantMessageID") or "")
                    ordinal = int(data.get("ordinal") or 0)
                    r_key = (msg_id, ordinal)
                    r_text = " ".join(str(data.get("text") or "").split()).strip()
                    if r_text and r_key not in seen_reasoning:
                        seen_reasoning.add(r_key)
                        if len(r_text) > 90:
                            r_text = r_text[:89] + "…"
                        on_event(StreamEvent(kind="log", level="info", text=f"💭 思考: {r_text}"))
                elif ev_type in ("session.tool.input.started", "session.tool.called"):
                    msg_id = str(data.get("assistantMessageID") or "")
                    call_id = str(data.get("id") or "")
                    tkey = (msg_id, call_id)
                    if ev_type == "session.tool.input.started":
                        tool_names[tkey] = str(data.get("name") or "tool")
                    else:
                        raw_name = tool_names.get(tkey) or str(data.get("name") or "tool")
                        inp = data.get("input") if isinstance(data.get("input"), dict) else {}
                        payload = {"tool": raw_name, "state": {"input": inp}}
                        eff_name = extract_effective_tool_name(raw_name, payload)
                        tool_names[tkey] = eff_name
                        if tkey not in tool_started:
                            tool_started.add(tkey)
                            on_event(make_tool_event(eff_name, payload))
                elif ev_type in ("session.tool.success", "session.tool.failed"):
                    msg_id = str(data.get("assistantMessageID") or "")
                    call_id = str(data.get("id") or "")
                    tkey = (msg_id, call_id)
                    eff_name = tool_names.get(tkey, "tool")
                    if tkey not in tool_finished:
                        tool_finished.add(tkey)
                        if ev_type == "session.tool.failed":
                            err_d = self._extract_error_message(data.get("error"))
                            on_event(make_tool_result_event(eff_name, is_error=True, detail=err_d))
                        else:
                            on_event(make_tool_result_event(eff_name, is_error=False))
                elif ev_type == "session.execution.succeeded":
                    on_event(StreamEvent(kind="done"))
                    break
                elif ev_type == "session.execution.interrupted":
                    return InteractiveTurnOutcome(
                        handled=True,
                        text=full_text.strip(),
                        session_id=sid,
                        returncode=0,
                        cancelled=True,
                    )
                elif ev_type in ("session.execution.failed", "session.error"):
                    saw_error = True
                    error_msg = self._extract_error_message(data.get("error"))
                    on_event(StreamEvent(kind="error", text=error_msg))
                    break

            return InteractiveTurnOutcome(
                handled=True,
                text=full_text.strip(),
                session_id=sid,
                returncode=1 if saw_error else 0,
                error=error_msg,
            )
        except OSError as exc:
            on_log("warn", f"OpenCode サーバー通信エラー ({exc}); CLIモードへ切替")
            return InteractiveTurnOutcome(handled=False, fallback_needed=True)
        finally:
            if set_steer_handler is not None:
                set_steer_handler(None)
            sse_stop.set()
            try:
                sse_conn.close()
            except OSError:
                pass


# ---------------------------------------------------------------------------
# 2. Claude Code Bidirectional Control Protocol Runner
# ---------------------------------------------------------------------------


class ClaudeInteractiveRunner:
    """Runs Claude Code with `--permission-prompt-tool stdio` and `--input-format stream-json`."""

    def __init__(self, adapter: ClaudeAdapter) -> None:
        self._adapter = adapter

    @staticmethod
    def _build_ask_user_fields(questions: Any) -> list[InteractionField]:
        fields: list[InteractionField] = []
        if not isinstance(questions, list):
            return fields
        for idx, q in enumerate(questions):
            if not isinstance(q, dict):
                continue
            q_text = str(q.get("question") or f"質問 {idx + 1}").strip()
            header = str(q.get("header") or "").strip()
            title = f"{header}: {q_text}" if header else q_text
            multi = bool(q.get("multiSelect", False))
            opts: list[InteractionOption] = []
            for raw_opt in q.get("options") or []:
                if isinstance(raw_opt, dict):
                    lbl = str(raw_opt.get("label") or "").strip()
                    if lbl:
                        opts.append(
                            InteractionOption(
                                value=lbl,
                                label=lbl,
                                description=str(raw_opt.get("description") or ""),
                            )
                        )
            fields.append(
                InteractionField(
                    key=q_text,
                    title=title,
                    field_type="multiselect" if multi else ("choice" if opts else "text"),
                    required=True,
                    options=opts,
                    allow_custom=True,
                )
            )
        return fields

    def _handle_control_request(
        self,
        stdin_writer: _LockedJsonStdin,
        data: dict[str, Any],
        *,
        auto_approve: bool,
        sister_name: str,
        session_allowed_tools: set[str],
        on_interaction: InteractionCallback,
    ) -> None:
        req_id = str(data.get("request_id") or data.get("id") or "")
        req_obj = data.get("request") if isinstance(data.get("request"), dict) else data
        subtype = str(req_obj.get("subtype") or data.get("subtype") or "")

        if subtype == "can_use_tool":
            tool_name = str(req_obj.get("tool_name") or req_obj.get("tool") or "tool")
            tool_input = req_obj.get("input") if isinstance(req_obj.get("input"), dict) else {}

            if tool_name == "AskUserQuestion":
                fields = self._build_ask_user_fields(tool_input.get("questions"))
                req = InteractionRequest(
                    agent_id="claude",
                    sister_name=sister_name,
                    kind="question",
                    title="Claude からの質問",
                    description="回答を選択または入力してください。",
                    tool_name=tool_name,
                    fields=fields,
                )
                resp = on_interaction(req)
                if resp.cancelled:
                    inner_resp: dict[str, Any] = {
                        "behavior": "deny",
                        "message": resp.message or "ユーザーがキャンセルしました",
                    }
                else:
                    answers_str: dict[str, str] = {}
                    for k, v in resp.answers.items():
                        answers_str[k] = ", ".join(str(x) for x in v) if isinstance(v, list) else str(v)
                    inner_resp = {
                        "behavior": "allow",
                        "updatedInput": {**tool_input, "answers": answers_str},
                    }
            elif auto_approve or tool_name in session_allowed_tools:
                inner_resp = {"behavior": "allow", "updatedInput": tool_input}
            else:
                detail = summarize_tool_payload(tool_name, {"input": tool_input}) or _format_detail_json(
                    tool_input
                )
                req = InteractionRequest(
                    agent_id="claude",
                    sister_name=sister_name,
                    kind="permission",
                    title=f"ツール実行許可: {tool_name}",
                    description=str(req_obj.get("description") or f"Claude が `{tool_name}` を実行しようとしています。"),
                    tool_name=tool_name,
                    detail=detail,
                    actions=[
                        InteractionAction("allow_once", "✅ 許可", "success"),
                        InteractionAction("allow_session", "🔓 このツールを常に許可", "primary"),
                        InteractionAction("deny", "❌ 拒否", "danger"),
                    ],
                )
                resp = on_interaction(req)
                if not resp.cancelled and resp.action_id in ("allow_once", "allow_session"):
                    if resp.action_id == "allow_session":
                        session_allowed_tools.add(tool_name)
                    inner_resp = {"behavior": "allow", "updatedInput": tool_input}
                else:
                    inner_resp = {
                        "behavior": "deny",
                        "message": resp.message or "ユーザーにより拒否されました",
                    }
        elif subtype == "initialize":
            inner_resp = {"status": "success"}
        else:
            inner_resp = {}

        stdin_writer.write_json(
            {
                "type": "control_response",
                "response": {
                    "subtype": "success",
                    "request_id": req_id,
                    "response": inner_resp,
                },
            },
        )

    def run(
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
        sister_name: str = "",
        cancel_event: threading.Event | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_event: Callable[[StreamEvent], None],
        on_interaction: InteractionCallback,
        on_log: Callable[[str, str], None],
        register_process: Callable[[subprocess.Popen[str]], None],
        unregister_process: Callable[[subprocess.Popen[str]], None],
        terminate_process: Callable[[subprocess.Popen[str] | None], None],
        set_steer_handler: Callable[[SteerHandler | None], None] | None = None,
    ) -> InteractiveTurnOutcome:
        resolved_cwd = cwd.strip() or str(Path.home())
        base_cmd, _ = self._adapter.build_command(
            "",
            model=model,
            effort=effort,
            session_id=session_id,
            cwd=resolved_cwd,
            system_prompt=system_prompt,
            auto_approve=auto_approve,
            attachments=attachments,
        )
        # Replace `-p ""` with `-p --input-format stream-json --permission-prompt-tool stdio`.
        cmd: list[str] = []
        skip_next = False
        for tok in base_cmd:
            if skip_next:
                skip_next = False
                continue
            if tok == "-p":
                skip_next = True
                cmd.extend(["-p", "--input-format", "stream-json", "--permission-prompt-tool", "stdio"])
                continue
            cmd.append(tok)

        popen_kwargs: dict[str, Any] = {
            "cwd": resolved_cwd if Path(resolved_cwd).is_dir() else str(Path.home()),
            "stdin": subprocess.PIPE,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
            "start_new_session": True,
        }
        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as exc:
            return InteractiveTurnOutcome(
                handled=True, returncode=-1, error=f"CLIの起動に失敗しました: {exc}"
            )

        register_process(proc)
        stderr_lines: list[str] = []
        stderr_thread = _start_stderr_reader(
            proc, stderr_lines, lambda ln: on_log("warn", f"[stderr] {ln[:240]}")
        )
        msg_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        _build_jsonrpc_reader(
            proc, msg_queue, lambda raw: on_log("warn", f"[stdout] {raw[:240]}")
        )

        stdin_writer = _LockedJsonStdin(proc)
        self._adapter.reset_state()
        stdin_writer.write_json(
            {"type": "user", "message": {"role": "user", "content": prompt}},
        )

        session_allowed_tools: set[str] = set()
        full_text = ""
        latest_sid = session_id
        saw_error = False
        error_msg = ""

        def _check_cancel() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            return bool(is_cancelled is not None and is_cancelled())

        def _steer(text: str, _attachments: Sequence[AttachmentItem] = ()) -> bool:
            if _check_cancel():
                return False
            return stdin_writer.write_json(
                {"type": "user", "message": {"role": "user", "content": text}}
            )

        if set_steer_handler is not None:
            set_steer_handler(_steer)

        try:
            while True:
                if _check_cancel():
                    terminate_process(proc)
                    return InteractiveTurnOutcome(
                        handled=True,
                        text=full_text.strip(),
                        session_id=latest_sid,
                        returncode=-15,
                        cancelled=True,
                    )
                try:
                    item = msg_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if item is None:
                    break

                if item.get("type") == "control_request":
                    self._handle_control_request(
                        stdin_writer,
                        item,
                        auto_approve=auto_approve,
                        sister_name=sister_name,
                        session_allowed_tools=session_allowed_tools,
                        on_interaction=on_interaction,
                    )
                    continue

                evs = self._adapter.parse_line(json.dumps(item, ensure_ascii=False))
                finished_turn = False
                for ev in evs:
                    if ev.kind == "session" and ev.session_id:
                        latest_sid = ev.session_id
                    elif ev.kind == "delta" and ev.text:
                        full_text += ev.text
                    elif ev.kind == "replace":
                        full_text = ev.text
                    elif ev.kind == "error":
                        saw_error = True
                        error_msg = ev.text
                        finished_turn = True
                    elif ev.kind == "done":
                        finished_turn = True
                    on_event(ev)
                if finished_turn:
                    if set_steer_handler is not None:
                        set_steer_handler(None)
                    stdin_writer.close()
                    break
        finally:
            if set_steer_handler is not None:
                set_steer_handler(None)
            stdin_writer.close()
            returncode = proc.wait()
            stderr_thread.join(timeout=1.0)
            unregister_process(proc)

        if _check_cancel():
            return InteractiveTurnOutcome(
                handled=True,
                text=full_text.strip(),
                session_id=latest_sid,
                returncode=returncode,
                cancelled=True,
            )
        if not saw_error and returncode != 0 and not full_text.strip():
            err_detail = "\n".join(stderr_lines[-6:]).strip()
            error_msg = err_detail or f"Claude Code が途中で終了しました (exit={returncode})"
            on_event(StreamEvent(kind="error", text=error_msg))
            saw_error = True

        return InteractiveTurnOutcome(
            handled=True,
            text=full_text.strip(),
            session_id=latest_sid,
            returncode=returncode,
            error=error_msg if saw_error else "",
        )


# ---------------------------------------------------------------------------
# 3. Codex App-Server JSON-RPC Runner
# ---------------------------------------------------------------------------


class CodexInteractiveRunner:
    """Runs OpenAI Codex turns via `codex app-server --stdio` with live approvals."""

    def __init__(self, adapter: CodexAdapter) -> None:
        self._adapter = adapter

    @staticmethod
    def _build_user_input_fields(questions: Any) -> list[InteractionField]:
        fields: list[InteractionField] = []
        if not isinstance(questions, list):
            return fields
        for q in questions:
            if not isinstance(q, dict):
                continue
            qid = str(q.get("id") or "").strip()
            if not qid:
                continue
            header = str(q.get("header") or "").strip()
            question = str(q.get("question") or qid).strip()
            title = f"{header}: {question}" if header else question
            opts = [
                InteractionOption(
                    value=str(o.get("label") or ""),
                    label=str(o.get("label") or ""),
                    description=str(o.get("description") or ""),
                )
                for o in (q.get("options") or [])
                if isinstance(o, dict) and o.get("label")
            ]
            fields.append(
                InteractionField(
                    key=qid,
                    title=title,
                    field_type="choice" if opts else "text",
                    required=True,
                    options=opts,
                    allow_custom=bool(q.get("isOther", False)),
                    is_secret=bool(q.get("isSecret", False)),
                )
            )
        return fields

    def _handle_server_request(
        self,
        stdin_writer: _LockedJsonStdin,
        msg: dict[str, Any],
        *,
        sister_name: str,
        on_interaction: InteractionCallback,
    ) -> None:
        rid = msg.get("id")
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        if method == "item/commandExecution/requestApproval":
            cmd_str = str(params.get("command") or "").strip()
            cwd_str = str(params.get("cwd") or "").strip()
            reason = str(params.get("reason") or "").strip()
            detail = cmd_str + (f"\n(cwd: {cwd_str})" if cwd_str else "")
            req = InteractionRequest(
                agent_id="codex",
                sister_name=sister_name,
                kind="permission",
                title="コマンド実行の許可 (Codex)",
                description=reason or "Codex がコマンドを実行しようとしています。",
                tool_name="commandExecution",
                detail=detail,
                actions=[
                    InteractionAction("accept", "✅ 許可", "success"),
                    InteractionAction("acceptForSession", "🔓 セッション中許可", "primary"),
                    InteractionAction("decline", "❌ 拒否", "danger"),
                    InteractionAction("cancel", "⏹ 中断", "secondary"),
                ],
            )
            resp = on_interaction(req)
            decision = "cancel" if resp.cancelled else (resp.action_id or "decline")
            if decision not in ("accept", "acceptForSession", "decline", "cancel"):
                decision = "decline"
            stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": {"decision": decision}})
            return

        if method == "item/fileChange/requestApproval":
            reason = str(params.get("reason") or "").strip()
            grant_root = str(params.get("grantRoot") or "").strip()
            req = InteractionRequest(
                agent_id="codex",
                sister_name=sister_name,
                kind="permission",
                title="ファイル変更の許可 (Codex)",
                description=reason or "Codex がファイルへの書き込み許可を求めています。",
                tool_name="fileChange",
                detail=f"許可対象ルート: {grant_root}" if grant_root else "",
                actions=[
                    InteractionAction("accept", "✅ 許可", "success"),
                    InteractionAction("acceptForSession", "🔓 セッション中許可", "primary"),
                    InteractionAction("decline", "❌ 拒否", "danger"),
                    InteractionAction("cancel", "⏹ 中断", "secondary"),
                ],
            )
            resp = on_interaction(req)
            decision = "cancel" if resp.cancelled else (resp.action_id or "decline")
            if decision not in ("accept", "acceptForSession", "decline", "cancel"):
                decision = "decline"
            stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": {"decision": decision}})
            return

        if method == "item/permissions/requestApproval":
            requested_perms = params.get("permissions") if isinstance(params.get("permissions"), dict) else {}
            reason = str(params.get("reason") or "").strip()
            req = InteractionRequest(
                agent_id="codex",
                sister_name=sister_name,
                kind="permission",
                title="追加権限の許可 (Codex)",
                description=reason or "Codex が追加のシステム/ネットワーク権限を求めています。",
                tool_name="permissions",
                detail=_format_detail_json(requested_perms),
                actions=[
                    InteractionAction("turn", "✅ このターンで許可", "success"),
                    InteractionAction("session", "🔓 セッション中許可", "primary"),
                    InteractionAction("deny", "❌ 拒否", "danger"),
                ],
            )
            resp = on_interaction(req)
            if resp.cancelled or resp.action_id == "deny":
                result = {"permissions": {}, "scope": "turn"}
            else:
                scope = "session" if resp.action_id == "session" else "turn"
                result = {"permissions": requested_perms, "scope": scope}
            stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": result})
            return

        if method == "item/tool/requestUserInput":
            fields = self._build_user_input_fields(params.get("questions"))
            req = InteractionRequest(
                agent_id="codex",
                sister_name=sister_name,
                kind="question",
                title="Codex からの質問",
                description="内容を確認して回答してください。",
                fields=fields,
            )
            resp = on_interaction(req)
            answers_map: dict[str, dict[str, list[str]]] = {}
            if not resp.cancelled:
                for qid, val in resp.answers.items():
                    vals = [str(x) for x in val] if isinstance(val, list) else ([str(val)] if str(val) else [])
                    answers_map[qid] = {"answers": vals}
            stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": {"answers": answers_map}})
            return

        if method == "mcpServer/elicitation/request":
            srv = str(params.get("serverName") or "MCP")
            msg_txt = str(params.get("message") or "")
            mode = str(params.get("mode") or "form")
            url = str(params.get("url") or "")
            req_schema = params.get("requestedSchema")
            fields = _build_elicitation_fields(req_schema)
            if mode == "url" and url:
                fields = [
                    InteractionField(
                        key="url",
                        title=f"{srv} 認証/確認ページ",
                        description=msg_txt,
                        field_type="external",
                        url=url,
                    )
                ]
            req = InteractionRequest(
                agent_id="codex",
                sister_name=sister_name,
                kind="form" if fields else "permission",
                title=f"{srv} からの確認",
                description=msg_txt or f"MCP サーバー `{srv}` が入力または承認を求めています。",
                fields=fields,
                actions=(
                    []
                    if fields
                    else [
                        InteractionAction("accept", "✅ 許可", "success"),
                        InteractionAction("decline", "❌ 拒否", "danger"),
                    ]
                ),
            )
            resp = on_interaction(req)
            if resp.cancelled or resp.action_id == "decline":
                action = "cancel" if resp.cancelled else "decline"
                result = {"action": action, "content": None}
            else:
                result = {
                    "action": "accept",
                    "content": _coerce_elicitation_answers(req_schema, resp.answers) if fields else None,
                }
            stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": result})
            return

        if method in ("execCommandApproval", "applyPatchApproval"):
            req = InteractionRequest(
                agent_id="codex",
                sister_name=sister_name,
                kind="permission",
                title="実行許可 (Codex)",
                description=str(params.get("reason") or "操作の実行許可を求めています。"),
                detail=_format_detail_json(params),
                actions=[
                    InteractionAction("approved", "✅ 許可", "success"),
                    InteractionAction("denied", "❌ 拒否", "danger"),
                ],
            )
            resp = on_interaction(req)
            decision = "denied" if resp.cancelled else (resp.action_id or "denied")
            if decision not in ("approved", "denied"):
                decision = "denied"
            stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": {"decision": decision}})
            return

        stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": {}})

    def run(
        self,
        prompt: str,
        *,
        model: str = "",
        effort: str = "",
        session_id: str | None = None,
        cwd: str = "",
        auto_approve: bool = True,
        attachments: Sequence[AttachmentItem] = (),
        sister_name: str = "",
        cancel_event: threading.Event | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_event: Callable[[StreamEvent], None],
        on_interaction: InteractionCallback,
        on_log: Callable[[str, str], None],
        register_process: Callable[[subprocess.Popen[str]], None],
        unregister_process: Callable[[subprocess.Popen[str]], None],
        terminate_process: Callable[[subprocess.Popen[str] | None], None],
        set_steer_handler: Callable[[SteerHandler | None], None] | None = None,
    ) -> InteractiveTurnOutcome:
        effective_cwd = cwd.strip() or str(Path.home())
        safe_cwd = effective_cwd if Path(effective_cwd).is_dir() else str(Path.home())
        cmd = [self._adapter.resolved_binary(), "app-server", "--stdio"]
        try:
            proc = subprocess.Popen(
                cmd,
                cwd=safe_cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError:
            return InteractiveTurnOutcome(handled=False, fallback_needed=True)

        register_process(proc)
        stderr_lines: list[str] = []
        stderr_thread = _start_stderr_reader(
            proc, stderr_lines, lambda ln: on_log("warn", f"[stderr] {ln[:240]}")
        )
        msg_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        _build_jsonrpc_reader(proc, msg_queue)

        def _check_cancel() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            return bool(is_cancelled is not None and is_cancelled())

        def _await_response(target_id: int, timeout_s: float = 12.0) -> dict[str, Any] | None:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if _check_cancel():
                    return None
                try:
                    msg = msg_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if msg is None:
                    return None
                if msg.get("id") == target_id and ("result" in msg or "error" in msg):
                    return msg
            return None

        cleaned_model = model.strip()
        cleaned_effort = (effort or "").strip().lower()
        thread_id = session_id or ""
        turn_id = ""
        turn_ready = threading.Event()
        id_lock = threading.Lock()
        next_rpc_id = [100]
        stdin_writer = _LockedJsonStdin(proc)
        full_text = ""
        saw_error = False
        error_msg = ""
        deltas_seen: set[str] = set()
        prompt_dispatched = False

        try:
            # 1. Initialize handshake
            if not stdin_writer.write_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {"clientInfo": {"name": "sisters-mascot", "version": "0.1.0"}},
                },
            ):
                terminate_process(proc)
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)
            init_resp = _await_response(1, timeout_s=12.0)
            if not init_resp or "error" in init_resp:
                terminate_process(proc)
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)
            stdin_writer.write_json({"jsonrpc": "2.0", "method": "initialized"})

            # 2. Thread start / resume
            approval_policy = "on-request"
            approvals_reviewer = "auto_review" if auto_approve else "user"
            if thread_id:
                thread_params: dict[str, Any] = {
                    "threadId": thread_id,
                    "cwd": safe_cwd,
                    "approvalPolicy": approval_policy,
                    "approvalsReviewer": approvals_reviewer,
                    "sandbox": "workspace-write",
                }
                if cleaned_model:
                    thread_params["model"] = cleaned_model
                stdin_writer.write_json(
                    {"jsonrpc": "2.0", "id": 2, "method": "thread/resume", "params": thread_params},
                )
            else:
                thread_params = {
                    "cwd": safe_cwd,
                    "approvalPolicy": approval_policy,
                    "approvalsReviewer": approvals_reviewer,
                    "sandbox": "workspace-write",
                }
                if cleaned_model:
                    thread_params["model"] = cleaned_model
                stdin_writer.write_json(
                    {"jsonrpc": "2.0", "id": 2, "method": "thread/start", "params": thread_params},
                )

            thr_resp = _await_response(2, timeout_s=12.0)
            if not thr_resp or "error" in thr_resp:
                terminate_process(proc)
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)
            thr_res = thr_resp.get("result") if isinstance(thr_resp.get("result"), dict) else {}
            thr_obj = thr_res.get("thread") if isinstance(thr_res.get("thread"), dict) else {}
            thread_id = str(thr_obj.get("id") or thread_id).strip()
            if not thread_id:
                terminate_process(proc)
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)
            on_event(StreamEvent(kind="session", session_id=thread_id))

            # 3. Turn start
            input_items: list[dict[str, Any]] = [{"type": "text", "text": prompt}]
            for img_path in collect_image_paths(attachments):
                input_items.append({"type": "localImage", "path": img_path})
            turn_params: dict[str, Any] = {
                "threadId": thread_id,
                "input": input_items,
                "approvalPolicy": approval_policy,
                "approvalsReviewer": approvals_reviewer,
            }
            if cleaned_model:
                turn_params["model"] = cleaned_model
            if cleaned_effort:
                turn_params["effort"] = cleaned_effort
            stdin_writer.write_json(
                {"jsonrpc": "2.0", "id": 3, "method": "turn/start", "params": turn_params}
            )
            prompt_dispatched = True

            def _steer(text: str, steer_attachments: Sequence[AttachmentItem] = ()) -> bool:
                if _check_cancel():
                    return False
                if not turn_id:
                    turn_ready.wait(timeout=1.5)
                active_tid = turn_id
                if not active_tid:
                    return False
                steer_items: list[dict[str, Any]] = [{"type": "text", "text": text}]
                for img_path in collect_image_paths(steer_attachments):
                    steer_items.append({"type": "localImage", "path": img_path})
                with id_lock:
                    next_rpc_id[0] += 1
                    rid = next_rpc_id[0]
                return stdin_writer.write_json(
                    {
                        "jsonrpc": "2.0",
                        "id": rid,
                        "method": "turn/steer",
                        "params": {
                            "threadId": thread_id,
                            "expectedTurnId": active_tid,
                            "input": steer_items,
                        },
                    }
                )

            if set_steer_handler is not None:
                set_steer_handler(_steer)

            # 4. Event & approval loop
            while True:
                if _check_cancel():
                    if turn_id:
                        stdin_writer.write_json(
                            {
                                "jsonrpc": "2.0",
                                "id": 99,
                                "method": "turn/interrupt",
                                "params": {"threadId": thread_id, "turnId": turn_id},
                            },
                        )
                    terminate_process(proc)
                    return InteractiveTurnOutcome(
                        handled=True,
                        text=full_text.strip(),
                        session_id=thread_id,
                        returncode=-15,
                        cancelled=True,
                    )

                try:
                    msg = msg_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if msg is None:
                    break

                # Response to our turn/start
                if "method" not in msg:
                    if msg.get("id") == 3:
                        if "error" in msg:
                            err_obj = msg.get("error") or {}
                            error_msg = str(
                                err_obj.get("message") if isinstance(err_obj, dict) else err_obj
                            )
                            saw_error = True
                            on_event(StreamEvent(kind="error", text=error_msg))
                            break
                        res_obj = msg.get("result") if isinstance(msg.get("result"), dict) else {}
                        t_obj = res_obj.get("turn") if isinstance(res_obj.get("turn"), dict) else {}
                        if t_obj.get("id"):
                            turn_id = str(t_obj["id"])
                            turn_ready.set()
                    continue

                method = str(msg.get("method") or "")
                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

                # Server-initiated request (has both id and method)
                if "id" in msg and msg["id"] is not None:
                    self._handle_server_request(
                        stdin_writer, msg, sister_name=sister_name, on_interaction=on_interaction
                    )
                    continue

                # Notifications
                if method == "turn/started":
                    t_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                    if t_obj.get("id"):
                        turn_id = str(t_obj["id"])
                        turn_ready.set()
                elif method == "item/agentMessage/delta":
                    delta = str(params.get("delta") or "")
                    item_id = str(params.get("itemId") or "")
                    if delta:
                        if item_id:
                            deltas_seen.add(item_id)
                        full_text += delta
                        on_event(StreamEvent(kind="delta", text=delta))
                elif method == "item/started":
                    item = params.get("item") if isinstance(params.get("item"), dict) else {}
                    itype = str(item.get("type") or "")
                    if itype == "commandExecution":
                        cmd_snip = str(item.get("command") or "")[:90]
                        on_event(
                            StreamEvent(
                                kind="tool",
                                tool_name="command_execution",
                                text="💻 コマンド実行中...",
                                detail=cmd_snip,
                                level="tool",
                            )
                        )
                    elif itype == "mcpToolCall":
                        tname = str(item.get("tool") or item.get("name") or "mcp_tool")
                        on_event(make_tool_event(tname, item))
                    elif itype == "fileChange":
                        on_event(make_tool_event("file_change", item))
                elif method == "item/completed":
                    item = params.get("item") if isinstance(params.get("item"), dict) else {}
                    itype = str(item.get("type") or "")
                    iid = str(item.get("id") or "")
                    if itype == "agentMessage":
                        if iid not in deltas_seen:
                            msg_txt = str(item.get("text") or "")
                            if msg_txt:
                                full_text += msg_txt
                                on_event(StreamEvent(kind="delta", text=msg_txt))
                    elif itype == "commandExecution":
                        ec = item.get("exitCode")
                        is_err = ec is not None and ec != 0
                        cmd_snip = str(item.get("command") or "")[:70]
                        detail = f"exit={ec} {cmd_snip}".strip() if ec is not None else cmd_snip
                        on_event(
                            make_tool_result_event("command_execution", is_error=is_err, detail=detail)
                        )
                    elif itype == "mcpToolCall":
                        tname = str(item.get("tool") or item.get("name") or "mcp_tool")
                        err_obj = item.get("error")
                        is_err = bool(err_obj) or str(item.get("status") or "").lower() == "failed"
                        on_event(make_tool_result_event(tname, is_error=is_err, detail=err_obj or ""))
                    elif itype == "fileChange":
                        is_err = str(item.get("status") or "").lower() == "failed"
                        on_event(make_tool_result_event("file_change", is_error=is_err))
                    elif itype == "reasoning":
                        summary = item.get("summary")
                        r_txt = (
                            " ".join(str(x) for x in summary)
                            if isinstance(summary, list)
                            else str(item.get("text") or "")
                        )
                        r_clean = " ".join(r_txt.split()).strip()
                        if r_clean:
                            if len(r_clean) > 90:
                                r_clean = r_clean[:89] + "…"
                            on_event(StreamEvent(kind="log", level="info", text=f"💭 思考: {r_clean}"))
                elif method == "error":
                    if not params.get("willRetry"):
                        err_obj = params.get("error") if isinstance(params.get("error"), dict) else {}
                        error_msg = str(err_obj.get("message") or params.get("error") or "Codex error")
                        saw_error = True
                        on_event(StreamEvent(kind="error", text=error_msg))
                elif method == "turn/completed":
                    turn_obj = params.get("turn") if isinstance(params.get("turn"), dict) else {}
                    t_status = str(turn_obj.get("status") or "").lower()
                    if t_status == "failed" and not saw_error:
                        err_obj = turn_obj.get("error") if isinstance(turn_obj.get("error"), dict) else {}
                        error_msg = str(err_obj.get("message") or "Codex turn failed")
                        saw_error = True
                        on_event(StreamEvent(kind="error", text=error_msg))
                    elif t_status != "failed":
                        on_event(StreamEvent(kind="done"))
                    break
        finally:
            if set_steer_handler is not None:
                set_steer_handler(None)
            stdin_writer.close()
            if proc.poll() is None:
                terminate_process(proc)
            returncode = proc.wait()
            stderr_thread.join(timeout=1.0)
            unregister_process(proc)

        if _check_cancel():
            return InteractiveTurnOutcome(
                handled=True,
                text=full_text.strip(),
                session_id=thread_id,
                returncode=returncode,
                cancelled=True,
            )
        if not prompt_dispatched:
            return InteractiveTurnOutcome(handled=False, fallback_needed=True)
        return InteractiveTurnOutcome(
            handled=True,
            text=full_text.strip(),
            session_id=thread_id,
            returncode=1 if saw_error else 0,
            error=error_msg if saw_error else "",
        )


# ---------------------------------------------------------------------------
# 4. Antigravity CLI (`agy acp --stdio`) Runner
# ---------------------------------------------------------------------------


class AgyInteractiveRunner:
    """Runs Antigravity CLI via `agy acp --stdio` when interactive approvals are active."""

    def __init__(self, adapter: AgyAdapter) -> None:
        self._adapter = adapter

    @staticmethod
    def _find_effort_config_id(config_options: Any) -> str:
        if not isinstance(config_options, list):
            return ""
        for opt in config_options:
            if not isinstance(opt, dict):
                continue
            cid = str(opt.get("id") or opt.get("configId") or "").strip()
            cat = str(opt.get("category") or "").strip().lower()
            name = str(opt.get("name") or "").strip().lower()
            hay = f"{cid.lower()} {cat} {name}"
            if any(k in hay for k in ("thought", "effort", "reasoning")):
                return cid
        return ""

    @staticmethod
    def _extract_chunk_text(update: dict[str, Any]) -> str:
        content = update.get("content")
        if isinstance(content, dict):
            return str(content.get("text") or "")
        if isinstance(content, list):
            return "".join(
                str(b.get("text") or "")
                for b in content
                if isinstance(b, dict) and b.get("text") is not None
            )
        return str(update.get("text") or "")

    def _handle_server_request(
        self,
        stdin_writer: _LockedJsonStdin,
        msg: dict[str, Any],
        *,
        sister_name: str,
        on_interaction: InteractionCallback,
    ) -> None:
        rid = msg.get("id")
        method = str(msg.get("method") or "")
        params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

        if method == "session/request_permission":
            tool_call = params.get("toolCall") if isinstance(params.get("toolCall"), dict) else {}
            tc_title = str(tool_call.get("title") or params.get("title") or "操作")
            raw_input = tool_call.get("rawInput") or tool_call.get("input")
            detail = _format_detail_json(raw_input)
            raw_opts = params.get("options") if isinstance(params.get("options"), list) else []

            actions: list[InteractionAction] = []
            for opt in raw_opts:
                if not isinstance(opt, dict):
                    continue
                oid = str(opt.get("optionId") or opt.get("id") or "").strip()
                if not oid:
                    continue
                oname = str(opt.get("name") or oid)
                okind = str(opt.get("kind") or "").lower()
                if "always" in okind:
                    style = "primary"
                elif "allow" in okind:
                    style = "success"
                elif "reject" in okind or "deny" in okind:
                    style = "danger"
                else:
                    style = "secondary"
                actions.append(InteractionAction(oid, oname, style))
            if not actions:
                actions = [
                    InteractionAction("allow_once", "✅ 1回許可", "success"),
                    InteractionAction("reject_once", "❌ 拒否", "danger"),
                ]

            req = InteractionRequest(
                agent_id="agy",
                sister_name=sister_name,
                kind="permission",
                title=f"実行許可 (Antigravity): {tc_title}",
                description=str(params.get("description") or f"Antigravity が `{tc_title}` の実行許可を求めています。"),
                tool_name=tc_title,
                detail=detail,
                actions=actions,
            )
            resp = on_interaction(req)
            if resp.cancelled or not resp.action_id:
                outcome: dict[str, Any] = {"outcome": "cancelled"}
            else:
                outcome = {"outcome": "selected", "optionId": resp.action_id}
            stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": {"outcome": outcome}})
            return

        if method == "elicitation/create":
            msg_txt = str(params.get("message") or "")
            req_schema = params.get("requestedSchema")
            fields = _build_elicitation_fields(req_schema)
            req = InteractionRequest(
                agent_id="agy",
                sister_name=sister_name,
                kind="question" if fields else "permission",
                title="Antigravity からの確認",
                description=msg_txt or "内容を確認して回答してください。",
                fields=fields,
                actions=(
                    []
                    if fields
                    else [
                        InteractionAction("accept", "✅ 許可", "success"),
                        InteractionAction("decline", "❌ 拒否", "danger"),
                    ]
                ),
            )
            resp = on_interaction(req)
            if resp.cancelled or resp.action_id == "decline":
                result = {"action": "cancel" if resp.cancelled else "decline"}
            else:
                result = {
                    "action": "accept",
                    "content": _coerce_elicitation_answers(req_schema, resp.answers) if fields else {},
                }
            stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": result})
            return

        stdin_writer.write_json({"jsonrpc": "2.0", "id": rid, "result": {}})

    def run(
        self,
        prompt: str,
        *,
        model: str = "",
        effort: str = "",
        session_id: str | None = None,
        cwd: str = "",
        auto_approve: bool = False,
        attachments: Sequence[AttachmentItem] = (),
        sister_name: str = "",
        cancel_event: threading.Event | None = None,
        is_cancelled: Callable[[], bool] | None = None,
        on_event: Callable[[StreamEvent], None],
        on_interaction: InteractionCallback,
        on_log: Callable[[str, str], None],
        register_process: Callable[[subprocess.Popen[str]], None],
        unregister_process: Callable[[subprocess.Popen[str]], None],
        terminate_process: Callable[[subprocess.Popen[str] | None], None],
        set_steer_handler: Callable[[SteerHandler | None], None] | None = None,
    ) -> InteractiveTurnOutcome:
        effective_cwd = cwd.strip() or str(Path.home())
        safe_cwd = effective_cwd if Path(effective_cwd).is_dir() else str(Path.home())
        cmd = [self._adapter.resolved_binary(), "acp", "--stdio"]
        if auto_approve:
            cmd.append("--dangerously-skip-permissions")
        for extra_dir in collect_attachment_dirs(attachments, safe_cwd):
            cmd.extend(["--add-dir", extra_dir])

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=safe_cwd,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError:
            return InteractiveTurnOutcome(handled=False, fallback_needed=True)

        register_process(proc)
        stderr_lines: list[str] = []
        stderr_thread = _start_stderr_reader(
            proc, stderr_lines, lambda ln: on_log("warn", f"[stderr] {ln[:240]}")
        )
        msg_queue: queue.Queue[dict[str, Any] | None] = queue.Queue()
        _build_jsonrpc_reader(proc, msg_queue)

        def _check_cancel() -> bool:
            if cancel_event is not None and cancel_event.is_set():
                return True
            return bool(is_cancelled is not None and is_cancelled())

        def _await_response(target_id: int, timeout_s: float = 15.0) -> dict[str, Any] | None:
            deadline = time.monotonic() + timeout_s
            while time.monotonic() < deadline:
                if _check_cancel():
                    return None
                try:
                    msg = msg_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if msg is None:
                    return None
                if msg.get("id") == target_id and ("result" in msg or "error" in msg):
                    return msg
            return None

        eff_model, eff_effort = resolve_model_and_effort(model, effort)
        sid = session_id or ""
        stdin_writer = _LockedJsonStdin(proc)
        full_text = ""
        saw_error = False
        error_msg = ""
        prompt_dispatched = False
        seen_tools: dict[str, str] = {}

        try:
            # 1. Initialize
            if not stdin_writer.write_json(
                {
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": 1,
                        "clientCapabilities": {"elicitation": {"form": {}}},
                        "clientInfo": {"name": "sisters-mascot", "version": "0.1.0"},
                    },
                },
            ):
                terminate_process(proc)
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)
            init_resp = _await_response(1, timeout_s=15.0)
            if not init_resp or "error" in init_resp:
                terminate_process(proc)
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)

            # 2. Session load or new
            if sid:
                stdin_writer.write_json(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/load",
                        "params": {"sessionId": sid, "cwd": safe_cwd, "mcpServers": []},
                    },
                )
            else:
                stdin_writer.write_json(
                    {
                        "jsonrpc": "2.0",
                        "id": 2,
                        "method": "session/new",
                        "params": {"cwd": safe_cwd, "mcpServers": []},
                    },
                )
            sess_resp = _await_response(2, timeout_s=20.0)
            if not sess_resp or "error" in sess_resp:
                terminate_process(proc)
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)

            sess_res = sess_resp.get("result") if isinstance(sess_resp.get("result"), dict) else {}
            sid = str(sess_res.get("sessionId") or sid).strip()
            if not sid:
                terminate_process(proc)
                return InteractiveTurnOutcome(handled=False, fallback_needed=True)
            on_event(StreamEvent(kind="session", session_id=sid))

            # 3. Model & Effort config options
            req_id_counter = 3
            if eff_model:
                stdin_writer.write_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id_counter,
                        "method": "session/set_model",
                        "params": {"sessionId": sid, "modelId": eff_model},
                    },
                )
                _await_response(req_id_counter, timeout_s=8.0)
                req_id_counter += 1

            if eff_effort:
                cfg_id = self._find_effort_config_id(sess_res.get("configOptions")) or "thought_level"
                stdin_writer.write_json(
                    {
                        "jsonrpc": "2.0",
                        "id": req_id_counter,
                        "method": "session/set_config_option",
                        "params": {"sessionId": sid, "configId": cfg_id, "value": eff_effort},
                    },
                )
                _await_response(req_id_counter, timeout_s=8.0)
                req_id_counter += 1

            # 4. Prompt turn
            prompt_req_id = req_id_counter
            stdin_writer.write_json(
                {
                    "jsonrpc": "2.0",
                    "id": prompt_req_id,
                    "method": "session/prompt",
                    "params": {"sessionId": sid, "prompt": [{"type": "text", "text": prompt}]},
                },
            )
            prompt_dispatched = True

            while True:
                if _check_cancel():
                    stdin_writer.write_json(
                        {"jsonrpc": "2.0", "method": "session/cancel", "params": {"sessionId": sid}},
                    )
                    terminate_process(proc)
                    return InteractiveTurnOutcome(
                        handled=True,
                        text=full_text.strip(),
                        session_id=sid,
                        returncode=-15,
                        cancelled=True,
                    )

                try:
                    msg = msg_queue.get(timeout=0.1)
                except queue.Empty:
                    continue
                if msg is None:
                    break

                if "method" not in msg:
                    if msg.get("id") == prompt_req_id:
                        if "error" in msg:
                            err_obj = msg.get("error") or {}
                            error_msg = str(
                                err_obj.get("message") if isinstance(err_obj, dict) else err_obj
                            )
                            saw_error = True
                            on_event(StreamEvent(kind="error", text=error_msg))
                        else:
                            on_event(StreamEvent(kind="done"))
                        break
                    continue

                method = str(msg.get("method") or "")
                params = msg.get("params") if isinstance(msg.get("params"), dict) else {}

                if "id" in msg and msg["id"] is not None:
                    self._handle_server_request(
                        stdin_writer, msg, sister_name=sister_name, on_interaction=on_interaction
                    )
                    continue

                if method == "session/update":
                    upd = params.get("update") if isinstance(params.get("update"), dict) else {}
                    kind = str(upd.get("sessionUpdate") or "")
                    if kind == "agent_message_chunk":
                        chunk = self._extract_chunk_text(upd)
                        if chunk:
                            full_text += chunk
                            on_event(StreamEvent(kind="delta", text=chunk))
                    elif kind == "agent_thought_chunk":
                        t_txt = " ".join(self._extract_chunk_text(upd).split()).strip()
                        if t_txt:
                            if len(t_txt) > 90:
                                t_txt = t_txt[:89] + "…"
                            on_event(StreamEvent(kind="log", level="info", text=f"💭 思考: {t_txt}"))
                    elif kind in ("tool_call", "tool_call_update"):
                        tc_id = str(upd.get("toolCallId") or "")
                        raw_title = str(upd.get("title") or upd.get("kind") or "tool")
                        status = str(upd.get("status") or "").lower()
                        payload = {
                            "tool": raw_title,
                            "input": upd.get("rawInput") or {},
                        }
                        if status in ("completed", "done", "success"):
                            if seen_tools.get(tc_id) != "done":
                                seen_tools[tc_id] = "done"
                                on_event(make_tool_result_event(raw_title, is_error=False))
                        elif status in ("failed", "error", "cancelled"):
                            if seen_tools.get(tc_id) != "error":
                                seen_tools[tc_id] = "error"
                                on_event(
                                    make_tool_result_event(
                                        raw_title,
                                        is_error=True,
                                        detail=upd.get("rawOutput") or status,
                                    )
                                )
                        elif seen_tools.get(tc_id) != "start":
                            seen_tools[tc_id] = "start"
                            on_event(make_tool_event(raw_title, payload))
        finally:
            if set_steer_handler is not None:
                set_steer_handler(None)
            stdin_writer.close()
            if proc.poll() is None:
                terminate_process(proc)
            returncode = proc.wait()
            stderr_thread.join(timeout=1.0)
            unregister_process(proc)

        if _check_cancel():
            return InteractiveTurnOutcome(
                handled=True,
                text=full_text.strip(),
                session_id=sid,
                returncode=returncode,
                cancelled=True,
            )
        if not prompt_dispatched:
            return InteractiveTurnOutcome(handled=False, fallback_needed=True)
        return InteractiveTurnOutcome(
            handled=True,
            text=full_text.strip(),
            session_id=sid,
            returncode=1 if saw_error else 0,
            error=error_msg if saw_error else "",
        )
