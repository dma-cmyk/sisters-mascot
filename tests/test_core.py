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

from PyQt6.QtCore import QEvent, QMimeData, QPoint, QPointF, QRect, QSize, Qt, QUrl  # noqa: E402
from PyQt6.QtGui import (  # noqa: E402
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
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
from sisters_mascot.attachments import (  # noqa: E402
    DEFAULT_ATTACHMENT_ONLY_PROMPT,
    MAX_ATTACHMENTS,
    build_attachment_item,
    build_prompt_with_attachments,
    collect_attachment_dirs,
    collect_file_paths,
    collect_image_paths,
)
from sisters_mascot.audio.stt import VoiceRecorder  # noqa: E402
from sisters_mascot.conversation import (  # noqa: E402
    MAX_HANDOFF_CHARS,
    ConversationState,
    load_conversation,
    save_conversation,
)
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
from sisters_mascot.sprites.renderer import render_sister_pixmap, render_sister_sprite  # noqa: E402
from sisters_mascot.ui.bubble_panel import BubblePanel  # noqa: E402
from sisters_mascot.ui.mascot_window import MascotWindow  # noqa: E402
from sisters_mascot.ui.platform import supports_window_positioning  # noqa: E402
from sisters_mascot.ui.widgets import (  # noqa: E402
    ChatInputEdit,
    LauncherItemDialog,
    ToastBubble,
    extract_local_paths_from_mime,
)

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
SPRITE_STATES = ("idle", "happy", "thinking", "talking", "dragged")


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
            for state in SPRITE_STATES:
                sprite_path = SPRITES_DIR / sister_id / f"{state}.png"
                self.assertTrue(sprite_path.is_file(), f"Missing sprite: {sprite_path}")

                img = QImage(str(sprite_path))
                self.assertFalse(img.isNull(), f"Failed to load sprite: {sprite_path}")
                self.assertTrue(img.hasAlphaChannel(), f"Sprite lacks alpha channel: {sprite_path}")
                self.assertEqual(img.width(), 340, f"Unexpected width for {sprite_path}: {img.width()}")
                self.assertLessEqual(img.height(), 600, f"Sprite taller than 600px: {sprite_path}")
                self.assertEqual(img.height(), 600, f"Unexpected height for {sprite_path}: {img.height()}")

                rgba = img.convertToFormat(QImage.Format.Format_RGBA8888)
                w, h = rgba.width(), rgba.height()

                # All 4 corners must be fully transparent
                for cx, cy in ((0, 0), (w - 1, 0), (0, h - 1), (w - 1, h - 1)):
                    self.assertEqual(
                        rgba.pixelColor(cx, cy).alpha(),
                        0,
                        f"{sister_id}/{state} corner ({cx},{cy}) is not transparent",
                    )

                # Center region must contain opaque subject pixels
                opaque_center = 0
                for y in range(h // 3, (2 * h) // 3, 4):
                    for x in range(w // 4, (3 * w) // 4, 4):
                        if rgba.pixelColor(x, y).alpha() >= 220:
                            opaque_center += 1
                self.assertGreater(opaque_center, 25, f"{sister_id}/{state} center lacks opaque pixels")

    def test_all_17_sisters_render_standing_sprites(self) -> None:
        _ensure_qapp()
        sister_map = get_sister_map()
        for sid in EXPECTED_SISTER_IDS:
            self.assertTrue(has_custom_or_generated_sprite(sid), f"{sid} should have bundled sprite")
            for state in SPRITE_STATES:
                pm = render_sister_pixmap(sister_map[sid], state, 210, 1.0)
                self.assertFalse(pm.isNull(), f"Sprite render failed for {sid}/{state}")
                self.assertGreater(pm.width(), 100)
                self.assertGreater(pm.height(), 200)
                rendered = render_sister_sprite(sister_map[sid], state, 210, 1.0)
                aspect = rendered.logical_h / max(1, rendered.logical_w)
                self.assertAlmostEqual(aspect, 600.0 / 340.0, delta=0.02, msg=f"Aspect mismatch {sid}/{state}")
                self.assertIsNotNone(rendered.hit_image)
                if sid == "ritsu" and state == "dragged":
                    self.assertGreater(rendered.logical_w, rendered.base_w)


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


class TestSpriteAlignmentAndHitTesting(unittest.TestCase):
    """All 17x5 sprite states keep stable geometry, zero clipping, and accurate hit-testing."""

    def test_all_sisters_states_keep_window_size_and_avoid_clipping(self) -> None:
        app = _ensure_qapp()
        sisters = load_all_sisters()
        for target_width in (100, 210, 340):
            cfg = AppConfig(tts_engine="off", mascot_width=target_width, animation_mode="off")
            mascot = MascotWindow(sisters, sisters[0], cfg)
            try:
                mascot.show()
                app.processEvents()
                expected_size = mascot.size()

                for sister in sisters:
                    mascot._temp_sprite_state = "happy"
                    mascot.set_sister(sister)
                    self.assertIsNone(mascot._temp_sprite_state)
                    self.assertFalse(mascot._temp_state_timer.isActive())

                    for st in SPRITE_STATES:
                        mascot._temp_sprite_state = st if st in ("happy", "dragged") else None
                        if st == "thinking":
                            mascot.set_activity("thinking")
                        elif st == "talking":
                            mascot.set_activity("speaking")
                        else:
                            mascot.set_activity("idle")
                        mascot._rebuild_geometry_and_pixmap()
                        self.assertEqual(
                            mascot.size(),
                            expected_size,
                            f"Window size changed on {sister.id}/{st} at width={target_width}",
                        )

                        ox, oy = mascot._sprite_draw_origin()
                        cr = mascot._sprite_content_rect
                        self.assertGreaterEqual(
                            ox + cr.left(), 0.0, f"Left clip on {sister.id}/{st} at width={target_width}"
                        )
                        self.assertLessEqual(
                            ox + cr.right(),
                            float(mascot.width()),
                            f"Right clip on {sister.id}/{st} at width={target_width}",
                        )
                        self.assertGreaterEqual(
                            oy + cr.top(), 0.0, f"Top clip on {sister.id}/{st} at width={target_width}"
                        )
                        self.assertLessEqual(
                            oy + cr.bottom(),
                            float(mascot.height()),
                            f"Bottom clip on {sister.id}/{st} at width={target_width}",
                        )
            finally:
                mascot.close()
                app.processEvents()

    def test_hit_testing_and_drag_interaction(self) -> None:
        from PyQt6.QtGui import QContextMenuEvent, QMouseEvent

        app = _ensure_qapp()
        sisters = load_all_sisters()
        yui = next(s for s in sisters if s.id == "yui")
        cfg = AppConfig(tts_engine="off", mascot_width=340, animation_mode="off")
        mascot = MascotWindow(sisters, yui, cfg)
        panel = BubblePanel(sisters, yui, cfg, AgentRouter(), VoiceRecorder())
        toggles: list[bool] = []
        mascot.toggle_panel_requested.connect(lambda: toggles.append(True))

        try:
            mascot.move(480, 260)
            mascot.show()
            panel.show_for_mascot(mascot.frameGeometry())
            app.processEvents()
            panel_pos_before = panel.pos()

            # Far transparent horizontal margin must not trigger click or context menu
            margin_pt = QPointF(10.0, float(mascot.height() // 2))
            self.assertFalse(mascot._is_point_on_mascot(margin_pt))
            press_margin = QMouseEvent(
                QEvent.Type.MouseButtonPress,
                margin_pt,
                QPointF(mascot.mapToGlobal(margin_pt.toPoint())),
                Qt.MouseButton.LeftButton,
                Qt.MouseButton.LeftButton,
                Qt.KeyboardModifier.NoModifier,
            )
            mascot.mousePressEvent(press_margin)
            self.assertFalse(press_margin.isAccepted())
            self.assertFalse(mascot._pressing)
            self.assertEqual(toggles, [])

            ctx_margin = QContextMenuEvent(
                QContextMenuEvent.Reason.Mouse,
                margin_pt.toPoint(),
                mascot.mapToGlobal(margin_pt.toPoint()),
            )
            mascot.contextMenuEvent(ctx_margin)
            self.assertFalse(ctx_margin.isAccepted())

            # Center character click must trigger happy bounce & toggle_panel_requested
            center_pt = QPointF(float(mascot.width() // 2), float(mascot.height() // 2))
            self.assertTrue(mascot._is_point_on_mascot(center_pt))
            g_center = QPointF(mascot.mapToGlobal(center_pt.toPoint()))
            mascot.mousePressEvent(
                QMouseEvent(
                    QEvent.Type.MouseButtonPress,
                    center_pt,
                    g_center,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            self.assertTrue(mascot._pressing)
            mascot.mouseReleaseEvent(
                QMouseEvent(
                    QEvent.Type.MouseButtonRelease,
                    center_pt,
                    g_center,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            self.assertEqual(mascot._temp_sprite_state, "happy")
            self.assertEqual(len(toggles), 1)

            # Dragging from character center switches to dragged state, moves mascot, leaves panel alone
            start_pos = mascot.pos()
            drag_end_global = g_center + QPointF(40.0, 25.0)
            mascot.mousePressEvent(
                QMouseEvent(
                    QEvent.Type.MouseButtonPress,
                    center_pt,
                    g_center,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            mascot.mouseMoveEvent(
                QMouseEvent(
                    QEvent.Type.MouseMove,
                    center_pt + QPointF(40.0, 25.0),
                    drag_end_global,
                    Qt.MouseButton.NoButton,
                    Qt.MouseButton.LeftButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            self.assertTrue(mascot._dragging)
            self.assertEqual(mascot._temp_sprite_state, "dragged")
            mascot.mouseReleaseEvent(
                QMouseEvent(
                    QEvent.Type.MouseButtonRelease,
                    center_pt + QPointF(40.0, 25.0),
                    drag_end_global,
                    Qt.MouseButton.LeftButton,
                    Qt.MouseButton.NoButton,
                    Qt.KeyboardModifier.NoModifier,
                )
            )
            self.assertNotEqual(mascot.pos(), start_pos)
            self.assertEqual(panel.pos(), panel_pos_before)
        finally:
            panel.close()
            mascot.close()
            app.processEvents()


class TestAttachmentsAndDragDrop(unittest.TestCase):
    """File/folder attachment modeling, CLI adapter flags, and headless D&D/paste UI."""

    def test_attachment_item_and_prompt_helpers(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "workspace"
            cwd.mkdir()
            cwd_file = cwd / "inside.py"
            cwd_file.write_text("print('ok')\n", encoding="utf-8")

            ext_dir = root / "shared_docs"
            ext_dir.mkdir()
            (ext_dir / "sub").mkdir()
            doc = ext_dir / "readme.md"
            doc.write_text("# Hello\n", encoding="utf-8")
            photo = ext_dir / "shot.png"
            photo.write_bytes(b"\x89PNG\r\n\x1a\n")
            svg = ext_dir / "icon.svg"
            svg.write_text("<svg></svg>", encoding="utf-8")

            self.assertIsNone(build_attachment_item(""))
            self.assertIsNone(build_attachment_item(root / "does_not_exist.txt"))

            doc_att = build_attachment_item(doc)
            photo_att = build_attachment_item(photo)
            svg_att = build_attachment_item(svg)
            dir_att = build_attachment_item(ext_dir)
            cwd_att = build_attachment_item(cwd_file)
            assert doc_att and photo_att and svg_att and dir_att and cwd_att

            self.assertEqual(doc_att.kind, "file")
            self.assertEqual(doc_att.icon, "📄")
            self.assertEqual(photo_att.kind, "image")
            self.assertEqual(photo_att.icon, "🖼️")
            self.assertEqual(svg_att.kind, "file")  # raster images only for vision
            self.assertEqual(dir_att.kind, "folder")
            self.assertEqual(dir_att.icon, "📁")
            self.assertEqual(dir_att.item_count, 4)
            self.assertEqual(dir_att.preview_entries, ("sub/", "icon.svg", "readme.md", "shot.png"))

            dirs = collect_attachment_dirs([cwd_att, dir_att, doc_att, photo_att], cwd=str(cwd))
            self.assertEqual(dirs, [str(ext_dir.resolve())])

            self.assertEqual(collect_image_paths([dir_att, doc_att, photo_att, svg_att]), [photo_att.path])
            self.assertEqual(
                collect_file_paths([dir_att, doc_att, photo_att, svg_att]),
                [doc_att.path, photo_att.path, svg_att.path],
            )

            with_text = build_prompt_with_attachments("要約して", [dir_att, photo_att, doc_att])
            self.assertIn("【添付されたファイル・フォルダ】", with_text)
            self.assertIn(f"- [フォルダ] {dir_att.path}", with_text)
            self.assertIn(f"- [画像] {photo_att.path}", with_text)
            self.assertIn(f"- [ファイル] {doc_att.path}", with_text)
            self.assertTrue(with_text.endswith("要約して"))

            only_att = build_prompt_with_attachments("   ", [doc_att])
            self.assertTrue(only_att.endswith(DEFAULT_ATTACHMENT_ONLY_PROMPT))

    def test_adapters_and_router_with_attachments(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cwd = root / "cwd"
            cwd.mkdir()
            local_file = cwd / "local.txt"
            local_file.write_text("local", encoding="utf-8")

            ext = root / "ext"
            ext.mkdir()
            pic = ext / "preview.webp"
            pic.write_bytes(b"RIFF....WEBP")
            svg = ext / "diagram.svg"
            svg.write_text("<svg/>", encoding="utf-8")
            note = ext / "notes.md"
            note.write_text("notes", encoding="utf-8")

            atts = [
                build_attachment_item(ext),
                build_attachment_item(pic),
                build_attachment_item(svg),
                build_attachment_item(note),
                build_attachment_item(local_file),
            ]
            assert all(atts)

            agy_cmd, _ = AgyAdapter().build_command("check", cwd=str(cwd), attachments=atts)
            self.assertIn("--add-dir", agy_cmd)
            self.assertEqual(agy_cmd.count(str(ext)), 1)
            self.assertNotIn(str(cwd), agy_cmd)

            claude_cmd, _ = ClaudeAdapter().build_command("check", cwd=str(cwd), attachments=atts)
            self.assertEqual(claude_cmd[-2:], ["--add-dir", str(ext)])

            codex_new, _ = CodexAdapter().build_command(
                "check", cwd=str(cwd), session_id=None, attachments=atts
            )
            self.assertIn("--add-dir", codex_new)
            self.assertIn(str(ext), codex_new)
            self.assertIn(f"--image={pic}", codex_new)
            self.assertNotIn(f"--image={svg}", codex_new)
            self.assertEqual(codex_new[-1], "-")

            codex_resume, _ = CodexAdapter().build_command(
                "follow up", cwd=str(cwd), session_id="thread-9", attachments=atts
            )
            self.assertNotIn("--add-dir", codex_resume)
            self.assertIn(f"--image={pic}", codex_resume)
            self.assertEqual(codex_resume[-2:], ["thread-9", "-"])

            oc_cmd, _ = OpenCodeAdapter().build_command("check", cwd=str(cwd), attachments=atts)
            self.assertEqual(oc_cmd[-1], "check")
            self.assertIn(str(pic), oc_cmd)
            self.assertIn(str(svg), oc_cmd)
            self.assertIn(str(note), oc_cmd)
            self.assertIn(str(local_file), oc_cmd)
            self.assertNotIn(str(ext), oc_cmd)

            router = AgentRouter()
            codex_adapter = router.get_adapter("codex")
            sisters = load_all_sisters()
            aoi = sisters[0]
            captured: dict = {}

            def _fake_build(prompt, **kwargs):
                captured["prompt"] = prompt
                captured["kwargs"] = kwargs
                return ["codex", "exec", "-"], prompt

            with (
                patch.object(codex_adapter, "is_available", return_value=True),
                patch.object(codex_adapter, "build_command", side_effect=_fake_build),
                patch.object(router, "_run_subprocess"),
            ):
                router.send_message(
                    "codex",
                    "確認して",
                    sister=aoi,
                    cwd=str(cwd),
                    attachments=[atts[0], atts[1]],
                )
            self.assertIn("【添付されたファイル・フォルダ】", captured["prompt"])
            self.assertIn(str(ext), captured["prompt"])
            self.assertIn(str(pic), captured["prompt"])
            self.assertIn("確認して", captured["prompt"])
            self.assertEqual(len(captured["kwargs"]["attachments"]), 2)

    def test_panel_chat_input_and_mascot_drag_drop(self) -> None:
        from sisters_mascot.main import MascotAppController

        app = _ensure_qapp()
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            sample_dir = root / "my_folder"
            sample_dir.mkdir()
            sample_file = root / "notes.txt"
            sample_file.write_text("hello", encoding="utf-8")
            missing_file = root / "missing.txt"

            mime = QMimeData()
            mime.setUrls(
                [
                    QUrl("https://example.com"),
                    QUrl.fromLocalFile(str(missing_file)),
                    QUrl.fromLocalFile(str(sample_file)),
                    QUrl.fromLocalFile(str(sample_dir)),
                    QUrl.fromLocalFile(str(sample_file)),  # duplicate
                ]
            )
            extracted = extract_local_paths_from_mime(mime)
            self.assertEqual(extracted, [str(sample_file), str(sample_dir)])

            cfg = AppConfig(tts_engine="off", animation_mode="off")
            with (
                patch("sisters_mascot.main.load_config", return_value=cfg),
                patch("sisters_mascot.main.save_config", return_value=root / "cfg.json"),
                patch.object(BubblePanel, "ensure_models_discovered", lambda self, *a, **k: None),
                patch.object(MascotAppController, "_setup_single_instance_server", lambda self: None),
            ):
                controller = MascotAppController(app)

            try:
                mascot, panel = controller.mascot, controller.panel
                mascot.show()
                panel.show()

                # 1. Drop onto BubblePanel
                enter_ev = QDragEnterEvent(
                    QPoint(40, 40), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
                )
                panel.dragEnterEvent(enter_ev)
                self.assertTrue(enter_ev.isAccepted())
                self.assertTrue(panel._drop_overlay.isVisible())

                drop_ev = QDropEvent(
                    QPointF(40, 40), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
                )
                panel.dropEvent(drop_ev)
                self.assertTrue(drop_ev.isAccepted())
                self.assertFalse(panel._drop_overlay.isVisible())
                self.assertEqual(len(panel.get_attachments()), 2)
                self.assertTrue(panel._attachments_bar.isVisible())

                # Removing and clearing attachments
                panel.remove_attachment(str(sample_file))
                self.assertEqual([a.path for a in panel.get_attachments()], [str(sample_dir)])
                panel.clear_attachments()
                self.assertEqual(panel.get_attachments(), [])
                self.assertFalse(panel._attachments_bar.isVisible())

                # 2. Drop and Ctrl+V paste on ChatInputEdit (never inserts file:/// URLs)
                panel._chat_input.setPlainText("既存テキスト")
                edit_drop = QDropEvent(
                    QPointF(10, 10), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
                )
                panel._chat_input.dropEvent(edit_drop)
                self.assertEqual(panel._chat_input.toPlainText(), "既存テキスト")
                self.assertEqual(len(panel.get_attachments()), 2)

                panel.clear_attachments()
                self.assertTrue(panel._chat_input.canInsertFromMimeData(mime))
                panel._chat_input.insertFromMimeData(mime)
                self.assertEqual(panel._chat_input.toPlainText(), "既存テキスト")
                self.assertEqual(len(panel.get_attachments()), 2)

                plain_mime = QMimeData()
                plain_mime.setText("追加")
                panel._chat_input.insertFromMimeData(plain_mime)
                self.assertIn("追加", panel._chat_input.toPlainText())

                # 3. Drop onto MascotWindow opens panel & queues attachments
                panel.clear_attachments()
                panel.hide()
                m_enter = QDragEnterEvent(
                    QPoint(50, 50), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
                )
                mascot.dragEnterEvent(m_enter)
                self.assertTrue(mascot._file_drop_hover)
                self.assertEqual(mascot._file_drop_count, 2)

                mascot.dragLeaveEvent(QDragLeaveEvent())
                self.assertFalse(mascot._file_drop_hover)

                mascot.dragEnterEvent(m_enter)
                m_drop = QDropEvent(
                    QPointF(50, 50), Qt.DropAction.CopyAction, mime, Qt.MouseButton.LeftButton, Qt.KeyboardModifier.NoModifier
                )
                mascot.dropEvent(m_drop)
                self.assertFalse(mascot._file_drop_hover)
                self.assertEqual(mascot._temp_sprite_state, "happy")
                self.assertTrue(panel.isVisible())
                self.assertEqual(panel._tabs.currentIndex(), 0)
                self.assertEqual(len(panel.get_attachments()), 2)

                # 4. Busy router preserves queued attachments
                with patch.object(controller.router, "is_running", return_value=True):
                    panel.send_prompt("")
                self.assertEqual(len(panel.get_attachments()), 2)

                # 5. Attachment-only send clears queue, sends attachments, and renders badges in HTML
                sent_calls: list[dict] = []

                def _fake_send(agent_id, prompt, **kwargs):
                    sent_calls.append({"agent_id": agent_id, "prompt": prompt, **kwargs})

                with patch.object(controller.router, "send_message", side_effect=_fake_send):
                    panel._chat_input.clear()
                    panel._on_send_or_stop()

                self.assertEqual(panel.get_attachments(), [])
                self.assertFalse(panel._attachments_bar.isVisible())
                self.assertEqual(len(sent_calls), 1)
                self.assertEqual(len(sent_calls[0]["attachments"]), 2)
                log_html = panel._chat_browser.toHtml()
                self.assertIn("notes.txt", log_html)
                self.assertIn("my_folder", log_html)

                # 6. Respect MAX_ATTACHMENTS cap cleanly
                many_files: list[Path] = []
                for idx in range(MAX_ATTACHMENTS + 3):
                    fp = root / f"f{idx}.txt"
                    fp.write_text("x", encoding="utf-8")
                    many_files.append(fp)
                added = panel.add_attachments(many_files, notify=True)
                self.assertEqual(len(added), MAX_ATTACHMENTS)
                self.assertEqual(len(panel.get_attachments()), MAX_ATTACHMENTS)

                app.processEvents()
            finally:
                try:
                    app.aboutToQuit.disconnect(controller.shutdown)
                except (TypeError, RuntimeError):
                    pass
                controller.shutdown()
                controller.panel.close()
                controller.mascot.close()
                app.processEvents()


class TestConversationContinuityAndCompact(unittest.TestCase):
    """Cross-agent/sister conversation handoff, AI/local compaction, and persistence."""

    def test_handoff_delta_compaction_and_persistence(self) -> None:
        state = ConversationState()
        state.add_user_turn("まずプロジェクト構造を教えて")
        state.add_sister_turn(
            "真白だよ！src/main.py があるよ。",
            author="真白",
            sister_id="mashiro",
            agent_id="opencode",
        )
        state.mark_session_synced("opencode", "mashiro")

        # Synced same (agent, sister) needs zero redundant handoff
        self.assertEqual(
            state.build_handoff_context("opencode", "mashiro", has_session=True),
            "",
        )

        # Switching to another sister & agent receives full prior context
        handoff_aoi = state.build_handoff_context("agy", "aoi", has_session=False)
        self.assertIn("<conversation_handoff>", handoff_aoi)
        self.assertIn("まずプロジェクト構造を教えて", handoff_aoi)
        self.assertIn("真白 (opencode)", handoff_aoi)
        self.assertIn("src/main.py", handoff_aoi)

        # Continue on (agy, aoi), then switch back to (opencode, mashiro) -> delta only
        state.add_user_turn("テストも確認して", attachments=("📄 test_core.py",))
        state.add_sister_turn(
            "碧です。43件全部OKです。",
            author="碧",
            sister_id="aoi",
            agent_id="agy",
        )
        state.mark_session_synced("agy", "aoi")

        delta_back = state.build_handoff_context("opencode", "mashiro", has_session=True)
        self.assertIn("テストも確認して", delta_back)
        self.assertIn("test_core.py", delta_back)
        self.assertIn("碧 (agy)", delta_back)
        self.assertNotIn("まずプロジェクト構造を教えて", delta_back)

        # Compaction shrinks active chars, clears sync, and passes summary to all sessions
        local_sum = state.build_local_fallback_summary()
        self.assertIn("真白", local_sum)
        before_chars, after_chars = state.apply_compaction(local_sum)
        self.assertGreater(before_chars, 0)
        self.assertEqual(state.active_turns, 0)
        self.assertEqual(state.summary_generation, 1)
        self.assertEqual(state.compacted_count, 1)
        self.assertEqual(after_chars, len(local_sum))

        post_compact = state.build_handoff_context("opencode", "mashiro", has_session=True)
        self.assertIn("コンパクト済み", post_compact)
        self.assertIn(local_sum[:40], post_compact)

        # Very long histories are bounded near MAX_HANDOFF_CHARS
        long_state = ConversationState()
        for idx in range(12):
            long_state.add_user_turn(f"U{idx}: " + ("あ" * 1800))
            long_state.add_sister_turn(
                f"S{idx}: " + ("い" * 1800),
                author="碧",
                sister_id="aoi",
                agent_id="agy",
            )
        bounded = long_state.build_handoff_context("claude", "ruri", has_session=False)
        self.assertLessEqual(len(bounded), MAX_HANDOFF_CHARS + 300)
        self.assertIn("省略", bounded)
        self.assertIn("S11:", bounded)

        # Atomic persistence round-trip & corrupt/missing fallback
        with tempfile.TemporaryDirectory() as tmpdir:
            conv_path = Path(tmpdir) / "conversation.json"
            self.assertEqual(load_conversation(conv_path)[1], [])

            sample_msgs = [{"role": "user", "author": "美緒ちゃん", "text": "hi"}]
            sample_sessions = {"opencode:mashiro": "oc-123", "agy:aoi": "agy-456"}
            save_conversation(
                long_state,
                display_messages=sample_msgs,
                sessions=sample_sessions,
                path=conv_path,
            )
            loaded_state, loaded_msgs, loaded_sessions = load_conversation(conv_path)
            self.assertEqual(loaded_state.active_turns, long_state.active_turns)
            self.assertEqual(loaded_msgs, sample_msgs)
            self.assertEqual(loaded_sessions, sample_sessions)

            conv_path.write_text("{corrupt json", encoding="utf-8")
            bad_state, bad_msgs, bad_sessions = load_conversation(conv_path)
            self.assertEqual(bad_state.active_turns, 0)
            self.assertEqual(bad_msgs, [])
            self.assertEqual(bad_sessions, {})

    def test_config_auto_compact_round_trip_and_clamp(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "config.json"
            cfg = AppConfig(auto_compact_enabled=True, auto_compact_chars=25000)
            save_config(cfg, cfg_path)
            loaded = load_config(cfg_path)
            self.assertTrue(loaded.auto_compact_enabled)
            self.assertEqual(loaded.auto_compact_chars, 25000)

            cfg_path.write_text(
                json.dumps({"auto_compact_chars": 100, "auto_compact_enabled": 1}),
                encoding="utf-8",
            )
            clamped_low = load_config(cfg_path)
            self.assertTrue(clamped_low.auto_compact_enabled)
            self.assertEqual(clamped_low.auto_compact_chars, 4000)

    def test_router_handoff_and_compact_conversation(self) -> None:
        app = _ensure_qapp()
        router = AgentRouter()
        codex = router.get_adapter("codex")
        aoi = load_all_sisters()[0]
        captured: dict = {}

        def _fake_build(prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return ["codex", "exec", "-"], prompt

        with (
            patch.object(codex, "is_available", return_value=True),
            patch.object(codex, "build_command", side_effect=_fake_build),
            patch.object(router, "_run_subprocess"),
        ):
            router.send_message(
                "codex",
                "次の質問",
                sister=aoi,
                handoff_context="<conversation_handoff>前の話</conversation_handoff>",
            )
        self.assertIn("<conversation_handoff>前の話</conversation_handoff>", captured["prompt"])
        self.assertIn("次の質問", captured["prompt"])

        # Isolated compact_conversation run must emit compact_finished without mutating sessions
        compact_router = AgentRouter()
        codex_compact = compact_router.get_adapter("codex")
        compact_router._sessions[("agy", "aoi")] = "keep-until-panel-clears"
        finished_summaries: list[str] = []
        compact_errors: list[str] = []
        compact_router.compact_finished.connect(finished_summaries.append)
        compact_router.compact_error.connect(compact_errors.append)

        fake_stdout = iter(
            [
                '{"type":"thread.started","thread_id":"should-not-be-saved"}\n',
                '{"type":"item.completed","item":{"id":"m1","type":"agent_message","text":"### 要約\\n完了"}}\n',
                '{"type":"turn.completed"}\n',
            ]
        )

        class _FakeProc:
            stdin = None
            stdout = fake_stdout
            stderr = iter(())

            def wait(self) -> int:
                return 0

            def poll(self):
                return 0

        class _SyncThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None) -> None:
                self._target = target
                self._args = args
                self._kwargs = kwargs or {}

            def start(self) -> None:
                if self._target:
                    self._target(*self._args, **self._kwargs)

            def join(self, timeout=None) -> None:  # noqa: ANN001
                _ = timeout

        with (
            patch.object(codex_compact, "is_available", return_value=True),
            patch("sisters_mascot.agents.router.subprocess.Popen", return_value=_FakeProc()),
            patch("sisters_mascot.agents.router.threading.Thread", _SyncThread),
        ):
            started = compact_router.compact_conversation("codex", "要約してください")

        self.assertTrue(started)
        app.processEvents()
        self.assertEqual(compact_errors, [])
        self.assertEqual(finished_summaries, ["### 要約\n完了"])
        self.assertEqual(compact_router.export_sessions(), {"agy:aoi": "keep-until-panel-clears"})

    def test_bubble_panel_cross_agent_continuity_and_compact_ui(self) -> None:
        app = _ensure_qapp()
        sisters = load_all_sisters()
        sister_map = {s.id: s for s in sisters}
        cfg = AppConfig(tts_engine="off", agent_id="opencode", sister_id="mashiro")
        router = AgentRouter()
        recorder = VoiceRecorder()
        panel = BubblePanel(sisters, sister_map["mashiro"], cfg, router, recorder)

        sent_calls: list[dict] = []

        def _fake_send(agent_id, prompt, **kwargs):
            sent_calls.append({"agent_id": agent_id, "prompt": prompt, **kwargs})
            router._sessions[(agent_id, kwargs["sister"].id)] = f"sess-{len(sent_calls)}"

        try:
            panel.show()

            # Turn 1 on (opencode, mashiro)
            with patch.object(router, "send_message", side_effect=_fake_send):
                panel.send_prompt("最初の相談")
            self.assertEqual(sent_calls[-1]["handoff_context"], "")
            panel.finalize_stream_reply("了解！ファイルを確認したよ。")
            self.assertIn("2発言", panel._context_lbl.text())
            self.assertTrue(panel._compact_btn.isEnabled())

            # Switch to (agy, aoi) -> receives full handoff of Turn 1
            cfg.agent_id = "agy"
            panel.notify_sister_switched(sister_map["aoi"])
            with patch.object(router, "send_message", side_effect=_fake_send):
                panel.send_prompt("その続きをお願い")
            handoff2 = sent_calls[-1]["handoff_context"]
            self.assertIn("最初の相談", handoff2)
            self.assertIn("真白 (opencode)", handoff2)
            panel.finalize_stream_reply("碧が引き継ぎました。")

            # Switch back to (opencode, mashiro) -> receives delta of Turn 2 only
            cfg.agent_id = "opencode"
            panel.notify_sister_switched(sister_map["mashiro"])
            with patch.object(router, "send_message", side_effect=_fake_send):
                panel.send_prompt("さらに確認")
            handoff3 = sent_calls[-1]["handoff_context"]
            self.assertIn("その続きをお願い", handoff3)
            self.assertIn("碧 (agy)", handoff3)
            self.assertNotIn("最初の相談", handoff3)
            panel.finalize_stream_reply("真白に戻ったよ。")

            # Local compaction clears all CLI sessions and records compact bubble
            self.assertTrue(panel.request_compact(mode="local"))
            self.assertEqual(router.export_sessions(), {})
            self.assertEqual(panel._conversation.active_turns, 0)
            self.assertEqual(panel._conversation.compacted_count, 1)
            self.assertIn("コンパクト化", panel._chat_browser.toHtml())

            # Next turn on any agent/sister receives compacted summary
            with patch.object(router, "send_message", side_effect=_fake_send):
                panel.send_prompt("次は何をしよう？")
            handoff4 = sent_calls[-1]["handoff_context"]
            self.assertIn("コンパクト済み", handoff4)
            self.assertIn("真白", handoff4)
            panel.finalize_stream_reply("次はテストだね。")

            # AI compaction flow
            with patch.object(router, "send_message", side_effect=_fake_send):
                panel.send_prompt("了解")
            panel.finalize_stream_reply("進めます。")

            with patch.object(router, "compact_conversation", return_value=True) as mock_compact:
                self.assertTrue(panel.request_compact(mode="ai"))
            self.assertTrue(panel._compacting)
            mock_compact.assert_called_once()
            panel.handle_compact_finished("### 🎯 AI要約\n完了したテスト")
            self.assertFalse(panel._compacting)
            self.assertEqual(panel._conversation.compacted_count, 2)
            self.assertEqual(panel._conversation.compacted_summary, "### 🎯 AI要約\n完了したテスト")
            self.assertEqual(router.export_sessions(), {})

            # New session clears summary, turns, and sessions
            panel._on_new_session()
            self.assertEqual(panel._conversation.active_turns, 0)
            self.assertEqual(panel._conversation.compacted_summary, "")
            self.assertEqual(router.export_sessions(), {})

            app.processEvents()
        finally:
            panel.close()
            app.processEvents()


class TestActivityMonitoringAndErrorVisibility(unittest.TestCase):
    """Live activity log, stderr/non-zero exit visibility, stall detection, and cancel dedup."""

    def test_adapter_tool_lifecycle_error_reasoning_and_multipart_text(self) -> None:
        oc = OpenCodeAdapter()
        oc.reset_state()

        e1 = oc.parse_line('{"type":"text","part":{"id":"p1","type":"text","text":"10番を始めます。"}}')
        self.assertEqual([(e.kind, e.text) for e in e1], [("delta", "10番を始めます。")])
        e2 = oc.parse_line('{"type":"text","part":{"id":"p2","type":"text","text":"11番へ進みます。"}}')
        self.assertEqual([(e.kind, e.text) for e in e2], [("delta", "\n\n11番へ進みます。")])

        tool_run = oc.parse_line(
            json.dumps(
                {
                    "type": "tool",
                    "part": {
                        "id": "t1",
                        "type": "tool",
                        "tool": "execute",
                        "state": {
                            "status": "running",
                            "input": {"code": "return await tools.playwright.browser_evaluate({target:\"e1\"});"},
                        },
                    },
                }
            )
        )
        self.assertEqual(len(tool_run), 1)
        self.assertEqual(tool_run[0].kind, "tool")
        self.assertEqual(tool_run[0].tool_name, "playwright.browser_evaluate")
        self.assertIn("Playwright", tool_run[0].text)

        tool_err = oc.parse_line(
            json.dumps(
                {
                    "type": "tool",
                    "part": {
                        "id": "t1",
                        "type": "tool",
                        "tool": "execute",
                        "state": {
                            "status": "error",
                            "error": {"type": "aborted", "message": "Tool execution interrupted"},
                            "metadata": {"toolCalls": [{"tool": "playwright.browser_evaluate"}]},
                        },
                    },
                }
            )
        )
        self.assertEqual(len(tool_err), 1)
        self.assertEqual((tool_err[0].kind, tool_err[0].level), ("log", "error"))
        self.assertIn("Tool execution interrupted", tool_err[0].text)

        tool_ok = oc.parse_line(
            json.dumps(
                {
                    "type": "tool",
                    "part": {
                        "id": "t2",
                        "type": "tool",
                        "tool": "execute",
                        "state": {
                            "status": "completed",
                            "metadata": {"toolCalls": [{"tool": "playwright.browser_click"}]},
                        },
                    },
                }
            )
        )
        self.assertEqual((tool_ok[0].kind, tool_ok[0].level), ("log", "info"))
        self.assertIn("playwright.browser_click", tool_ok[0].text)

        reason = oc.parse_line('{"type":"reasoning","part":{"id":"r1","type":"reasoning","text":"Checking selector state"}}')
        self.assertEqual(reason[0].kind, "log")
        self.assertIn("💭 思考:", reason[0].text)

        claude = ClaudeAdapter()
        c_res = claude.parse_line('{"type":"user","message":{"content":[{"type":"tool_result","tool_name":"Bash","is_error":true,"content":"Permission denied"}]}}')
        self.assertEqual((c_res[0].kind, c_res[0].level), ("log", "error"))
        self.assertIn("Permission denied", c_res[0].text)

        codex = CodexAdapter()
        cx_start = codex.parse_line('{"type":"item.started","item":{"id":"c1","type":"command_execution","command":"pytest -q"}}')
        self.assertEqual(cx_start[0].detail, "pytest -q")
        cx_fail = codex.parse_line('{"type":"item.completed","item":{"id":"c1","type":"command_execution","command":"pytest -q","exit_code":1}}')
        self.assertEqual((cx_fail[0].kind, cx_fail[0].level), ("log", "error"))
        self.assertIn("exit=1", cx_fail[0].text)

    def test_router_surfaces_stderr_nonzero_exit_after_text_and_cancel_no_dup(self) -> None:
        app = _ensure_qapp()

        class _SyncThread:
            def __init__(self, target=None, args=(), kwargs=None, daemon=None) -> None:
                self._target = target
                self._args = args
                self._kwargs = kwargs or {}

            def start(self) -> None:
                if self._target:
                    self._target(*self._args, **self._kwargs)

            def join(self, timeout=None) -> None:  # noqa: ANN001
                _ = timeout

        # 1. Partial text followed by stderr + non-zero exit must surface error
        r1 = AgentRouter()
        oc = r1.get_adapter("opencode")
        replies: list[str] = []
        errors: list[str] = []
        logs: list[tuple[str, str]] = []
        r1.reply_finished.connect(replies.append)
        r1.error_occurred.connect(errors.append)
        r1.activity_logged.connect(lambda lvl, msg: logs.append((lvl, msg)))

        class _CrashProc:
            stdin = None
            stdout = iter(['{"type":"text","part":{"id":"p1","type":"text","text":"途中まで出力"}}\n'])
            stderr = iter(["Error: ECONNREFUSED 127.0.0.1:9222\n"])

            def wait(self) -> int:
                return 1

            def poll(self):
                return 1

        with (
            patch.object(oc, "is_available", return_value=True),
            patch("sisters_mascot.agents.router.subprocess.Popen", return_value=_CrashProc()),
            patch("sisters_mascot.agents.router.threading.Thread", _SyncThread),
        ):
            r1.send_message("opencode", "作業して", sister=load_all_sisters()[0])

        app.processEvents()
        self.assertEqual(replies, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("exit=1", errors[0])
        self.assertIn("ECONNREFUSED", errors[0])
        self.assertTrue(any(lvl == "error" and "ECONNREFUSED" in msg for lvl, msg in logs))

        # 2. User cancel emits turn_cancelled instead of reply_finished
        r2 = AgentRouter()
        oc2 = r2.get_adapter("opencode")
        r2_replies: list[str] = []
        r2_cancelled: list[str] = []
        r2.reply_finished.connect(r2_replies.append)
        r2.turn_cancelled.connect(r2_cancelled.append)

        class _CancelProc:
            stdin = None
            stdout = iter(['{"type":"text","part":{"id":"p1","type":"text","text":"途中"}}\n'])
            stderr = iter(())

            def wait(self) -> int:
                return -15

            def poll(self):
                return -15

        def _fake_parse_and_cancel(line: str):
            r2._cancel_requested = True
            return []

        with (
            patch.object(oc2, "is_available", return_value=True),
            patch.object(oc2, "parse_line", side_effect=_fake_parse_and_cancel),
            patch("sisters_mascot.agents.router.subprocess.Popen", return_value=_CancelProc()),
            patch("sisters_mascot.agents.router.threading.Thread", _SyncThread),
        ):
            r2.send_message("opencode", "作業して", sister=load_all_sisters()[0])

        app.processEvents()
        self.assertEqual(r2_replies, [])
        self.assertEqual(len(r2_cancelled), 1)

    def test_bubble_panel_activity_drawer_heartbeat_stall_and_cancel_dedup(self) -> None:
        import time as _time

        app = _ensure_qapp()
        sisters = load_all_sisters()
        cfg = AppConfig(tts_engine="off")
        router = AgentRouter()
        panel = BubblePanel(sisters, sisters[0], cfg, router, VoiceRecorder())
        try:
            panel.show()
            self.assertFalse(panel._activity_drawer.isVisible())
            panel._log_toggle_btn.click()
            self.assertTrue(panel._activity_drawer.isVisible())
            panel._log_toggle_btn.click()
            self.assertFalse(panel._activity_drawer.isVisible())

            panel.set_running_state(True, "🌐 Chrome (Playwright) 操作中...")
            self.assertTrue(panel._status_bar.isVisible())
            self.assertIn("経過 00:", panel._status_bar.text())
            self.assertIn("●", panel._log_toggle_btn.text())

            panel.append_activity_log("error", "[stderr] Connection reset")
            self.assertIn("⚠️1", panel._log_toggle_btn.text())
            self.assertIn("Connection reset", panel._activity_view.toPlainText())
            self.assertIn("⚠️エラー1件", panel._status_bar.text())

            # Simulate 50s without activity -> stall warning banner + log line
            panel._last_activity_ts = _time.monotonic() - 50.0
            panel._on_activity_tick()
            self.assertIn("秒間更新なし", panel._status_bar.text())
            self.assertTrue(panel._stall_notified)
            self.assertIn("新しい応答がありません", panel._activity_view.toPlainText())

            # Cancel deduplication: _on_send_or_stop + handle_turn_cancelled saves once
            with patch.object(router, "send_message"):
                panel.send_prompt("キャンセルテスト")
            panel.append_stream_delta("途中までのテキスト")
            with (
                patch.object(router, "is_running", return_value=True),
                patch.object(router, "cancel"),
            ):
                panel._on_send_or_stop()
            panel.handle_turn_cancelled("途中までのテキスト")

            self.assertEqual(len(panel._conversation.turns), 2)
            cancelled_msgs = [m for m in panel._messages if "(中断)" in str(m.get("author", ""))]
            self.assertEqual(len(cancelled_msgs), 1)
            app.processEvents()
        finally:
            panel.close()
            app.processEvents()


if __name__ == "__main__":
    unittest.main()
