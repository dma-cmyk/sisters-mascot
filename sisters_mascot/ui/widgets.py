"""Shared UI widgets, dialogs, toast speech bubble, and Markdown HTML formatter."""

from __future__ import annotations

import html
import re
import uuid
from pathlib import Path
from typing import Any

from PyQt6.QtCore import QPoint, QRect, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QColor,
    QFont,
    QGuiApplication,
    QInputMethodEvent,
    QKeyEvent,
    QPainter,
    QPainterPath,
    QPen,
)
from PyQt6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QListWidget,
    QListWidgetItem,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from sisters_mascot.agents.router import AGENT_IDS
from sisters_mascot.config import AppItem, BookmarkItem, FolderItem, QuickPromptItem
from sisters_mascot.launcher.desktop_apps import DesktopAppEntry, discover_desktop_apps


DARK_DIALOG_QSS = """
QDialog {
    background-color: #131625;
    color: #f1f5f9;
    font-family: "Noto Sans JP", "Inter", sans-serif;
}
QLabel {
    color: #e2e8f0;
    font-size: 12px;
}
QLineEdit, QPlainTextEdit, QComboBox, QListWidget {
    background-color: #1d2238;
    color: #f8fafc;
    border: 1px solid rgba(148, 163, 184, 0.28);
    border-radius: 8px;
    padding: 6px 9px;
    font-size: 12.5px;
    selection-background-color: #6366f1;
}
QLineEdit:focus, QPlainTextEdit:focus, QComboBox:focus, QListWidget:focus {
    border: 1px solid #818cf8;
}
QPushButton {
    background-color: #252b45;
    color: #f1f5f9;
    border: 1px solid rgba(148, 163, 184, 0.25);
    border-radius: 8px;
    padding: 6px 12px;
    font-size: 12px;
}
QPushButton:hover {
    background-color: #31395c;
    border-color: rgba(165, 180, 252, 0.55);
}
QPushButton:pressed {
    background-color: #1f253c;
}
QCheckBox {
    color: #e2e8f0;
    spacing: 8px;
    font-size: 12px;
}
"""


def _safe_css_color(color: str, fallback: str = "#818cf8") -> str:
    c = QColor((color or "").strip())
    return c.name() if c.isValid() else fallback


def _format_inline_markdown(escaped_text: str, accent: str) -> str:
    """Apply inline formatting on already-HTML-escaped text without code blocks."""
    tokens: list[str] = []

    def _stash(html_fragment: str) -> str:
        idx = len(tokens)
        tokens.append(html_fragment)
        return f"\x00TOK{idx}\x00"

    # 1. Inline code `...`
    def _repl_code(match: re.Match[str]) -> str:
        code_body = match.group(1)
        return _stash(
            '<code style="background-color:rgba(15,23,42,0.75); '
            'color:#e2e8f0; font-family:monospace; padding:1px 4px;">'
            f"{code_body}</code>"
        )

    out = re.sub(r"`([^`\n]+)`", _repl_code, escaped_text)

    # 2. Markdown links [label](https://...)
    def _repl_md_link(match: re.Match[str]) -> str:
        label = match.group(1)
        url = match.group(2)
        if not re.match(r"^https?://", url, re.IGNORECASE):
            return match.group(0)
        return _stash(
            f'<a href="{url}" style="color:{accent}; text-decoration:underline;">{label}</a>'
        )

    out = re.sub(r"\[([^\]]+)\]\((https?://[^\s)]+)\)", _repl_md_link, out)

    # 3. Bare URLs
    def _repl_bare_url(match: re.Match[str]) -> str:
        url = match.group(1)
        trailing = ""
        while url and url[-1] in ".,;:!?)":
            trailing = url[-1] + trailing
            url = url[:-1]
        return (
            _stash(
                f'<a href="{url}" style="color:{accent}; text-decoration:underline;">{url}</a>'
            )
            + trailing
        )

    out = re.sub(r"(https?://[^\s<>\x00]+)", _repl_bare_url, out)

    # 4. Bold **...** and __...__
    out = re.sub(r"\*\*([^\n*]+?)\*\*", r"<b>\1</b>", out)
    out = re.sub(r"__([^\n_]+?)__", r"<b>\1</b>", out)

    # 5. Subtle italic *...* (avoid breaking paths or single *)
    out = re.sub(r"(?<!\w)\*([^\s*][^\n*]*?[^\s*]?)\*(?!\w)", r"<i>\1</i>", out)

    # Restore stashed HTML tokens
    for idx, frag in enumerate(tokens):
        out = out.replace(f"\x00TOK{idx}\x00", frag)
    return out


