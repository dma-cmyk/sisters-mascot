"""Configuration schema, defaults, and atomic persistence for Sisters Mascot."""

from __future__ import annotations

import json
import logging
import os
import shutil
import sys
import tempfile
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from sisters_mascot import CONFIG_DIR

logger = logging.getLogger(__name__)

SCALE_WIDTHS: dict[str, int] = {
    "small": 165,
    "medium": 210,
    "large": 275,
    "xlarge": 340,
}

MASCOT_WIDTH_MIN = 100
MASCOT_WIDTH_MAX = 420
PANEL_WIDTH_MIN = 360
# Generous sanity bounds for persisted JSON; at runtime the panel is clamped
# to the current screen's available geometry (see BubblePanel._update_maximum_size).
PANEL_WIDTH_MAX = 7680
PANEL_HEIGHT_MIN = 440
PANEL_HEIGHT_MAX = 4320
ANIMATION_MODES = ("rich", "subtle", "off")
VALID_AGENT_IDS: tuple[str, ...] = ("agy", "claude", "codex", "opencode")


@dataclass
class SisterAgentAssignment:
    agent_id: str = ""
    model: str = ""
    enabled: bool = True


def _clamp_int(value: Any, default: int, low: int, high: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        parsed = default
    return max(low, min(high, parsed))


@dataclass
class QuickPromptItem:
    id: str
    label: str
    icon: str
    prompt: str
    send_immediately: bool = False
    preferred_agent: str = ""


@dataclass
class AppItem:
    id: str
    label: str
    icon: str
    command: str


@dataclass
class FolderItem:
    id: str
    label: str
    icon: str
    path: str


@dataclass
class BookmarkItem:
    id: str
    label: str
    icon: str
    url: str


def _default_quick_prompts() -> list[QuickPromptItem]:
    return [
        QuickPromptItem(
            id="chrome-current-tab",
            label="開いているページを調査",
            icon="🌐",
            prompt="いまGoogle Chromeで開いているページの内容をPlaywrightで確認して、分かりやすく要約してください。",
            send_immediately=False,
        ),
        QuickPromptItem(
            id="chrome-web-search",
            label="ChromeでWeb調査",
            icon="🔍",
            prompt="Playwrightを使ってChromeで次のテーマを調査して要点をまとめてください:\n",
            send_immediately=False,
        ),
        QuickPromptItem(
            id="git-status-review",
            label="作業状態を確認",
            icon="📊",
            prompt="現在の作業ディレクトリのGit状態や最近の変更を確認して、次にやると良いことを整理してください。",
            send_immediately=False,
        ),
        QuickPromptItem(
            id="wiki-note",
            label="Wikiメモを作成",
            icon="📝",
            prompt="~/sisters-wiki/docs/memory/ に今回の作業知見をMarkdownで整理する案を作ってください（タグ5つ以上・関連記事リンク3つ以上）。",
            send_immediately=False,
        ),
        QuickPromptItem(
            id="debug-helper",
            label="エラー原因を解析",
            icon="🛠️",
            prompt="次のエラー内容または挙動について原因と修正方針を教えてください:\n",
            send_immediately=False,
        ),
    ]


def _default_apps() -> list[AppItem]:
    chrome_fallback = "/usr/bin/google-chrome-stable" if sys.platform.startswith("linux") else "chrome"
    candidates = [
        ("chrome", "Google Chrome", "🌐", ["google-chrome-stable", "google-chrome", "chromium", "chrome"], chrome_fallback),
        ("vscode", "VS Code", "💻", ["code"], "code"),
        ("konsole", "Konsole", "🖥️", ["konsole", "wt", "powershell"], "konsole"),
        ("dolphin", "Dolphin", "📁", ["dolphin", "nautilus", "thunar"], "dolphin"),
        ("firefox", "Firefox", "🦊", ["firefox"], "firefox"),
        ("antigravity", "Antigravity CLI", "✨", ["agy"], "agy"),
    ]
    apps: list[AppItem] = []
    for app_id, label, icon, bins, fallback_cmd in candidates:
        found = next((shutil.which(b) for b in bins if shutil.which(b)), None)
        if found:
            apps.append(AppItem(id=app_id, label=label, icon=icon, command=found))
        elif app_id in ("chrome", "vscode") and (not fallback_cmd.startswith("/") or Path(fallback_cmd).exists()):
            apps.append(AppItem(id=app_id, label=label, icon=icon, command=fallback_cmd))
    return apps


def _default_folders() -> list[FolderItem]:
    home = Path.home()
    folders: list[FolderItem] = [
        FolderItem(id="home", label="ホーム", icon="🏠", path=str(home)),
    ]
    optional_groups = [
        ("projects", "プロジェクト", "💻", [home / "プロジェクト", home / "Projects", home / "projects"]),
        ("sisters-wiki", "Sisters Wiki", "📚", [home / "sisters-wiki"]),
        ("downloads", "ダウンロード", "📥", [home / "ダウンロード", home / "Downloads"]),
        ("shared", "共有", "🤝", [home / "共有", home / "Documents"]),
    ]
    for fid, label, icon, candidates in optional_groups:
        existing = next((c for c in candidates if c.is_dir()), None)
        if existing is not None:
            folders.append(FolderItem(id=fid, label=label, icon=icon, path=str(existing)))
    return folders


def _default_bookmarks() -> list[BookmarkItem]:
    return [
        BookmarkItem(id="sisters-wiki", label="Sisters Wiki", icon="📚", url="http://localhost:8017"),
        BookmarkItem(id="forgejo", label="Forgejo", icon="🦊", url="http://localhost:4100"),
        BookmarkItem(id="google", label="Google", icon="🔍", url="https://www.google.com"),
        BookmarkItem(id="github", label="GitHub", icon="🐙", url="https://github.com"),
        BookmarkItem(id="youtube", label="YouTube", icon="▶️", url="https://www.youtube.com"),
    ]


@dataclass
class AppConfig:
    sister_id: str = "aoi"
    agent_id: str = "agy"
    models_by_agent: dict[str, str] = field(default_factory=dict)
    orchestrator_mode: bool = False
    orchestrator_confirm_plan: bool = False
    orchestrator_max_parallel: int = 2
    orchestrator_switch_mascot: bool = True
    sister_assignments: dict[str, SisterAgentAssignment] = field(default_factory=dict)
    working_dir: str = field(default_factory=lambda: str(Path.home()))
    auto_approve: bool = True
    auto_compact_enabled: bool = False
    auto_compact_chars: int = 18000
    mascot_x: int | None = None
    mascot_y: int | None = None
    mascot_scale: str = "medium"
    mascot_width: int = 210
    panel_x: int | None = None
    panel_y: int | None = None
    panel_width: int = 440
    panel_height: int = 600
    animation_mode: str = "rich"
    always_on_top: bool = True
    tts_engine: str = "edge"
    tts_volume: int = 80
    tts_rate_offset: int = 0
    tts_max_chars: int = 220
    tts_click_reactions: bool = True
    tts_skip_code: bool = True
    tts_reading_overrides: dict[str, str] = field(default_factory=dict)
    voicevox_url: str = "http://127.0.0.1:50021"
    voicevox_speaker: int = 3
    stt_engine: str = "google"
    stt_command: str = ""
    stt_auto_send: bool = False
    quick_prompts: list[QuickPromptItem] = field(default_factory=_default_quick_prompts)
    apps: list[AppItem] = field(default_factory=_default_apps)
    folders: list[FolderItem] = field(default_factory=_default_folders)
    bookmarks: list[BookmarkItem] = field(default_factory=_default_bookmarks)


def _deserialize_items(raw_list: Any, cls: type, defaults_factory: Any) -> list[Any]:
    if not isinstance(raw_list, list):
        return defaults_factory()
    valid_fields = set(cls.__dataclass_fields__.keys())
    items: list[Any] = []
    for entry in raw_list:
        if isinstance(entry, cls):
            items.append(entry)
        elif isinstance(entry, dict):
            filtered = {k: v for k, v in entry.items() if k in valid_fields}
            try:
                items.append(cls(**filtered))
            except TypeError:
                continue
    return items


def _config_path(path: Path | None) -> Path:
    return Path(path) if path is not None else CONFIG_DIR / "config.json"


def load_config(path: Path | None = None) -> AppConfig:
    """Load AppConfig from JSON, returning sensible defaults for missing/corrupt files."""
    target = _config_path(path)
    if not target.is_file():
        return AppConfig()

    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to load config %s (%s); using defaults.", target, exc)
        return AppConfig()

    if not isinstance(data, dict):
        return AppConfig()

    cfg = AppConfig()
    scalar_keys = (
        "sister_id",
        "agent_id",
        "orchestrator_mode",
        "orchestrator_confirm_plan",
        "orchestrator_max_parallel",
        "orchestrator_switch_mascot",
        "working_dir",
        "auto_approve",
        "auto_compact_enabled",
        "auto_compact_chars",
        "mascot_x",
        "mascot_y",
        "mascot_scale",
        "mascot_width",
        "panel_x",
        "panel_y",
        "panel_width",
        "panel_height",
        "animation_mode",
        "always_on_top",
        "tts_engine",
        "tts_volume",
        "tts_rate_offset",
        "tts_max_chars",
        "tts_click_reactions",
        "tts_skip_code",
        "voicevox_url",
        "voicevox_speaker",
        "stt_engine",
        "stt_command",
        "stt_auto_send",
    )
    for key in scalar_keys:
        if key in data:
            setattr(cfg, key, data[key])

    if isinstance(data.get("models_by_agent"), dict):
        cfg.models_by_agent = {str(k): str(v) for k, v in data["models_by_agent"].items()}

    if isinstance(data.get("tts_reading_overrides"), dict):
        cleaned_readings: dict[str, str] = {}
        for raw_k, raw_v in data["tts_reading_overrides"].items():
            kk = str(raw_k or "").strip()
            vv = str(raw_v or "").strip()
            if kk and vv:
                cleaned_readings[kk] = vv
        cfg.tts_reading_overrides = cleaned_readings

    cfg.orchestrator_mode = bool(cfg.orchestrator_mode)
    cfg.orchestrator_confirm_plan = bool(cfg.orchestrator_confirm_plan)
    cfg.orchestrator_max_parallel = _clamp_int(cfg.orchestrator_max_parallel, 2, 1, 4)
    cfg.orchestrator_switch_mascot = bool(cfg.orchestrator_switch_mascot)

    if isinstance(data.get("sister_assignments"), dict):
        parsed_assignments: dict[str, SisterAgentAssignment] = {}
        for raw_sid, raw_val in data["sister_assignments"].items():
            sid = str(raw_sid or "").strip()
            if not sid:
                continue
            if isinstance(raw_val, SisterAgentAssignment):
                aid = raw_val.agent_id if raw_val.agent_id in VALID_AGENT_IDS else ""
                parsed_assignments[sid] = SisterAgentAssignment(
                    agent_id=aid,
                    model=str(raw_val.model or "").strip(),
                    enabled=bool(raw_val.enabled),
                )
            elif isinstance(raw_val, dict):
                raw_aid = str(raw_val.get("agent_id") or "").strip()
                aid = raw_aid if raw_aid in VALID_AGENT_IDS else ""
                raw_model = str(raw_val.get("model") or "").strip()
                raw_enabled = bool(raw_val.get("enabled", True))
                parsed_assignments[sid] = SisterAgentAssignment(
                    agent_id=aid,
                    model=raw_model,
                    enabled=raw_enabled,
                )
        cfg.sister_assignments = parsed_assignments

    if cfg.mascot_scale not in SCALE_WIDTHS:
        cfg.mascot_scale = "medium"

    default_mascot_w = SCALE_WIDTHS.get(cfg.mascot_scale, 210)
    if "mascot_width" not in data or data.get("mascot_width") is None:
        cfg.mascot_width = default_mascot_w
    else:
        cfg.mascot_width = _clamp_int(
            cfg.mascot_width,
            default_mascot_w,
            MASCOT_WIDTH_MIN,
            MASCOT_WIDTH_MAX,
        )

    cfg.panel_width = _clamp_int(cfg.panel_width, 440, PANEL_WIDTH_MIN, PANEL_WIDTH_MAX)
    cfg.panel_height = _clamp_int(cfg.panel_height, 600, PANEL_HEIGHT_MIN, PANEL_HEIGHT_MAX)
    cfg.auto_compact_enabled = bool(cfg.auto_compact_enabled)
    cfg.auto_compact_chars = _clamp_int(cfg.auto_compact_chars, 18000, 4000, 100000)
    if cfg.animation_mode not in ANIMATION_MODES:
        cfg.animation_mode = "rich"

    if "quick_prompts" in data:
        cfg.quick_prompts = _deserialize_items(data["quick_prompts"], QuickPromptItem, _default_quick_prompts)
    if "apps" in data:
        cfg.apps = _deserialize_items(data["apps"], AppItem, _default_apps)
    if "folders" in data:
        cfg.folders = _deserialize_items(data["folders"], FolderItem, _default_folders)
    if "bookmarks" in data:
        cfg.bookmarks = _deserialize_items(data["bookmarks"], BookmarkItem, _default_bookmarks)

    return cfg


def save_config(cfg: AppConfig, path: Path | None = None) -> Path:
    """Persist AppConfig atomically using a temporary file and os.replace."""
    target = _config_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(asdict(cfg), ensure_ascii=False, indent=2) + "\n"

    fd, tmp_name = tempfile.mkstemp(prefix=".config-", suffix=".tmp", dir=str(target.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, target)
    except Exception:
        tmp_path.unlink(missing_ok=True)
        raise
    return target
