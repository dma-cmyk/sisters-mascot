"""Core unit and headless PyQt6 UI smoke tests for Sisters Mascot."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from PyQt6.QtCore import QPointF, QRect, Qt  # noqa: E402
from PyQt6.QtGui import QImage, QWheelEvent  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

from sisters_mascot import SPRITES_DIR  # noqa: E402
from sisters_mascot.agents.agy_adapter import AgyAdapter  # noqa: E402
from sisters_mascot.agents.base import ModelChoice  # noqa: E402
from sisters_mascot.agents.claude_adapter import ClaudeAdapter  # noqa: E402
from sisters_mascot.agents.codex_adapter import CodexAdapter  # noqa: E402
from sisters_mascot.agents.opencode_adapter import OpenCodeAdapter  # noqa: E402
from sisters_mascot.agents.router import AGENT_IDS, AgentRouter  # noqa: E402
from sisters_mascot.audio.stt import VoiceRecorder  # noqa: E402
from sisters_mascot.audio.tts import clean_text_for_speech  # noqa: E402
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
    load_all_sisters,
)
from sisters_mascot.sprites.renderer import render_sister_pixmap  # noqa: E402
from sisters_mascot.ui.bubble_panel import BubblePanel  # noqa: E402
from sisters_mascot.ui.mascot_window import MascotWindow  # noqa: E402
from sisters_mascot.ui.widgets import LauncherItemDialog, ToastBubble  # noqa: E402

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

ILLUSTRATED_SISTER_IDS = [
    "aoi",
    "ruri",
    "kohaku",
    "sumire",
    "akari",
    "shion",
    "hiiro",
    "mashiro",
    "yui",
    "ritsu",
    "nagi",
    "akane",
]


_QAPP: QApplication | None = None


def _ensure_qapp() -> QApplication:
    global _QAPP
    app = QApplication.instance()
    if isinstance(app, QApplication):
        _QAPP = app
        return app
    _QAPP = QApplication([])
    return _QAPP


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

    def test_save_and_load_round_trip(self) -> None:
        with tempfile.TemporaryDirectory() as tmpdir:
            cfg_path = Path(tmpdir) / "nested" / "config.json"

            cfg = AppConfig(
                sister_id="nagi",
                agent_id="claude",
                models_by_agent={"claude": "claude-sonnet-4-6", "agy": "gemini-2.5-pro"},
                working_dir="/home/dma/プロジェクト",
                auto_approve=False,
                mascot_x=120,
                mascot_y=240,
                mascot_scale="large",
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
                folders=[FolderItem(id="fld1", label="Home", icon="🏠", path="/home/dma")],
                bookmarks=[BookmarkItem(id="bm1", label="Example", icon="🌐", url="https://example.com")],
            )
            saved = save_config(cfg, cfg_path)
            self.assertEqual(saved, cfg_path)
            self.assertTrue(cfg_path.is_file())

            loaded = load_config(cfg_path)
            self.assertEqual(loaded.sister_id, "nagi")
            self.assertEqual(loaded.agent_id, "claude")
            self.assertEqual(loaded.models_by_agent, {"claude": "claude-sonnet-4-6", "agy": "gemini-2.5-pro"})
            self.assertEqual(loaded.working_dir, "/home/dma/プロジェクト")
            self.assertFalse(loaded.auto_approve)
            self.assertEqual(loaded.mascot_x, 120)
            self.assertEqual(loaded.mascot_y, 240)
            self.assertEqual(loaded.mascot_scale, "large")
            self.assertFalse(loaded.always_on_top)
            self.assertEqual(loaded.tts_engine, "voicevox")
            self.assertEqual(loaded.tts_volume, 65)
            self.assertEqual(loaded.tts_rate_offset, 10)
            self.assertTrue(loaded.stt_auto_send)
            self.assertEqual(len(loaded.quick_prompts), 1)
            self.assertEqual(loaded.quick_prompts[0].preferred_agent, "codex")
            self.assertEqual(loaded.apps[0].command, "konsole")
            self.assertEqual(loaded.folders[0].path, "/home/dma")
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
                        "panel_height": 2000,
                        "animation_mode": "invalid",
                    }
                ),
                encoding="utf-8",
            )
            loaded_extreme = load_config(extreme)
            self.assertEqual(loaded_extreme.mascot_width, 420)
            self.assertEqual(loaded_extreme.panel_width, 360)
            self.assertEqual(loaded_extreme.panel_height, 920)
            self.assertEqual(loaded_extreme.animation_mode, "rich")


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
            cwd="/home/dma/プロジェクト",
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


class TestGeneratedSprites(unittest.TestCase):
    def test_all_12_sprites_are_clean_rgba_with_transparent_corners(self) -> None:
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

    def test_medallion_fallback_sisters_render(self) -> None:
        _ensure_qapp()
        sister_map = get_sister_map()
        for sid in ("kokonoe", "towa", "aya", "kanon", "homura"):
            pm = render_sister_pixmap(sister_map[sid], "idle", 210, 1.0)
            self.assertFalse(pm.isNull(), f"Medallion render failed for {sid}")
            self.assertGreater(pm.width(), 100)
            self.assertGreater(pm.height(), 100)


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

            # Switch between illustrated and medallion-fallback sisters: aoi -> nagi -> homura
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


if __name__ == "__main__":
    unittest.main()