def markdown_to_bubble_html(text: str, accent: str) -> str:
    """Convert Markdown text safely into Qt RichText HTML for chat bubbles."""
    safe_accent = _safe_css_color(accent)
    raw = (text or "").replace("\r\n", "\n").replace("\r", "\n")

    # Split out fenced code blocks (including unclosed trailing block during streaming)
    code_blocks: list[str] = []
    fence_pattern = re.compile(r"```([a-zA-Z0-9_+-]*)\n?(.*?)(?:```|\Z)", re.DOTALL)

    def _repl_fence(match: re.Match[str]) -> str:
        lang = html.escape((match.group(1) or "").strip())
        body = html.escape(match.group(2).rstrip("\n"))
        lang_bar = (
            f'<div style="color:#94a3b8; font-size:10px; margin-bottom:2px;">{lang}</div>'
            if lang
            else ""
        )
        block_html = (
            '<table width="100%" cellspacing="0" cellpadding="6" '
            'style="background-color:#0b1020; border:1px solid #334155; margin:4px 0;">'
            "<tr><td>"
            f"{lang_bar}"
            '<pre style="margin:0; font-family:monospace; font-size:11.5px; '
            f'color:#e2e8f0; white-space:pre-wrap;">{body}</pre>'
            "</td></tr></table>"
        )
        idx = len(code_blocks)
        code_blocks.append(block_html)
        return f"\n\x00CODEBLOCK{idx}\x00\n"

    processed = fence_pattern.sub(_repl_fence, raw)
    lines = processed.split("\n")

    html_parts: list[str] = []
    in_ul = False
    in_ol = False

    def _close_lists() -> None:
        nonlocal in_ul, in_ol
        if in_ul:
            html_parts.append("</ul>")
            in_ul = False
        if in_ol:
            html_parts.append("</ol>")
            in_ol = False

    for line in lines:
        stripped = line.strip()

        cb_match = re.fullmatch(r"\x00CODEBLOCK(\d+)\x00", stripped)
        if cb_match:
            _close_lists()
            html_parts.append(code_blocks[int(cb_match.group(1))])
            continue

        heading_match = re.match(r"^(#{1,3})\s+(.+)$", stripped)
        if heading_match:
            _close_lists()
            level = len(heading_match.group(1))
            size = {1: 14, 2: 13, 3: 12.5}[level]
            content = _format_inline_markdown(html.escape(heading_match.group(2)), safe_accent)
            html_parts.append(
                f'<div style="font-size:{size}px; font-weight:bold; '
                f'color:{safe_accent}; margin:5px 0 2px 0;">{content}</div>'
            )
            continue

        ul_match = re.match(r"^[-*•]\s+(.+)$", stripped)
        if ul_match:
            if in_ol:
                html_parts.append("</ol>")
                in_ol = False
            if not in_ul:
                html_parts.append('<ul style="margin:2px 0 4px 16px; padding-left:4px;">')
                in_ul = True
            item_html = _format_inline_markdown(html.escape(ul_match.group(1)), safe_accent)
            html_parts.append(f'<li style="margin:1px 0;">{item_html}</li>')
            continue

        ol_match = re.match(r"^\d+[.)]\s+(.+)$", stripped)
        if ol_match:
            if in_ul:
                html_parts.append("</ul>")
                in_ul = False
            if not in_ol:
                html_parts.append('<ol style="margin:2px 0 4px 18px; padding-left:4px;">')
                in_ol = True
            item_html = _format_inline_markdown(html.escape(ol_match.group(1)), safe_accent)
            html_parts.append(f'<li style="margin:1px 0;">{item_html}</li>')
            continue

        _close_lists()
        if not stripped:
            html_parts.append('<div style="height:5px;"></div>')
        else:
            line_html = _format_inline_markdown(html.escape(line), safe_accent)
            html_parts.append(f"{line_html}<br>")

    _close_lists()

    result = "".join(html_parts).strip()
    if result.endswith("<br>"):
        result = result[:-4]
    return result or "&nbsp;"


