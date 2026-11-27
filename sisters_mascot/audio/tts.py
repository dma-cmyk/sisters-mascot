"""Text-to-Speech manager supporting Edge-TTS and VOICEVOX with lazy imports."""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import subprocess
import tempfile
import threading
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from PyQt6.QtCore import QObject, pyqtSignal

from sisters_mascot.config import AppConfig
from sisters_mascot.sisters_loader import SisterProfile

_FENCED_CODE_RE = re.compile(r"```[\s\S]*?```")
_INLINE_CODE_RE = re.compile(r"`([^`]+)`")
_MD_LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
_URL_RE = re.compile(r"https?://\S+")
_MD_DECOR_RE = re.compile(r"(^|\n)\s{0,3}(?:#{1,6}\s*|>\s*|[-*+]\s+|\d+\.\s+)")

SUPPORTED_EDGE_JA_VOICES: tuple[str, ...] = ("ja-JP-NanamiNeural", "ja-JP-KeitaNeural")
DEFAULT_EDGE_VOICE = "ja-JP-NanamiNeural"

_ROMAN_SISTER_PAREN_RE = re.compile(
    r"[（(]\s*(?:Aoi|Ruri|Kohaku|Sumire|Akari|Shion|Hiiro|Mashiro|Kokonoe|"
    r"Towa|Yui|Aya|Ritsu|Nagi|Kanon|Akane|Homura)\s*[）)]",
    re.IGNORECASE,
)
_RANK_DOT_RE = re.compile(
    r"(十七女|十六女|十五女|十四女|十三女|十二女|十一女|十女|九女|八女|"
    r"七女|六女|五女|四女|三女|次女|長女|秘書)[・·]+"
)
_RANK_READINGS: tuple[tuple[str, str], ...] = (
    ("十七女", "じゅうななじょ"),
    ("十六女", "じゅうろくじょ"),
    ("十五女", "じゅうごじょ"),
    ("十四女", "じゅうよんじょ"),
    ("十三女", "じゅうさんじょ"),
    ("十二女", "じゅうにじょ"),
    ("十一女", "じゅういちじょ"),
    ("十女", "じゅうじょ"),
    ("九女", "きゅうじょ"),
    ("八女", "はちじょ"),
    ("七女", "ななじょ"),
    ("六女", "ろくじょ"),
    ("五女", "ごじょ"),
    ("四女", "よんじょ"),
    ("三女", "さんじょ"),
    ("次女", "じじょ"),
    ("長女", "ちょうじょ"),
)
_HONORIFIC_READINGS: tuple[tuple[str, str], ...] = (
    ("姉さん", "ねえさん"),
    ("姉ちゃん", "ねえちゃん"),
)
_SINGLE_KANJI_SISTER_HONORIFIC_RE = re.compile(r"(碧|菫|結|彩|律|凪|茜|焔)(様|殿)")
_SINGLE_KANJI_HONORIFIC_MAP: dict[str, str] = {
    "碧": "あおい",
    "菫": "すみれ",
    "結": "ゆい",
    "彩": "あや",
    "律": "りつ",
    "凪": "なぎ",
    "茜": "あかね",
    "焔": "ほむら",
}
_SUFFIX_MAP: dict[str, str] = {"様": "さま", "殿": "どの"}
_MULTI_KANJI_NAMES: tuple[tuple[str, str], ...] = (
    ("美緒", "みお"),
    ("瑠璃", "るり"),
    ("琥珀", "こはく"),
    ("朱莉", "あかり"),
    ("紫音", "しおん"),
    ("緋色", "ひいろ"),
    ("真白", "ましろ"),
    ("九重", "ここのえ"),
    ("十和", "とわ"),
    ("華音", "かのん"),
)
_CONTEXT_SINGLE_KANJI_RULES: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"(?<![\u4e00-\u9fff])碧(?![\u4e00-\u9fffい])"), "あおい"),
    (re.compile(r"(?<![\u4e00-\u9fff])菫(?![\u4e00-\u9fff])"), "すみれ"),
    (re.compile(r"(?<![\u4e00-\u9fff])結(?![\u4e00-\u9fffびぶべばわっん])"), "ゆい"),
    (re.compile(r"(?<![\u4e00-\u9fff])彩(?![\u4e00-\u9fffりるれらっ])"), "あや"),
    (re.compile(r"(?<![\u4e00-\u9fff])律(?![\u4e00-\u9fff])"), "りつ"),
    (re.compile(r"(?<![\u4e00-\u9fff])凪(?![\u4e00-\u9fffぐぎげい])"), "なぎ"),
    (re.compile(r"(?<![\u4e00-\u9fff])茜(?![\u4e00-\u9fff])"), "あかね"),
    (re.compile(r"(?<![\u4e00-\u9fff])焔(?![\u4e00-\u9fff])"), "ほむら"),
)


