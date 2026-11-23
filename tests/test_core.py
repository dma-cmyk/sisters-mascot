"""Core unit and headless PyQt6 UI smoke tests for Sisters Mascot."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtCore import QEvent, QPoint, QPointF, QRect, QSize, Qt  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QFocusEvent,
    QGuiApplication,
    QImage,
    QInputMethodEvent,
    QKeyEvent,
    QWheelEvent,
)
from PyQt6.QtWidgets import QApplication  # noqa: E402

from sisters_mascot import PROJECT_ROOT as PKG_PROJECT_ROOT, SPRITES_DIR  # noqa: E402
from sisters_mascot.agents.agy_adapter import AgyAdapter  # noqa: E402
from sisters_mascot.agents.base import ModelChoice  # noqa: E402
from sisters_mascot.agents.claude_adapter import ClaudeAdapter  # noqa: E402
from sisters_mascot.agents.codex_adapter import CodexAdapter  # noqa: E402
from sisters_mascot.agents.opencode_adapter import OpenCodeAdapter  # noqa: E402
from sisters_mascot.agents.router import AGENT_IDS, AgentRouter  # noqa: E402
from sisters_mascot.audio.stt import VoiceRecorder  # noqa: E402
from sisters_mascot.audio.tts import (  # noqa: E402
    DEFAULT_EDGE_VOICE,
    SUPPORTED_EDGE_JA_VOICES,
    TTSManager,
    _normalize_pitch,
    _resolve_edge_voice,
    clean_text_for_speech,
)
from sisters_mascot.config import (  # noqa: E402
    AppConfig,
    AppItem,
    BookmarkItem,
    FolderItem,
    QuickPromptItem,
    load_config,
    save_config,
)
from sisters_mascot.sisters_loader import (  # noqa: E402
    get_sister_map,
    has_custom_or_generated_sprite,
    load_all_sisters,
)
from sisters_mascot.sprites.renderer import render_sister_pixmap  # noqa: E402
from sisters_mascot.ui.bubble_panel import BubblePanel  # noqa: E402
from sisters_mascot.ui.mascot_window import MascotWindow  # noqa: E402
from sisters_mascot.ui.platform import supports_window_positioning  # noqa: E402
from sisters_mascot.ui.widgets import ChatInputEdit, LauncherItemDialog, ToastBubble  # noqa: E402

EXPECTED_SISTER_IDS = [
    "aoi",
    "ruri",
    "kohaku",
    "sumire",
    "akari",
    "shion",
    "hiiro",
    "mashiro",
    "kokonoe",
    "towa",
    "yui",
    "aya",
    "ritsu",
    "nagi",
    "kanon",
    "akane",
    "homura",
]

ILLUSTRATED_SISTER_IDS = list(EXPECTED_SISTER_IDS)


_QAPP: QApplication | None = None


def _ensure_qapp() -> QApplication:
    global _QAPP
    app = QApplication.instance()
    if isinstance(app, QApplication):
        _QAPP = app
        return app
    _QAPP = QApplication([])
    return _QAPP


def _make_ui(cfg: AppConfig) -> tuple[MascotWindow, BubblePanel]:
    """Build a mascot + panel pair sharing `cfg` for headless positioning tests."""
    sisters = load_all_sisters()
    aoi = next(s for s in sisters if s.id == "aoi")
    mascot = MascotWindow(sisters, aoi, cfg)
    panel = BubblePanel(sisters, aoi, cfg, AgentRouter(), VoiceRecorder())
    return mascot, panel


class TestSistersLoader(unittest.TestCase):
    def test_load_all_sisters_birth_order_and_fields(self) -> None:
        sisters = load_all_sisters()
        self.assertEqual(len(sisters), 17)
        self.assertEqual([s.order for s in sisters], list(range(1, 18)))
        self.assertEqual([s.id for s in sisters], EXPECTED_SISTER_IDS)

        for sister in sisters:
            self.assertTrue(sister.display_name.strip(), f"{sister.id} display_name empty")
            self.assertTrue(sister.rank_label.strip(), f"{sister.id} rank_label empty")
            self.assertTrue(sister.role.strip(), f"{sister.id} role empty")
            self.assertTrue(sister.persona_prompt.strip(), f"{sister.id} persona_prompt empty")
            self.assertTrue(sister.voice_name.startswith("ja-JP-"), f"{sister.id} voice_name invalid")
            self.assertGreater(len(sister.click_lines), 0, f"{sister.id} click_lines empty")
            self.assertGreater(len(sister.drag_lines), 0, f"{sister.id} drag_lines empty")

        sister_map = get_sister_map()
        self.assertEqual(list(sister_map.keys()), EXPECTED_SISTER_IDS)

    def test_portability_without_sisters_wiki(self) -> None:
        self.assertEqual(PKG_PROJECT_ROOT, PROJECT_ROOT)
        sisters = load_all_sisters(agents_dir=Path("/nonexistent/sisters-wiki-agents"))
        self.assertEqual(len(sisters), 17)
        self.assertEqual([s.order for s in sisters], list(range(1, 18)))
        self.assertEqual([s.id for s in sisters], EXPECTED_SISTER_IDS)
        for sister in sisters:
            self.assertTrue(sister.display_name.strip())
            self.assertTrue(sister.persona_prompt.strip())


class TestConfigPersistence(unittest.TestCase):
    def test_defaults_include_playwright_and_launcher_items(self) -> None:
        cfg = AppConfig()
        self.assertGreaterEqual(len(cfg.quick_prompts), 3)
        self.assertTrue(
            any("Playwright" in qp.prompt for qp in cfg.quick_prompts),
            "Default quick prompts should include Playwright Chrome prompts",
        )
        self.assertGreater(len(cfg.apps), 0)
        self.assertGreater(len(cfg.folders), 0)
        self.assertGreater(len(cfg.bookmarks), 0)

    def test_portable_defaults_follow_home_directory(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            (fake_home / "Projects").mkdir()
            (fake_home / "Downloads").mkdir()
            with patch("pathlib.Path.home", return_value=fake_home):
                cfg = AppConfig()
        self.assertEqual(cfg.working_dir, str(fake_home))
        folder_paths = [f.path for f in cfg.folders]
        self.assertIn(str(fake_home), folder_paths)
        self.assertIn(str(fake_home / "Projects"), folder_paths)
        self.assertFalse(any("/home/dma" in p for p in folder_paths))

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "nested" / "config.json"

            cfg = AppConfig(
                sister_id="nagi",
                agent_id="claude",
                models_by_agent={"claude": "claude-sonnet-4-6", "agy": "gemini-2.5-pro"},
                working_dir=str(PROJECT_ROOT),
                auto_approve=False,
                mascot_x=120,
                mascot_y=240,
                mascot_scale="large",
                panel_x=64,
                panel_y=48,
                always_on_top=False,
                tts_engine="voicevox",
                tts_volume=65,
                tts_rate_offset=10,
                tts_max_chars=180,
                tts_click_reactions=False,
                tts_skip_code=False,
                voicevox_url="http://127.0.0.1:50021",
                voicevox_speaker=8,
                stt_engine="custom",
                stt_command="whisper {wav}",
                stt_auto_send=True,
                quick_prompts=[
                    QuickPromptItem(
                        id="qp1",
                        label="Test Prompt",
                        icon="🧪",
                        prompt="Run checks",
                        send_immediately=True,
                        preferred_agent="codex",
                    )
                ],
                apps=[AppItem(id="app1", label="Konsole", icon="🖥️", command="konsole")],
                folders=[FolderItem(id="fld1", label="Home", icon="🏠", path=str(Path.home()))],
                bookmarks=[BookmarkItem(id="bm1", label="Example", icon="🌐", url="https://example.com")],
            )
            saved = save_config(cfg, cfg_path)
            self.assertEqual(saved, cfg_path)
            self.assertTrue(cfg_path.is_file())

            loaded = load_config(cfg_path)
            self.assertEqual(loaded.sister_id, "nagi")
            self.assertEqual(loaded.agent_id, "claude")
            self.assertEqual(loaded.models_by_agent, {"claude": "claude-sonnet-4-6", "agy": "gemini-2.5-pro"})
            self.assertEqual(loaded.working_dir, str(PROJECT_ROOT))
            self.assertFalse(loaded.auto_approve)
            self.assertEqual(loaded.mascot_x, 120)
            self.assertEqual(loaded.mascot_y, 240)
            self.assertEqual(loaded.mascot_scale, "large")
            self.assertEqual(loaded.panel_x, 64)
            self.assertEqual(loaded.panel_y, 48)
            self.assertFalse(loaded.always_on_top)
            self.assertEqual(loaded.tts_engine, "voicevox")
            self.assertEqual(loaded.tts_volume, 65)
            self.assertEqual(loaded.tts_rate_offset, 10)
            self.assertTrue(loaded.stt_auto_send)
            self.assertEqual(len(loaded.quick_prompts), 1)
            self.assertEqual(loaded.quick_prompts[0].preferred_agent, "codex")
            self.assertEqual(loaded.apps[0].command, "konsole")
            self.assertEqual(loaded.folders[0].path, str(Path.home()))
            self.assertEqual(loaded.bookmarks[0].url, "https://example.com")

    def test_load_corrupt_or_missing_returns_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            missing = Path(tmpdir) / "missing.json"
            self.assertEqual(load_config(missing).sister_id, "aoi")

            corrupt = Path(tmpdir) / "corrupt.json"
            corrupt.write_text("{not valid json", encoding="utf-8")
            self.assertEqual(load_config(corrupt).agent_id, "agy")

    def test_clamping_and_legacy_scale_migration(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            legacy = Path(tmpdir) / "legacy.json"
            legacy.write_text(json.dumps({"mascot_scale": "large"}), encoding="utf-8")
            loaded_legacy = load_config(legacy)
            self.assertEqual(loaded_legacy.mascot_width, 275)
            self.assertEqual(loaded_legacy.animation_mode, "rich")
            self.assertEqual(loaded_legacy.panel_width, 440)
            self.assertEqual(loaded_legacy.panel_height, 600)

            extreme = Path(tmpdir) / "extreme.json"
            extreme.write_text(
                json.dumps(
                    {
                        "mascot_width": 999,
                        "panel_width": 120,
                        "panel_height": 99999,
                        "animation_mode": "invalid",
                    }
                ),
                encoding="utf-8",
            )
            loaded_extreme = load_config(extreme)
            self.assertEqual(loaded_extreme.mascot_width, 420)
            self.assertEqual(loaded_extreme.panel_width, 360)
            self.assertEqual(loaded_extreme.panel_height, 4320)
            self.assertEqual(loaded_extreme.animation_mode, "rich")

            # Large-but-valid panel sizes are no longer capped at the old 820x920.
            large = Path(tmpdir) / "large.json"
            large.write_text(json.dumps({"panel_width": 1200, "panel_height": 1000}), encoding="utf-8")
            loaded_large = load_config(large)
            self.assertEqual((loaded_large.panel_width, loaded_large.panel_height), (1200, 1000))

            self.assertIsNone(AppConfig().panel_x)
            self.assertIsNone(AppConfig().panel_y)


class TestAgentAdapters(unittest.TestCase):
    def test_agy_command_and_parsing(self) -> None:
        adapter = AgyAdapter()
        cmd, stdin_text = adapter.build_command(
            "hello",
            model="gemini-2.5-pro",
            session_id="conv-123",
            auto_approve=True,
        )
        self.assertIsNone(stdin_text)
        self.assertIn("--model", cmd)
        self.assertIn("gemini-2.5-pro", cmd)
        self.assertIn("--conversation", cmd)
        self.assertIn("conv-123", cmd)
        self.assertIn("--mode", cmd)
        self.assertIn("auto", cmd)

        cmd_no_auto, _ = adapter.build_command("hi", auto_approve=False)
        self.assertNotIn("--mode", cmd_no_auto)

        adapter.reset_state()
        evs = adapter.parse_line('{"type":"stream_event","session_id":"conv-99","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"こん"}}}')
        self.assertTrue(any(e.kind == "session" and e.session_id == "conv-99" for e in evs))
        self.assertTrue(any(e.kind == "delta" and e.text == "こん" for e in evs))

        evs = adapter.parse_line('{"type":"stream_event","event":{"type":"content_block_start","content_block":{"type":"tool_use","name":"mcp__playwright__browser_snapshot"}}}')
        self.assertTrue(any(e.kind == "tool" and "Playwright" in e.text for e in evs))

        # Final assistant & result must NOT re-emit duplicate text after stream_event deltas
        evs_asst = adapter.parse_line('{"type":"assistant","message":{"content":[{"type":"text","text":"こん"}]}}')
        self.assertFalse(any(e.kind == "delta" for e in evs_asst))
        evs_res = adapter.parse_line('{"type":"result","result":"こん"}')
        self.assertEqual([e.kind for e in evs_res], ["done"])

        evs_err = adapter.parse_line('{"type":"result","is_error":true,"result":"boom"}')
        self.assertEqual([(e.kind, e.text) for e in evs_err], [("error", "boom")])

    def test_claude_command_and_parsing(self) -> None:
        adapter = ClaudeAdapter()
        cmd, stdin_text = adapter.build_command(
            "hello",
            model="claude-sonnet-4-6",
            session_id="sess-42",
            system_prompt="Be gentle",
            auto_approve=True,
        )
        self.assertIsNone(stdin_text)
        self.assertIn("--model", cmd)
        self.assertIn("claude-sonnet-4-6", cmd)
        self.assertIn("--resume", cmd)
        self.assertIn("sess-42", cmd)
        self.assertIn("--append-system-prompt", cmd)
        self.assertIn("--permission-mode", cmd)
        self.assertIn("acceptEdits", cmd)

        cmd_default, _ = adapter.build_command("hello", model="default", auto_approve=False)
        self.assertNotIn("--model", cmd_default)
        self.assertNotIn("--permission-mode", cmd_default)

        adapter.reset_state()
        evs = adapter.parse_line('{"type":"system","subtype":"init","session_id":"sess-abc"}')
        self.assertEqual([(e.kind, e.session_id) for e in evs], [("session", "sess-abc")])

        evs = adapter.parse_line('{"type":"stream_event","event":{"type":"content_block_delta","delta":{"type":"text_delta","text":"やあ"}}}')
        self.assertEqual([(e.kind, e.text) for e in evs], [("delta", "やあ")])

        evs = adapter.parse_line('{"type":"assistant","message":{"content":[{"type":"tool_use","name":"browser_navigate"},{"type":"text","text":"やあ"}]}}')
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0].kind, "tool")
        self.assertIn("Playwright", evs[0].text)

        evs_res = adapter.parse_line('{"type":"result","session_id":"sess-abc","result":"やあ"}')
        self.assertEqual([e.kind for e in evs_res], ["session", "done"])

        evs_err = adapter.parse_line('{"type":"error","error":{"message":"rate limit"}}')
        self.assertEqual([(e.kind, e.text) for e in evs_err], [("error", "rate limit")])

    def test_codex_command_and_parsing(self) -> None:
        adapter = CodexAdapter()
        cmd, stdin_text = adapter.build_command(
            "inspect page",
            model="gpt-5.4",
            session_id=None,
            cwd=str(PROJECT_ROOT),
            auto_approve=True,
        )
        self.assertEqual(stdin_text, "inspect page")
        self.assertIn("exec", cmd)
        self.assertIn("-m", cmd)
        self.assertIn("gpt-5.4", cmd)
        self.assertIn("--approve-for-me", cmd)

        cmd_resume, stdin_resume = adapter.build_command(
            "follow up",
            model="o3",
            session_id="thread-77",
            auto_approve=False,
        )
        self.assertEqual(stdin_resume, "follow up")
        self.assertIn("resume", cmd_resume)
        self.assertIn("-m", cmd_resume)
        self.assertIn("o3", cmd_resume)
        self.assertIn("thread-77", cmd_resume)
        self.assertNotIn("--approve-for-me", cmd_resume)

        adapter.reset_state()
        evs = adapter.parse_line('{"type":"thread.started","thread_id":"thread-77"}')
        self.assertEqual([(e.kind, e.session_id) for e in evs], [("session", "thread-77")])

        evs = adapter.parse_line('{"type":"item.started","item":{"id":"t1","type":"mcp_tool_call","server":"playwright","tool":"browser_click"}}')
        self.assertEqual(len(evs), 1)
        self.assertIn("Playwright", evs[0].text)

        evs1 = adapter.parse_line('{"type":"item.updated","item":{"id":"m1","type":"agent_message","text":"了解"}}')
        self.assertEqual([(e.kind, e.text) for e in evs1], [("delta", "了解")])
        evs2 = adapter.parse_line('{"type":"item.completed","item":{"id":"m1","type":"agent_message","text":"了解です"}}')
        self.assertEqual([(e.kind, e.text) for e in evs2], [("delta", "です")])
        # Re-emitting identical completed text produces no duplicates
        evs3 = adapter.parse_line('{"type":"item.completed","item":{"id":"m1","type":"agent_message","text":"了解です"}}')
        self.assertEqual(evs3, [])

        evs_done = adapter.parse_line('{"type":"turn.completed"}')
        self.assertEqual([e.kind for e in evs_done], ["done"])

        evs_fail = adapter.parse_line('{"type":"turn.failed","error":{"message":"sandbox denied"}}')
        self.assertEqual([(e.kind, e.text) for e in evs_fail], [("error", "sandbox denied")])

    def test_opencode_command_and_parsing(self) -> None:
        adapter = OpenCodeAdapter()
        cmd, stdin_text = adapter.build_command(
            "summarize",
            model="opencode/big-pickle",
            session_id="oc-5",
            auto_approve=True,
        )
        self.assertIsNone(stdin_text)
        self.assertIn("-m", cmd)
        self.assertIn("opencode/big-pickle", cmd)
        self.assertIn("-s", cmd)
        self.assertIn("oc-5", cmd)
        self.assertIn("--auto", cmd)
        self.assertEqual(cmd[-1], "summarize")

        cmd_no_auto, _ = adapter.build_command("hi", auto_approve=False)
        self.assertNotIn("--auto", cmd_no_auto)

        adapter.reset_state()
        evs = adapter.parse_line('{"type":"text","sessionID":"oc-5","part":{"id":"p1","type":"text","text":"テスト"}}')
        self.assertTrue(any(e.kind == "session" and e.session_id == "oc-5" for e in evs))
        self.assertTrue(any(e.kind == "delta" and e.text == "テスト" for e in evs))

        evs2 = adapter.parse_line('{"type":"text","part":{"id":"p1","type":"text","text":"テスト完了"}}')
        self.assertEqual([(e.kind, e.text) for e in evs2], [("delta", "完了")])

        # Duplicate text emits nothing
        evs_dup = adapter.parse_line('{"type":"text","part":{"id":"p1","type":"text","text":"テスト完了"}}')
        self.assertEqual(evs_dup, [])

        evs_tool = adapter.parse_line('{"type":"tool_use","part":{"type":"tool","tool":"playwright_browser_take_screenshot"}}')
        self.assertEqual(len(evs_tool), 1)
        self.assertIn("Playwright", evs_tool[0].text)

        evs_done = adapter.parse_line('{"type":"step_finish","part":{"reason":"stop"}}')
        self.assertEqual([e.kind for e in evs_done], ["done"])

        evs_err = adapter.parse_line('{"type":"error","error":{"data":{"message":"provider offline"}}}')
        self.assertEqual([(e.kind, e.text) for e in evs_err], [("error", "provider offline")])

    def test_dynamic_model_discovery_parsers(self) -> None:
        agy = AgyAdapter()
        agy_stdout = (
            "Fetching available models...\n"
            "gemini-4-hydrogen-high\tGemini 4 Hydrogen (High)\n"
            "claude-sonnet-5-5-high\tClaude Sonnet 5.5 (High)\n"
        )
        with (
            patch.object(agy, "is_available", return_value=True),
            patch("sisters_mascot.agents.agy_adapter.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=["agy", "models"], returncode=0, stdout=agy_stdout, stderr=""
            )
            agy_choices = agy.discover_model_choices()
        self.assertEqual([c.id for c in agy_choices], ["gemini-4-hydrogen-high", "claude-sonnet-5-5-high"])
        self.assertIn("Gemini 4 Hydrogen (High)", agy_choices[0].label)
        self.assertNotIn("gemini-2.5-pro", [c.id for c in agy_choices])

        oc = OpenCodeAdapter()
        oc_stdout = "opencode/big-pickle\nopencode/mimo-v2.6-flash-free\n"
        with (
            patch.object(oc, "is_available", return_value=True),
            patch("sisters_mascot.agents.opencode_adapter.subprocess.run") as mock_run,
        ):
            mock_run.return_value = subprocess.CompletedProcess(
                args=["opencode", "models"], returncode=0, stdout=oc_stdout, stderr=""
            )
            oc_models = oc.discover_models()
        self.assertEqual(oc_models, ["opencode/big-pickle", "opencode/mimo-v2.6-flash-free"])

        with tempfile.TemporaryDirectory() as tmpdir:
            fake_home = Path(tmpdir)
            codex_dir = fake_home / ".codex"
            codex_dir.mkdir()
            (codex_dir / "config.toml").write_text('model = "gpt-6.1-sol"\n', encoding="utf-8")
            (codex_dir / "models_cache.json").write_text(
                json.dumps(
                    {
                        "models": [
                            {"slug": "gpt-6-astra", "display_name": "GPT-6-Astra", "visibility": "list", "priority": 2},
                            {"slug": "gpt-reserve", "display_name": "GPT-Reserve", "visibility": "hide", "priority": 0},
                            {"slug": "gpt-6.1-sol", "display_name": "GPT-6.1-Sol", "visibility": "list", "priority": 1},
                        ]
                    }
                ),
                encoding="utf-8",
            )
            claude_dir = fake_home / ".claude"
            claude_dir.mkdir()
            (claude_dir / "settings.json").write_text(
                json.dumps(
                    {
                        "model": "claude-opus-5-5",
                        "env": {
                            "ANTHROPIC_DEFAULT_SONNET_MODEL": "claude-sonnet-4-6",
                            "ANTHROPIC_API_KEY": "should-never-appear",
                        },
                    }
                ),
                encoding="utf-8",
            )
            with (
                patch("pathlib.Path.home", return_value=fake_home),
                patch.dict(os.environ, {}, clear=True),
            ):
                codex_choices = CodexAdapter().discover_model_choices()
                claude_models = ClaudeAdapter().discover_models()

        self.assertEqual([c.id for c in codex_choices], ["gpt-6.1-sol", "gpt-6-astra"])
        self.assertEqual(codex_choices[0].label, "GPT-6.1-Sol (gpt-6.1-sol)")
        self.assertNotIn("gpt-5.4", [c.id for c in codex_choices])
        self.assertIn("claude-opus-5-5", claude_models)
        self.assertIn("claude-sonnet-4-6", claude_models)
        self.assertIn("sonnet", claude_models)
        self.assertNotIn("should-never-appear", claude_models)


class TestSpeechSanitizer(unittest.TestCase):
    def test_clean_text_for_speech_strips_markdown_and_truncates_at_sentence(self) -> None:
        raw = (
            "## 概要\n"
            "- **重要**: `sisters_mascot` の [公式ドキュメント](https://example.com/docs) を確認しました。\n"
            "詳細は https://example.com/raw を参照してください！\n"
            "```python\nprint('should be removed')\n```\n"
            "次の文はとても長くて、ここで綺麗に句点で区切られるべき追加の説明文です。"
        )
        cleaned = clean_text_for_speech(raw, skip_code=True, max_chars=220)
        self.assertNotIn("```", cleaned)
        self.assertNotIn("should be removed", cleaned)
        self.assertNotIn("`", cleaned)
        self.assertNotIn("https://", cleaned)
        self.assertNotIn("**", cleaned)
        self.assertIn("sisters_mascot", cleaned)
        self.assertIn("公式ドキュメント", cleaned)

        long_text = (
            "最初の文は十分な長さがあります。二番目の文もさらに続いて長さを増やしていきます。"
            "三番目の文が制限を超えて途中で切られるはずですが、二番目の句点で止まります。"
        )
        truncated = clean_text_for_speech(long_text, max_chars=45)
        self.assertLessEqual(len(truncated), 45)
        self.assertTrue(truncated.endswith("。"))

    def test_all_17_sisters_use_supported_edge_voices_and_fallback(self) -> None:
        sisters = load_all_sisters()
        for sister in sisters:
            self.assertIn(
                sister.voice_name,
                SUPPORTED_EDGE_JA_VOICES,
                f"{sister.id} has unsupported Edge-TTS voice {sister.voice_name}",
            )
            self.assertRegex(sister.voice_pitch, r"^[+-]\d+Hz$")

        self.assertEqual(_resolve_edge_voice("ja-JP-AoiNeural"), DEFAULT_EDGE_VOICE)
        self.assertEqual(_resolve_edge_voice("ja-JP-ShioriNeural"), DEFAULT_EDGE_VOICE)
        self.assertEqual(_resolve_edge_voice("ja-JP-KeitaNeural"), "ja-JP-KeitaNeural")
        self.assertEqual(_normalize_pitch("8Hz"), "+8Hz")
        self.assertEqual(_normalize_pitch("-12Hz"), "-12Hz")
        self.assertEqual(_normalize_pitch("bad"), "+0Hz")

        # Verify TTSManager retries/falls back cleanly when given an unsupported voice
        _ensure_qapp()
        cfg = AppConfig(tts_engine="edge")
        sister_bad = load_all_sisters()[0]
        sister_bad.voice_name = "ja-JP-AoiNeural"
        seen_calls: list[tuple[str, str, str]] = []

        class FakeComm:
            def __init__(self, text: str, voice: str, pitch: str, rate: str) -> None:
                seen_calls.append((voice, pitch, rate))

            async def save(self, out_path: str) -> None:
                Path(out_path).write_bytes(b"ID3fake-mp3-bytes")

        fake_edge_mod = type("edge_tts", (), {"Communicate": FakeComm})
        fake_proc = type(
            "FakeProc",
            (),
            {"returncode": 0, "poll": lambda self: 0, "communicate": lambda self: (b"", b""), "terminate": lambda self: None},
        )()

        tts = TTSManager()
        errors: list[str] = []
        tts.error_occurred.connect(errors.append)
        with (
            patch.dict(sys.modules, {"edge_tts": fake_edge_mod}),
            patch("sisters_mascot.audio.tts._resolve_audio_player", return_value=("mpv", "/usr/bin/mpv")),
            patch("sisters_mascot.audio.tts.subprocess.Popen", return_value=fake_proc),
        ):
            tts._generation = 1
            tts._speaking = True
            tts._speak_worker(1, "テスト", sister_bad, cfg, "edge")
        self.assertEqual(errors, [])
        self.assertEqual(len(seen_calls), 1)
        self.assertTrue(all(v == DEFAULT_EDGE_VOICE for v, _, _ in seen_calls))


class TestGeneratedSprites(unittest.TestCase):
    def test_all_17_sprites_are_clean_rgba_with_transparent_corners(self) -> None:
        self.assertEqual(len(ILLUSTRATED_SISTER_IDS), 17)
        for sister_id in ILLUSTRATED_SISTER_IDS:
            sprite_path = SPRITES_DIR / sister_id / "idle.png"
            self.assertTrue(sprite_path.is_file(), f"Missing generated sprite: {sprite_path}")

            img = QImage(str(sprite_path))
            self.assertFalse(img.isNull(), f"Failed to load sprite: {sprite_path}")
            self.assertTrue(img.hasAlphaChannel(), f"Sprite lacks alpha channel: {sprite_path}")
            self.assertLessEqual(img.height(), 540, f"Sprite taller than 540px: {sprite_path}")
            self.assertGreater(img.width(), 80)

            rgba = img.convertToFormat(QImage.Format.Format_RGBA8888)
            w, h = rgba.width(), rgba.height()

            # All 4 corners must be fully transparent
            for cx, cy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
                self.assertEqual(
                    rgba.pixelColor(cx, cy).alpha(),
                    0,
                    f"{sister_id} corner ({cx},{cy}) is not transparent",
                )

            # Center region must contain opaque subject pixels
            opaque_center = 0
            for y in range(h // 3, (2 * h) // 3, 4):
                for x in range(w // 3, (2 * w) // 3, 4):
                    if rgba.pixelColor(x, y).alpha() >= 220:
                        opaque_center += 1
            self.assertGreater(opaque_center, 25, f"{sister_id} center lacks opaque pixels")

    def test_all_17_sisters_render_standing_sprites(self) -> None:
        _ensure_qapp()
        sister_map = get_sister_map()
        for sid in EXPECTED_SISTER_IDS:
            self.assertTrue(has_custom_or_generated_sprite(sid), f"{sid} should have bundled sprite")
            pm = render_sister_pixmap(sister_map[sid], "idle", 210, 1.0)
            self.assertFalse(pm.isNull(), f"Sprite render failed for {sid}")
            self.assertGreater(pm.width(), 100)
            self.assertGreater(pm.height(), 200)


class TestHeadlessUI(unittest.TestCase):
    def test_mascot_panel_toast_smoke(self) -> None:
        app = _ensure_qapp()
        sisters = load_all_sisters()
        sister_map = {s.id: s for s in sisters}
        cfg = AppConfig(tts_engine="off")
        router = AgentRouter()
        recorder = VoiceRecorder()

        mascot = MascotWindow(sisters, sister_map["aoi"], cfg)
        panel = BubblePanel(sisters, sister_map["aoi"], cfg, router, recorder)
        toast = ToastBubble()

        try:
            mascot.show()
            toast.show_toast(
                QRect(100, 100, 200, 300),
                "テストトーストです。",
                title="長女 · 碧",
                accent="#4A7CFF",
                duration_ms=1500,
            )
            self.assertTrue(toast.isVisible())

            # Switch across sisters including newly illustrated sprites: aoi -> nagi -> homura
            for sid in ("nagi", "homura"):
                cfg.sister_id = sid
                mascot.set_sister(sister_map[sid])
                panel.notify_sister_switched(sister_map[sid])
                self.assertIn(sister_map[sid].display_name, mascot._minibar_label.text())

            # Switch all 4 agents & custom models
            for aid in AGENT_IDS:
                custom_model = f"test-{aid}-model"
                cfg.agent_id = aid
                cfg.models_by_agent[aid] = custom_model
                mascot.set_agent_id(aid)
                panel.sync_from_config(sister_map[cfg.sister_id])
                self.assertEqual(panel._model_combo.currentText(), custom_model)

            # Toggle panel visibility
            self.assertFalse(panel.isVisible())
            panel.toggle_for_mascot(mascot.frameGeometry())
            self.assertTrue(panel.isVisible())
            panel.toggle_for_mascot(mascot.frameGeometry())
            self.assertFalse(panel.isVisible())

            # Add and edit a launcher item via LauncherItemDialog without blocking modal exec
            add_dlg = LauncherItemDialog("bookmark", None, panel)
            add_dlg._label_edit.setText("Playwright Docs")
            add_dlg._url_edit.setText("playwright.dev")
            add_dlg._on_accept()
            created = add_dlg.get_item()
            self.assertIsInstance(created, BookmarkItem)
            self.assertEqual(created.url, "https://playwright.dev")

            panel._launcher_list_for_kind("bookmark").append(created)
            panel._rebuild_launcher_sections()
            self.assertTrue(any(b.id == created.id for b in cfg.bookmarks))

            edit_dlg = LauncherItemDialog("bookmark", created, panel)
            edit_dlg._label_edit.setText("Playwright Updated")
            edit_dlg._on_accept()
            updated = edit_dlg.get_item()
            self.assertEqual(updated.id, created.id)
            self.assertEqual(updated.label, "Playwright Updated")

            target_list = panel._launcher_list_for_kind("bookmark")
            for idx, existing in enumerate(target_list):
                if existing.id == created.id:
                    target_list[idx] = updated
                    break
            panel._rebuild_launcher_sections()
            self.assertTrue(any(b.label == "Playwright Updated" for b in cfg.bookmarks))

            # Animation modes, particles, activity states, and mascot resizing
            mascot.set_animation_mode("rich")
            mascot.trigger_bounce()
            self.assertGreater(len(mascot._particles), 0)
            mascot.set_activity("speaking")
            mascot._on_anim_tick()
            mascot.set_activity("tool")
            mascot._on_anim_tick()
            mascot.set_animation_mode("subtle")
            mascot._on_anim_tick()
            mascot.set_animation_mode("off")
            self.assertEqual(len(mascot._particles), 0)
            mascot.set_animation_mode("rich")

            mascot.set_mascot_width(130, emit_signal=True)
            self.assertEqual(cfg.mascot_width, 130)
            mascot.set_mascot_width(340, emit_signal=True)
            self.assertEqual(cfg.mascot_width, 340)

            wheel_up = QWheelEvent(
                QPointF(50.0, 50.0),
                QPointF(50.0, 50.0),
                QPointF(0.0, 0.0).toPoint(),
                QPointF(0.0, 120.0).toPoint(),
                Qt.MouseButton.NoButton,
                Qt.KeyboardModifier.NoModifier,
                Qt.ScrollPhase.NoScrollPhase,
                False,
            )
            mascot.wheelEvent(wheel_up)
            self.assertEqual(cfg.mascot_width, 354)

            # Panel resizing & dynamic model choice mapping
            self.assertEqual(panel.minimumWidth(), 360)
            self.assertEqual(panel.minimumHeight(), 440)
            panel.resize(520, 680)
            panel._on_resize_save_timeout()
            self.assertEqual((cfg.panel_width, cfg.panel_height), (520, 680))

            cfg.agent_id = "agy"
            panel.on_model_choices_discovered(
                "agy",
                [ModelChoice(id="gemini-4-hydrogen-high", label="Gemini 4 Hydrogen (High) (gemini-4-hydrogen-high)")],
            )
            idx = panel._model_combo.findData("gemini-4-hydrogen-high")
            self.assertGreater(idx, 0)
            panel._model_combo.setCurrentIndex(idx)
            panel._on_model_edited()
            self.assertEqual(cfg.models_by_agent.get("agy"), "gemini-4-hydrogen-high")

            app.processEvents()
        finally:
            toast.close()
            panel.close()
            mascot.close()
            app.processEvents()


class TestWindowIndependence(unittest.TestCase):
    """Mascot and chat panel must move/resize independently (offscreen platform)."""

    def _make_controller(self, cfg: AppConfig):
        from sisters_mascot.main import MascotAppController

        app = _ensure_qapp()
        with (
            patch("sisters_mascot.main.load_config", return_value=cfg),
            patch("sisters_mascot.main.save_config", return_value=Path("/nonexistent/config.json")),
            patch.object(BubblePanel, "ensure_models_discovered", lambda self, *a, **k: None),
            patch.object(MascotAppController, "_setup_single_instance_server", lambda self: None),
        ):
            return MascotAppController(app)

    def _cleanup_controller(self, controller) -> None:
        app = _ensure_qapp()
        try:
            app.aboutToQuit.disconnect(controller.shutdown)
        except (TypeError, RuntimeError):
            pass
        controller.shutdown()
        controller.panel.close()
        controller.mascot.close()
        app.processEvents()

    def test_mascot_move_does_not_drag_panel(self) -> None:
        app = _ensure_qapp()
        cfg = AppConfig(tts_engine="off")
        controller = self._make_controller(cfg)
        try:
            mascot, panel = controller.mascot, controller.panel
            mascot.move(480, 380)
            app.processEvents()
            panel.show_for_mascot(mascot.frameGeometry())
            app.processEvents()
            self.assertTrue(panel.isVisible())
            panel_pos = panel.pos()
            self.assertLess(panel_pos.x(), mascot.x())  # first show anchors beside mascot

            mascot.move(40, 40)
            app.processEvents()
            self.assertEqual(panel.pos(), panel_pos)
            mascot.reset_to_corner(save=True)
            app.processEvents()
            self.assertEqual(panel.pos(), panel_pos)

            controller._toggle_panel()
            self.assertFalse(panel.isVisible())
            controller._toggle_panel()
            self.assertTrue(panel.isVisible())
            self.assertEqual(panel.pos(), panel_pos)  # re-show never re-anchors

            mascot.move(100, 120)
            mascot._emit_position_changed()
            self.assertEqual((cfg.mascot_x, cfg.mascot_y), (100, 120))
        finally:
            self._cleanup_controller(controller)

    def test_panel_position_persisted_and_restored(self) -> None:
        app = _ensure_qapp()
        cfg = AppConfig(tts_engine="off")
        mascot, panel = _make_ui(cfg)
        try:
            mascot.show()
            mascot.move(480, 380)
            app.processEvents()
            panel.show_for_mascot(mascot.frameGeometry())
            app.processEvents()

            panel.move(120, 90)
            app.processEvents()
            panel._emit_position_changed()
            self.assertEqual((cfg.panel_x, cfg.panel_y), (120, 90))

            panel.hide()
            mascot.move(300, 200)
            app.processEvents()
            panel.show_for_mascot(mascot.frameGeometry())
            app.processEvents()
            self.assertEqual(panel.pos(), QPoint(120, 90))
        finally:
            panel.close()
            mascot.close()
            app.processEvents()

    def test_wayland_does_not_save_or_restore_positions(self) -> None:
        app = _ensure_qapp()
        cfg = AppConfig(tts_engine="off")
        controller = self._make_controller(cfg)
        try:
            panel = controller.panel
            panel.show_for_mascot(controller.mascot.frameGeometry())
            app.processEvents()
            panel._move_save_timer.stop()  # clear the pending offscreen placement save
            with patch.object(QGuiApplication, "platformName", return_value="wayland"):
                self.assertFalse(supports_window_positioning())

                controller._on_mascot_position_changed(11, 22)
                self.assertIsNone(cfg.mascot_x)
                self.assertIsNone(cfg.mascot_y)

                panel.move(150, 160)
                app.processEvents()
                self.assertFalse(panel._move_save_timer.isActive())
                panel._emit_position_changed()
                self.assertIsNone(cfg.panel_x)

                cfg.panel_x, cfg.panel_y = 300, 300
                panel.hide()
                panel.move(10, 10)
                app.processEvents()
                before = panel.pos()
                panel.show_for_mascot(controller.mascot.frameGeometry())
                app.processEvents()
                self.assertEqual(panel.pos(), before)  # no restore move on Wayland
        finally:
            self._cleanup_controller(controller)

    def test_panel_resize_grows_to_screen(self) -> None:
        app = _ensure_qapp()
        cfg = AppConfig(tts_engine="off")
        mascot, panel = _make_ui(cfg)
        try:
            fake_screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 2560, 1440))
            with patch.object(panel, "screen", return_value=fake_screen):
                panel._update_maximum_size()
            self.assertEqual((panel.maximumWidth(), panel.maximumHeight()), (2560, 1440))
            self.assertEqual((panel.minimumWidth(), panel.minimumHeight()), (360, 440))

            panel.resize(1200, 1000)
            panel._on_resize_save_timeout()
            self.assertEqual((cfg.panel_width, cfg.panel_height), (1200, 1000))
            self.assertEqual(panel._size_grip.size(), QSize(22, 22))
        finally:
            panel.close()
            mascot.close()
            app.processEvents()


class TestChatInputIME(unittest.TestCase):
    """Enter must send only when the IME is not converting/confirming."""

    def setUp(self) -> None:
        self.app = _ensure_qapp()
        self.edit = ChatInputEdit()
        self.sent: list[str] = []
        self.edit.send_requested.connect(lambda: self.sent.append(self.edit.toPlainText()))

    def tearDown(self) -> None:
        self.edit.close()
        self.edit.deleteLater()
        self.app.processEvents()

    @staticmethod
    def _enter(mods: Qt.KeyboardModifier = Qt.KeyboardModifier.NoModifier) -> QKeyEvent:
        return QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Return, mods, "\r")

    @staticmethod
    def _preedit(text: str) -> QInputMethodEvent:
        return QInputMethodEvent(text, [])

    @staticmethod
    def _commit(text: str) -> QInputMethodEvent:
        ev = QInputMethodEvent("", [])
        ev.setCommitString(text, 0, 0)
        return ev

    def test_plain_enter_sends(self) -> None:
        self.edit.setPlainText("こんにちは")
        self.edit.keyPressEvent(self._enter())
        self.assertEqual(self.sent, ["こんにちは"])

    def test_shift_enter_inserts_newline_without_send(self) -> None:
        self.edit.setPlainText("一行目")
        self.edit.keyPressEvent(self._enter(Qt.KeyboardModifier.ShiftModifier))
        self.assertEqual(self.sent, [])
        self.assertIn("\n", self.edit.toPlainText())

    def test_preedit_enter_confirms_without_send(self) -> None:
        self.edit.inputMethodEvent(self._preedit("にほん"))
        self.assertTrue(self.edit._composing)
        self.edit.keyPressEvent(self._enter())
        self.assertEqual(self.sent, [])
        self.assertEqual(self.edit.toPlainText(), "")  # no stray newline either

    def test_commit_then_enter_in_same_pass_does_not_send(self) -> None:
        self.edit.inputMethodEvent(self._preedit("にほん"))
        self.edit.inputMethodEvent(self._commit("日本語"))
        self.assertEqual(self.edit.toPlainText(), "日本語")
        self.assertFalse(self.edit._composing)
        self.assertTrue(self.edit._commit_guard)

        self.edit.keyPressEvent(self._enter())  # trailing confirmation key
        self.assertEqual(self.sent, [])

        self.app.processEvents()  # event loop drains -> guard clears
        self.assertFalse(self.edit._commit_guard)
        self.edit.keyPressEvent(self._enter())  # deliberate send
        self.assertEqual(self.sent, ["日本語"])

    def test_focus_out_resets_composition(self) -> None:
        self.edit.inputMethodEvent(self._preedit("にほん"))
        self.edit.focusOutEvent(QFocusEvent(QEvent.Type.FocusOut))
        self.assertFalse(self.edit._composing)
        self.edit.setPlainText("確定済み")
        self.edit.keyPressEvent(self._enter())
        self.assertEqual(self.sent, ["確定済み"])


class TestQtPlatformSelection(unittest.TestCase):
    """XWayland-first platform choice and IM-module handling (run.sh + main.py)."""

    def _configure(self, env: dict) -> dict:
        from sisters_mascot.main import _configure_qt_environment

        with (
            patch.dict(os.environ, env, clear=True),
            patch("sisters_mascot.main._ensure_system_qt_plugins"),
        ):
            _configure_qt_environment()
            return dict(os.environ)

    def test_xwayland_prefers_xcb_and_keeps_im_module(self) -> None:
        got = self._configure({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0", "QT_IM_MODULE": "fcitx"})
        self.assertEqual(got["QT_QPA_PLATFORM"], "xcb")
        self.assertEqual(got["QT_IM_MODULE"], "fcitx")

    def test_pure_wayland_unsets_im_module(self) -> None:
        got = self._configure({"WAYLAND_DISPLAY": "wayland-1", "QT_IM_MODULE": "fcitx"})
        self.assertEqual(got["QT_QPA_PLATFORM"], "wayland")
        self.assertNotIn("QT_IM_MODULE", got)

    def test_x11_keeps_im_module(self) -> None:
        got = self._configure({"DISPLAY": ":0", "QT_IM_MODULE": "fcitx"})
        self.assertEqual(got["QT_QPA_PLATFORM"], "xcb")
        self.assertEqual(got["QT_IM_MODULE"], "fcitx")

    def test_explicit_override_wins(self) -> None:
        got = self._configure(
            {"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0", "SISTERS_MASCOT_PLATFORM": "wayland", "QT_IM_MODULE": "fcitx"}
        )
        self.assertEqual(got["QT_QPA_PLATFORM"], "wayland")
        self.assertNotIn("QT_IM_MODULE", got)

    def test_keep_im_module_escape_hatch(self) -> None:
        got = self._configure(
            {"WAYLAND_DISPLAY": "wayland-1", "QT_IM_MODULE": "fcitx", "SISTERS_MASCOT_KEEP_IM_MODULE": "1"}
        )
        self.assertEqual(got["QT_QPA_PLATFORM"], "wayland")
        self.assertEqual(got["QT_IM_MODULE"], "fcitx")

    def test_respects_existing_platform(self) -> None:
        got = self._configure({"QT_QPA_PLATFORM": "offscreen", "WAYLAND_DISPLAY": "wayland-1"})
        self.assertEqual(got["QT_QPA_PLATFORM"], "offscreen")

    def test_run_script_selects_platform_and_im_module(self) -> None:
        cases = [
            ({"WAYLAND_DISPLAY": "wayland-1", "DISPLAY": ":0", "QT_IM_MODULE": "fcitx"}, "xcb", "fcitx"),
            ({"DISPLAY": ":0", "QT_IM_MODULE": "fcitx"}, "xcb", "fcitx"),
            ({"WAYLAND_DISPLAY": "wayland-1", "QT_IM_MODULE": "fcitx"}, "wayland", None),
            ({"WAYLAND_DISPLAY": "wayland-1", "SISTERS_MASCOT_PLATFORM": "xcb", "QT_IM_MODULE": "fcitx"}, "xcb", "fcitx"),
            ({"WAYLAND_DISPLAY": "wayland-1", "QT_IM_MODULE": "fcitx", "SISTERS_MASCOT_KEEP_IM_MODULE": "1"}, "wayland", "fcitx"),
            ({"QT_QPA_PLATFORM": "offscreen", "WAYLAND_DISPLAY": "wayland-1"}, "offscreen", None),
        ]
        strip = {
            "QT_QPA_PLATFORM", "QT_IM_MODULE", "WAYLAND_DISPLAY", "DISPLAY",
            "SISTERS_MASCOT_PLATFORM", "SISTERS_MASCOT_KEEP_IM_MODULE",
        }
        base_env = {k: v for k, v in os.environ.items() if k not in strip}
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            shutil.copy(PROJECT_ROOT / "run.sh", root / "run.sh")
            stub_dir = root / ".venv" / "bin"
            stub_dir.mkdir(parents=True)
            stub = stub_dir / "python"
            stub.write_text("#!/usr/bin/env bash\nenv\n")
            stub.chmod(0o755)
            for overrides, want_platform, want_im in cases:
                env = dict(base_env)
                env.update(overrides)
                proc = subprocess.run(
                    ["bash", str(root / "run.sh")], env=env, capture_output=True, text=True, timeout=30
                )
                self.assertEqual(proc.returncode, 0, proc.stderr)
                got = dict(line.split("=", 1) for line in proc.stdout.splitlines() if "=" in line)
                self.assertEqual(got.get("QT_QPA_PLATFORM"), want_platform, overrides)
                self.assertEqual(got.get("QT_IM_MODULE"), want_im, overrides)


class TestQtPluginPath(unittest.TestCase):
    """System Qt6 IM plugins must be discoverable by PyQt6's bundled Qt on xcb."""

    def test_fcitx_adds_system_plugin_dir(self) -> None:
        from sisters_mascot.main import _ensure_system_qt_plugins

        with (
            patch.dict(os.environ, {"QT_IM_MODULE": "fcitx"}, clear=True),
            patch("sisters_mascot.main.sys.platform", "linux"),
            patch("sisters_mascot.main._system_qt_plugin_dir", return_value="/usr/lib/qt6/plugins"),
        ):
            _ensure_system_qt_plugins()
            self.assertIn("/usr/lib/qt6/plugins", os.environ.get("QT_PLUGIN_PATH", ""))

    def test_existing_plugin_path_not_duplicated(self) -> None:
        from sisters_mascot.main import _ensure_system_qt_plugins

        env = {"QT_IM_MODULE": "fcitx", "QT_PLUGIN_PATH": "/usr/lib/qt6/plugins"}
        with (
            patch.dict(os.environ, env, clear=True),
            patch("sisters_mascot.main.sys.platform", "linux"),
            patch("sisters_mascot.main._system_qt_plugin_dir", return_value="/usr/lib/qt6/plugins"),
        ):
            _ensure_system_qt_plugins()
            self.assertEqual(os.environ["QT_PLUGIN_PATH"], "/usr/lib/qt6/plugins")

    def test_no_im_module_skips_plugin_path(self) -> None:
        from sisters_mascot.main import _ensure_system_qt_plugins

        with (
            patch.dict(os.environ, {}, clear=True),
            patch("sisters_mascot.main.sys.platform", "linux"),
            patch("sisters_mascot.main._system_qt_plugin_dir", return_value="/usr/lib/qt6/plugins"),
        ):
            _ensure_system_qt_plugins()
            self.assertNotIn("QT_PLUGIN_PATH", os.environ)


