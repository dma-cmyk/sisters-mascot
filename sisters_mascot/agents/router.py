"""Qt-integrated router orchestrating the 4 AI CLI adapters."""

from __future__ import annotations

import os
import re
import signal
import subprocess
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Sequence

from PyQt6.QtCore import QObject, pyqtSignal

from sisters_mascot import CONFIG_DIR
from sisters_mascot.agents.agy_adapter import AgyAdapter
from sisters_mascot.agents.base import BaseAgentAdapter, ModelChoice
from sisters_mascot.agents.claude_adapter import ClaudeAdapter
from sisters_mascot.agents.codex_adapter import CodexAdapter
from sisters_mascot.agents.opencode_adapter import OpenCodeAdapter
from sisters_mascot.attachments import AttachmentItem, build_prompt_with_attachments
from sisters_mascot.conversation import COMPACT_SYSTEM_PROMPT
from sisters_mascot.sisters_loader import SisterProfile

AGENT_IDS: tuple[str, ...] = ("agy", "claude", "codex", "opencode")
ACTIVITY_LOG_FILE: Path = CONFIG_DIR / "activity.log"
_ANSI_RE = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_ERROR_KEYWORDS = (
    "error",
    "traceback",
    "exception",
    "fatal",
    "econn",
    "429",
    "timeout",
    "failed",
    "denied",
    "enoent",
)