def apply_speech_readings(
    text: str,
    custom_readings: dict[str, str] | None = None,
) -> str:
    """Normalize Mio's name, all 17 sisters' names, and birth-order ranks for TTS."""
    if not text:
        return ""
    out = text

    if custom_readings:
        for src in sorted(custom_readings, key=len, reverse=True):
            target = (custom_readings[src] or "").strip()
            if src and target:
                out = out.replace(src, target)

    out = _ROMAN_SISTER_PAREN_RE.sub("", out)
    out = _RANK_DOT_RE.sub(r"\1、", out)

    for rank_kanji, rank_reading in _RANK_READINGS:
        out = out.replace(rank_kanji, rank_reading)

    for suffix_kanji, suffix_reading in _HONORIFIC_READINGS:
        out = out.replace(suffix_kanji, suffix_reading)

    out = _SINGLE_KANJI_SISTER_HONORIFIC_RE.sub(
        lambda m: _SINGLE_KANJI_HONORIFIC_MAP[m.group(1)] + _SUFFIX_MAP[m.group(2)],
        out,
    )

    for kanji_name, reading in _MULTI_KANJI_NAMES:
        out = out.replace(kanji_name, reading)

    for pattern, reading in _CONTEXT_SINGLE_KANJI_RULES:
        out = pattern.sub(reading, out)

    return re.sub(r"\s+", " ", out).strip()


def _normalize_pitch(pitch: str) -> str:
    match = re.match(r"^\s*([+-]?\d+)\s*(?:Hz)?\s*$", pitch or "+0Hz", flags=re.IGNORECASE)
    val = int(match.group(1)) if match else 0
    clamped = max(-50, min(50, val))
    return f"{clamped:+d}Hz"


def _resolve_edge_voice(voice_name: str) -> str:
    cleaned = (voice_name or "").strip()
    if cleaned in SUPPORTED_EDGE_JA_VOICES:
        return cleaned
    return DEFAULT_EDGE_VOICE


def clean_text_for_speech(
    text: str,
    *,
    skip_code: bool = True,
    max_chars: int = 220,
    reading_overrides: dict[str, str] | None = None,
) -> str:
    """Sanitize Markdown/code text and normalize character readings for natural TTS."""
    if not text:
        return ""

    cleaned = text
    if skip_code:
        cleaned = _FENCED_CODE_RE.sub(" ", cleaned)
        cleaned = _INLINE_CODE_RE.sub(r"\1", cleaned)
    else:
        cleaned = cleaned.replace("```", " ").replace("`", "")

    cleaned = _MD_LINK_RE.sub(r"\1", cleaned)
    cleaned = _URL_RE.sub(" ", cleaned)
    cleaned = _MD_DECOR_RE.sub(r"\1", cleaned)
    cleaned = cleaned.replace("**", "").replace("__", "").replace("~~", "")
    cleaned = re.sub(r"\s+", " ", cleaned).strip()

    if max_chars > 0 and len(cleaned) > max_chars:
        snippet = cleaned[:max_chars]
        cut: str | None = None
        for sep in ("。", "！", "？", ".", "!", "?"):
            idx = snippet.rfind(sep)
            if idx >= max_chars // 2:
                cut = snippet[: idx + 1].strip()
                break
        cleaned = cut if cut is not None else snippet.strip()

    return apply_speech_readings(cleaned, custom_readings=reading_overrides)


