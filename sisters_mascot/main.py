"""Application entrypoint, single-instance guard, and controller wiring."""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import sys
from pathlib import Path

from PyQt6.QtCore import QObject, QTimer
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import QApplication, QSystemTrayIcon

from sisters_mascot import APP_NAME, APP_VERSION
from sisters_mascot.agents.router import AGENT_IDS, AgentRouter
from sisters_mascot.attachments import MAX_ATTACHMENTS
from sisters_mascot.audio.stt import VoiceRecorder
from sisters_mascot.audio.tts import TTSManager
from sisters_mascot.config import AppConfig, load_config, save_config
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
        self._shutting_down: bool = False

        self._size_save_timer = QTimer(self)
        self._size_save_timer.setSingleShot(True)
        self._size_save_timer.setInterval(250)
        self._size_save_timer.timeout.connect(self._save_config_safe)

        self.router = AgentRouter(self)
        self.tts = TTSManager(self)
        self.recorder = VoiceRecorder(self)

        current = self.current_sister()
        self.mascot = MascotWindow(self.sisters, current, self.config)
        self.panel = BubblePanel(self.sisters, current, self.config, self.router, self.recorder)
        self.tray = SistersTrayIcon(muted=self.config.tts_engine == "off", parent=self.mascot)

        self._local_server = QLocalServer(self)
        self._setup_single_instance_server()
        self._connect_signals()

        self.tray.update_tooltip(f"{current.rank_label} · {current.display_name}", self.config.agent_id)
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray.show()

        self.mascot.show()
        self.mascot.raise_()
        if current.greeting:
            self.mascot.show_toast(current.greeting, duration_ms=3200)

        self.panel.ensure_models_discovered(self.config.agent_id)

    def current_sister(self) -> SisterProfile:
        return self.sister_map[self.config.sister_id]

    def _save_config_safe(self) -> None:
        try:
            save_config(self.config)
        except OSError as exc:
            self.mascot.show_toast(f"設定の保存に失敗しました: {exc}", is_error=True)

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
        self.mascot.quit_requested.connect(self.quit)

        # Bubble panel
        self.panel.sister_changed.connect(self.switch_sister)
        self.panel.agent_changed.connect(self.switch_agent)
        self.panel.config_updated.connect(self._on_panel_config_updated)
        self.panel.mascot_size_changed.connect(self._on_panel_mascot_size_changed)
        self.panel.mute_toggled.connect(self.toggle_mute)
        self.panel.sprite_updated.connect(self.mascot.refresh_sprite)
        self.panel.reset_corner_requested.connect(lambda: self.mascot.reset_to_corner(save=True))
        self.panel.test_tts_requested.connect(self._on_test_tts)
        self.panel.toast_requested.connect(
            lambda msg, err: self.mascot.show_toast(msg, is_error=err)
        )

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
        self.router.error_occurred.connect(self._on_agent_error)
        self.router.model_choices_discovered.connect(self.panel.on_model_choices_discovered)

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

    def switch_sister(self, sister_id: str) -> None:
        sister = self.sister_map.get(sister_id)
        if sister is None or sister.id == self.config.sister_id:
            return
        self.tts.stop()
        self.config.sister_id = sister.id
        self._save_config_safe()
        self.mascot.set_sister(sister)
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
            and not self.router.is_running()
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

    def _on_agent_error(self, err: str) -> None:
        self.panel.append_error_message(err)
        self.mascot.set_activity("idle")
        self.mascot.show_toast(err, is_error=True, duration_ms=4500)

    def _sync_mascot_idle_or_speaking(self, speaking: bool) -> None:
        if self.recorder.is_recording() or self.router.is_running():
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
        if not self.router.is_running():
            self.mascot.set_activity("idle")

    def _on_stt_transcribing(self) -> None:
        self.panel.set_voice_state("transcribing")
        self.mascot.set_activity("thinking", "📝 文字起こし中…")

    def _on_stt_ready(self, text: str) -> None:
        self.panel.set_voice_state("idle")
        if not self.router.is_running():
            self.mascot.set_activity("idle")
        self.panel.handle_transcript(text)
        if not self.panel.isVisible() and text.strip():
            self.panel.show_for_mascot(self.mascot.frameGeometry(), tab_index=0)

    def _on_stt_error(self, err: str) -> None:
        self.panel.set_voice_state("idle")
        if not self.router.is_running():
            self.mascot.set_activity("idle")
        self.mascot.show_toast(err, is_error=True, duration_ms=4000)

    def shutdown(self) -> None:
        if self._shutting_down:
            return
        self._shutting_down = True
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
