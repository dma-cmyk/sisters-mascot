"""Transparent desktop mascot window with animations, hover mini-bar, and drag/click UX."""

from __future__ import annotations

import math
import random
from typing import Sequence

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QFont,
    QGuiApplication,
    QLinearGradient,
    QPainter,
    QPen,
    QRadialGradient,
)
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QMenu,
    QToolButton,
    QWidget,
)

from sisters_mascot.agents.router import AGENT_IDS
from sisters_mascot.config import SCALE_WIDTHS, AppConfig
from sisters_mascot.sisters_loader import SisterProfile, has_custom_or_generated_sprite
from sisters_mascot.sprites.renderer import render_sister_pixmap
from sisters_mascot.ui.widgets import ToastBubble


_PAD_X = 22
_PAD_TOP = 38
_PAD_BOTTOM = 44

_MENU_QSS = """
QMenu {
    background-color: rgba(17, 21, 37, 245);
    color: #f1f5f9;
    border: 1px solid rgba(148, 163, 184, 0.30);
    border-radius: 10px;
    padding: 6px;
    font-size: 12.5px;
}
QMenu::item {
    padding: 6px 14px;
    border-radius: 6px;
}
QMenu::item:selected {
    background-color: rgba(99, 102, 241, 0.45);
    color: #ffffff;
}
QMenu::separator {
    height: 1px;
    background: rgba(148, 163, 184, 0.22);
    margin: 4px 8px;
}
"""


def _short_name(display_name: str) -> str:
    name = (display_name or "").strip()
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    if "（" in name:
        name = name.split("（", 1)[0].strip()
    return name or display_name