def _combine_rate(sister_rate: str, offset: int) -> str:
    match = re.match(r"^\s*([+-]?\d+)\s*%?\s*$", sister_rate or "+0%")
    base = int(match.group(1)) if match else 0
    total = max(-50, min(100, base + int(offset)))
    return f"{total:+d}%"


def _resolve_audio_player() -> tuple[str, str] | None:
    for candidate in ("/usr/bin/mpv", shutil.which("mpv")):
        if candidate and Path(candidate).is_file():
            return ("mpv", candidate)
    for candidate in ("/usr/bin/ffplay", shutil.which("ffplay")):
        if candidate and Path(candidate).is_file():
            return ("ffplay", candidate)
    return None


class TTSManager(QObject):
    """Synthesize and play Sister voice responses via Edge-TTS or VOICEVOX."""

    speaking_started = pyqtSignal()
    speaking_finished = pyqtSignal()
    error_occurred = pyqtSignal(str)

    def __init__(self, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._lock = threading.Lock()
        self._speaking = False
        self._generation = 0
        self._player_proc: subprocess.Popen[bytes] | None = None

    def is_speaking(self) -> bool:
        with self._lock:
            return self._speaking

    def stop(self) -> None:
        """Stop any ongoing synthesis or audio playback."""
        with self._lock:
            self._generation += 1
            proc = self._player_proc
            was_speaking = self._speaking
            self._speaking = False

        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=1.0)
            except (OSError, subprocess.SubprocessError):
                try:
                    proc.kill()
                except OSError:
                    pass
        if was_speaking:
            self.speaking_finished.emit()

    def speak(self, text: str, sister: SisterProfile, config: AppConfig) -> None:
        """Speak `text` asynchronously using the configured TTS engine."""
        engine = (config.tts_engine or "edge").lower()
        if engine == "off":
            return

        cleaned = clean_text_for_speech(
            text,
            skip_code=config.tts_skip_code,
            max_chars=config.tts_max_chars,
            reading_overrides=getattr(config, "tts_reading_overrides", None),
        )
        if not cleaned:
            return

        self.stop()
        with self._lock:
            self._generation += 1
            gen = self._generation
            self._speaking = True

        self.speaking_started.emit()
        worker = threading.Thread(
            target=self._speak_worker,
            args=(gen, cleaned, sister, config, engine),
            daemon=True,
        )
        worker.start()

    def _is_current(self, gen: int) -> bool:
        with self._lock:
            return self._generation == gen

    def _finish_generation(self, gen: int) -> None:
        should_emit = False
        with self._lock:
            self._player_proc = None
            if self._generation == gen and self._speaking:
                self._speaking = False
                should_emit = True
        if should_emit:
            self.speaking_finished.emit()

    def _speak_worker(
        self,
        gen: int,
        text: str,
        sister: SisterProfile,
        config: AppConfig,
        engine: str,
    ) -> None:
        tmp_path: Path | None = None
        try:
            player = _resolve_audio_player()
            if player is None:
                if self._is_current(gen):
                    self.error_occurred.emit("音声再生プレイヤー (mpv または ffplay) が見つかりません。")
                return

            if engine == "edge":
                try:
                    import edge_tts  # type: ignore[import-not-found]
                except ImportError:
                    if self._is_current(gen):
                        self.error_occurred.emit(
                            "edge-tts が未インストールです。`pip install edge-tts` を実行してください。"
                        )
                    return

                fd, name = tempfile.mkstemp(prefix="sisters-tts-", suffix=".mp3")
                os.close(fd)
                tmp_path = Path(name)
                voice = _resolve_edge_voice(sister.voice_name)
                pitch = _normalize_pitch(sister.voice_pitch)
                rate = _combine_rate(sister.voice_rate, config.tts_rate_offset)

                async def _synthesize(v: str, p: str, r: str) -> None:
                    comm = edge_tts.Communicate(text, voice=v, pitch=p, rate=r)
                    await comm.save(str(tmp_path))

                try:
                    asyncio.run(_synthesize(voice, pitch, rate))
                    if not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
                        raise RuntimeError("Empty audio received from Edge-TTS")
                except Exception:
                    if not self._is_current(gen):
                        return
                    if (voice, pitch, rate) == (DEFAULT_EDGE_VOICE, "+0Hz", "+0%"):
                        raise
                    tmp_path.unlink(missing_ok=True)
                    asyncio.run(_synthesize(DEFAULT_EDGE_VOICE, "+0Hz", "+0%"))

            elif engine == "voicevox":
                base_url = (config.voicevox_url or "http://127.0.0.1:50021").rstrip("/")
                speaker = int(config.voicevox_speaker)
                query_qs = urllib.parse.urlencode({"text": text, "speaker": speaker})
                req_query = urllib.request.Request(f"{base_url}/audio_query?{query_qs}", method="POST")
                with urllib.request.urlopen(req_query, timeout=8) as resp:
                    query_json = resp.read()

                # Apply config rate offset to VOICEVOX speedScale lightly if non-zero
                if config.tts_rate_offset:
                    try:
                        parsed_q = json.loads(query_json.decode("utf-8"))
                        parsed_q["speedScale"] = max(0.5, min(2.0, 1.0 + config.tts_rate_offset / 100.0))
                        query_json = json.dumps(parsed_q).encode("utf-8")
                    except (ValueError, TypeError):
                        pass

                synth_qs = urllib.parse.urlencode({"speaker": speaker})
                req_synth = urllib.request.Request(
                    f"{base_url}/synthesis?{synth_qs}",
                    data=query_json,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(req_synth, timeout=15) as resp:
                    wav_bytes = resp.read()

                fd, name = tempfile.mkstemp(prefix="sisters-vv-", suffix=".wav")
                os.close(fd)
                tmp_path = Path(name)
                tmp_path.write_bytes(wav_bytes)
            else:
                if self._is_current(gen):
                    self.error_occurred.emit(f"未対応のTTSエンジンです: {engine}")
                return

            if not self._is_current(gen) or tmp_path is None:
                return
            if not tmp_path.is_file() or tmp_path.stat().st_size <= 0:
                if self._is_current(gen):
                    self.error_occurred.emit("音声データが生成されませんでした（0バイト）。")
                return

            player_kind, player_bin = player
            volume = max(0, min(100, int(config.tts_volume)))
            if player_kind == "mpv":
                cmd = [player_bin, "--no-video", "--really-quiet", f"--volume={volume}", str(tmp_path)]
            else:
                cmd = [player_bin, "-nodisp", "-autoexit", "-loglevel", "quiet", "-volume", str(volume), str(tmp_path)]

            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            with self._lock:
                if self._generation != gen:
                    proc.terminate()
                    return
                self._player_proc = proc
            _, err_bytes = proc.communicate()
            if proc.returncode not in (0, -15, -9) and self._is_current(gen):
                err_txt = (err_bytes or b"").decode("utf-8", errors="replace").strip()
                self.error_occurred.emit(
                    f"音声再生プレイヤー ({player_kind}) が失敗しました (code {proc.returncode}): {err_txt or '不明なエラー'}"
                )

        except urllib.error.URLError as exc:
            if self._is_current(gen):
                self.error_occurred.emit(f"VOICEVOXに接続できませんでした: {exc.reason}")
        except Exception as exc:
            if self._is_current(gen):
                self.error_occurred.emit(f"音声合成/再生に失敗しました: {exc}")
        finally:
            if tmp_path is not None:
                tmp_path.unlink(missing_ok=True)
            self._finish_generation(gen)