class TestOpenCodeBinaryResolution(unittest.TestCase):
    """opencode must be found even when GUI launch PATH omits ~/.opencode/bin."""

    def _fake_home_with_opencode(self, tmpdir: str) -> Path:
        fake_home = Path(tmpdir)
        oc_dir = fake_home / ".opencode" / "bin"
        oc_dir.mkdir(parents=True)
        exe = oc_dir / "opencode"
        exe.write_text("#!/bin/sh\nexit 0\n")
        exe.chmod(0o755)
        return exe

    def test_fallback_to_opencode_install_dir(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            exe = self._fake_home_with_opencode(tmpdir)
            with (
                patch("pathlib.Path.home", return_value=Path(tmpdir)),
                patch("sisters_mascot.agents.base.shutil.which", return_value=None),
            ):
                adapter = OpenCodeAdapter()
                self.assertEqual(adapter.resolved_binary(), str(exe))
                self.assertTrue(adapter.is_available())

    def test_discovery_uses_fallback_binary_and_parses(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            exe = self._fake_home_with_opencode(tmpdir)
            stdout = "opencode/beam-free\nopencode/big-pickle\nsurplus/claude-sonnet-5\n"
            with (
                patch("pathlib.Path.home", return_value=Path(tmpdir)),
                patch("sisters_mascot.agents.base.shutil.which", return_value=None),
                patch("sisters_mascot.agents.opencode_adapter.subprocess.run") as mock_run,
            ):
                mock_run.return_value = subprocess.CompletedProcess(
                    args=[str(exe), "models"], returncode=0, stdout=stdout, stderr=""
                )
                adapter = OpenCodeAdapter()
                choices = adapter.discover_model_choices()
            self.assertEqual(
                [c.id for c in choices],
                ["opencode/beam-free", "opencode/big-pickle", "surplus/claude-sonnet-5"],
            )
            self.assertEqual(mock_run.call_args[0][0], [str(exe), "models"])


if __name__ == "__main__":
    unittest.main()
