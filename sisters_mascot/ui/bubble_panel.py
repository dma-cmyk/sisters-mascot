"""Floating rounded dark-glass chat, launcher, and settings bubble panel."""

from __future__ import annotations

import html
from datetime import datetime
from pathlib import Path
from typing import Sequence

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QGuiApplication
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QFileDialog,
    QFormLayout,
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizeGrip,
    QSlider,
    QSpinBox,
    QTabWidget,
    QTextBrowser,
    QToolButton,
    QVBoxLayout,
    QWidget,
)

from sisters_mascot import USER_SPRITES_DIR
from sisters_mascot.agents.base import ModelChoice
from sisters_mascot.agents.router import AGENT_IDS, AgentRouter
from sisters_mascot.config import (
    SCALE_WIDTHS,
    AppConfig,
    AppItem,
    BookmarkItem,
    FolderItem,
    QuickPromptItem,
)
from sisters_mascot.launcher.actions import launch_app, open_bookmark, open_folder
from sisters_mascot.sisters_loader import (
    SisterProfile,
    find_sprite_path,
    has_custom_or_generated_sprite,
    import_user_sprite,
    reset_user_sprite,
)
from sisters_mascot.sprites.renderer import clear_sprite_cache
from sisters_mascot.ui.widgets import (
    ChatInputEdit,
    LauncherItemDialog,
    markdown_to_bubble_html,
)


_SPRITE_STATES: list[tuple[str, str]] = [
    ("idle", "待機 (idle)"),
    ("happy", "うれしい (happy)"),
    ("thinking", "考え中 (thinking)"),
    ("talking", "お話し中 (talking)"),
    ("dragged", "ドラッグ中 (dragged)"),
]

_PANEL_QSS = """
#BubbleGlassFrame {
    background-color: rgba(15, 19, 34, 240);
    border: 1px solid rgba(148, 163, 184, 0.30);
    border-radius: 18px;
}
QLabel {
    color: #e2e8f0;
    background: transparent;
}
QTabWidget::pane {
    border: none;
    background: transparent;
}
QTabBar::tab {
    background: rgba(30, 36, 60, 175);
    color: #94a3b8;
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 10px;
    padding: 6px 12px;
    margin-right: 6px;
    font-size: 12px;
    font-weight: 600;
}
QTabBar::tab:selected {
    background: rgba(99, 102, 241, 0.35);
    color: #f8fafc;
    border-color: rgba(165, 180, 252, 0.55);
}
QTabBar::tab:hover:!selected {
    background: rgba(51, 65, 95, 190);
    color: #e2e8f0;
}
QComboBox, QLineEdit, QSpinBox, QPlainTextEdit {
    background-color: rgba(25, 30, 50, 225);
    color: #f8fafc;
    border: 1px solid rgba(148, 163, 184, 0.26);
    border-radius: 8px;
    padding: 4px 8px;
    font-size: 12px;
}
QComboBox:focus, QLineEdit:focus, QSpinBox:focus, QPlainTextEdit:focus {
    border-color: #818cf8;
}
QComboBox QAbstractItemView {
    background-color: #171c2f;
    color: #f8fafc;
    selection-background-color: #4f46e5;
    border: 1px solid rgba(148, 163, 184, 0.3);
}
QPushButton, QToolButton {
    background-color: rgba(38, 45, 72, 210);
    color: #f1f5f9;
    border: 1px solid rgba(148, 163, 184, 0.24);
    border-radius: 8px;
    padding: 5px 10px;
    font-size: 12px;
}
QPushButton:hover, QToolButton:hover {
    background-color: rgba(79, 86, 138, 225);
    border-color: rgba(165, 180, 252, 0.55);
}
QPushButton:pressed, QToolButton:pressed {
    background-color: rgba(30, 36, 60, 240);
}
QScrollArea {
    background: transparent;
    border: none;
}
QScrollBar:vertical {
    background: transparent;
    width: 6px;
    margin: 2px;
}
QScrollBar::handle:vertical {
    background: rgba(148, 163, 184, 0.35);
    border-radius: 3px;
    min-height: 24px;
}
QScrollBar:horizontal {
    background: transparent;
    height: 5px;
    margin: 1px;
}
QScrollBar::handle:horizontal {
    background: rgba(148, 163, 184, 0.35);
    border-radius: 2px;
}
QScrollBar::add-line, QScrollBar::sub-line {
    width: 0px;
    height: 0px;
}
QTextBrowser {
    background-color: rgba(10, 13, 24, 170);
    border: 1px solid rgba(148, 163, 184, 0.16);
    border-radius: 12px;
    padding: 6px;
    color: #f1f5f9;
    font-size: 12.5px;
}
QCheckBox {
    color: #e2e8f0;
    spacing: 8px;
    font-size: 12px;
}
QFrame.SectionCard {
    background-color: rgba(23, 28, 48, 180);
    border: 1px solid rgba(148, 163, 184, 0.18);
    border-radius: 12px;
}
"""


def _short_name(display_name: str) -> str:
    name = (display_name or "").strip()
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    if "（" in name:
        name = name.split("（", 1)[0].strip()
    return name or display_name