class ToastBubble(QWidget):
    """Translucent frameless speech bubble shown cleanly above the mascot."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowFlags(
            Qt.WindowType.Tool
            | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)

        self._accent = QColor("#818cf8")
        self._is_error = False
        self._tail_on_top = False
        self._tail_offset_x = 0

        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 20)
        layout.setSpacing(3)

        self._title_label = QLabel(self)
        self._title_label.setStyleSheet(
            "color: #c7d2fe; font-size: 11px; font-weight: 700; background: transparent;"
        )
        self._title_label.hide()

        self._body_label = QLabel(self)
        self._body_label.setWordWrap(True)
        self._body_label.setMaximumWidth(270)
        self._body_label.setStyleSheet(
            "color: #f8fafc; font-size: 12.5px; line-height: 1.35; background: transparent;"
        )

        layout.addWidget(self._title_label)
        layout.addWidget(self._body_label)

        self._hide_timer = QTimer(self)
        self._hide_timer.setSingleShot(True)
        self._hide_timer.timeout.connect(self.hide)

    def show_toast(
        self,
        anchor_rect: QRect,
        text: str,
        *,
        title: str = "",
        accent: str = "#818cf8",
        duration_ms: int = 3500,
        is_error: bool = False,
    ) -> None:
        """Show speech bubble anchored near mascot geometry for `duration_ms`."""
        clean_text = (text or "").strip()
        if not clean_text:
            return

        self._is_error = is_error
        self._accent = QColor("#fb7185" if is_error else _safe_css_color(accent))

        if title:
            self._title_label.setText(title)
            title_col = "#fecdd3" if is_error else self._accent.lighter(135).name()
            self._title_label.setStyleSheet(
                f"color: {title_col}; font-size: 11px; font-weight: 700; background: transparent;"
            )
            self._title_label.show()
        else:
            self._title_label.hide()

        self._body_label.setText(clean_text)
        self._body_label.adjustSize()
        self.adjustSize()

        screen = (
            QGuiApplication.screenAt(anchor_rect.center())
            or QGuiApplication.primaryScreen()
        )
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

        w = self.width()
        h = self.height()

        target_x = anchor_rect.center().x() - w // 2
        target_x = max(avail.left() + 8, min(target_x, avail.right() - w - 8))

        above_y = anchor_rect.top() - h + 6
        if above_y >= avail.top() + 8:
            self._tail_on_top = False
            self.layout().setContentsMargins(16, 12, 16, 20)
            target_y = above_y
        else:
            self._tail_on_top = True
            self.layout().setContentsMargins(16, 20, 16, 12)
            self.adjustSize()
            h = self.height()
            target_y = min(anchor_rect.bottom() - 4, avail.bottom() - h - 8)

        self._tail_offset_x = max(
            22,
            min(anchor_rect.center().x() - target_x, w - 22),
        )

        self.move(target_x, target_y)
        self.show()
        self.raise_()
        self._hide_timer.start(max(1000, duration_ms))
        self.update()

    def reposition_near(self, anchor_rect: QRect) -> None:
        """Keep visible toast aligned while mascot is dragged."""
        if not self.isVisible():
            return
        screen = (
            QGuiApplication.screenAt(anchor_rect.center())
            or QGuiApplication.primaryScreen()
        )
        avail = screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)
        w = self.width()
        h = self.height()
        target_x = max(avail.left() + 8, min(anchor_rect.center().x() - w // 2, avail.right() - w - 8))
        if not self._tail_on_top:
            target_y = max(avail.top() + 8, anchor_rect.top() - h + 6)
        else:
            target_y = min(anchor_rect.bottom() - 4, avail.bottom() - h - 8)
        self._tail_offset_x = max(22, min(anchor_rect.center().x() - target_x, w - 22))
        self.move(target_x, target_y)

    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        self._hide_timer.stop()
        self.hide()
        super().mousePressEvent(event)

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

        rect = self.rect()
        tail_h = 9
        body_top = tail_h if self._tail_on_top else 2
        body_bot = rect.height() - (2 if self._tail_on_top else tail_h)
        body_rect = QRect(2, body_top, rect.width() - 4, max(20, body_bot - body_top))

        path = QPainterPath()
        path.addRoundedRect(
            float(body_rect.left()),
            float(body_rect.top()),
            float(body_rect.width()),
            float(body_rect.height()),
            14.0,
            14.0,
        )

        tx = float(self._tail_offset_x or rect.width() // 2)
        tail = QPainterPath()
        if self._tail_on_top:
            tail.moveTo(tx - 8, float(body_rect.top()) + 1)
            tail.lineTo(tx, 1.0)
            tail.lineTo(tx + 8, float(body_rect.top()) + 1)
        else:
            tail.moveTo(tx - 8, float(body_rect.bottom()) - 1)
            tail.lineTo(tx, float(rect.height()) - 1)
            tail.lineTo(tx + 8, float(body_rect.bottom()) - 1)
        tail.closeSubpath()
        united = path.united(tail)

        bg = QColor(42, 18, 28, 238) if self._is_error else QColor(16, 20, 36, 238)
        border = QColor(self._accent)
        border.setAlpha(205)

        painter.setPen(QPen(border, 1.4))
        painter.setBrush(bg)
        painter.drawPath(united)


class ChatInputEdit(QPlainTextEdit):
    """Multi-line chat composer: Enter sends, Shift+Enter inserts newline.

    IME (Mozc etc.) safety: QKeyEvent has no isComposing flag in Qt, so the
    preedit string is tracked via QInputMethodEvent. While a preedit is
    active, Enter belongs to the IME. Some IMEs also deliver the confirming
    Enter key *after* the commit event, so a short guard keeps that key from
    sending the message as well.
    """

    send_requested = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setPlaceholderText("メッセージを入力… (Enterで送信 / Shift+Enterで改行)")
        self.setTabChangesFocus(True)
        self._composing = False
        self._commit_guard = False
        self._commit_guard_timer = QTimer(self)
        self._commit_guard_timer.setSingleShot(True)
        self._commit_guard_timer.setInterval(0)
        self._commit_guard_timer.timeout.connect(self._clear_commit_guard)

    def inputMethodEvent(self, event: QInputMethodEvent) -> None:
        self._composing = bool(event.preeditString())
        if event.commitString():
            # Swallow a trailing Enter until the event loop drains; a real
            # user send always arrives in a later event-loop pass.
            self._commit_guard = True
            self._commit_guard_timer.start()
        super().inputMethodEvent(event)

    def _clear_commit_guard(self) -> None:
        self._commit_guard = False

    def keyPressEvent(self, event: QKeyEvent) -> None:
        key = event.key()
        mods = event.modifiers()
        if key in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if self._composing or self._commit_guard:
                # IME conversion/confirmation key: neither send nor newline.
                event.accept()
                return
            if mods & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
                return
            event.accept()
            self.send_requested.emit()
            return
        super().keyPressEvent(event)

    def focusOutEvent(self, event) -> None:  # noqa: ANN001
        self._composing = False
        self._commit_guard = False
        self._commit_guard_timer.stop()
        super().focusOutEvent(event)


class AppChooserDialog(QDialog):
    """Searchable dialog listing installed desktop applications."""

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setWindowTitle("アプリ一覧から選択")
        self.resize(440, 460)
        self.setStyleSheet(DARK_DIALOG_QSS)

        self._entries: list[DesktopAppEntry] = discover_desktop_apps()
        self.selected_entry: DesktopAppEntry | None = None

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        self._search_edit = QLineEdit(self)
        self._search_edit.setPlaceholderText("アプリ名やコマンドで絞り込み…")
        self._search_edit.textChanged.connect(self._populate_list)
        layout.addWidget(self._search_edit)

        self._list = QListWidget(self)
        self._list.itemDoubleClicked.connect(self._accept_selected)
        layout.addWidget(self._list, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._accept_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self._populate_list("")

    def _populate_list(self, query: str) -> None:
        q = (query or "").strip().lower()
        self._list.clear()
        for entry in self._entries:
            haystack = f"{entry.name} {entry.command}".lower()
            if q and q not in haystack:
                continue
            item = QListWidgetItem(f"🚀  {entry.name}\n     {entry.command}")
            item.setData(Qt.ItemDataRole.UserRole, entry)
            self._list.addItem(item)
        if self._list.count() > 0:
            self._list.setCurrentRow(0)

    def _accept_selected(self) -> None:
        current = self._list.currentItem()
        if current is None:
            return
        self.selected_entry = current.data(Qt.ItemDataRole.UserRole)
        self.accept()


class LauncherItemDialog(QDialog):
    """Dark-glass modal dialog to add or edit launcher items."""

    KIND_TITLES = {
        "prompt": "定型プロンプト",
        "app": "よく使うアプリ",
        "folder": "フォルダ",
        "bookmark": "Webブックマーク",
    }

    DEFAULT_ICONS = {
        "prompt": "⭐",
        "app": "📱",
        "folder": "📁",
        "bookmark": "🌐",
    }

    def __init__(
        self,
        kind: str,
        item: Any = None,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self.kind = kind
        self._existing_id: str | None = getattr(item, "id", None) if item is not None else None
        action = "編集" if item is not None else "追加"
        self.setWindowTitle(f"{self.KIND_TITLES.get(kind, 'アイテム')}を{action}")
        self.resize(430, 300 if kind == "prompt" else 220)
        self.setStyleSheet(DARK_DIALOG_QSS)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(16, 16, 16, 14)
        outer.setSpacing(12)

        form = QFormLayout()
        form.setSpacing(10)

        self._icon_edit = QLineEdit(self)
        self._icon_edit.setMaximumWidth(72)
        self._icon_edit.setText(getattr(item, "icon", "") or self.DEFAULT_ICONS.get(kind, "✨"))

        self._label_edit = QLineEdit(self)
        self._label_edit.setPlaceholderText("表示名")
        if item is not None:
            self._label_edit.setText(getattr(item, "label", ""))

        top_row = QHBoxLayout()
        top_row.setSpacing(8)
        top_row.addWidget(self._icon_edit)
        top_row.addWidget(self._label_edit, 1)
        form.addRow("アイコン / 名前:", top_row)

        # Kind-specific controls
        self._prompt_edit: QPlainTextEdit | None = None
        self._send_now_check: QCheckBox | None = None
        self._preferred_agent_combo: QComboBox | None = None
        self._command_edit: QLineEdit | None = None
        self._path_edit: QLineEdit | None = None
        self._url_edit: QLineEdit | None = None

        if kind == "prompt":
            self._prompt_edit = QPlainTextEdit(self)
            self._prompt_edit.setPlaceholderText("AIに送るプロンプト内容…")
            self._prompt_edit.setMinimumHeight(105)
            if item is not None:
                self._prompt_edit.setPlainText(getattr(item, "prompt", ""))
            form.addRow("プロンプト:", self._prompt_edit)

            self._preferred_agent_combo = QComboBox(self)
            self._preferred_agent_combo.addItem("指定なし（現在のエージェント）", "")
            for aid in AGENT_IDS:
                self._preferred_agent_combo.addItem(aid, aid)
            pref = getattr(item, "preferred_agent", "") if item is not None else ""
            idx = self._preferred_agent_combo.findData(pref)
            if idx >= 0:
                self._preferred_agent_combo.setCurrentIndex(idx)
            form.addRow("優先AI:", self._preferred_agent_combo)

            self._send_now_check = QCheckBox("クリックですぐに送信する", self)
            self._send_now_check.setChecked(bool(getattr(item, "send_immediately", False)))
            form.addRow("", self._send_now_check)

        elif kind == "app":
            self._command_edit = QLineEdit(self)
            self._command_edit.setPlaceholderText("例: google-chrome-stable / kitty")
            if item is not None:
                self._command_edit.setText(getattr(item, "command", ""))

            pick_btn = QPushButton("アプリ一覧から選択", self)
            pick_btn.clicked.connect(self._choose_desktop_app)

            cmd_row = QHBoxLayout()
            cmd_row.setSpacing(6)
            cmd_row.addWidget(self._command_edit, 1)
            cmd_row.addWidget(pick_btn)
            form.addRow("コマンド:", cmd_row)

        elif kind == "folder":
            self._path_edit = QLineEdit(self)
            self._path_edit.setPlaceholderText(f"{Path.home()}/...")
            if item is not None:
                self._path_edit.setText(getattr(item, "path", ""))

            browse_btn = QPushButton("参照…", self)
            browse_btn.clicked.connect(self._browse_folder)

            path_row = QHBoxLayout()
            path_row.setSpacing(6)
            path_row.addWidget(self._path_edit, 1)
            path_row.addWidget(browse_btn)
            form.addRow("パス:", path_row)

        elif kind == "bookmark":
            self._url_edit = QLineEdit(self)
            self._url_edit.setPlaceholderText("https://...")
            if item is not None:
                self._url_edit.setText(getattr(item, "url", ""))
            form.addRow("URL:", self._url_edit)

        outer.addLayout(form, 1)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            self,
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)

    def _choose_desktop_app(self) -> None:
        dlg = AppChooserDialog(self)
        if dlg.exec() == QDialog.DialogCode.Accepted and dlg.selected_entry is not None:
            entry = dlg.selected_entry
            if self._command_edit is not None:
                self._command_edit.setText(entry.command)
            if not self._label_edit.text().strip():
                self._label_edit.setText(entry.name)

    def _browse_folder(self) -> None:
        start_dir = (self._path_edit.text().strip() if self._path_edit else "") or str(Path.home())
        chosen = QFileDialog.getExistingDirectory(self, "フォルダを選択", start_dir)
        if chosen and self._path_edit is not None:
            self._path_edit.setText(chosen)
            if not self._label_edit.text().strip():
                self._label_edit.setText(Path(chosen).name or chosen)

    def _on_accept(self) -> None:
        if not self._label_edit.text().strip():
            QMessageBox.warning(self, "入力エラー", "表示名を入力してください。")
            return
        if self.kind == "prompt" and self._prompt_edit is not None:
            if not self._prompt_edit.toPlainText().strip():
                QMessageBox.warning(self, "入力エラー", "プロンプトを入力してください。")
                return
        elif self.kind == "app" and self._command_edit is not None:
            if not self._command_edit.text().strip():
                QMessageBox.warning(self, "入力エラー", "コマンドを入力してください。")
                return
        elif self.kind == "folder" and self._path_edit is not None:
            if not self._path_edit.text().strip():
                QMessageBox.warning(self, "入力エラー", "フォルダパスを入力してください。")
                return
        elif self.kind == "bookmark" and self._url_edit is not None:
            if not self._url_edit.text().strip():
                QMessageBox.warning(self, "入力エラー", "URLを入力してください。")
                return
        self.accept()

    def get_item(self) -> QuickPromptItem | AppItem | FolderItem | BookmarkItem:
        """Build the resulting dataclass item from form fields."""
        item_id = self._existing_id or f"{self.kind}-{uuid.uuid4().hex[:8]}"
        label = self._label_edit.text().strip()
        icon = self._icon_edit.text().strip() or self.DEFAULT_ICONS.get(self.kind, "✨")

        if self.kind == "prompt":
            assert self._prompt_edit is not None
            assert self._send_now_check is not None
            assert self._preferred_agent_combo is not None
            return QuickPromptItem(
                id=item_id,
                label=label,
                icon=icon,
                prompt=self._prompt_edit.toPlainText().strip(),
                send_immediately=self._send_now_check.isChecked(),
                preferred_agent=str(self._preferred_agent_combo.currentData() or ""),
            )
        if self.kind == "app":
            assert self._command_edit is not None
            return AppItem(
                id=item_id,
                label=label,
                icon=icon,
                command=self._command_edit.text().strip(),
            )
        if self.kind == "folder":
            assert self._path_edit is not None
            return FolderItem(
                id=item_id,
                label=label,
                icon=icon,
                path=self._path_edit.text().strip(),
            )
        assert self._url_edit is not None
        url = self._url_edit.text().strip()
        if url and "://" not in url:
            url = f"https://{url}"
        return BookmarkItem(
            id=item_id,
            label=label,
            icon=icon,
            url=url,
        )
