"""Qt-integrated router orchestrating the 4 AI CLI adapters."""

from __future__ import annotations

import os
import signal
import subprocess
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from sisters_mascot.agents.agy_adapter import AgyAdapter
from sisters_mascot.agents.base import BaseAgentAdapter
from sisters_mascot.agents.claude_adapter import ClaudeAdapter
from sisters_mascot.agents.codex_adapter import CodexAdapter
from sisters_mascot.agents.opencode_adapter import OpenCodeAdapter
from sisters_mascot.sisters_loader import SisterProfile

AGENT_IDS: tuple[str, ...] = ("agy", "claude", "codex", "opencode")


class AgentRouter(QObject):
    """Manage agent selection, per-session persona injection, and streaming subprocess I/O."""

    delta_received = pyqtSignal(str)
    replace_received = pyqtSignal(str)
    status_changed = pyqtSignal(str)
    reply_finished = pyqtSignal(str)
    error_occurred = pyqtSignal(str)
    models_discovered = pyqtSignal(str, list)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._adapters: dict[str, BaseAgentAdapter] = {
            "agy": AgyAdapter(),
            "claude": ClaudeAdapter(),
            "codex": CodexAdapter(),
            "opencode": OpenCodeAdapter(),
        }
        self._sessions: dict[tuple[str, str], str] = {}
        self._lock = threading.Lock()
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

    def _build_persona_context(self, sister: SisterProfile) -> str:
        return (
            f"{sister.persona_prompt}\n\n"
            "【デスクトップ・マスコットとしての対話環境】\n"
            "- あなたは現在、美緒ちゃん（マスター）のLinuxデスクトップ上のマスコットランチャーから話しかけられています。\n"
            "- キャラクターの口調と温かさを保ちつつ、簡潔でわかりやすい日本語で応答してください。\n"
            "- Webページの調査・操作やGoogle Chromeでの確認を求められた場合は、利用可能な Playwright / Chrome MCP ツール（`browser_*` 等）を優先して使用してください。"
        )

    def send_message(
        self,
        agent_id: str,
        prompt: str,
        *,
        sister: SisterProfile,
        model: str = "",
        cwd: str = "/home/dma",
        auto_approve: bool = True,
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

        session_key = (agent_id, sister.id)
        session_id = self._sessions.get(session_key)
        persona_context = self._build_persona_context(sister)

        system_prompt_arg = ""
        effective_prompt = prompt
        if session_id is None:
            if adapter.spec.supports_system_prompt:
                system_prompt_arg = persona_context
            else:
                effective_prompt = (
                    f"<system_instructions>\n{persona_context}\n</system_instructions>\n\n"
                    f"<user_message>\n{prompt}\n</user_message>"
                )

        adapter.reset_state()
        cmd, stdin_text = adapter.build_command(
            effective_prompt,
            model=model,
            session_id=session_id,
            cwd=cwd,
            system_prompt=system_prompt_arg,
            auto_approve=auto_approve,
        )

        worker = threading.Thread(
            target=self._run_subprocess,
            args=(agent_id, sister.id, adapter, cmd, stdin_text, cwd),
            daemon=True,
        )
        worker.start()

    def _run_subprocess(
        self,
        agent_id: str,
        sister_id: str,
        adapter: BaseAgentAdapter,
        cmd: list[str],
        stdin_text: str | None,
        cwd: str,
    ) -> None:
        resolved_cwd = cwd if Path(cwd).is_dir() else "/home/dma"
        full_text = ""
        saw_error = False
        stderr_lines: list[str] = []

        self.status_changed.emit("✨ 考え中...")

        try:
            proc = subprocess.Popen(
                cmd,
                cwd=resolved_cwd,
                stdin=subprocess.PIPE if stdin_text is not None else subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding="utf-8",
                errors="replace",
                bufsize=1,
                start_new_session=True,
            )
        except OSError as exc:
            with self._lock:
                self._running = False
                self._process = None
            self.status_changed.emit("")
            self.error_occurred.emit(f"CLIの起動に失敗しました: {exc}")
            return

        with self._lock:
            self._process = proc

        def read_stderr() -> None:
            if proc.stderr is None:
                return
            for err_line in proc.stderr:
                cleaned = err_line.strip()
                if cleaned:
                    stderr_lines.append(cleaned)
                    if len(stderr_lines) > 40:
                        stderr_lines.pop(0)

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
                    for ev in adapter.parse_line(line):
                        if ev.kind == "session" and ev.session_id:
                            self._sessions[(agent_id, sister_id)] = ev.session_id
                        elif ev.kind == "delta" and ev.text:
                            full_text += ev.text
                            self.delta_received.emit(ev.text)
                        elif ev.kind == "replace":
                            full_text = ev.text
                            self.replace_received.emit(ev.text)
                        elif ev.kind in ("tool", "status") and ev.text:
                            self.status_changed.emit(ev.text)
                        elif ev.kind == "error":
                            saw_error = True
                            self.error_occurred.emit(ev.text or f"{agent_id} でエラーが発生しました。")
        finally:
            returncode = proc.wait()
            stderr_thread.join(timeout=1.0)
            with self._lock:
                cancelled = self._cancel_requested
                self._process = None
                self._running = False
                self._cancel_requested = False

        self.status_changed.emit("")

        if cancelled:
            self.reply_finished.emit(full_text)
            return

        if returncode != 0 and not saw_error and not full_text.strip():
            err_detail = "\n".join(stderr_lines[-6:]).strip()
            self.error_occurred.emit(err_detail or f"{adapter.spec.display_name} が終了コード {returncode} で終了しました。")
            return

        self.reply_finished.emit(full_text)

    def cancel(self) -> None:
        """Terminate the running agent subprocess cleanly via its process group."""
        with self._lock:
            self._cancel_requested = True
            proc = self._process

        if proc is not None and proc.poll() is None:
            try:
                pgid = os.getpgid(proc.pid)
                os.killpg(pgid, signal.SIGTERM)
            except OSError:
                try:
                    proc.terminate()
                except OSError:
                    pass

            def _force_kill_if_stuck() -> None:
                if proc.poll() is None:
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                    except OSError:
                        try:
                            proc.kill()
                        except OSError:
                            pass

            threading.Timer(1.5, _force_kill_if_stuck).start()

    def new_session(self, agent_id: str | None = None) -> None:
        """Clear saved conversation/session IDs for one agent or all agents."""
        if agent_id is None:
            self._sessions.clear()
            return
        for key in [k for k in self._sessions if k[0] == agent_id]:
            self._sessions.pop(key, None)

    def refresh_models_async(self, agent_id: str) -> None:
        """Discover models for `agent_id` in a background thread and emit `models_discovered`."""
        if agent_id not in self._adapters:
            return
        adapter = self._adapters[agent_id]

        def _worker() -> None:
            models = adapter.discover_models()
            self.models_discovered.emit(agent_id, models)

        threading.Thread(target=_worker, daemon=True).start()