class AgentRouter(QObject):
    """Manage agent selection, per-session persona injection, and streaming subprocess I/O."""

    delta_received = pyqtSignal(str)
    replace_received = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    reply_finished = pyqtSignal(str)
    turn_cancelled = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    compact_finished = pyqtSignal(str)
    compact_error = pyqtSignal(str)
    activity_logged = pyqtSignal(str, str)
    models_discovered = pyqtSignal(str, list)
    model_choices_discovered = pyqtSignal(str, list)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._adapters: dict[str, BaseAgentAdapter] = {
            "agy": AgyAdapter(),
            "claude": ClaudeAdapter(),
            "codex": CodexAdapter(),
            "opencode": OpenCodeAdapter(),
        }
        self._sessions: dict[tuple[str, str], str] = {}
        self._model_cache: dict[str, list[ModelChoice]] = {}
        self._discovering_agents: set[str] = set()
        self._lock = threading.Lock()
        self._log_lock = threading.Lock()
        self._process: subprocess.Popen[str] | None = None
        self._running = False
        self._cancel_requested = False

    def list_agents(self) -> list[dict]:
        """Return metadata dictionaries for all supported CLI agents in order."""
        items: list[dict] = []
        for agent_id in AGENT_IDS:
            adapter = self._adapters[agent_id]
            items.append(
                {
                    "id": adapter.spec.id,
                    "display_name": adapter.spec.display_name,
                    "binary": adapter.resolved_binary(),
                    "available": adapter.is_available(),
                    "default_models": list(adapter.spec.default_models),
                    "supports_system_prompt": adapter.spec.supports_system_prompt,
                }
            )
        return items

    def get_adapter(self, agent_id: str) -> BaseAgentAdapter:
        """Return the adapter instance for `agent_id`."""
        if agent_id not in self._adapters:
            raise KeyError(f"Unknown agent_id: {agent_id}")
        return self._adapters[agent_id]

    def is_running(self) -> bool:
        """Return True if an agent turn subprocess is currently active."""
        with self._lock:
            return self._running

    def has_session(self, agent_id: str, sister_id: str) -> bool:
        """Return True if `(agent_id, sister_id)` already has a native CLI session ID."""
        with self._lock:
            return bool(self._sessions.get((agent_id, sister_id)))

    def clear_session(self, agent_id: str, sister_id: str) -> None:
        """Remove a single `(agent_id, sister_id)` session ID."""
        with self._lock:
            self._sessions.pop((agent_id, sister_id), None)

    def export_sessions(self) -> dict[str, str]:
        """Serialize active CLI session IDs into `{agent:sister: session_id}`."""
        with self._lock:
            return {
                f"{aid}:{sid}": sess_id
                for (aid, sid), sess_id in self._sessions.items()
                if sess_id
            }

    def import_sessions(self, sessions: dict[str, str]) -> None:
        """Restore previously persisted CLI session IDs."""
        with self._lock:
            self._sessions.clear()
            for key, sess_id in sessions.items():
                if not isinstance(key, str) or ":" not in key:
                    continue
                aid, sid = key.split(":", 1)
                val = str(sess_id or "").strip()
                if aid in self._adapters and sid and val:
                    self._sessions[(aid, sid)] = val

    def _build_persona_context(self, sister: SisterProfile) -> str:
        return (
            f"{sister.persona_prompt}\n\n"
            "【デスクトップ・マスコットとしての対話環境】\n"
            "- あなたは現在、美緒ちゃん（マスター）のデスクトップ上のマスコットランチャーから話しかけられています。\n"
            "- キャラクターの口調と温かさを保ちつつ、簡潔でわかりやすい日本語で応答してください。\n"
            "- `<conversation_handoff>` が含まれている場合は、これまでの会話や他の姉妹・AIとのやり取りを引き継いで、自然な続きとして応答してください。\n"
            "- ユーザーからファイルやフォルダが添付された場合は、提示された絶対パスを直接読み込み・確認して丁寧にサポートしてください。\n"
            "- Webページの調査・操作やGoogle Chromeでの確認を求められた場合は、利用可能な Playwright / Chrome MCP ツール（`browser_*` 等）を優先して使用してください。"
        )

    def send_message(
        self,
        agent_id: str,
        prompt: str,
        *,
        sister: SisterProfile,
        model: str = "",
        cwd: str = "",
        auto_approve: bool = True,
        attachments: Sequence[AttachmentItem] = (),
        handoff_context: str = "",
    ) -> None:
        """Start an asynchronous streaming agent turn."""
        if agent_id not in self._adapters:
            self.error_occurred.emit(f"未対応のエージェントです: {agent_id}")
            return

        adapter = self._adapters[agent_id]
        if not adapter.is_available():
            self.error_occurred.emit(
                f"{adapter.spec.display_name} のCLI ({adapter.spec.binary}) が見つかりません。"
            )
            return

        with self._lock:
            if self._running:
                self.error_occurred.emit("現在応答を生成中です。停止してから再送信してください。")
                return
            self._running = True
            self._cancel_requested = False
            session_id = self._sessions.get((agent_id, sister.id))

        persona_context = self._build_persona_context(sister)

        att_tuple = tuple(attachments)
        user_prompt = build_prompt_with_attachments(prompt, att_tuple)
        clean_handoff = (handoff_context or "").strip()
        if clean_handoff:
            user_prompt = f"{clean_handoff}\n\n{user_prompt}"

        system_prompt_arg = ""
        effective_prompt = user_prompt
        if session_id is None:
            if adapter.spec.supports_system_prompt:
                system_prompt_arg = persona_context
            else:
                effective_prompt = (
                    f"<system_instructions>\n{persona_context}\n</system_instructions>\n\n"
                    f"<user_message>\n{user_prompt}\n</user_message>"
                )

        effective_cwd = cwd.strip() or str(Path.home())
        adapter.reset_state()
        cmd, stdin_text = adapter.build_command(
            effective_prompt,
            model=model,
            session_id=session_id,
            cwd=effective_cwd,
            system_prompt=system_prompt_arg,
            auto_approve=auto_approve,
            attachments=att_tuple,
        )

        start_desc = f"{'継続セッション' if session_id else '新規セッション'}, model={model or '既定'}"
        worker = threading.Thread(
            target=self._run_subprocess,
            args=(agent_id, sister.id, adapter, cmd, stdin_text, effective_cwd),
            kwargs={"start_desc": start_desc},
            daemon=True,
        )
        worker.start()

    def compact_conversation(
        self,
        agent_id: str,
        compact_prompt: str,
        *,
        model: str = "",
        cwd: str = "",
    ) -> bool:
        """Run an isolated non-session AI turn to summarize/compact conversation history."""
        if agent_id not in self._adapters:
            self.compact_error.emit(f"未対応のエージェントです: {agent_id}")
            return False

        adapter = self._adapters[agent_id]
        if not adapter.is_available():
            self.compact_error.emit(
                f"{adapter.spec.display_name} のCLI ({adapter.spec.binary}) が見つかりません。"
            )
            return False

        with self._lock:
            if self._running:
                self.compact_error.emit("現在応答を生成中です。停止してから実行してください。")
                return False
            self._running = True
            self._cancel_requested = False

        system_prompt_arg = ""
        effective_prompt = compact_prompt
        if adapter.spec.supports_system_prompt:
            system_prompt_arg = COMPACT_SYSTEM_PROMPT
        else:
            effective_prompt = (
                f"<system_instructions>\n{COMPACT_SYSTEM_PROMPT}\n</system_instructions>\n\n"
                f"<user_message>\n{compact_prompt}\n</user_message>"
            )

        effective_cwd = cwd.strip() or str(Path.home())
        adapter.reset_state()
        cmd, stdin_text = adapter.build_command(
            effective_prompt,
            model=model,
            session_id=None,
            cwd=effective_cwd,
            system_prompt=system_prompt_arg,
            auto_approve=False,
            attachments=(),
        )

        start_desc = f"コンパクト, model={model or '既定'}"
        worker = threading.Thread(
            target=self._run_subprocess,
            args=(agent_id, "", adapter, cmd, stdin_text, effective_cwd),
            kwargs={"compact_mode": True, "start_desc": start_desc},
            daemon=True,
        )
        worker.start()
        return True

    def _write_activity_log(self, level: str, message: str) -> None:
        if os.environ.get("QT_QPA_PLATFORM") == "offscreen":
            return
        line = f"[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] [{level.upper():5s}] {message}\n"
        with self._log_lock:
            try:
                ACTIVITY_LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
                if ACTIVITY_LOG_FILE.is_file() and ACTIVITY_LOG_FILE.stat().st_size > 512 * 1024:
                    raw = ACTIVITY_LOG_FILE.read_bytes()[-(256 * 1024) :]
                    nl = raw.find(b"\n")
                    if nl >= 0:
                        raw = raw[nl + 1 :]
                    ACTIVITY_LOG_FILE.write_bytes(raw)
                with ACTIVITY_LOG_FILE.open("a", encoding="utf-8") as handle:
                    handle.write(line)
            except OSError:
                pass

    def _emit_activity(self, level: str, message: str) -> None:
        clean = _ANSI_RE.sub("", message or "").strip()
        if not clean:
            return
        self._write_activity_log(level, clean)
        self.activity_logged.emit(level, clean)

    def _run_subprocess(
        self,
        agent_id: str,
        sister_id: str,
        adapter: BaseAgentAdapter,
        cmd: list[str],
        stdin_text: str | None,
        cwd: str,
        *,
        compact_mode: bool = False,
        start_desc: str = "",
    ) -> None:
        resolved_cwd = cwd if (cwd and Path(cwd).is_dir()) else str(Path.home())
        full_text = ""
        saw_error = False
        stderr_lines: list[str] = []
        started_at = time.monotonic()

        self.status_changed.emit(
            "🗜️ 会話をコンパクト化中…" if compact_mode else "✨ 考え中..."
        )
        self._emit_activity(
            "info",
            f"▶ {adapter.spec.display_name} 開始" + (f" ({start_desc})" if start_desc else ""),
        )

        popen_kwargs: dict = {
            "cwd": resolved_cwd,
            "stdin": subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
            "stdout": subprocess.PIPE,
            "stderr": subprocess.PIPE,
            "text": True,
            "encoding": "utf-8",
            "errors": "replace",
            "bufsize": 1,
        }
        if os.name == "posix":
            popen_kwargs["start_new_session"] = True
        else:
            popen_kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)

        try:
            proc = subprocess.Popen(cmd, **popen_kwargs)
        except OSError as exc:
            with self._lock:
                self._running = False
                self._process = None
            self.status_changed.emit("")
            err_msg = f"CLIの起動に失敗しました: {exc}"
            self._emit_activity("error", f"❌ {err_msg}")
            if compact_mode:
                self.compact_error.emit(err_msg)
            else:
                self.error_occurred.emit(err_msg)
            return

        with self._lock:
            self._process = proc

        def read_stderr() -> None:
            if proc.stderr is None:
                return
            for err_line in proc.stderr:
                cleaned = _ANSI_RE.sub("", err_line).strip()
                if cleaned:
                    stderr_lines.append(cleaned)
                    if len(stderr_lines) > 40:
                        stderr_lines.pop(0)
                    lowered = cleaned.lower()
                    lvl = "error" if any(kw in lowered for kw in _ERROR_KEYWORDS) else "warn"
                    self._emit_activity(lvl, f"[stderr] {cleaned[:240]}")

        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stderr_thread.start()

        if stdin_text is not None and proc.stdin is not None:
            try:
                proc.stdin.write(stdin_text)
                proc.stdin.close()
            except OSError:
                pass

        try:
            if proc.stdout is not None:
                for line in proc.stdout:
                    with self._lock:
                        if self._cancel_requested:
                            break
                    evs = adapter.parse_line(line)
                    if not evs:
                        stripped_line = _ANSI_RE.sub("", line).strip()
                        if stripped_line and not stripped_line.startswith("{"):
                            self._emit_activity("warn", f"[stdout] {stripped_line[:240]}")
                        continue
                    for ev in evs:
                        if ev.kind == "session" and ev.session_id and not compact_mode:
                            with self._lock:
                                self._sessions[(agent_id, sister_id)] = ev.session_id
                        elif ev.kind == "delta" and ev.text:
                            full_text += ev.text
                            if not compact_mode:
                                self.delta_received.emit(ev.text)
                        elif ev.kind == "replace":
                            full_text = ev.text
                            if not compact_mode:
                                self.replace_received.emit(ev.text)
                        elif ev.kind in ("tool", "status") and ev.text:
                            status_msg = f"{ev.text} ({ev.detail})" if ev.detail else ev.text
                            if not compact_mode:
                                self.status_changed.emit(status_msg)
                            self._emit_activity(
                                "tool" if ev.kind == "tool" else "info",
                                status_msg,
                            )
                        elif ev.kind == "log" and ev.text:
                            self._emit_activity(ev.level or "info", ev.text)
                        elif ev.kind == "error":
                            saw_error = True
                            msg = ev.text or f"{agent_id} でエラーが発生しました。"
                            self._emit_activity("error", f"❌ {msg}")
                            if compact_mode:
                                self.compact_error.emit(msg)
                            else:
                                self.error_occurred.emit(msg)
        finally:
            returncode = proc.wait()
            stderr_thread.join(timeout=1.0)
            with self._lock:
                cancelled = self._cancel_requested
                self._process = None
                self._running = False
                self._cancel_requested = False

        elapsed = max(0.0, time.monotonic() - started_at)
        self.status_changed.emit("")

        if compact_mode:
            if cancelled:
                self._emit_activity(
                    "warn", f"⏹ コンパクト化を中断 (exit={returncode}, {elapsed:.1f}s)"
                )
                self.compact_error.emit("コンパクト化を中断しました。")
                return
            if saw_error:
                return
            if returncode != 0:
                self._emit_activity(
                    "error",
                    f"❌ コンパクト化プロセス異常終了 (exit={returncode}, {elapsed:.1f}s)",
                )
                err_detail = "\n".join(stderr_lines[-6:]).strip()
                self.compact_error.emit(
                    err_detail
                    or f"{adapter.spec.display_name} が終了コード {returncode} で終了しました。"
                )
                return
            self._emit_activity("done", f"✔ コンパクト完了 (exit={returncode}, {elapsed:.1f}s)")
            self.compact_finished.emit(full_text.strip())
            return

        if cancelled:
            self._emit_activity("warn", f"⏹ ユーザーにより中断 (exit={returncode}, {elapsed:.1f}s)")
            self.turn_cancelled.emit(full_text)
            return

        if returncode != 0 and not saw_error:
            self._emit_activity("error", f"❌ プロセス異常終了 (exit={returncode}, {elapsed:.1f}s)")
            err_detail = "\n".join(stderr_lines[-6:]).strip()
            summary_msg = f"{adapter.spec.display_name} が途中で終了しました (exit={returncode})"
            if err_detail:
                summary_msg = f"{summary_msg}\n{err_detail}"
            self.error_occurred.emit(summary_msg)
            return

        self._emit_activity("done", f"✔ 完了 (exit={returncode}, {elapsed:.1f}s)")
        self.reply_finished.emit(full_text)

    def cancel(self) -> None:
        """Terminate the running agent subprocess cleanly via its process group."""
        with self._lock:
            self._cancel_requested = True
            proc = self._process

        if proc is not None and proc.poll() is None:
            if os.name == "posix" and hasattr(os, "killpg"):
                try:
                    pgid = os.getpgid(proc.pid)
                    os.killpg(pgid, signal.SIGTERM)
                except OSError:
                    try:
                        proc.terminate()
                    except OSError:
                        pass
            else:
                try:
                    proc.terminate()
                except OSError:
                    pass

            def _force_kill_if_stuck() -> None:
                if proc.poll() is None:
                    if os.name == "posix" and hasattr(os, "killpg"):
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                            return
                        except OSError:
                            pass
                    try:
                        proc.kill()
                    except OSError:
                        pass

            threading.Timer(1.5, _force_kill_if_stuck).start()

    def new_session(self, agent_id: str | None = None) -> None:
        """Clear saved conversation/session IDs for one agent or all agents."""
        with self._lock:
            if agent_id is None:
                self._sessions.clear()
                return
            for key in [k for k in self._sessions if k[0] == agent_id]:
                self._sessions.pop(key, None)

    def get_cached_model_choices(self, agent_id: str) -> list[ModelChoice] | None:
        """Return cached ModelChoices for `agent_id`, or None if not discovered yet."""
        with self._lock:
            cached = self._model_cache.get(agent_id)
            return list(cached) if cached is not None else None

    def refresh_models_async(self, agent_id: str, *, force: bool = False) -> bool:
        """Discover models for `agent_id` in a background thread.

        Returns True if a background refresh was started, False if served from
        cache, invalid agent_id, or a refresh is already running.
        """
        if agent_id not in self._adapters:
            return False
        adapter = self._adapters[agent_id]

        with self._lock:
            if not force and agent_id in self._model_cache:
                cached = list(self._model_cache[agent_id])
            else:
                cached = None
                if agent_id in self._discovering_agents:
                    return False
                if cached is None:
                    self._discovering_agents.add(agent_id)

        if cached is not None:
            self.models_discovered.emit(agent_id, [c.id for c in cached])
            self.model_choices_discovered.emit(agent_id, cached)
            return False

        def _worker() -> None:
            try:
                choices = adapter.discover_model_choices()
            except Exception:
                choices = [ModelChoice(id=m, label=m) for m in adapter.spec.default_models]
            else:
                with self._lock:
                    self._model_cache[agent_id] = list(choices)
            finally:
                with self._lock:
                    self._discovering_agents.discard(agent_id)

            self.models_discovered.emit(agent_id, [c.id for c in choices])
            self.model_choices_discovered.emit(agent_id, list(choices))

        threading.Thread(target=_worker, daemon=True).start()
        return True