class MascotWindow(QWidget):
    """Frameless transparent desktop mascot widget."""

    toggle_panel_requested = pyqtSignal()
    open_panel_requested = pyqtSignal()
    sister_selected = pyqtSignal(str)
    agent_selected = pyqtSignal(str)
    voice_toggle_requested = pyqtSignal()
    mute_toggle_requested = pyqtSignal()
    always_on_top_toggled = pyqtSignal(bool)
    position_changed = pyqtSignal(int, int)
    moved_geometry = pyqtSignal(QRect)
    mascot_clicked_line = pyqtSignal(str)
    quit_requested = pyqtSignal()

    def __init__(
        self,
        sisters: Sequence[SisterProfile],
        current_sister: SisterProfile,
        config: AppConfig,
        parent: QWidget | None = None,
    ) -> None:
        super().__init__(parent)
        self._sisters: list[SisterProfile] = sorted(sisters, key=lambda s: s.order)
        self._sister: SisterProfile = current_sister
        self._config: AppConfig = config
        self._agent_id: str = config.agent_id
        self._muted: bool = config.tts_engine == "off"

        self._activity_kind: str = "idle"
        self._status_text: str = ""
        self._temp_sprite_state: str | None = None

        # Animation state
        self._anim_phase: float = 0.0
        self._bounce_vel: float = 0.0
        self._bounce_y: float = 0.0
        self._tilt_angle: float = 0.0
        self._target_tilt: float = 0.0
        self._hovered: bool = False

        # Drag state
        self._pressing: bool = False
        self._dragging: bool = False
        self._drag_toast_shown: bool = False
        self._press_global_pos = QPoint()
        self._win_start_pos = QPoint()
        self._last_drag_x: int = 0

        # Line anti-repeat tracking
        self._last_click_line: str = ""
        self._last_drag_line: str = ""

        self._toast = ToastBubble(None)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self._apply_window_flags(initial=True)

        self._init_minibar()
        self._rebuild_geometry_and_pixmap()
        self._restore_or_dock_position()

        # 50ms animation timer (breathing float, spring bounce, drag tilt decay)
        self._anim_timer = QTimer(self)
        self._anim_timer.setInterval(50)
        self._anim_timer.timeout.connect(self._on_anim_tick)
        self._anim_timer.start()

        # Temporary expression reset timer (happy after click)
        self._temp_state_timer = QTimer(self)
        self._temp_state_timer.setSingleShot(True)
        self._temp_state_timer.timeout.connect(self._clear_temp_state)

        # Hover leave debounce so child mini-bar buttons never flicker
        self._hover_leave_timer = QTimer(self)
        self._hover_leave_timer.setSingleShot(True)
        self._hover_leave_timer.setInterval(180)
        self._hover_leave_timer.timeout.connect(self._check_hover_leave)

        # Debounced position persistence (especially useful on Wayland startSystemMove)
        self._pos_save_timer = QTimer(self)
        self._pos_save_timer.setSingleShot(True)
        self._pos_save_timer.setInterval(260)
        self._pos_save_timer.timeout.connect(self._emit_position_changed)

    def _apply_window_flags(self, *, initial: bool = False) -> None:
        flags = Qt.WindowType.FramelessWindowHint | Qt.WindowType.Tool
        if self._config.always_on_top:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        if not initial and self.windowFlags() == flags:
            return
        was_visible = False if initial else self.isVisible()
        self.setWindowFlags(flags)
        if was_visible:
            self.show()
            self.raise_()

    def _init_minibar(self) -> None:
        self._minibar = QWidget(self)
        self._minibar.setObjectName("MascotMiniBar")
        self._minibar.setStyleSheet(
            """
            #MascotMiniBar {
                background-color: rgba(15, 19, 34, 224);
                border: 1px solid rgba(148, 163, 184, 0.35);
                border-radius: 15px;
            }
            QLabel {
                color: #f1f5f9;
                font-size: 11px;
                font-weight: 600;
                background: transparent;
                padding: 0 4px;
            }
            QToolButton {
                background-color: rgba(255, 255, 255, 0.08);
                color: #f8fafc;
                border: 1px solid rgba(255, 255, 255, 0.12);
                border-radius: 11px;
                font-size: 12px;
                min-width: 22px;
                max-width: 22px;
                min-height: 22px;
                max-height: 22px;
            }
            QToolButton:hover {
                background-color: rgba(129, 140, 248, 0.42);
                border-color: rgba(165, 180, 252, 0.75);
            }
            """
        )

        row = QHBoxLayout(self._minibar)
        row.setContentsMargins(8, 3, 6, 3)
        row.setSpacing(4)

        self._minibar_label = QLabel(self._minibar)
        row.addWidget(self._minibar_label)

        self._chat_btn = QToolButton(self._minibar)
        self._chat_btn.setText("💬")
        self._chat_btn.setToolTip("チャット＆ランチャー")
        self._chat_btn.clicked.connect(self.toggle_panel_requested.emit)
        row.addWidget(self._chat_btn)

        self._mic_btn = QToolButton(self._minibar)
        self._mic_btn.setText("🎤")
        self._mic_btn.setToolTip("音声入力を切替")
        self._mic_btn.clicked.connect(self.voice_toggle_requested.emit)
        row.addWidget(self._mic_btn)

        self._mute_btn = QToolButton(self._minibar)
        self._mute_btn.setText("🔇" if self._muted else "🔊")
        self._mute_btn.setToolTip("音声読み上げ ON/OFF")
        self._mute_btn.clicked.connect(self.mute_toggle_requested.emit)
        row.addWidget(self._mute_btn)

        self._update_minibar_text()
        self._minibar.hide()

    def _update_minibar_text(self) -> None:
        self._minibar_label.setText(f"💠 {_short_name(self._sister.display_name)} · {self._agent_id}")
        self._mute_btn.setText("🔇" if self._muted else "🔊")
        self._minibar.adjustSize()
        self._layout_minibar()

    def _layout_minibar(self) -> None:
        bw = min(self.width() - 8, max(168, self._minibar.sizeHint().width()))
        bh = 30
        bx = (self.width() - bw) // 2
        by = self.height() - bh - 6
        self._minibar.setGeometry(bx, by, bw, bh)

    def _sprite_state_for_render(self) -> str:
        if self._temp_sprite_state:
            return self._temp_sprite_state
        if self._activity_kind in ("thinking", "tool"):
            return "thinking"
        if self._activity_kind == "speaking":
            return "talking"
        if self._activity_kind == "listening":
            return "happy"
        return "idle"

    def _rebuild_geometry_and_pixmap(self) -> None:
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        target_width = SCALE_WIDTHS.get(self._config.mascot_scale, SCALE_WIDTHS["medium"])
        self._pixmap = render_sister_pixmap(
            self._sister,
            self._sprite_state_for_render(),
            target_width,
            dpr,
        )
        dpr_val = max(1.0, self._pixmap.devicePixelRatio())
        self._sprite_w = int(round(self._pixmap.width() / dpr_val))
        self._sprite_h = int(round(self._pixmap.height() / dpr_val))

        win_w = max(196, self._sprite_w + _PAD_X * 2)
        win_h = self._sprite_h + _PAD_TOP + _PAD_BOTTOM
        if self.size().width() != win_w or self.size().height() != win_h:
            self.setFixedSize(win_w, win_h)
        self._layout_minibar()
        self.update()

    def _screen_avail_for_point(self, pt: QPoint | None = None) -> QRect:
        screen = (
            QGuiApplication.screenAt(pt) if pt is not None else None
        ) or self.screen() or QGuiApplication.primaryScreen()
        return screen.availableGeometry() if screen else QRect(0, 0, 1920, 1080)

    def _clamp_pos_to_screen(self, pos: QPoint) -> QPoint:
        center = QPoint(pos.x() + self.width() // 2, pos.y() + self.height() // 2)
        avail = self._screen_avail_for_point(center)
        max_x = max(avail.left(), avail.right() - self.width() + 1)
        max_y = max(avail.top(), avail.bottom() - self.height() + 1)
        x = max(avail.left(), min(pos.x(), max_x))
        y = max(avail.top(), min(pos.y(), max_y))
        return QPoint(x, y)

    def _restore_or_dock_position(self) -> None:
        if self._config.mascot_x is not None and self._config.mascot_y is not None:
            pos = self._clamp_pos_to_screen(QPoint(self._config.mascot_x, self._config.mascot_y))
            self.move(pos)
            return
        self.reset_to_corner(save=False)

    def reset_to_corner(self, *, save: bool = True) -> None:
        """Dock mascot cleanly to bottom-right corner with 24px margin."""
        avail = self._screen_avail_for_point(self.geometry().center() if self.isVisible() else None)
        x = max(avail.left(), avail.right() - self.width() - 24)
        y = max(avail.top(), avail.bottom() - self.height() - 24)
        self.move(x, y)
        self._toast.reposition_near(self.frameGeometry())
        self.moved_geometry.emit(self.frameGeometry())
        if save:
            self.position_changed.emit(x, y)

    # Public control API
    def set_sisters_list(self, sisters: Sequence[SisterProfile]) -> None:
        self._sisters = sorted(sisters, key=lambda s: s.order)

    def set_sister(self, sister: SisterProfile) -> None:
        self._sister = sister
        self._update_minibar_text()
        self._rebuild_geometry_and_pixmap()

    def set_agent_id(self, agent_id: str) -> None:
        self._agent_id = agent_id
        self._update_minibar_text()

    def set_muted(self, muted: bool) -> None:
        self._muted = muted
        self._update_minibar_text()

    def set_always_on_top(self, enabled: bool) -> None:
        if self._config.always_on_top != enabled:
            self._config.always_on_top = enabled
        self._apply_window_flags()

    def set_scale(self, scale: str) -> None:
        if scale not in SCALE_WIDTHS:
            return
        old_bottom_right = self.frameGeometry().bottomRight()
        self._config.mascot_scale = scale
        self._rebuild_geometry_and_pixmap()
        new_pos = QPoint(
            old_bottom_right.x() - self.width() + 1,
            old_bottom_right.y() - self.height() + 1,
        )
        clamped = self._clamp_pos_to_screen(new_pos)
        self.move(clamped)
        self.moved_geometry.emit(self.frameGeometry())
        self.position_changed.emit(clamped.x(), clamped.y())

    def refresh_sprite(self) -> None:
        """Re-render sprite (e.g. after importing or resetting custom sprite)."""
        self._rebuild_geometry_and_pixmap()

    def set_activity(self, kind: str, label: str = "") -> None:
        """Update activity state ('idle', 'thinking', 'tool', 'listening', 'speaking')."""
        self._activity_kind = kind or "idle"
        default_labels = {
            "thinking": "✨ 考え中…",
            "tool": "🌐 Chrome操作中…",
            "listening": "🎤 聞いてるよ…",
            "speaking": "🔊 お話し中…",
        }
        self._status_text = label or default_labels.get(self._activity_kind, "")
        self._mic_btn.setStyleSheet(
            "background-color: rgba(244, 63, 94, 0.72); border-color: #fda4af;"
            if self._activity_kind == "listening"
            else ""
        )
        self._rebuild_geometry_and_pixmap()

    def show_toast(
        self,
        text: str,
        *,
        title: str = "",
        is_error: bool = False,
        duration_ms: int = 3500,
    ) -> None:
        speaker = title or f"{self._sister.rank_label} · {_short_name(self._sister.display_name)}"
        self._toast.show_toast(
            self.frameGeometry(),
            text,
            title=speaker,
            accent=self._sister.accent_color,
            duration_ms=duration_ms,
            is_error=is_error,
        )

    def trigger_bounce(self) -> None:
        self._bounce_vel = -5.6
        self._temp_sprite_state = "happy"
        self._temp_state_timer.start(1600)
        self._rebuild_geometry_and_pixmap()

    def _clear_temp_state(self) -> None:
        if self._temp_sprite_state is not None:
            self._temp_sprite_state = None
            self._rebuild_geometry_and_pixmap()

    def _pick_line(self, lines: list[str], last_line: str, fallback: str) -> str:
        pool = [ln.strip() for ln in (lines or []) if ln and ln.strip()]
        if not pool:
            return fallback
        if len(pool) > 1 and last_line in pool:
            pool = [ln for ln in pool if ln != last_line]
        return random.choice(pool)

    # Animation & Paint
    def _on_anim_tick(self) -> None:
        self._anim_phase = (self._anim_phase + 0.14) % (math.tau * 100.0)

        # Spring physics for click bounce
        if abs(self._bounce_y) > 0.05 or abs(self._bounce_vel) > 0.05:
            self._bounce_vel += (-self._bounce_y) * 0.28
            self._bounce_vel *= 0.72
            self._bounce_y += self._bounce_vel
        else:
            self._bounce_y = 0.0
            self._bounce_vel = 0.0

        # Smooth tilt interpolation
        if not self._dragging:
            self._target_tilt = 0.0
        self._tilt_angle += (self._target_tilt - self._tilt_angle) * 0.28
        if abs(self._tilt_angle) < 0.05 and not self._dragging:
            self._tilt_angle = 0.0

        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        float_y = math.sin(self._anim_phase) * 2.5
        cx = self.width() * 0.5
        cy = _PAD_TOP + self._sprite_h * 0.5 + float_y + self._bounce_y

        # Glowing aura behind sprite when active or hovered
        accent = QColor(self._sister.accent_color or "#818cf8")
        secondary = QColor(self._sister.secondary_color or "#38bdf8")
        if not accent.isValid():
            accent = QColor("#818cf8")
        if not secondary.isValid():
            secondary = QColor("#38bdf8")

        if self._activity_kind != "idle" or self._hovered:
            pulse = 0.75 + 0.25 * math.sin(self._anim_phase * 1.8)
            glow_color = QColor(accent)
            if self._activity_kind == "tool":
                glow_color = QColor("#38bdf8")
            elif self._activity_kind == "listening":
                glow_color = QColor("#fb7185")
            elif self._activity_kind == "speaking":
                glow_color = QColor("#a78bfa")

            radius = max(self._sprite_w, self._sprite_h) * 0.55
            aura = QRadialGradient(QPointF(cx, cy), radius)
            c0 = QColor(glow_color)
            c0.setAlpha(int((95 if self._activity_kind != "idle" else 52) * pulse))
            c1 = QColor(secondary)
            c1.setAlpha(int(36 * pulse))
            aura.setColorAt(0.0, c0)
            aura.setColorAt(0.55, c1)
            aura.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(aura)
            painter.drawEllipse(QPointF(cx, cy), radius, radius)

        # Draw tilted / floating sprite
        painter.save()
        painter.translate(cx, cy)
        if abs(self._tilt_angle) > 0.05:
            painter.rotate(self._tilt_angle)
        target_rect = QRectF(
            -self._sprite_w * 0.5,
            -self._sprite_h * 0.5,
            float(self._sprite_w),
            float(self._sprite_h),
        )
        painter.drawPixmap(target_rect.toRect(), self._pixmap)
        painter.restore()

        # Draw top status pill when active
        if self._activity_kind != "idle" and self._status_text:
            font = QFont("Noto Sans JP", 9, QFont.Weight.Bold)
            painter.setFont(font)
            fm = painter.fontMetrics()
            text_w = fm.horizontalAdvance(self._status_text)
            pill_w = min(float(self.width() - 8), float(text_w + 26))
            pill_h = 24.0
            pill_rect = QRectF((self.width() - pill_w) * 0.5, 4.0, pill_w, pill_h)

            bg_grad = QLinearGradient(pill_rect.topLeft(), pill_rect.bottomRight())
            bg_grad.setColorAt(0.0, QColor(15, 20, 38, 232))
            bg_grad.setColorAt(1.0, QColor(28, 34, 62, 232))
            painter.setBrush(bg_grad)
            painter.setPen(QPen(accent.lighter(125), 1.2))
            painter.drawRoundedRect(pill_rect, 12.0, 12.0)

            elided = fm.elidedText(self._status_text, Qt.TextElideMode.ElideRight, int(pill_w - 14))
            painter.setPen(QColor(248, 250, 252))
            painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, elided)

    # Hover handling
    def enterEvent(self, event) -> None:  # noqa: ANN001
        self._hover_leave_timer.stop()
        if not self._hovered:
            self._hovered = True
            self._minibar.show()
            self._minibar.raise_()
            self.update()
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # noqa: ANN001
        self._hover_leave_timer.start()
        super().leaveEvent(event)

    def _check_hover_leave(self) -> None:
        local_pos = self.mapFromGlobal(QCursor.pos())
        if not self.rect().contains(local_pos) and not self._pressing:
            self._hovered = False
            self._minibar.hide()
            self.update()

    # Mouse & Drag (XCB + Wayland fallback)
    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            self._pressing = True
            self._dragging = False
            self._drag_toast_shown = False
            self._press_global_pos = event.globalPosition().toPoint()
            self._win_start_pos = self.pos()
            self._last_drag_x = self._press_global_pos.x()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: ANN001
        if not self._pressing or not (event.buttons() & Qt.MouseButton.LeftButton):
            super().mouseMoveEvent(event)
            return

        global_pos = event.globalPosition().toPoint()
        delta = global_pos - self._press_global_pos
        if not self._dragging and delta.manhattanLength() > 6:
            self._dragging = True
            self._temp_sprite_state = "dragged"
            self._temp_state_timer.stop()
            self._rebuild_geometry_and_pixmap()

            if not self._drag_toast_shown:
                self._drag_toast_shown = True
                line = self._pick_line(
                    self._sister.drag_lines,
                    self._last_drag_line,
                    "わっ、どこへ連れて行ってくれるの？",
                )
                self._last_drag_line = line
                self.show_toast(line, duration_ms=2600)

            # Pure Wayland compositors require startSystemMove() for top-level drag
            if QGuiApplication.platformName().startswith("wayland"):
                handle = self.windowHandle()
                if handle is not None:
                    self._pressing = False
                    self._dragging = False
                    self._temp_state_timer.start(900)
                    handle.startSystemMove()
                    event.accept()
                    return

        if self._dragging:
            dx = global_pos.x() - self._last_drag_x
            self._last_drag_x = global_pos.x()
            self._target_tilt = max(-6.0, min(6.0, dx * 0.7))

            target_pos = self._clamp_pos_to_screen(self._win_start_pos + delta)
            self.move(target_pos)
            self._toast.reposition_near(self.frameGeometry())
            self.moved_geometry.emit(self.frameGeometry())
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton and self._pressing:
            was_dragging = self._dragging
            self._pressing = False
            self._dragging = False
            self._target_tilt = 0.0

            if was_dragging:
                self._pos_save_timer.stop()
                self._temp_state_timer.start(350)
                clamped = self._clamp_pos_to_screen(self.pos())
                if clamped != self.pos():
                    self.move(clamped)
                self._toast.reposition_near(self.frameGeometry())
                self.moved_geometry.emit(self.frameGeometry())
                self.position_changed.emit(self.x(), self.y())
            else:
                self.trigger_bounce()
                line = self._pick_line(
                    self._sister.click_lines,
                    self._last_click_line,
                    self._sister.greeting or "いつでもお話ししましょう！",
                )
                self._last_click_line = line
                self.show_toast(line, duration_ms=3500)
                self.mascot_clicked_line.emit(line)
                self.toggle_panel_requested.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def moveEvent(self, event) -> None:  # noqa: ANN001
        super().moveEvent(event)
        self._toast.reposition_near(self.frameGeometry())
        self.moved_geometry.emit(self.frameGeometry())
        self._pos_save_timer.start()

    def _emit_position_changed(self) -> None:
        self.position_changed.emit(self.x(), self.y())

    # Context menu
    def contextMenuEvent(self, event) -> None:  # noqa: ANN001
        menu = QMenu(self)
        menu.setStyleSheet(_MENU_QSS)

        open_act = QAction("💬 チャット＆ランチャー", menu)
        open_act.triggered.connect(self.open_panel_requested.emit)
        menu.addAction(open_act)

        sisters_menu = menu.addMenu("👯 姉妹を切り替え")
        sisters_menu.setStyleSheet(_MENU_QSS)
        for s in self._sisters:
            badge = "✨" if has_custom_or_generated_sprite(s.id) else "💠"
            act = QAction(f"{badge} {s.rank_label} · {s.display_name}", sisters_menu)
            act.setCheckable(True)
            act.setChecked(s.id == self._sister.id)
            act.triggered.connect(lambda _checked=False, sid=s.id: self.sister_selected.emit(sid))
            sisters_menu.addAction(act)

        agents_menu = menu.addMenu("🤖 AIエージェント")
        agents_menu.setStyleSheet(_MENU_QSS)
        for aid in AGENT_IDS:
            act = QAction(aid, agents_menu)
            act.setCheckable(True)
            act.setChecked(aid == self._agent_id)
            act.triggered.connect(lambda _checked=False, a=aid: self.agent_selected.emit(a))
            agents_menu.addAction(act)

        menu.addSeparator()

        voice_act = QAction("🔊 音声読み上げ (ON/OFF)", menu)
        voice_act.setCheckable(True)
        voice_act.setChecked(not self._muted)
        voice_act.triggered.connect(self.mute_toggle_requested.emit)
        menu.addAction(voice_act)

        top_act = QAction("📌 常に最前面に表示", menu)
        top_act.setCheckable(True)
        top_act.setChecked(self._config.always_on_top)
        top_act.triggered.connect(self.always_on_top_toggled.emit)
        menu.addAction(top_act)

        reset_pos_act = QAction("📍 画面右下に戻す", menu)
        reset_pos_act.triggered.connect(lambda: self.reset_to_corner(save=True))
        menu.addAction(reset_pos_act)

        menu.addSeparator()

        quit_act = QAction("✖ 終了", menu)
        quit_act.triggered.connect(self.quit_requested.emit)
        menu.addAction(quit_act)

        menu.exec(event.globalPos())

    def closeEvent(self, event) -> None:  # noqa: ANN001
        self._anim_timer.stop()
        self._temp_state_timer.stop()
        self._hover_leave_timer.stop()
        self._pos_save_timer.stop()
        self._toast.close()
        super().closeEvent(event)