class BubblePanel(QWidget):
    """Floating dark-glass panel with Chat, Launcher, and Settings tabs."""

    sister_changed = pyqtSignal(str)
    agent_changed = pyqtSignal(str)
    config_updated = pyqtSignal()
    mascot_size_changed = pyqtSignal(int)
    mute_toggled = pyqtSignal()
    sprite_updated = pyqtSignal()
    reset_corner_requested = pyqtSignal()
    test_tts_requested = pyqtSignal()
    toast_requested = pyqtSignal(str, bool)

    def __init__(
        self,
        sisters: Sequence[SisterProfile],
        current_sister: SisterProfile,
        config: AppConfig,
        router: AgentRouter,
        voice_recorder,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sisters: list[SisterProfile] = sorted(sisters, key=lambda s: s.order)
        self._sister: SisterProfile = current_sister
        self._config: AppConfig = config
        self._router: AgentRouter = router
        self._voice_recorder = voice_recorder

        self._updating_controls: bool = False
        self._ui_ready: bool = False
        self._messages: list[dict[str, str]] = []
        self._streaming_text: str | None = None
        self._discovered_choices: dict[str, list[ModelChoice]] = {}
        self._manual_refresh_pending: set[str] = set()
        self._header_drag_pos: QPoint | None = None

        self.setMinimumSize(360, 440)
        self.setMaximumSize(820, 920)
        init_w = max(360, min(820, int(getattr(self._config, "panel_width", 440) or 440)))
        init_h = max(440, min(920, int(getattr(self._config, "panel_height", 600) or 600)))
        self.resize(init_w, init_h)
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self._apply_window_flags(initial=True)

        self._stream_flush_timer = QTimer(self)
        self._stream_flush_timer.setSingleShot(True)
        self._stream_flush_timer.setInterval(40)
        self._stream_flush_timer.timeout.connect(self._render_chat_log)

        self._resize_save_timer = QTimer(self)
        self._resize_save_timer.setSingleShot(True)
        self._resize_save_timer.setInterval(300)
        self._resize_save_timer.timeout.connect(self._on_resize_save_timeout)

        self._build_ui()
        self._ui_ready = True
        self.sync_from_config(self._sister)
        self._append_system_or_greeting()

    def _apply_window_flags(self, *, initial: bool = False) -> None:
        flags = Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
        if self._config.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if not initial and self.windowFlags() == flags:
            return
        was_visible = False if initial else self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()

    def _build_ui(self) -> None:
        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)

        self._frame = QFrame(self)
        self._frame.setObjectName("BubbleGlassFrame")
        self._frame.setStyleSheet(_PANEL_QSS)
        root.addWidget(self._frame)

        layout = QVBoxLayout(self._frame)
        layout.setContentsMargins(14, 12, 14, 6)
        layout.setSpacing(8)

        # Header
        self._header_widget = QWidget(self._frame)
        header_layout = QVBoxLayout(self._header_widget)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(6)

        row1 = QHBoxLayout()
        row1.setSpacing(6)

        self._sister_combo = QComboBox(self._header_widget)
        self._sister_combo.setToolTip("シスターズ（17姉妹）を選択")
        self._sister_combo.currentIndexChanged.connect(self._on_sister_combo_changed)
        row1.addWidget(self._sister_combo, 1)

        self._new_session_btn = QToolButton(self._header_widget)
        self._new_session_btn.setText("🆕")
        self._new_session_btn.setToolTip("新しい会話を開始")
        self._new_session_btn.clicked.connect(self._on_new_session)
        row1.addWidget(self._new_session_btn)

        self._mute_btn = QToolButton(self._header_widget)
        self._mute_btn.setText("🔊")
        self._mute_btn.setToolTip("音声読み上げ ON/OFF")
        self._mute_btn.clicked.connect(self.mute_toggled.emit)
        row1.addWidget(self._mute_btn)

        self._close_btn = QToolButton(self._header_widget)
        self._close_btn.setText("✕")
        self._close_btn.setToolTip("パネルを閉じる")
        self._close_btn.clicked.connect(self.hide)
        row1.addWidget(self._close_btn)

        header_layout.addLayout(row1)

        row2 = QHBoxLayout()
        row2.setSpacing(6)

        self._agent_combo = QComboBox(self._header_widget)
        self._agent_combo.setMaximumWidth(135)
        self._agent_combo.setToolTip("AIエージェントを選択")
        self._agent_combo.currentIndexChanged.connect(self._on_agent_combo_changed)
        row2.addWidget(self._agent_combo)

        self._model_combo = QComboBox(self._header_widget)
        self._model_combo.setEditable(True)
        self._model_combo.setInsertPolicy(QComboBox.InsertPolicy.NoInsert)
        if self._model_combo.lineEdit() is not None:
            self._model_combo.lineEdit().setPlaceholderText("モデル名 (空=既定)")
            self._model_combo.lineEdit().editingFinished.connect(self._on_model_edited)
        self._model_combo.activated.connect(self._on_model_activated)
        row2.addWidget(self._model_combo, 1)

        self._refresh_models_btn = QToolButton(self._header_widget)
        self._refresh_models_btn.setText("↻")
        self._refresh_models_btn.setToolTip("モデル一覧を再取得")
        self._refresh_models_btn.clicked.connect(self._on_refresh_models)
        row2.addWidget(self._refresh_models_btn)

        header_layout.addLayout(row2)
        layout.addWidget(self._header_widget)

        # Tabs
        self._tabs = QTabWidget(self._frame)
        layout.addWidget(self._tabs, 1)

        self._tabs.addTab(self._build_chat_tab(), "💬 会話")
        self._tabs.addTab(self._build_launcher_tab(), "🚀 ランチャー")
        self._tabs.addTab(self._build_settings_tab(), "⚙️ 設定")

        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch(1)
        self._size_grip = QSizeGrip(self._frame)
        self._size_grip.setFixedSize(16, 16)
        self._size_grip.setToolTip("ドラッグしてサイズ変更")
        self._size_grip.setStyleSheet(
            "QSizeGrip { background: transparent; width: 16px; height: 16px; }"
        )
        grip_row.addWidget(
            self._size_grip,
            0,
            Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignBottom,
        )
        layout.addLayout(grip_row)

    def _build_chat_tab(self) -> QWidget:
        tab = QWidget()
        layout = QVBoxLayout(tab)
        layout.setContentsMargins(0, 6, 0, 0)
        layout.setSpacing(7)

        self._chat_browser = QTextBrowser(tab)
        self._chat_browser.setOpenExternalLinks(True)
        layout.addWidget(self._chat_browser, 1)

        self._status_bar = QLabel(tab)
        self._status_bar.setStyleSheet(
            "background-color: rgba(99, 102, 241, 0.22); "
            "border: 1px solid rgba(129, 140, 248, 0.45); "
            "border-radius: 8px; padding: 4px 10px; font-size: 11.5px; color: #e0e7ff;"
        )
        self._status_bar.hide()
        layout.addWidget(self._status_bar)

        # Horizontal quick-prompt chips
        self._chips_scroll = QScrollArea(tab)
        self._chips_scroll.setWidgetResizable(True)
        self._chips_scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self._chips_scroll.setFixedHeight(38)
        self._chips_container = QWidget()
        self._chips_layout = QHBoxLayout(self._chips_container)
        self._chips_layout.setContentsMargins(0, 2, 0, 2)
        self._chips_layout.setSpacing(6)
        self._chips_scroll.setWidget(self._chips_container)
        layout.addWidget(self._chips_scroll)

        # Input row
        input_row = QHBoxLayout()
        input_row.setSpacing(6)

        self._chat_input = ChatInputEdit(tab)
        self._chat_input.setFixedHeight(58)
        self._chat_input.send_requested.connect(self._on_send_or_stop)
        input_row.addWidget(self._chat_input, 1)

        btn_col = QVBoxLayout()
        btn_col.setSpacing(4)

        self._mic_btn = QPushButton("🎤", tab)
        self._mic_btn.setFixedSize(64, 26)
        self._mic_btn.setToolTip("音声入力を録音 / 停止")
        self._mic_btn.clicked.connect(self._on_mic_clicked)
        btn_col.addWidget(self._mic_btn)

        self._send_btn = QPushButton("送信", tab)
        self._send_btn.setFixedSize(64, 28)
        self._send_btn.clicked.connect(self._on_send_or_stop)
        btn_col.addWidget(self._send_btn)

        input_row.addLayout(btn_col)
        layout.addLayout(input_row)
        return tab

    def _build_launcher_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 6, 0, 0)

        scroll = QScrollArea(tab)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        self._launcher_body = QWidget()
        self._launcher_layout = QVBoxLayout(self._launcher_body)
        self._launcher_layout.setContentsMargins(0, 0, 4, 4)
        self._launcher_layout.setSpacing(10)
        scroll.setWidget(self._launcher_body)
        return tab

    def _build_settings_tab(self) -> QWidget:
        tab = QWidget()
        outer = QVBoxLayout(tab)
        outer.setContentsMargins(0, 6, 0, 0)

        scroll = QScrollArea(tab)
        scroll.setWidgetResizable(True)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        outer.addWidget(scroll)

        body = QWidget()
        layout = QVBoxLayout(body)
        layout.setContentsMargins(0, 0, 4, 8)
        layout.setSpacing(10)

        # 1. Sprite management card
        sprite_card = QFrame(body)
        sprite_card.setProperty("class", "SectionCard")
        sprite_card.setStyleSheet(
            "QFrame { background-color: rgba(23, 28, 48, 185); "
            "border: 1px solid rgba(148, 163, 184, 0.18); border-radius: 12px; }"
        )
        sp_lay = QVBoxLayout(sprite_card)
        sp_lay.setContentsMargins(12, 10, 12, 10)
        sp_lay.setSpacing(8)

        sp_title = QLabel("🎨 立ち絵管理", sprite_card)
        sp_title.setStyleSheet("font-weight: 700; font-size: 13px; border: none;")
        sp_lay.addWidget(sp_title)

        state_row = QHBoxLayout()
        state_row.setSpacing(6)
        self._sprite_state_combo = QComboBox(sprite_card)
        for key, label in _SPRITE_STATES:
            self._sprite_state_combo.addItem(label, key)
        self._sprite_state_combo.currentIndexChanged.connect(self._update_sprite_status_label)
        state_row.addWidget(self._sprite_state_combo, 1)

        self._sprite_status_lbl = QLabel("", sprite_card)
        self._sprite_status_lbl.setStyleSheet("font-size: 11px; color: #94a3b8; border: none;")
        state_row.addWidget(self._sprite_status_lbl)
        sp_lay.addLayout(state_row)

        sp_btns_1 = QHBoxLayout()
        sp_btns_1.setSpacing(6)
        pick_sprite_btn = QPushButton("🖼️ 画像を選択 (PNG/WebP)", sprite_card)
        pick_sprite_btn.clicked.connect(self._on_import_sprite)
        open_sprite_dir_btn = QPushButton("📂 フォルダを開く", sprite_card)
        open_sprite_dir_btn.clicked.connect(self._on_open_sprite_folder)
        sp_btns_1.addWidget(pick_sprite_btn, 1)
        sp_btns_1.addWidget(open_sprite_dir_btn)
        sp_lay.addLayout(sp_btns_1)

        sp_btns_2 = QHBoxLayout()
        sp_btns_2.setSpacing(6)
        reset_one_btn = QPushButton("↩️ この状態を標準に戻す", sprite_card)
        reset_one_btn.clicked.connect(lambda: self._on_reset_sprite(all_states=False))
        reset_all_btn = QPushButton("🔄 全状態を標準に戻す", sprite_card)
        reset_all_btn.clicked.connect(lambda: self._on_reset_sprite(all_states=True))
        sp_btns_2.addWidget(reset_one_btn, 1)
        sp_btns_2.addWidget(reset_all_btn, 1)
        sp_lay.addLayout(sp_btns_2)

        layout.addWidget(sprite_card)

        # 2. Voice (TTS & STT) card
        voice_card = QFrame(body)
        voice_card.setStyleSheet(
            "QFrame { background-color: rgba(23, 28, 48, 185); "
            "border: 1px solid rgba(148, 163, 184, 0.18); border-radius: 12px; }"
        )
        vo_lay = QVBoxLayout(voice_card)
        vo_lay.setContentsMargins(12, 10, 12, 10)
        vo_lay.setSpacing(8)

        vo_title = QLabel("🔊 音声設定 (TTS / STT)", voice_card)
        vo_title.setStyleSheet("font-weight: 700; font-size: 13px; border: none;")
        vo_lay.addWidget(vo_title)

        vo_form = QFormLayout()
        vo_form.setSpacing(7)

        self._tts_engine_combo = QComboBox(voice_card)
        self._tts_engine_combo.addItem("Edge-TTS (日本語ニューラル音声)", "edge")
        self._tts_engine_combo.addItem("VOICEVOX (ローカルAPI)", "voicevox")
        self._tts_engine_combo.addItem("OFF (音声なし)", "off")
        self._tts_engine_combo.currentIndexChanged.connect(self._on_voice_setting_changed)
        vo_form.addRow("読み上げ:", self._tts_engine_combo)

        vol_row = QHBoxLayout()
        self._tts_vol_slider = QSlider(Qt.Orientation.Horizontal, voice_card)
        self._tts_vol_slider.setRange(0, 100)
        self._tts_vol_slider.valueChanged.connect(self._on_vol_slider_changed)
        self._tts_vol_lbl = QLabel("80%", voice_card)
        self._tts_vol_lbl.setStyleSheet("border: none; min-width: 36px;")
        vol_row.addWidget(self._tts_vol_slider, 1)
        vol_row.addWidget(self._tts_vol_lbl)
        vo_form.addRow("音量:", vol_row)

        rate_row = QHBoxLayout()
        self._tts_rate_slider = QSlider(Qt.Orientation.Horizontal, voice_card)
        self._tts_rate_slider.setRange(-30, 40)
        self._tts_rate_slider.valueChanged.connect(self._on_rate_slider_changed)
        self._tts_rate_lbl = QLabel("+0%", voice_card)
        self._tts_rate_lbl.setStyleSheet("border: none; min-width: 38px;")
        rate_row.addWidget(self._tts_rate_slider, 1)
        rate_row.addWidget(self._tts_rate_lbl)
        vo_form.addRow("話速補正:", rate_row)

        self._vv_url_edit = QLineEdit(voice_card)
        self._vv_url_edit.editingFinished.connect(self._on_voice_setting_changed)
        vo_form.addRow("VOICEVOX URL:", self._vv_url_edit)

        vv_row = QHBoxLayout()
        self._vv_speaker_spin = QSpinBox(voice_card)
        self._vv_speaker_spin.setRange(0, 999)
        self._vv_speaker_spin.valueChanged.connect(self._on_voice_setting_changed)
        test_tts_btn = QPushButton("🔊 テスト再生", voice_card)
        test_tts_btn.clicked.connect(self.test_tts_requested.emit)
        vv_row.addWidget(self._vv_speaker_spin)
        vv_row.addWidget(test_tts_btn)
        vo_form.addRow("話者ID:", vv_row)

        self._click_voice_check = QCheckBox("マスコットクリック時にボイス再生", voice_card)
        self._click_voice_check.toggled.connect(self._on_voice_setting_changed)
        vo_lay.addLayout(vo_form)
        vo_lay.addWidget(self._click_voice_check)

        self._skip_code_check = QCheckBox("コードブロックやURLを読み上げない", voice_card)
        self._skip_code_check.toggled.connect(self._on_voice_setting_changed)
        vo_lay.addWidget(self._skip_code_check)

        stt_form = QFormLayout()
        stt_form.setSpacing(7)

        self._stt_engine_combo = QComboBox(voice_card)
        self._stt_engine_combo.addItem("Google音声認識 (ja-JP)", "google")
        self._stt_engine_combo.addItem("カスタムコマンド ({wav})", "custom")
        self._stt_engine_combo.currentIndexChanged.connect(self._on_voice_setting_changed)
        stt_form.addRow("音声認識:", self._stt_engine_combo)

        self._stt_cmd_edit = QLineEdit(voice_card)
        self._stt_cmd_edit.setPlaceholderText("例: whisper {wav} --language ja")
        self._stt_cmd_edit.editingFinished.connect(self._on_voice_setting_changed)
        stt_form.addRow("カスタムコマンド:", self._stt_cmd_edit)

        vo_lay.addLayout(stt_form)

        self._stt_auto_send_check = QCheckBox("音声認識の結果をそのまま自動送信", voice_card)
        self._stt_auto_send_check.toggled.connect(self._on_voice_setting_changed)
        vo_lay.addWidget(self._stt_auto_send_check)

        layout.addWidget(voice_card)

        # 3. Agent & Display card
        disp_card = QFrame(body)
        disp_card.setStyleSheet(
            "QFrame { background-color: rgba(23, 28, 48, 185); "
            "border: 1px solid rgba(148, 163, 184, 0.18); border-radius: 12px; }"
        )
        di_lay = QVBoxLayout(disp_card)
        di_lay.setContentsMargins(12, 10, 12, 10)
        di_lay.setSpacing(8)

        di_title = QLabel("🤖 エージェント＆表示", disp_card)
        di_title.setStyleSheet("font-weight: 700; font-size: 13px; border: none;")
        di_lay.addWidget(di_title)

        di_form = QFormLayout()
        di_form.setSpacing(7)

        cwd_row = QHBoxLayout()
        self._cwd_edit = QLineEdit(disp_card)
        self._cwd_edit.editingFinished.connect(self._on_display_setting_changed)
        cwd_btn = QPushButton("参照…", disp_card)
        cwd_btn.clicked.connect(self._on_browse_cwd)
        cwd_row.addWidget(self._cwd_edit, 1)
        cwd_row.addWidget(cwd_btn)
        di_form.addRow("作業フォルダ:", cwd_row)

        size_container = QWidget(disp_card)
        size_vbox = QVBoxLayout(size_container)
        size_vbox.setContentsMargins(0, 0, 0, 0)
        size_vbox.setSpacing(5)

        size_slider_row = QHBoxLayout()
        size_slider_row.setSpacing(8)
        self._size_slider = QSlider(Qt.Orientation.Horizontal, size_container)
        self._size_slider.setRange(100, 420)
        self._size_slider.setSingleStep(5)
        self._size_slider.setPageStep(20)
        self._size_slider.valueChanged.connect(self._on_size_slider_changed)
        self._size_slider.sliderReleased.connect(self._on_size_slider_released)
        self._size_value_lbl = QLabel("210px", size_container)
        self._size_value_lbl.setStyleSheet("border: none; min-width: 44px; font-weight: 600;")
        size_slider_row.addWidget(self._size_slider, 1)
        size_slider_row.addWidget(self._size_value_lbl)
        size_vbox.addLayout(size_slider_row)

        preset_row = QHBoxLayout()
        preset_row.setSpacing(5)
        for label_prefix, preset_w in (("小", 165), ("中", 210), ("大", 275), ("特大", 340)):
            p_btn = QPushButton(f"{label_prefix} {preset_w}", size_container)
            p_btn.setStyleSheet("padding: 3px 6px; font-size: 11px;")
            p_btn.clicked.connect(lambda _=False, w=preset_w: self._apply_size_preset(w))
            preset_row.addWidget(p_btn, 1)
        size_vbox.addLayout(preset_row)

        di_form.addRow("📏 マスコットサイズ:", size_container)

        self._anim_mode_combo = QComboBox(disp_card)
        self._anim_mode_combo.addItem("リッチ (おすすめ)", "rich")
        self._anim_mode_combo.addItem("控えめ", "subtle")
        self._anim_mode_combo.addItem("OFF", "off")
        self._anim_mode_combo.currentIndexChanged.connect(self._on_display_setting_changed)
        di_form.addRow("✨ アニメーション:", self._anim_mode_combo)

        di_lay.addLayout(di_form)

        self._auto_approve_check = QCheckBox("ツール・コマンド実行を自動承認 (auto-approve)", disp_card)
        self._auto_approve_check.toggled.connect(self._on_display_setting_changed)
        di_lay.addWidget(self._auto_approve_check)

        self._always_on_top_check = QCheckBox("常に最前面に表示", disp_card)
        self._always_on_top_check.toggled.connect(self._on_display_setting_changed)
        di_lay.addWidget(self._always_on_top_check)

        reset_corner_btn = QPushButton("📍 マスコットを画面右下に戻す", disp_card)
        reset_corner_btn.clicked.connect(self.reset_corner_requested.emit)
        di_lay.addWidget(reset_corner_btn)

        layout.addWidget(disp_card)
        layout.addStretch(1)

        scroll.setWidget(body)
        return tab

    # Syncing & Anchoring
    def sync_from_config(self, sister: SisterProfile | None = None) -> None:
        if sister is not None:
            self._sister = sister

        self._updating_controls = True
        try:
            # Sisters combo
            self._sister_combo.clear()
            for s in self._sisters:
                badge = "✨" if has_custom_or_generated_sprite(s.id) else "💠"
                self._sister_combo.addItem(f"{badge} {s.rank_label} · {s.display_name}", s.id)
            idx = self._sister_combo.findData(self._sister.id)
            if idx >= 0:
                self._sister_combo.setCurrentIndex(idx)

            # Agent combo
            self._agent_combo.clear()
            for aid in AGENT_IDS:
                adapter = self._router.get_adapter(aid)
                disp = getattr(getattr(adapter, "spec", None), "display_name", aid) or aid
                avail = adapter.is_available() if hasattr(adapter, "is_available") else True
                label = f"{disp}" if avail else f"{disp} (未導入)"
                self._agent_combo.addItem(label, aid)
            aidx = self._agent_combo.findData(self._config.agent_id)
            if aidx >= 0:
                self._agent_combo.setCurrentIndex(aidx)

            self._populate_model_combo(self._config.agent_id)
            self._sync_mute_button()

            # Settings widgets
            t_idx = self._tts_engine_combo.findData(self._config.tts_engine)
            if t_idx >= 0:
                self._tts_engine_combo.setCurrentIndex(t_idx)
            self._tts_vol_slider.setValue(int(self._config.tts_volume))
            self._tts_vol_lbl.setText(f"{int(self._config.tts_volume)}%")
            self._tts_rate_slider.setValue(int(self._config.tts_rate_offset))
            self._tts_rate_lbl.setText(f"{int(self._config.tts_rate_offset):+d}%")
            self._vv_url_edit.setText(self._config.voicevox_url)
            self._vv_speaker_spin.setValue(int(self._config.voicevox_speaker))
            self._click_voice_check.setChecked(bool(self._config.tts_click_reactions))
            self._skip_code_check.setChecked(bool(self._config.tts_skip_code))

            s_idx = self._stt_engine_combo.findData(self._config.stt_engine)
            if s_idx >= 0:
                self._stt_engine_combo.setCurrentIndex(s_idx)
            self._stt_cmd_edit.setText(self._config.stt_command)
            self._stt_cmd_edit.setEnabled(self._config.stt_engine == "custom")
            self._stt_auto_send_check.setChecked(bool(self._config.stt_auto_send))

            self._cwd_edit.setText(self._config.working_dir)
            fallback_w = SCALE_WIDTHS.get(self._config.mascot_scale, 210)
            mascot_w = max(100, min(420, int(getattr(self._config, "mascot_width", fallback_w) or fallback_w)))
            self._size_slider.setValue(mascot_w)
            self._size_value_lbl.setText(f"{mascot_w}px")

            anim_mode = str(getattr(self._config, "animation_mode", "rich") or "rich")
            am_idx = self._anim_mode_combo.findData(anim_mode)
            if am_idx >= 0:
                self._anim_mode_combo.setCurrentIndex(am_idx)

            self._auto_approve_check.setChecked(bool(self._config.auto_approve))
            self._always_on_top_check.setChecked(bool(self._config.always_on_top))

            self._update_sprite_status_label()
            self._rebuild_quick_chips()
            self._rebuild_launcher_sections()
            self._apply_window_flags()
        finally:
            self._updating_controls = False

    def _sync_mute_button(self) -> None:
        muted = self._config.tts_engine == "off"
        self._mute_btn.setText("🔇" if muted else "🔊")

    def ensure_models_discovered(self, agent_id: str, *, force: bool = False) -> None:
        """Trigger async model discovery for `agent_id` if not cached (or when `force=True`)."""
        if not force:
            cached = self._router.get_cached_model_choices(agent_id)
            if cached is not None:
                self._discovered_choices[agent_id] = list(cached)
                if agent_id == self._config.agent_id:
                    self._refresh_models_btn.setText("↻")
                    self._refresh_models_btn.setEnabled(True)
                    self._updating_controls = True
                    try:
                        self._populate_model_combo(agent_id)
                    finally:
                        self._updating_controls = False
                return

        started = self._router.refresh_models_async(agent_id, force=force)
        if started and agent_id == self._config.agent_id:
            self._refresh_models_btn.setText("…")
            self._refresh_models_btn.setEnabled(False)

    def _populate_model_combo(self, agent_id: str) -> None:
        self._model_combo.clear()
        self._model_combo.addItem("（既定モデル）", "")

        choices = self._discovered_choices.get(agent_id)
        if choices is None:
            cached = self._router.get_cached_model_choices(agent_id)
            if cached is not None:
                choices = list(cached)
                self._discovered_choices[agent_id] = choices

        seen: set[str] = set()
        if choices:
            for choice in choices:
                cid = (choice.id or "").strip()
                if not cid or cid in seen:
                    continue
                seen.add(cid)
                display = (choice.label or cid).strip()
                self._model_combo.addItem(display, cid)
        else:
            adapter = self._router.get_adapter(agent_id)
            defaults = list(getattr(getattr(adapter, "spec", None), "default_models", []) or [])
            for m in defaults:
                m_clean = (m or "").strip()
                if m_clean and m_clean not in seen:
                    seen.add(m_clean)
                    self._model_combo.addItem(m_clean, m_clean)

        saved_model = (self._config.models_by_agent.get(agent_id, "") or "").strip()
        if saved_model:
            idx = self._model_combo.findData(saved_model)
            if idx >= 0:
                self._model_combo.setCurrentIndex(idx)
            else:
                self._model_combo.addItem(saved_model, saved_model)
                self._model_combo.setCurrentIndex(self._model_combo.count() - 1)
                self._model_combo.setEditText(saved_model)
        else:
            self._model_combo.setCurrentIndex(0)
            self._model_combo.setEditText("")

    def reposition_near_mascot(self, mascot_rect: QRect) -> None:
        """Anchor panel cleanly beside MascotWindow, auto-flipping and clamping."""
        screen = (
            QGuiApplication.screenAt(mascot_rect.center())
            or QGuiApplication.primaryScreen()
        )
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

        w = self.width()
        h = self.height()

        left_x = mascot_rect.left() - w - 12
        right_x = mascot_rect.right() + 12

        if left_x >= avail.left() + 8:
            x = left_x
        elif right_x + w <= avail.right() - 8:
            x = right_x
        else:
            # Choose whichever side has more room, then clamp
            space_left = mascot_rect.left() - avail.left()
            space_right = avail.right() - mascot_rect.right()
            x = left_x if space_left >= space_right else right_x

        x = max(avail.left() + 8, min(x, avail.right() - w - 8))
        y = mascot_rect.bottom() - h + 8
        y = max(avail.top() + 8, min(y, avail.bottom() - h - 8))
        self.move(x, y)

    def show_for_mascot(self, mascot_rect: QRect, *, tab_index: int | None = None) -> None:
        self.ensure_models_discovered(self._config.agent_id)
        self.reposition_near_mascot(mascot_rect)
        if tab_index is not None:
            self._tabs.setCurrentIndex(tab_index)
        self.show()
        self.raise_()
        self.activateWindow()
        if self._tabs.currentIndex() == 0:
            self._chat_input.setFocus()

    def toggle_for_mascot(self, mascot_rect: QRect) -> None:
        if self.isVisible():
            self.hide()
        else:
            self.show_for_mascot(mascot_rect)

    # Header drag convenience
    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton and event.position().y() <= 84:
            self._header_drag_pos = event.globalPosition().toPoint() - self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if self._header_drag_pos is not None and (event.buttons() & Qt.MouseButton.LeftButton):
            if QGuiApplication.platformName().startswith("wayland"):
                handle = self.windowHandle()
                if handle is not None:
                    self._header_drag_pos = None
                    handle.startSystemMove()
                    return
            target = event.globalPosition().toPoint() - self._header_drag_pos
            self.move(target)
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        self._header_drag_pos = None
        super().mouseReleaseEvent(event)

    # Chat & Router handling
    def _append_system_or_greeting(self) -> None:
        greeting = (self._sister.greeting or "").strip() or (
            f"{self._sister.display_name}です。いつでもお手伝いしますよ。"
        )
        self._messages.append(
            {
                "role": "sister",
                "author": f"💠 {self._sister.rank_label} · {_short_name(self._sister.display_name)}",
                "accent": self._sister.accent_color,
                "text": greeting,
                "time": datetime.now().strftime("%H:%M"),
            }
        )
        self._render_chat_log()

    def notify_sister_switched(self, sister: SisterProfile) -> None:
        self.sync_from_config(sister)
        greeting = (sister.greeting or "").strip() or f"{sister.display_name}に交代しました！"
        self._messages.append(
            {
                "role": "sister",
                "author": f"💠 {sister.rank_label} · {_short_name(sister.display_name)}",
                "accent": sister.accent_color,
                "text": greeting,
                "time": datetime.now().strftime("%H:%M"),
            }
        )
        self._render_chat_log()

    def set_running_state(self, running: bool, status: str = "") -> None:
        if running:
            self._send_btn.setText("⏹ 停止")
            self._send_btn.setStyleSheet(
                "background-color: rgba(225, 29, 72, 0.72); border-color: #fda4af; color: #fff;"
            )
            if status:
                self._status_bar.setText(status)
                self._status_bar.show()
        else:
            self._send_btn.setText("送信")
            self._send_btn.setStyleSheet("")
            self._status_bar.hide()

    def set_status_text(self, text: str) -> None:
        if text:
            self._status_bar.setText(text)
            self._status_bar.show()
        else:
            self._status_bar.hide()

    def append_stream_delta(self, chunk: str) -> None:
        if self._streaming_text is None:
            self._streaming_text = ""
        self._streaming_text += chunk
        if not self._stream_flush_timer.isActive():
            self._stream_flush_timer.start()

    def replace_stream_text(self, full_text: str) -> None:
        self._streaming_text = full_text
        if not self._stream_flush_timer.isActive():
            self._stream_flush_timer.start()

    def finalize_stream_reply(self, full_text: str) -> None:
        self._stream_flush_timer.stop()
        final_text = (full_text or self._streaming_text or "").strip()
        self._streaming_text = None
        if final_text:
            self._messages.append(
                {
                    "role": "sister",
                    "author": f"💠 {_short_name(self._sister.display_name)} ({self._config.agent_id})",
                    "accent": self._sister.accent_color,
                    "text": final_text,
                    "time": datetime.now().strftime("%H:%M"),
                }
            )
        self.set_running_state(False)
        self._render_chat_log()

    def append_error_message(self, err: str) -> None:
        self._stream_flush_timer.stop()
        if self._streaming_text:
            self._messages.append(
                {
                    "role": "sister",
                    "author": f"💠 {_short_name(self._sister.display_name)} ({self._config.agent_id})",
                    "accent": self._sister.accent_color,
                    "text": self._streaming_text,
                    "time": datetime.now().strftime("%H:%M"),
                }
            )
        self._streaming_text = None
        self._messages.append(
            {
                "role": "error",
                "author": "⚠️ エラー",
                "accent": "#fb7185",
                "text": err,
                "time": datetime.now().strftime("%H:%M"),
            }
        )
        self.set_running_state(False)
        self._render_chat_log()

    def on_model_choices_discovered(self, agent_id: str, choices: list) -> None:
        normalized: list[ModelChoice] = []
        for entry in choices:
            if isinstance(entry, ModelChoice):
                cid = (entry.id or "").strip()
                if cid:
                    normalized.append(ModelChoice(id=cid, label=(entry.label or cid).strip()))
            else:
                cid = str(entry).strip()
                if cid:
                    normalized.append(ModelChoice(id=cid, label=cid))

        self._discovered_choices[agent_id] = normalized
        if agent_id == self._config.agent_id:
            self._refresh_models_btn.setText("↻")
            self._refresh_models_btn.setEnabled(True)
            self._updating_controls = True
            try:
                self._populate_model_combo(agent_id)
            finally:
                self._updating_controls = False

        if agent_id in self._manual_refresh_pending:
            self._manual_refresh_pending.discard(agent_id)
            adapter = self._router.get_adapter(agent_id)
            disp = getattr(getattr(adapter, "spec", None), "display_name", agent_id) or agent_id
            self.toast_requested.emit(f"{disp}: {len(normalized)} 件のモデルを取得しました", False)

    def on_models_discovered(self, agent_id: str, models: list) -> None:
        choices = [ModelChoice(id=str(m).strip(), label=str(m).strip()) for m in models if str(m).strip()]
        self.on_model_choices_discovered(agent_id, choices)

    def set_voice_state(self, state: str) -> None:
        """Update mic button look: 'idle' | 'recording' | 'transcribing'."""
        if state == "recording":
            self._mic_btn.setText("⏹ 録音")
            self._mic_btn.setStyleSheet(
                "background-color: rgba(244, 63, 94, 0.80); border-color: #fda4af; color: #fff;"
            )
        elif state == "transcribing":
            self._mic_btn.setText("…")
            self._mic_btn.setStyleSheet(
                "background-color: rgba(234, 179, 8, 0.45); border-color: #fde047; color: #fff;"
            )
        else:
            self._mic_btn.setText("🎤")
            self._mic_btn.setStyleSheet("")

    def handle_transcript(self, text: str) -> None:
        clean = (text or "").strip()
        if not clean:
            return
        existing = self._chat_input.toPlainText().strip()
        combined = f"{existing} {clean}".strip() if existing else clean
        self._chat_input.setPlainText(combined)
        cursor = self._chat_input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._chat_input.setTextCursor(cursor)
        if self._config.stt_auto_send and not self._router.is_running():
            self.send_prompt(combined)
            self._chat_input.clear()

    def _render_chat_log(self) -> None:
        blocks: list[str] = []
        for msg in self._messages[-80:]:
            blocks.append(self._format_message_block(msg))

        if self._streaming_text is not None:
            blocks.append(
                self._format_message_block(
                    {
                        "role": "sister",
                        "author": f"✨ {_short_name(self._sister.display_name)} ({self._config.agent_id}) · 入力中…",
                        "accent": self._sister.accent_color,
                        "text": self._streaming_text or "…",
                        "time": "",
                    }
                )
            )

        doc_html = (
            '<html><body style="font-family: \'Noto Sans JP\', sans-serif; '
            'color: #f1f5f9; font-size: 12.5px; margin: 2px;">'
            + "".join(blocks)
            + "</body></html>"
        )
        self._chat_browser.setHtml(doc_html)
        sb = self._chat_browser.verticalScrollBar()
        sb.setValue(sb.maximum())

    def _format_message_block(self, msg: dict[str, str]) -> str:
        role = msg.get("role", "sister")
        author = html.escape(msg.get("author", ""))
        ts = html.escape(msg.get("time", ""))
        accent = msg.get("accent") or self._sister.accent_color or "#818cf8"
        body_html = markdown_to_bubble_html(msg.get("text", ""), accent)

        if role == "user":
            return (
                '<table width="100%" cellspacing="0" cellpadding="7" '
                'style="background-color:#262f52; border:1px solid #4338ca; margin:4px 0 6px 26px;">'
                "<tr><td>"
                f'<div style="font-size:10.5px; color:#c7d2fe; font-weight:bold; margin-bottom:2px;">'
                f'美緒ちゃん <span style="color:#94a3b8; font-weight:normal;">{ts}</span></div>'
                f'<div style="color:#f8fafc;">{body_html}</div>'
                "</td></tr></table>"
            )
        if role == "error":
            return (
                '<table width="100%" cellspacing="0" cellpadding="7" '
                'style="background-color:#3b1523; border-left:3px solid #fb7185; margin:4px 0 6px 0;">'
                "<tr><td>"
                f'<div style="font-size:10.5px; color:#fecdd3; font-weight:bold; margin-bottom:2px;">{author} {ts}</div>'
                f'<div style="color:#ffe4e6;">{body_html}</div>'
                "</td></tr></table>"
            )
        return (
            '<table width="100%" cellspacing="0" cellpadding="7" '
            f'style="background-color:#171d33; border-left:3px solid {html.escape(accent)}; margin:4px 12px 6px 0;">'
            "<tr><td>"
            f'<div style="font-size:10.5px; color:{html.escape(accent)}; font-weight:bold; margin-bottom:2px;">'
            f'{author} <span style="color:#64748b; font-weight:normal;">{ts}</span></div>'
            f'<div style="color:#f1f5f9;">{body_html}</div>'
            "</td></tr></table>"
        )

    def send_prompt(self, text: str, *, preferred_agent: str = "") -> None:
        prompt = (text or "").strip()
        if not prompt:
            return
        if self._router.is_running():
            self.toast_requested.emit("現在応答中です。停止してから送信してください。", True)
            return

        if preferred_agent and preferred_agent in AGENT_IDS and preferred_agent != self._config.agent_id:
            self._config.agent_id = preferred_agent
            self.agent_changed.emit(preferred_agent)
            self.sync_from_config(self._sister)

        self._messages.append(
            {
                "role": "user",
                "author": "美緒ちゃん",
                "accent": "#6366f1",
                "text": prompt,
                "time": datetime.now().strftime("%H:%M"),
            }
        )
        self._streaming_text = ""
        self._render_chat_log()
        self.set_running_state(True, "✨ 考え中…")

        agent_id = self._config.agent_id
        model = (self._config.models_by_agent.get(agent_id, "") or "").strip()
        cwd = (self._config.working_dir or str(Path.home())).strip() or str(Path.home())
        self._router.send_message(
            agent_id,
            prompt,
            sister=self._sister,
            model=model,
            cwd=cwd,
            auto_approve=bool(self._config.auto_approve),
        )

    def _on_send_or_stop(self) -> None:
        if self._router.is_running():
            self._router.cancel()
            self.set_running_state(False)
            if self._streaming_text:
                self._messages.append(
                    {
                        "role": "sister",
                        "author": f"💠 {_short_name(self._sister.display_name)} (中断)",
                        "accent": self._sister.accent_color,
                        "text": self._streaming_text,
                        "time": datetime.now().strftime("%H:%M"),
                    }
                )
            self._streaming_text = None
            self._render_chat_log()
            return

        text = self._chat_input.toPlainText().strip()
        if not text:
            return
        self._chat_input.clear()
        self.send_prompt(text)

    def _on_mic_clicked(self) -> None:
        self._voice_recorder.toggle(self._config)

    # Quick-prompt chips & Launcher tab
    def _clear_layout(self, lay) -> None:  # noqa: ANN001
        while lay.count():
            child = lay.takeAt(0)
            w = child.widget()
            if w is not None:
                w.deleteLater()
            elif child.layout() is not None:
                self._clear_layout(child.layout())

    def _rebuild_quick_chips(self) -> None:
        self._clear_layout(self._chips_layout)
        for item in self._config.quick_prompts:
            btn = QPushButton(f"{item.icon} {item.label}", self._chips_container)
            btn.setToolTip(item.prompt)
            btn.setStyleSheet(
                "QPushButton { background-color: rgba(35, 42, 70, 200); "
                "border-radius: 13px; padding: 3px 10px; font-size: 11.5px; }"
                "QPushButton:hover { background-color: rgba(99, 102, 241, 0.42); }"
            )
            btn.clicked.connect(lambda _=False, qp=item: self._activate_quick_prompt(qp))
            self._chips_layout.addWidget(btn)
        self._chips_layout.addStretch(1)

    def _activate_quick_prompt(self, item: QuickPromptItem) -> None:
        self._tabs.setCurrentIndex(0)
        if item.send_immediately:
            self.send_prompt(item.prompt, preferred_agent=item.preferred_agent)
            return
        if item.preferred_agent and item.preferred_agent in AGENT_IDS and item.preferred_agent != self._config.agent_id:
            self._config.agent_id = item.preferred_agent
            self.agent_changed.emit(item.preferred_agent)
            self.sync_from_config(self._sister)
        self._chat_input.setPlainText(item.prompt)
        self._chat_input.setFocus()
        cursor = self._chat_input.textCursor()
        cursor.movePosition(cursor.MoveOperation.End)
        self._chat_input.setTextCursor(cursor)

    def _rebuild_launcher_sections(self) -> None:
        self._clear_layout(self._launcher_layout)

        sections = [
            ("prompt", "⭐ 定型プロンプト", self._config.quick_prompts),
            ("app", "📱 よく使うアプリ", self._config.apps),
            ("folder", "📁 フォルダ", self._config.folders),
            ("bookmark", "🌐 Webブックマーク", self._config.bookmarks),
        ]
        for kind, title, items in sections:
            card = QFrame(self._launcher_body)
            card.setStyleSheet(
                "QFrame { background-color: rgba(23, 28, 48, 185); "
                "border: 1px solid rgba(148, 163, 184, 0.18); border-radius: 12px; }"
            )
            c_lay = QVBoxLayout(card)
            c_lay.setContentsMargins(10, 8, 10, 10)
            c_lay.setSpacing(8)

            hdr = QHBoxLayout()
            hdr_lbl = QLabel(title, card)
            hdr_lbl.setStyleSheet("font-weight: 700; font-size: 12.5px; border: none;")
            add_btn = QPushButton("+ 追加", card)
            add_btn.setFixedHeight(24)
            add_btn.clicked.connect(lambda _=False, k=kind: self._add_launcher_item(k))
            hdr.addWidget(hdr_lbl, 1)
            hdr.addWidget(add_btn)
            c_lay.addLayout(hdr)

            if not items:
                empty_lbl = QLabel("（アイテムがありません）", card)
                empty_lbl.setStyleSheet("color: #64748b; font-size: 11px; border: none;")
                c_lay.addWidget(empty_lbl)
            else:
                grid = QGridLayout()
                grid.setSpacing(6)
                for idx, item in enumerate(items):
                    btn = QPushButton(f"{item.icon} {item.label}", card)
                    btn.setStyleSheet("text-align: left; padding: 6px 10px;")
                    btn.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
                    btn.customContextMenuRequested.connect(
                        lambda pos, b=btn, k=kind, it=item: self._show_launcher_item_menu(b, pos, k, it)
                    )
                    if kind == "prompt":
                        btn.setToolTip(item.prompt)
                        btn.clicked.connect(lambda _=False, qp=item: self._activate_quick_prompt(qp))
                    elif kind == "app":
                        btn.setToolTip(item.command)
                        btn.clicked.connect(lambda _=False, ap=item: self._run_launcher_action(launch_app(ap.command)))
                    elif kind == "folder":
                        btn.setToolTip(item.path)
                        btn.clicked.connect(lambda _=False, fd=item: self._run_launcher_action(open_folder(fd.path)))
                    elif kind == "bookmark":
                        btn.setToolTip(item.url)
                        btn.clicked.connect(lambda _=False, bm=item: self._run_launcher_action(open_bookmark(bm.url)))
                    grid.addWidget(btn, idx // 2, idx % 2)
                c_lay.addLayout(grid)

            self._launcher_layout.addWidget(card)

        self._launcher_layout.addStretch(1)

    def _run_launcher_action(self, result: tuple[bool, str]) -> None:
        ok, msg = result
        if not ok and msg:
            self.toast_requested.emit(msg, True)

    def _launcher_list_for_kind(self, kind: str) -> list:
        return {
            "prompt": self._config.quick_prompts,
            "app": self._config.apps,
            "folder": self._config.folders,
            "bookmark": self._config.bookmarks,
        }[kind]

    def _add_launcher_item(self, kind: str) -> None:
        dlg = LauncherItemDialog(kind, None, self)
        if dlg.exec() == LauncherItemDialog.DialogCode.Accepted:
            self._launcher_list_for_kind(kind).append(dlg.get_item())
            self.config_updated.emit()
            self._rebuild_quick_chips()
            self._rebuild_launcher_sections()

    def _show_launcher_item_menu(self, widget: QWidget, pos: QPoint, kind: str, item) -> None:  # noqa: ANN001
        menu = QMenu(widget)
        menu.setStyleSheet(
            "QMenu { background:#14192b; color:#f1f5f9; border:1px solid #334155; border-radius:8px; padding:4px; }"
            "QMenu::item { padding:5px 12px; border-radius:5px; }"
            "QMenu::item:selected { background:#4f46e5; }"
        )

        if kind == "bookmark":
            pw_act = QAction("🌐 Playwrightで調査", menu)
            pw_act.triggered.connect(lambda: self._investigate_bookmark_with_playwright(item))
            menu.addAction(pw_act)
            menu.addSeparator()

        edit_act = QAction("✏️ 編集", menu)
        edit_act.triggered.connect(lambda: self._edit_launcher_item(kind, item))
        menu.addAction(edit_act)

        del_act = QAction("🗑️ 削除", menu)
        del_act.triggered.connect(lambda: self._delete_launcher_item(kind, item))
        menu.addAction(del_act)

        menu.exec(widget.mapToGlobal(pos))

    def _investigate_bookmark_with_playwright(self, bm: BookmarkItem) -> None:
        self._tabs.setCurrentIndex(0)
        prompt = (
            f"Playwright MCP（chrome-playwright-browser）を使って「{bm.label}」({bm.url}) "
            "を開き、現在のページ内容や重要なポイントを簡潔に調査して報告してください。"
        )
        self.send_prompt(prompt)

    def _edit_launcher_item(self, kind: str, item) -> None:  # noqa: ANN001
        dlg = LauncherItemDialog(kind, item, self)
        if dlg.exec() == LauncherItemDialog.DialogCode.Accepted:
            updated = dlg.get_item()
            target_list = self._launcher_list_for_kind(kind)
            for idx, existing in enumerate(target_list):
                if existing.id == item.id:
                    target_list[idx] = updated
                    break
            self.config_updated.emit()
            self._rebuild_quick_chips()
            self._rebuild_launcher_sections()

    def _delete_launcher_item(self, kind: str, item) -> None:  # noqa: ANN001
        target_list = self._launcher_list_for_kind(kind)
        for idx, existing in enumerate(target_list):
            if existing.id == item.id:
                del target_list[idx]
                break
        self.config_updated.emit()
        self._rebuild_quick_chips()
        self._rebuild_launcher_sections()

    # Header event handlers
    def _on_sister_combo_changed(self, idx: int) -> None:
        if self._updating_controls or idx < 0:
            return
        sid = str(self._sister_combo.itemData(idx) or "")
        if sid and sid != self._sister.id:
            self.sister_changed.emit(sid)

    def _on_agent_combo_changed(self, idx: int) -> None:
        if self._updating_controls or idx < 0:
            return
        aid = str(self._agent_combo.itemData(idx) or "")
        if aid and aid != self._config.agent_id:
            self._config.agent_id = aid
            self._updating_controls = True
            try:
                self._populate_model_combo(aid)
            finally:
                self._updating_controls = False
            self.ensure_models_discovered(aid)
            self.agent_changed.emit(aid)
            self.config_updated.emit()

    def _on_model_activated(self, idx: int) -> None:
        if self._updating_controls:
            return
        data = self._model_combo.itemData(idx)
        model_name = str(data).strip() if data is not None else self._model_combo.currentText().strip()
        if idx == 0 and (data == "" or data is None):
            self._model_combo.setEditText("")
            model_name = ""
        self._save_model_selection(model_name)

    def _on_model_edited(self) -> None:
        if self._updating_controls:
            return
        text = self._model_combo.currentText().strip()
        if not text or text == "（既定モデル）":
            self._model_combo.setEditText("")
            self._save_model_selection("")
            return
        for idx in range(self._model_combo.count()):
            item_text = self._model_combo.itemText(idx).strip()
            item_data = str(self._model_combo.itemData(idx) or "").strip()
            if text == item_text or (item_data and text == item_data):
                self._save_model_selection(item_data)
                return
        self._save_model_selection(text)

    def _save_model_selection(self, model_name: str) -> None:
        aid = self._config.agent_id
        if model_name:
            self._config.models_by_agent[aid] = model_name
        else:
            self._config.models_by_agent.pop(aid, None)
        self.config_updated.emit()

    def _on_refresh_models(self) -> None:
        aid = self._config.agent_id
        self._manual_refresh_pending.add(aid)
        self.ensure_models_discovered(aid, force=True)

    def _on_new_session(self) -> None:
        if self._router.is_running():
            self._router.cancel()
            self.set_running_state(False)
        self._streaming_text = None
        self._router.new_session(self._config.agent_id)
        self._messages.clear()
        self._append_system_or_greeting()

    # Settings handlers
    def _update_sprite_status_label(self) -> None:
        state = str(self._sprite_state_combo.currentData() or "idle")
        user_dir = USER_SPRITES_DIR / self._sister.id
        has_user = any((user_dir / f"{state}{ext}").exists() for ext in (".png", ".webp"))
        if has_user:
            self._sprite_status_lbl.setText("✨ カスタム")
        elif find_sprite_path(self._sister.id, state) is not None:
            self._sprite_status_lbl.setText("🎨 標準画像")
        else:
            self._sprite_status_lbl.setText("💠 メダル表示")

    def _on_import_sprite(self) -> None:
        state = str(self._sprite_state_combo.currentData() or "idle")
        path_str, _ = QFileDialog.getOpenFileName(
            self,
            "立ち絵画像を選択",
            str(Path.home()),
            "Images (*.png *.webp *.jpg *.jpeg)",
        )
        if not path_str:
            return
        try:
            import_user_sprite(self._sister.id, state, Path(path_str))
            clear_sprite_cache()
            self._update_sprite_status_label()
            self.sprite_updated.emit()
            self.sync_from_config(self._sister)
            self.toast_requested.emit(f"{_short_name(self._sister.display_name)}の {state} 立ち絵を設定しました！", False)
        except Exception as exc:
            QMessageBox.warning(self, "読み込みエラー", f"画像の取り込みに失敗しました:\n{exc}")

    def _on_open_sprite_folder(self) -> None:
        target = USER_SPRITES_DIR / self._sister.id
        target.mkdir(parents=True, exist_ok=True)
        ok, msg = open_folder(str(target))
        if not ok and msg:
            self.toast_requested.emit(msg, True)

    def _on_reset_sprite(self, *, all_states: bool) -> None:
        state = None if all_states else str(self._sprite_state_combo.currentData() or "idle")
        reset_user_sprite(self._sister.id, state)
        clear_sprite_cache()
        self._update_sprite_status_label()
        self.sprite_updated.emit()
        self.sync_from_config(self._sister)
        self.toast_requested.emit("カスタム立ち絵を標準に戻しました。", False)

    def _on_vol_slider_changed(self, value: int) -> None:
        self._tts_vol_lbl.setText(f"{value}%")
        if not self._updating_controls:
            self._config.tts_volume = int(value)
            self.config_updated.emit()

    def _on_rate_slider_changed(self, value: int) -> None:
        self._tts_rate_lbl.setText(f"{value:+d}%")
        if not self._updating_controls:
            self._config.tts_rate_offset = int(value)
            self.config_updated.emit()

    def _on_voice_setting_changed(self) -> None:
        if self._updating_controls:
            return
        self._config.tts_engine = str(self._tts_engine_combo.currentData() or "edge")
        self._config.voicevox_url = self._vv_url_edit.text().strip() or "http://127.0.0.1:50021"
        self._config.voicevox_speaker = int(self._vv_speaker_spin.value())
        self._config.tts_click_reactions = self._click_voice_check.isChecked()
        self._config.tts_skip_code = self._skip_code_check.isChecked()
        self._config.stt_engine = str(self._stt_engine_combo.currentData() or "google")
        self._config.stt_command = self._stt_cmd_edit.text().strip()
        self._stt_cmd_edit.setEnabled(self._config.stt_engine == "custom")
        self._config.stt_auto_send = self._stt_auto_send_check.isChecked()
        self._sync_mute_button()
        self.config_updated.emit()

    def _on_browse_cwd(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self,
            "デフォルト作業フォルダを選択",
            self._cwd_edit.text().strip() or str(Path.home()),
        )
        if chosen:
            self._cwd_edit.setText(chosen)
            self._on_display_setting_changed()

    def set_mascot_width_display(self, width: int) -> None:
        """Sync size slider and readout label without re-emitting size-change signals."""
        clamped = max(100, min(420, int(width)))
        self._updating_controls = True
        try:
            self._size_slider.setValue(clamped)
            self._size_value_lbl.setText(f"{clamped}px")
        finally:
            self._updating_controls = False

    def _on_size_slider_changed(self, value: int) -> None:
        clamped = max(100, min(420, int(value)))
        self._size_value_lbl.setText(f"{clamped}px")
        if not self._updating_controls:
            self._config.mascot_width = clamped
            self.mascot_size_changed.emit(clamped)

    def _on_size_slider_released(self) -> None:
        if not self._updating_controls:
            self.config_updated.emit()

    def _apply_size_preset(self, width: int) -> None:
        clamped = max(100, min(420, int(width)))
        self._size_slider.setValue(clamped)
        self.config_updated.emit()

    def _on_display_setting_changed(self) -> None:
        if self._updating_controls:
            return
        self._config.working_dir = self._cwd_edit.text().strip() or str(Path.home())
        self._config.animation_mode = str(self._anim_mode_combo.currentData() or "rich")
        self._config.auto_approve = self._auto_approve_check.isChecked()
        self._config.always_on_top = self._always_on_top_check.isChecked()
        self._apply_window_flags()
        self.config_updated.emit()

    def resizeEvent(self, event) -> None:  # noqa: ANN001
        super().resizeEvent(event)
        if self._ui_ready:
            self._resize_save_timer.start()

    def _on_resize_save_timeout(self) -> None:
        w = max(360, min(820, self.width()))
        h = max(440, min(920, self.height()))
        if (
            getattr(self._config, "panel_width", None) != w
            or getattr(self._config, "panel_height", None) != h
        ):
            self._config.panel_width = w
            self._config.panel_height = h
            self.config_updated.emit()

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self._stream_flush_timer.stop()
        self._resize_save_timer.stop()
        super().closeEvent(event)
