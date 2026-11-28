"""Application entrypoint, single-instance guard, and controller wiring."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtGui import QGuiApplication
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from sisters_mascot import APP_NAME, APP_VERSION
from sisters_mascot.agents.orchestrator import OrchestratorEngine
from sisters_mascot.agents.router import AGENT_IDS, AgentRouter
from sisters_mascot.attachments import MAX_ATTACHMENTS
from sisters_mascot.audio.stt import VoiceRecorder
from sisters_mascot.audio.tts import TTSManager
from sisters_mascot.config import AppConfig, load_config, save_config
from sisters_mascot.conversation import load_conversation, save_conversation
from sisters_mascot.sisters_loader import SisterProfile, get_sister_map, load_all_sisters
from sisters_mascot.ui.bubble_panel import BubblePanel
from sisters_mascot.ui.mascot_window import MascotWindow
from sisters_mascot.ui.platform import supports_window_positioning
from sisters_mascot.ui.tray import SistersTrayIcon


_SINGLETON_NAME = "sisters-mascot-singleton"


def _system_qt_plugin_dir() -> str | None:
    """Locate the system Qt6 plugin directory (holds IM plugins like fcitx5)."""
    for tool, args in (
        ("qtpaths6", ["--plugin-dir"]),
        ("qmake6", ["-query", "QT_INSTALL_PLUGINS"]),
    ):
        exe = shutil.which(tool)
        if not exe:
            continue
        try:
            out = subprocess.run(
                [exe, *args], capture_output=True, text=True, timeout=5, check=False
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            continue
        if out and Path(out).is_dir():
            return out
    for cand in (
        "/usr/lib/qt6/plugins",
        "/usr/lib64/qt6/plugins",
        "/usr/lib/x86_64-linux-gnu/qt6/plugins",
    ):
        if Path(cand).is_dir():
            return cand
    return None


def _ensure_system_qt_plugins() -> None:
    """Make system Qt6 IM plugins discoverable by PyQt6's bundled Qt.

    The pip PyQt6 wheel bundles its own Qt plugins (compose/ibus only) and does
    not search the system plugin dir, so ``QT_IM_MODULE=fcitx`` silently fails to
    load the fcitx5 Mozc frontend on X11/XWayland. Adding the system plugin dir to
    ``QT_PLUGIN_PATH`` restores Japanese input there.
    """
    if not sys.platform.startswith("linux"):
        return
    if os.environ.get("QT_IM_MODULE", "") not in ("fcitx", "fcitx5", "ibus"):
        return
    plugin_dir = _system_qt_plugin_dir()
    if not plugin_dir:
        return
    parts = [p for p in os.environ.get("QT_PLUGIN_PATH", "").split(os.pathsep) if p]
    if plugin_dir not in parts:
        parts.append(plugin_dir)
        os.environ["QT_PLUGIN_PATH"] = os.pathsep.join(parts)


def _configure_qt_environment() -> None:
    """Pick the Qt platform and IM module before QApplication exists.

    Mirrors run.sh for direct ``python -m sisters_mascot.main`` launches.
    X11/XWayland (``DISPLAY`` set) prefers ``xcb``: the desktop mascot needs
    X11-style window management for always-on-top, free positioning and toast
    anchoring, which native Wayland compositors do not allow clients to control.
    Pure Wayland (no ``DISPLAY``) falls back to ``wayland`` with the text-input
    protocol. Explicit overrides always win.
    """
    override = os.environ.get("SISTERS_MASCOT_PLATFORM")
    if override:
        os.environ["QT_QPA_PLATFORM"] = override
    platform = os.environ.get("QT_QPA_PLATFORM", "")
    if not platform:
        if os.environ.get("DISPLAY"):
            platform = "xcb"
        elif os.environ.get("WAYLAND_DISPLAY"):
            platform = "wayland"
        if platform:
            os.environ["QT_QPA_PLATFORM"] = platform
    if platform.startswith("wayland") and not os.environ.get("SISTERS_MASCOT_KEEP_IM_MODULE"):
        os.environ.pop("QT_IM_MODULE", None)
    _ensure_system_qt_plugins()


def _try_activate_existing() -> bool:
    """Return True if another instance is already listening and was notified."""
    sock = QLocalSocket()
    sock.connectToServer(_SINGLETON_NAME)
    if sock.waitForConnected(350):
        sock.write(b"activate\n")
        sock.flush()
        sock.waitForBytesWritten(300)
        sock.disconnectFromServer()
        return True
    return False


class MascotAppController(QObject):
    """Coordinates MascotWindow, BubblePanel, Tray, AgentRouter, TTS, and STT."""

    def __init__(self, app: QApplication) -> None:
        super().__init__(app)
        self.app = app
        self.config: AppConfig = load_config()
        self.sisters: list[SisterProfile] = load_all_sisters()
        self.sister_map: dict[str, SisterProfile] = get_sister_map()
        if not self.sister_map and self.sisters:
            self.sister_map = {s.id: s for s in self.sisters}

        if self.config.sister_id not in self.sister_map:
            self.config.sister_id = "aoi" if "aoi" in self.sister_map else self.sisters[0].id
        if self.config.agent_id not in AGENT_IDS:
            self.config.agent_id = AGENT_IDS[0]

        self._preferred_tts_engine: str = (
            self.config.tts_engine if self.config.tts_engine != "off" else "edge"
        )
        self._last_scale: str = self.config.mascot_scale
        self._last_anim_mode: str = str(getattr(self.config, "animation_mode", "rich") or "rich")
        self._last_top: bool = self.config.always_on_top
        self._last_orchestrator_mode: bool = bool(getattr(self.config, "orchestrator_mode", False))
        self._shutting_down: bool = False

        self._size_save_timer = QTimer(self)
        self._size_save_timer.setSingleShot(True)
        self._size_save_timer.setInterval(250)
        self._size_save_timer.timeout.connect(self._save_config_safe)

        self.router = AgentRouter(self)
        self.orchestrator = OrchestratorEngine(self.router, self.sisters, self)
        self.tts = TTSManager(self)
        self.recorder = VoiceRecorder(self)
        self._last_orch_tts_ts: float = 0.0
        self._last_summon_fx_ts: float = 0.0
        self._orch_transition_queue: list[tuple[str, str, str, str, str]] = []
        self._orch_transition_timer = QTimer(self)
        self._orch_transition_timer.setSingleShot(True)
        self._orch_transition_timer.timeout.connect(self._flush_next_orch_transition)

        current = self.current_sister()
        initial_mascot_sister = (
            self.sister_map.get("aoi", current)
            if getattr(self.config, "orchestrator_mode", False)
            else current
        )
        self._mascot_display_sister_id: str = initial_mascot_sister.id
        self.mascot = MascotWindow(self.sisters, initial_mascot_sister, self.config)
        self.panel = BubblePanel(
            self.sisters,
            current,
            self.config,
            self.router,
            self.recorder,
            orchestrator=self.orchestrator,
        )
        self.tray = SistersTrayIcon(muted=self.config.tts_engine == "off", parent=self.mascot)

        self._local_server = QLocalServer(self)
        self._setup_single_instance_server()
        self._connect_signals()
        self._load_conversation_safe()

        if getattr(self.config, "orchestrator_mode", False):
            self.tray.update_tooltip(f"🎼 統括: {initial_mascot_sister.display_name}", self.config.agent_id)
        else:
            self.tray.update_tooltip(f"{current.rank_label} · {current.display_name}", self.config.agent_id)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        self.mascot.show()
        self.mascot.raise_()
        if initial_mascot_sister.greeting:
            self.mascot.show_toast(initial_mascot_sister.greeting, duration_ms=3200)

        self.panel.ensure_models_discovered(self.config.agent_id)

    def current_sister(self) -> SisterProfile:
        return self.sister_map[self.config.sister_id]

    def _save_config_safe(self) -> None:
        try:
            save_config(self.config)
        except OSError as exc:
            self.mascot.show_toast(f"設定の保存に失敗しました: {exc}", is_error=True)

    @staticmethod
    def _should_persist_conversation() -> bool:
        return QGuiApplication.platformName() != "offscreen"

    def _load_conversation_safe(self) -> None:
        if not self._should_persist_conversation():
            return
        try:
            state, msgs, sessions = load_conversation()
        except Exception:
            return
        if state.turns or state.compacted_summary or msgs or sessions:
            self.panel.restore_conversation(state, msgs, sessions)

    def _save_conversation_safe(self) -> None:
        if not self._should_persist_conversation():
            return
        try:
            state, msgs, sessions = self.panel.export_conversation_payload()
            save_conversation(state, display_messages=msgs, sessions=sessions)
        except OSError:
            pass

    def _setup_single_instance_server(self) -> None:
        QLocalServer.removeServer(_SINGLETON_NAME)
        self._local_server.listen(_SINGLETON_NAME)
        self._local_server.newConnection.connect(self._on_new_instance_connection)

    def _on_new_instance_connection(self) -> None:
        while self._local_server.hasPendingConnections():
            client = self._local_server.nextPendingConnection()
            if client is not None:
                client.readyRead.connect(lambda c=client: self._read_activation(c))
                client.disconnected.connect(client.deleteLater)

    def _read_activation(self, client: QLocalSocket) -> None:
        data = bytes(client.readAll()).decode("utf-8", errors="ignore")
        client.close()
        if "activate" in data:
            self.mascot.show()
            self.mascot.raise_()
            self.panel.toggle_for_mascot(self.mascot.frameGeometry())

    def _connect_signals(self) -> None:
        # Mascot window
        self.mascot.toggle_panel_requested.connect(self._toggle_panel)
        self.mascot.open_panel_requested.connect(self._open_panel)
        self.mascot.sister_selected.connect(self.switch_sister)
        self.mascot.agent_selected.connect(self.switch_agent)
        self.mascot.voice_toggle_requested.connect(lambda: self.recorder.toggle(self.config))
        self.mascot.mute_toggle_requested.connect(self.toggle_mute)
        self.mascot.always_on_top_toggled.connect(self._on_always_on_top_toggled)
        self.mascot.position_changed.connect(self._on_mascot_position_changed)
        self.mascot.mascot_clicked_line.connect(self._on_mascot_click_line)
        if hasattr(self.mascot, "size_changed"):
            self.mascot.size_changed.connect(self._on_mascot_size_changed)
        self.mascot.files_dropped.connect(self._on_mascot_files_dropped)
        self.mascot.orchestrator_mode_toggled.connect(self.set_orchestrator_mode)
        self.mascot.quit_requested.connect(self.quit)

        # Bubble panel
        self.panel.sister_changed.connect(self.switch_sister)
        self.panel.agent_changed.connect(self.switch_agent)
        self.panel.orchestrator_mode_changed.connect(self.set_orchestrator_mode)
        self.panel.config_updated.connect(self._on_panel_config_updated)
        self.panel.mascot_size_changed.connect(self._on_panel_mascot_size_changed)
        self.panel.mute_toggled.connect(self.toggle_mute)
        self.panel.sprite_updated.connect(self.mascot.refresh_sprite)
        self.panel.reset_corner_requested.connect(lambda: self.mascot.reset_to_corner(save=True))
        self.panel.test_tts_requested.connect(self._on_test_tts)
        self.panel.toast_requested.connect(
            lambda msg, err: self.mascot.show_toast(msg, is_error=err)
        )
        self.panel.conversation_changed.connect(self._save_conversation_safe)

        # Tray
        self.tray.toggle_panel_requested.connect(self._toggle_panel)
        self.tray.reset_corner_requested.connect(lambda: self.mascot.reset_to_corner(save=True))
        self.tray.mute_toggle_requested.connect(self.toggle_mute)
        self.tray.quit_requested.connect(self.quit)

        # Agent router
        self.router.delta_received.connect(self.panel.append_stream_delta)
        self.router.replace_received.connect(self.panel.replace_stream_text)
        self.router.status_changed.connect(self._on_agent_status)
        self.router.reply_finished.connect(self._on_agent_finished)
        self.router.turn_cancelled.connect(self._on_agent_cancelled)
        self.router.error_occurred.connect(self._on_agent_error)
        self.router.compact_finished.connect(self._on_compact_finished)
        self.router.compact_error.connect(self._on_compact_error)
        self.router.activity_logged.connect(self.panel.append_activity_log)
        self.router.model_choices_discovered.connect(self.panel.on_model_choices_discovered)
        self.router.interaction_requested.connect(self._on_agent_interaction_requested)
        self.router.interaction_resolved.connect(self.panel.handle_interaction_resolved)

        # Orchestrator engine (mascot transitions & voice)
        self.orchestrator.active_sister_changed.connect(self._on_orch_active_sister_changed)
        self.orchestrator.orchestration_finished.connect(self._on_orch_finished)
        self.orchestrator.orchestration_cancelled.connect(self._on_orch_cancelled)
        self.orchestrator.orchestration_error.connect(self._on_orch_error)

        # TTS
        self.tts.speaking_started.connect(lambda: self._sync_mascot_idle_or_speaking(True))
        self.tts.speaking_finished.connect(lambda: self._sync_mascot_idle_or_speaking(False))
        self.tts.error_occurred.connect(lambda msg: self.mascot.show_toast(msg, is_error=True))

        # STT
        self.recorder.recording_started.connect(self._on_stt_started)
        self.recorder.recording_stopped.connect(self._on_stt_stopped)
        self.recorder.transcribing.connect(self._on_stt_transcribing)
        self.recorder.transcript_ready.connect(self._on_stt_ready)
        self.recorder.error_occurred.connect(self._on_stt_error)

        self.app.aboutToQuit.connect(self.shutdown)

    # Controller actions
    def _toggle_panel(self) -> None:
        self.panel.toggle_for_mascot(self.mascot.frameGeometry())

    def _open_panel(self) -> None:
        self.panel.show_for_mascot(self.mascot.frameGeometry())

    def _on_mascot_files_dropped(self, paths: list[str]) -> None:
        was_at_limit = len(self.panel.get_attachments()) >= MAX_ATTACHMENTS
        added = self.panel.add_attachments(paths, notify=False)
        self.panel.show_for_mascot(self.mascot.frameGeometry(), tab_index=0)
        if added:
            if len(added) == 1:
                msg = f"📎 {added[0].name} を受け取ったよ！メッセージと一緒に送ってね。"
            else:
                msg = f"📎 {len(added)}件を受け取ったよ！メッセージと一緒に送ってね。"
            self.mascot.show_toast(msg, duration_ms=3400)
        elif was_at_limit:
            self.mascot.show_toast(
                f"添付は最大 {MAX_ATTACHMENTS} 件までだよ。", is_error=True, duration_ms=3200
            )
        else:
            self.mascot.show_toast("すでに添付リストに入っているみたい！", duration_ms=2600)

    def _is_agent_busy(self) -> bool:
        return self.router.is_running() or self.orchestrator.is_running()

    def _clear_orch_transition_queue(self) -> None:
        self._orch_transition_queue.clear()
        self._orch_transition_timer.stop()

    def set_orchestrator_mode(self, enabled: bool) -> None:
        if self._is_agent_busy():
            self.mascot.show_toast(
                "処理の完了後にオーケストレーターモードを切り替えてね。", is_error=True
            )
            self.panel.sync_from_config(self.current_sister())
            return
        self._clear_orch_transition_queue()
        new_val = bool(enabled)
        changed = self._last_orchestrator_mode != new_val
        self._last_orchestrator_mode = new_val
        self.config.orchestrator_mode = new_val
        self._save_config_safe()
        self.panel.sync_from_config(self.current_sister())
        self.mascot.sync_orchestrator_badge()

        if new_val:
            aoi = self.sister_map.get("aoi") or self.current_sister()
            self.tts.stop()
            line = aoi.dispatch_lines[0] if getattr(aoi, "dispatch_lines", None) else aoi.greeting
            self.mascot.play_orchestrator_transition(
                aoi,
                role_tag="🎼 指揮モード起動 · 長女 碧",
                line=line,
                direction="command",
                activity_kind="idle",
                status_label="",
            )
            self._last_summon_fx_ts = time.monotonic()
            self._mascot_display_sister_id = aoi.id
            self.tray.update_tooltip(f"🎼 統括: {aoi.display_name}", self.config.agent_id)
        else:
            restored = self.current_sister()
            if self._mascot_display_sister_id != restored.id:
                self.mascot.set_sister(restored)
                self._mascot_display_sister_id = restored.id
            else:
                self.mascot.sync_orchestrator_badge()
            self.mascot.set_activity("idle")
            self.tray.update_tooltip(
                f"{restored.rank_label} · {restored.display_name}", self.config.agent_id
            )
            if changed:
                self.mascot.show_toast("💬 通常対話モードに戻りました", duration_ms=2600)

    def switch_sister(self, sister_id: str) -> None:
        sister = self.sister_map.get(sister_id)
        if sister is None:
            return
        self._clear_orch_transition_queue()
        if self.config.orchestrator_mode:
            self.config.orchestrator_mode = False
            self._last_orchestrator_mode = False
            self.mascot.sync_orchestrator_badge()
        elif sister.id == self.config.sister_id and self._mascot_display_sister_id == sister.id:
            return
        self.tts.stop()
        self.config.sister_id = sister.id
        self._save_config_safe()
        self.mascot.set_sister(sister)
        self._mascot_display_sister_id = sister.id
        self.panel.notify_sister_switched(sister)
        self.tray.update_tooltip(f"{sister.rank_label} · {sister.display_name}", self.config.agent_id)
        if sister.greeting:
            self.mascot.show_toast(sister.greeting, duration_ms=3500)

    def switch_agent(self, agent_id: str) -> None:
        if agent_id not in AGENT_IDS:
            return
        self.config.agent_id = agent_id
        self._save_config_safe()
        self.mascot.set_agent_id(agent_id)
        self.mascot.sync_orchestrator_badge()
        self.panel.sync_from_config(self.current_sister())
        self.panel.ensure_models_discovered(agent_id)
        self.tray.update_tooltip(
            f"{self.current_sister().rank_label} · {self.current_sister().display_name}",
            agent_id,
        )

    def toggle_mute(self) -> None:
        if self.config.tts_engine != "off":
            self._preferred_tts_engine = self.config.tts_engine
            self.config.tts_engine = "off"
            self.tts.stop()
        else:
            self.config.tts_engine = self._preferred_tts_engine or "edge"

        muted = self.config.tts_engine == "off"
        self.mascot.set_muted(muted)
        self.tray.set_muted(muted)
        self.panel.sync_from_config(self.current_sister())
        self._save_config_safe()

    def _on_always_on_top_toggled(self, enabled: bool) -> None:
        self.config.always_on_top = enabled
        self.mascot.set_always_on_top(enabled)
        self.panel.sync_from_config(self.current_sister())
        self._save_config_safe()

    def _on_mascot_position_changed(self, x: int, y: int) -> None:
        # Wayland coordinates are compositor-owned and unreliable; keep the
        # last trustworthy position instead of saving bogus values.
        if not supports_window_positioning():
            return
        self.config.mascot_x = x
        self.config.mascot_y = y
        self._save_config_safe()

    def _on_mascot_click_line(self, line: str) -> None:
        if (
            self.config.tts_click_reactions
            and self.config.tts_engine != "off"
            and not self._is_agent_busy()
            and not self.tts.is_speaking()
            and not self.recorder.is_recording()
        ):
            self.tts.speak(line, self.current_sister(), self.config)

    def _on_panel_mascot_size_changed(self, width: int) -> None:
        clamped = max(100, min(420, int(width)))
        self.config.mascot_width = clamped
        if hasattr(self.mascot, "set_mascot_width"):
            self.mascot.set_mascot_width(clamped, emit_signal=False)
        self._size_save_timer.start()

    def _on_mascot_size_changed(self, width: int) -> None:
        clamped = max(100, min(420, int(width)))
        self.config.mascot_width = clamped
        self.panel.set_mascot_width_display(clamped)
        self._size_save_timer.start()

    def _on_panel_config_updated(self) -> None:
        if self.config.tts_engine != "off":
            self._preferred_tts_engine = self.config.tts_engine

        muted = self.config.tts_engine == "off"
        self.mascot.set_muted(muted)
        self.tray.set_muted(muted)

        if self.config.mascot_scale != self._last_scale:
            self._last_scale = self.config.mascot_scale
            self.mascot.set_scale(self.config.mascot_scale)

        anim_mode = str(getattr(self.config, "animation_mode", "rich") or "rich")
        if anim_mode != self._last_anim_mode:
            self._last_anim_mode = anim_mode
            if hasattr(self.mascot, "set_animation_mode"):
                self.mascot.set_animation_mode(anim_mode)

        if self.config.always_on_top != self._last_top:
            self._last_top = self.config.always_on_top
            self.mascot.set_always_on_top(self.config.always_on_top)

        self.mascot.sync_orchestrator_badge()
        self._save_config_safe()

    def _on_test_tts(self) -> None:
        if self.config.tts_engine == "off":
            self.mascot.show_toast("音声設定が OFF になっています。", is_error=True)
            return
        sister = self.current_sister()
        sample = sister.greeting or f"{sister.display_name}です。音声テストを行います。"
        self.tts.speak(sample, sister, self.config)

    # Agent callbacks
    def _on_agent_status(self, status: str) -> None:
        self.panel.set_status_text(status)
        lowered = (status or "").lower()
        if "chrome" in lowered or "playwright" in lowered or "🌐" in status:
            self.mascot.set_activity("tool", status)
        else:
            self.mascot.set_activity("thinking", status or "✨ 考え中…")

    def _on_agent_finished(self, full_text: str) -> None:
        self.panel.finalize_stream_reply(full_text)
        if not self.tts.is_speaking():
            self.mascot.set_activity("idle")
        if full_text and self.config.tts_engine != "off":
            self.tts.speak(full_text, self.current_sister(), self.config)

    def _on_agent_cancelled(self, partial_text: str) -> None:
        self.panel.handle_turn_cancelled(partial_text)
        if not self.tts.is_speaking():
            self.mascot.set_activity("idle")

    def _on_agent_error(self, err: str) -> None:
        self.panel.append_error_message(err)
        self.mascot.set_activity("idle")
        self.mascot.show_toast(err, is_error=True, duration_ms=4500)

    def _on_compact_finished(self, summary: str) -> None:
        self.panel.handle_compact_finished(summary)
        if not self.tts.is_speaking():
            self.mascot.set_activity("idle")

    def _on_compact_error(self, err: str) -> None:
        self.panel.handle_compact_error(err)
        self.mascot.set_activity("idle")
        self.mascot.show_toast(err, is_error=True, duration_ms=4500)

    def _on_agent_interaction_requested(self, req: object) -> None:
        self.panel.handle_interaction_requested(req)  # type: ignore[arg-type]
        title = getattr(req, "title", "確認のお願い")
        sister_name = getattr(req, "sister_name", "") or (
            self.current_sister().display_name.split("（", 1)[0].split("(", 1)[0].strip()
        )
        self.mascot.set_activity("thinking", f"🔐 確認待ち: {title}")
        self.mascot.show_toast(f"🔐 {sister_name}から確認のお願い: {title}", duration_ms=5000)
        if not self.panel.isVisible():
            self.panel.show_for_mascot(self.mascot.frameGeometry(), tab_index=0)

    def _apply_orch_transition(
        self,
        sister_id: str,
        direction: str,
        activity_kind: str,
        status_label: str,
        line: str,
    ) -> None:
        target = self.sister_map.get(sister_id) or self.current_sister()
        short = target.display_name.split("（", 1)[0].split("(", 1)[0].strip() or target.display_name
        role_tag = {
            "command": f"🎼 指揮開始 · {target.rank_label} {short}",
            "return": f"✨ 統合フェーズ · {target.rank_label} {short}",
            "summon": f"⚡ 出動 · {target.rank_label} {short}",
        }.get(direction, f"⚡ 出動 · {target.rank_label} {short}")
        self.mascot.play_orchestrator_transition(
            target,
            role_tag=role_tag,
            line=line,
            direction=direction,
            activity_kind=activity_kind,
            status_label=status_label,
        )
        self._mascot_display_sister_id = target.id
        now = time.monotonic()
        if (
            self.config.tts_engine != "off"
            and line
            and not self.recorder.is_recording()
            and (direction == "return" or now - self._last_orch_tts_ts >= 1.4)
        ):
            self._last_orch_tts_ts = now
            self.tts.speak(line, target, self.config)

    def _flush_next_orch_transition(self) -> None:
        if not self.orchestrator.is_running() or not self._orch_transition_queue:
            self._orch_transition_queue.clear()
            return
        sister_id, direction, activity_kind, status_label, line = self._orch_transition_queue.pop(0)
        self._apply_orch_transition(sister_id, direction, activity_kind, status_label, line)
        self._last_summon_fx_ts = time.monotonic()
        if self._orch_transition_queue:
            self._orch_transition_timer.start(1800)

    def _on_orch_active_sister_changed(
        self,
        sister_id: str,
        direction: str,
        activity_kind: str,
        status_label: str,
        line: str,
    ) -> None:
        target = self.sister_map.get(sister_id) or self.current_sister()
        short = target.display_name.split("（", 1)[0].split("(", 1)[0].strip() or target.display_name
        if direction == "update":
            if (
                self._mascot_display_sister_id == target.id
                or sister_id == "aoi"
                or not self.config.orchestrator_switch_mascot
            ):
                self.mascot.set_activity(activity_kind, status_label)
            return

        if direction in ("command", "return"):
            self._clear_orch_transition_queue()
            self._apply_orch_transition(sister_id, direction, activity_kind, status_label, line)
            self._last_summon_fx_ts = time.monotonic()
            return

        if self.config.orchestrator_switch_mascot:
            elapsed = time.monotonic() - self._last_summon_fx_ts
            if elapsed < 1.8 or self._orch_transition_timer.isActive():
                self._orch_transition_queue.append(
                    (sister_id, direction, activity_kind, status_label, line)
                )
                if not self._orch_transition_timer.isActive():
                    delay_ms = max(250, int((1.8 - elapsed) * 1000))
                    self._orch_transition_timer.start(delay_ms)
            else:
                self._apply_orch_transition(sister_id, direction, activity_kind, status_label, line)
                self._last_summon_fx_ts = time.monotonic()
        else:
            self.mascot.set_activity(activity_kind, status_label)
            if line:
                self.mascot.show_toast(
                    line,
                    title=f"⚡ 出動 · {target.rank_label} {short}",
                    duration_ms=3400,
                )

    def _restore_orchestrator_mascot_idle(self) -> SisterProfile:
        self._clear_orch_transition_queue()
        home_sister = (
            self.sister_map.get("aoi")
            if getattr(self.config, "orchestrator_mode", False)
            else None
        ) or self.current_sister()
        if self._mascot_display_sister_id != home_sister.id:
            self.mascot.set_sister(home_sister)
            self._mascot_display_sister_id = home_sister.id
        if not self.tts.is_speaking():
            self.mascot.set_activity("idle")
        return home_sister

    def _on_orch_finished(self, final_text: str, _plan: object) -> None:
        aoi = self._restore_orchestrator_mascot_idle()
        if final_text and self.config.tts_engine != "off":
            self.tts.speak(final_text, aoi, self.config)

    def _on_orch_cancelled(self, _summary: str) -> None:
        self._restore_orchestrator_mascot_idle()

    def _on_orch_error(self, err: str) -> None:
        self._restore_orchestrator_mascot_idle()
        self.mascot.show_toast(err, is_error=True, duration_ms=4500)

    def _sync_mascot_idle_or_speaking(self, speaking: bool) -> None:
        if self.recorder.is_recording() or self._is_agent_busy():
            return
        if speaking:
            self.mascot.set_activity("speaking", "🔊 お話し中…")
        else:
            self.mascot.set_activity("idle")

    # STT callbacks
    def _on_stt_started(self) -> None:
        self.tts.stop()
        self.panel.set_voice_state("recording")
        self.mascot.set_activity("listening", "🎤 聞いてるよ…")

    def _on_stt_stopped(self) -> None:
        self.panel.set_voice_state("idle")
        if not self._is_agent_busy():
            self.mascot.set_activity("idle")

    def _on_stt_transcribing(self) -> None:
        self.panel.set_voice_state("transcribing")
        self.mascot.set_activity("thinking", "📝 文字起こし中…")

    def _on_stt_ready(self, text: str) -> None:
        self.panel.set_voice_state("idle")
        if not self._is_agent_busy():
            self.mascot.set_activity("idle")
        self.panel.handle_transcript(text)
        if not self.panel.isVisible() and text.strip():
            self.panel.show_for_mascot(self.mascot.frameGeometry(), tab_index=0)

    def _on_stt_error(self, err: str) -> None:
        self.panel.set_voice_state("idle")
        if not self._is_agent_busy():
            self.mascot.set_activity("idle")
        self.mascot.show_toast(err, is_error=True, duration_ms=4000)

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
        try:
            self._clear_orch_transition_queue()
        except Exception:
            pass
        try:
            self.orchestrator.cancel()
        except Exception:
            pass
        try:
            self.router.cancel()
        except Exception:
            pass
        try:
            self.tts.stop()
        except Exception:
            pass
        try:
            self.recorder.cancel()
        except Exception:
            pass
        try:
            self._local_server.close()
        except Exception:
            pass
        try:
            save_config(self.config)
        except Exception:
            pass
        try:
            self._save_conversation_safe()
        except Exception:
            pass

    def quit(self) -> None:
        self.shutdown()
        self.app.quit()


def main(argv: list[str] | None = None) -> int:
    _configure_qt_environment()
    app = QApplication(argv if argv is not None else sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setDesktopFileName("sisters-mascot")
    app.setQuitOnLastWindowClosed(False)

    if _try_activate_existing():
        return 0

    controller = MascotAppController(app)

    # Ensure SIGINT / SIGTERM shut down cleanly even inside Qt event loop
    def _handle_sig(_signum, _frame) -> None:  # noqa: ANN001
        QTimer.singleShot(0, controller.quit)

    signal.signal(signal.SIGINT, _handle_sig)
    signal.signal(signal.SIGTERM, _handle_sig)

    sig_pump = QTimer()
    sig_pump.setInterval(250)
    sig_pump.timeout.connect(lambda: None)
    sig_pump.start()

    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
