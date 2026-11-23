"""Speech-to-Text microphone voice recorder using pw-record/parecord + lazy STT."""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess
import tempfile
import threading
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from sisters_mascot.config import AppConfig

MAX_RECORD_SECONDS = 30.0


def _resolve_recorder() -> tuple[str, str] | None:
    for candidate in ("/usr/bin/pw-record", shutil.which("pw-record")):
        if candidate and Path(candidate).is_file():
            return ("pw-record", candidate)
    for candidate in ("/usr/bin/parecord", shutil.which("parecord")):
        if candidate and Path(candidate).is_file():
            return ("parecord", candidate)
    return None


class VoiceRecorder(QObject):
    """Push-to-toggle 16kHz mono WAV recorder with Google/custom STT transcription."""

    recording_started = pyqtSignal()
    recording_stopped = pyqtSignal()
    transcribing = pyqtSignal()
    transcript_ready = pyqtSignal(str)
    error_occurred = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._proc: subprocess.Popen[bytes] | None = None
        self._wav_path: Path | None = None
        self._timer: threading.Timer | None = None
        self._recording = False

    def is_recording(self) -> bool:
        with self._lock:
            return self._recording

    def toggle(self, config: AppConfig) -> None:
        """Toggle recording state."""
        if self.is_recording():
            self.stop(config)
        else:
            self.start(config)

    def start(self, config: AppConfig) -> None:
        """Start recording 16kHz mono s16 WAV audio."""
        with self._lock:
            if self._recording:
                return

        recorder = _resolve_recorder()
        if recorder is None:
            self.error_occurred.emit("録音ツール (pw-record / parecord) が見つかりません。")
            return

        rec_kind, rec_bin = recorder
        fd, name = tempfile.mkstemp(prefix="sisters-stt-", suffix=".wav")
        os.close(fd)
        wav_path = Path(name)

        if rec_kind == "pw-record":
            cmd = [rec_bin, "--rate", "16000", "--channels", "1", "--format", "s16", str(wav_path)]
        else:
            cmd = [rec_bin, "--rate=16000", "--channels=1", "--format=s16le", "--file-format=wav", str(wav_path)]

        try:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
        except OSError as exc:
            wav_path.unlink(missing_ok=True)
            self.error_occurred.emit(f"録音の開始に失敗しました: {exc}")
            return

        timer = threading.Timer(MAX_RECORD_SECONDS, lambda: self.stop(config))
        timer.daemon = True
        timer.start()

        with self._lock:
            self._proc = proc
            self._wav_path = wav_path
            self._timer = timer
            self._recording = True

        self.recording_started.emit()

    def stop(self, config: AppConfig) -> None:
        """Stop recording cleanly with SIGINT and transcribe in a background thread."""
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            proc = self._proc
            wav_path = self._wav_path
            timer = self._timer
            self._proc = None
            self._wav_path = None
            self._timer = None

        if timer is not None:
            timer.cancel()

        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=3.0)
            except (OSError, subprocess.SubprocessError):
                try:
                    proc.terminate()
                    proc.wait(timeout=1.0)
                except (OSError, subprocess.SubprocessError):
                    proc.kill()

        self.recording_stopped.emit()

        if wav_path is None or not wav_path.is_file() or wav_path.stat().st_size <= 44:
            if wav_path is not None:
                wav_path.unlink(missing_ok=True)
            self.error_occurred.emit("音声が録音されませんでした。")
            return

        self.transcribing.emit()
        worker = threading.Thread(
            target=self._transcribe_worker,
            args=(wav_path, config),
            daemon=True,
        )
        worker.start()

    def cancel(self) -> None:
        """Abort recording and discard the temporary WAV without transcribing."""
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            proc = self._proc
            wav_path = self._wav_path
            timer = self._timer
            self._proc = None
            self._wav_path = None
            self._timer = None

        if timer is not None:
            timer.cancel()

        if proc is not None and proc.poll() is None:
            try:
                proc.send_signal(signal.SIGINT)
                proc.wait(timeout=2.0)
            except (OSError, subprocess.SubprocessError):
                proc.kill()

        if wav_path is not None:
            wav_path.unlink(missing_ok=True)
        self.recording_stopped.emit()

    def _transcribe_worker(self, wav_path: Path, config: AppConfig) -> None:
        try:
            engine = (config.stt_engine or "google").lower()
            if engine == "custom" and config.stt_command.strip():
                raw_cmd = config.stt_command.strip()
                if "{wav}" in raw_cmd:
                    cmd_str = raw_cmd.replace("{wav}", shlex.quote(str(wav_path)))
                else:
                    cmd_str = f"{raw_cmd} {shlex.quote(str(wav_path))}"
                proc = subprocess.run(
                    shlex.split(cmd_str),
                    capture_output=True,
                    text=True,
                    timeout=30,
                    check=False,
                )
                if proc.returncode != 0:
                    err = proc.stderr.strip() or f"カスタムSTTが終了コード {proc.returncode} で失敗しました。"
                    self.error_occurred.emit(err)
                    return
                text = proc.stdout.strip()
                if not text:
                    self.error_occurred.emit("音声認識の結果が空でした。")
                    return
                self.transcript_ready.emit(text)
                return

            try:
                import speech_recognition as sr  # type: ignore[import-not-found]
            except ImportError:
                self.error_occurred.emit(
                    "SpeechRecognition が未インストールです。`pip install SpeechRecognition` を実行してください。"
                )
                return

            recognizer = sr.Recognizer()
            with sr.AudioFile(str(wav_path)) as source:
                audio = recognizer.record(source)
            try:
                text = recognizer.recognize_google(audio, language="ja-JP")
            except sr.UnknownValueError:
                self.error_occurred.emit("音声を聞き取れませんでした。もう一度お試しください。")
                return
            except sr.RequestError as exc:
                self.error_occurred.emit(f"音声認識サービスに接続できませんでした: {exc}")
                return

            cleaned = (text or "").strip()
            if cleaned:
                self.transcript_ready.emit(cleaned)
            else:
                self.error_occurred.emit("音声認識の結果が空でした。")
        except Exception as exc:
            self.error_occurred.emit(f"音声認識中にエラーが発生しました: {exc}")
        finally:
            wav_path.unlink(missing_ok=True)
