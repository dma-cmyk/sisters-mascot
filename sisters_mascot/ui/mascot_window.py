"""Transparent desktop mascot window with rich animations, hover mini-bar, and interactive resizing."""

from __future__ import annotations

import math
import random
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

from PyQt6.QtCore import QPoint, QPointF, QRect, QRectF, QSize, Qt, QTimer, pyqtSignal
from PyQt6.QtGui import (
    QAction,
    QColor,
    QCursor,
    QDragEnterEvent,
    QDragLeaveEvent,
    QDragMoveEvent,
    QDropEvent,
    QFont,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QImageReader,
    QLinearGradient,
    QMovie,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
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
from sisters_mascot.config import (
    ANIMATION_MODES,
    MASCOT_WIDTH_MAX,
    MASCOT_WIDTH_MIN,
    SCALE_WIDTHS,
    AppConfig,
)
from sisters_mascot.sisters_loader import SisterProfile, has_custom_or_generated_sprite
from sisters_mascot.sprites.renderer import (
    find_animated_sprite_path,
    render_sister_sprite,
)
from sisters_mascot.ui.platform import supports_window_positioning
from sisters_mascot.ui.widgets import ToastBubble, extract_local_paths_from_mime

_PAD_X = 28
_PAD_TOP = 42
_PAD_BOTTOM = 44

_SIZE_PRESETS: tuple[tuple[str, int], ...] = (
    ("極小 (130px)", 130),
    ("小 (165px)", 165),
    ("中 (210px)", 210),
    ("大 (275px)", 275),
    ("特大 (340px)", 340),
)

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


@dataclass(slots=True)
class _Particle:
    x: float
    y: float
    vx: float
    vy: float
    life: float
    max_life: float
    size: float
    kind: str  # "star" | "heart" | "dot"
    color: QColor
    rot: float
    vrot: float


@dataclass(slots=True)
class _AmbientMote:
    x_norm: float
    y_norm: float
    speed: float
    phase: float
    size: float
    twinkle_speed: float


@dataclass(slots=True)
class _Ripple:
    radius: float
    max_radius: float
    alpha: float
    speed: float
    color: QColor
    width: float


def _short_name(display_name: str) -> str:
    name = (display_name or "").strip()
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    if "（" in name:
        name = name.split("（", 1)[0].strip()
    return name or display_name


def _clamp_mascot_width(width: int) -> int:
    return max(MASCOT_WIDTH_MIN, min(MASCOT_WIDTH_MAX, int(width)))


def _draw_four_point_star(
    painter: QPainter,
    center: QPointF,
    radius: float,
    color: QColor,
    rotation_deg: float = 0.0,
) -> None:
    if radius <= 0.4 or color.alpha() <= 2:
        return
    inner = radius * 0.28
    path = QPainterPath()
    path.moveTo(0.0, -radius)
    path.lineTo(inner, -inner)
    path.lineTo(radius, 0.0)
    path.lineTo(inner, inner)
    path.lineTo(0.0, radius)
    path.lineTo(-inner, inner)
    path.lineTo(-radius, 0.0)
    path.lineTo(-inner, -inner)
    path.closeSubpath()

    painter.save()
    painter.translate(center)
    if abs(rotation_deg) > 0.1:
        painter.rotate(rotation_deg)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)
    painter.restore()


def _draw_heart(
    painter: QPainter,
    center: QPointF,
    size: float,
    color: QColor,
    rotation_deg: float = 0.0,
) -> None:
    if size <= 0.6 or color.alpha() <= 2:
        return
    s = size * 0.55
    path = QPainterPath()
    path.moveTo(0.0, s * 0.95)
    path.cubicTo(-s * 1.45, s * 0.10, -s * 1.10, -s * 1.05, 0.0, -s * 0.35)
    path.cubicTo(s * 1.10, -s * 1.05, s * 1.45, s * 0.10, 0.0, s * 0.95)
    path.closeSubpath()

    painter.save()
    painter.translate(center)
    if abs(rotation_deg) > 0.1:
        painter.rotate(rotation_deg)
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)
    painter.restore()


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
    size_changed = pyqtSignal(int)
    mascot_clicked_line = pyqtSignal(str)
    files_dropped = pyqtSignal(list)
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
        self._animation_mode: str = (
            config.animation_mode if config.animation_mode in ANIMATION_MODES else "rich"
        )
        self._config.mascot_width = _clamp_mascot_width(
            getattr(config, "mascot_width", SCALE_WIDTHS.get(config.mascot_scale, 210))
        )

        # Sprite / animated GIF state
        self._pixmap = QPixmap()
        self._sprite_w: int = self._config.mascot_width
        self._sprite_h: int = round(self._config.mascot_width * 1.76)
        self._base_w: int = self._sprite_w
        self._base_h: int = self._sprite_h
        self._sprite_offset_x: float = 0.0
        self._sprite_offset_y: float = 0.0
        self._sprite_content_rect = QRectF()
        self._hit_image: QImage | None = None
        self._suppress_anchor_reposition: bool = False
        self._movie: QMovie | None = None
        self._movie_path: Path | None = None

        # Core animation physics state
        self._anim_phase: float = 0.0
        self._last_tick_ts: float = time.monotonic()
        self._bounce_vel: float = 0.0
        self._bounce_y: float = 0.0
        self._squash_x: float = 1.0
        self._squash_y: float = 1.0
        self._squash_vx: float = 0.0
        self._squash_vy: float = 0.0

        # Tilt & pendulum drag physics
        self._tilt_angle: float = 0.0
        self._tilt_vel: float = 0.0
        self._target_tilt: float = 0.0

        # Hover cursor lean & friendly zoom
        self._hovered: bool = False
        self._hover_lean_angle: float = 0.0
        self._hover_zoom: float = 1.0

        # Sister switch entrance pop
        self._entrance_scale: float = 1.0
        self._entrance_vel: float = 0.0
        self._entrance_opacity: float = 1.0

        # Autonomous idle quirks
        self._quirk_tilt: float = 0.0
        self._quirk_tilt_target: float = 0.0
        self._quirk_tilt_remaining: float = 0.0
        self._quirk_hop_y: float = 0.0
        self._quirk_hop_vel: float = 0.0

        # Particles, ambient motes, and activity ripples
        self._particles: list[_Particle] = []
        self._ripples: list[_Ripple] = []
        self._ripple_cooldown: float = 0.0
        self._ambient_motes: list[_AmbientMote] = self._make_ambient_motes(5)

        # Drag state
        self._pressing: bool = False
        self._dragging: bool = False
        self._drag_toast_shown: bool = False
        self._press_global_pos = QPoint()
        self._win_start_pos = QPoint()
        self._last_drag_x: int = 0

        # Size pill overlay
        self._size_pill_text: str = ""

        # Line anti-repeat tracking
        self._last_click_line: str = ""
        self._last_drag_line: str = ""
        self._file_drop_hover: bool = False
        self._file_drop_count: int = 0

        self._toast = ToastBubble(None)

        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setAcceptDrops(True)
        self.setContextMenuPolicy(Qt.ContextMenuPolicy.DefaultContextMenu)
        self._apply_window_flags(initial=True)

        self._init_minibar()
        self._rebuild_geometry_and_pixmap()
        self._restore_or_dock_position()

        # Primary animation timer (configured by animation_mode)
        self._anim_timer = QTimer(self)
        self._anim_timer.timeout.connect(self._on_anim_tick)

        # Autonomous idle quirk timer (7-13s in rich mode)
        self._quirk_timer = QTimer(self)
        self._quirk_timer.setSingleShot(True)
        self._quirk_timer.timeout.connect(self._trigger_idle_quirk)

        # Temporary expression reset timer (happy after click / dragged)
        self._temp_state_timer = QTimer(self)
        self._temp_state_timer.setSingleShot(True)
        self._temp_state_timer.timeout.connect(self._clear_temp_state)

        # Hover leave debounce so child mini-bar buttons never flicker
        self._hover_leave_timer = QTimer(self)
        self._hover_leave_timer.setSingleShot(True)
        self._hover_leave_timer.setInterval(180)
        self._hover_leave_timer.timeout.connect(self._check_hover_leave)

        # Debounced position persistence
        self._pos_save_timer = QTimer(self)
        self._pos_save_timer.setSingleShot(True)
        self._pos_save_timer.setInterval(260)
        self._pos_save_timer.timeout.connect(self._emit_position_changed)

        # Wheel resize pill display timer
        self._size_pill_timer = QTimer(self)
        self._size_pill_timer.setSingleShot(True)
        self._size_pill_timer.timeout.connect(self._clear_size_pill)

        # Debounced wheel resize signal emission (300ms)
        self._size_save_timer = QTimer(self)
        self._size_save_timer.setSingleShot(True)
        self._size_save_timer.setInterval(300)
        self._size_save_timer.timeout.connect(self._emit_size_changed)

        self.set_animation_mode(self._animation_mode)

    @staticmethod
    def _make_ambient_motes(count: int) -> list[_AmbientMote]:
        rng = random.Random(42)
        motes: list[_AmbientMote] = []
        for idx in range(count):
            motes.append(
                _AmbientMote(
                    x_norm=0.14 + 0.72 * ((idx + 0.35 + rng.random() * 0.3) / max(1, count)),
                    y_norm=rng.random(),
                    speed=0.07 + rng.random() * 0.06,
                    phase=rng.random() * math.tau,
                    size=3.2 + rng.random() * 2.6,
                    twinkle_speed=1.6 + rng.random() * 1.4,
                )
            )
        return motes

    def _sister_colors(self) -> tuple[QColor, QColor]:
        accent = QColor(self._sister.accent_color or "#818cf8")
        secondary = QColor(self._sister.secondary_color or "#38bdf8")
        if not accent.isValid():
            accent = QColor("#818cf8")
        if not secondary.isValid():
            secondary = QColor("#38bdf8")
        return accent, secondary

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

    def _stop_movie(self) -> None:
        if self._movie is not None:
            try:
                self._movie.frameChanged.disconnect(self._on_movie_frame)
            except (TypeError, RuntimeError):
                pass
            self._movie.stop()
            self._movie.deleteLater()
            self._movie = None
            self._movie_path = None

    def _on_movie_frame(self, _frame_number: int) -> None:
        self.update()

    def _sync_movie_playback(self) -> None:
        if self._movie is None:
            return
        if self._animation_mode == "off":
            self._movie.stop()
            self._movie.jumpToFrame(0)
        else:
            if self._movie.state() != QMovie.MovieState.Running:
                self._movie.start()

    def _rebuild_geometry_and_pixmap(self) -> None:
        dpr = self.devicePixelRatioF() if hasattr(self, "devicePixelRatioF") else 1.0
        target_width = _clamp_mascot_width(self._config.mascot_width)
        self._config.mascot_width = target_width
        state = self._sprite_state_for_render()

        gif_path = find_animated_sprite_path(self._sister.id, state)
        if gif_path is not None:
            reader = QImageReader(str(gif_path))
            raw_size = reader.size()
            if raw_size.isValid() and raw_size.width() > 0:
                aspect = min(2.2, raw_size.height() / raw_size.width())
            else:
                aspect = 1.76
            self._sprite_w = target_width
            self._sprite_h = max(64, int(round(target_width * aspect)))
            self._base_w = self._sprite_w
            self._base_h = self._sprite_h
            self._sprite_offset_x = 0.0
            self._sprite_offset_y = 0.0
            self._sprite_content_rect = QRectF(
                0.0, 0.0, float(self._sprite_w), float(self._sprite_h)
            )
            self._hit_image = None

            if self._movie is None or self._movie_path != gif_path:
                self._stop_movie()
                movie = QMovie(str(gif_path), parent=self)
                movie.setCacheMode(QMovie.CacheMode.CacheAll)
                movie.frameChanged.connect(self._on_movie_frame)
                self._movie = movie
                self._movie_path = gif_path

            phys_w = max(1, int(round(self._sprite_w * max(1.0, dpr))))
            phys_h = max(1, int(round(self._sprite_h * max(1.0, dpr))))
            self._movie.setScaledSize(QSize(phys_w, phys_h))
            self._sync_movie_playback()
            self._pixmap = QPixmap()
        else:
            self._stop_movie()
            rendered = render_sister_sprite(
                self._sister,
                state,
                target_width,
                dpr,
            )
            self._pixmap = rendered.pixmap
            self._sprite_w = rendered.logical_w
            self._sprite_h = rendered.logical_h
            self._base_w = rendered.base_w
            self._base_h = rendered.base_h
            self._sprite_offset_x = rendered.offset_x
            self._sprite_offset_y = rendered.offset_y
            self._sprite_content_rect = rendered.content_rect
            self._hit_image = rendered.hit_image

        pad_x = max(_PAD_X, int(math.ceil(self._base_w * 0.24)))
        win_w = max(196, self._base_w + pad_x * 2)
        win_h = self._base_h + _PAD_TOP + _PAD_BOTTOM
        if self.size().width() != win_w or self.size().height() != win_h:
            old_geo = self.frameGeometry()
            was_shown = self.isVisible()
            self.setFixedSize(win_w, win_h)
            if (
                was_shown
                and supports_window_positioning()
                and not self._suppress_anchor_reposition
            ):
                new_x = old_geo.center().x() - win_w // 2
                new_y = old_geo.bottom() - win_h + 1
                self.move(self._clamp_pos_to_screen(QPoint(new_x, new_y)))
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
        if save:
            self.position_changed.emit(x, y)

    # Public control API
    def set_sisters_list(self, sisters: Sequence[SisterProfile]) -> None:
        self._sisters = sorted(sisters, key=lambda s: s.order)

    def set_sister(self, sister: SisterProfile) -> None:
        self._sister = sister
        self._temp_state_timer.stop()
        self._temp_sprite_state = None
        self._update_minibar_text()
        self._rebuild_geometry_and_pixmap()
        if self._animation_mode == "off":
            self._entrance_scale = 1.0
            self._entrance_vel = 0.0
            self._entrance_opacity = 1.0
            self.update()
            return
        # Celebratory entrance pop (0.84 -> ~1.05 -> 1.00) + fade-in + sparkle burst
        self._entrance_scale = 0.84
        self._entrance_vel = 2.4
        self._entrance_opacity = 0.0
        burst_count = 9 if self._animation_mode == "rich" else 4
        self._spawn_burst_particles(burst_count, include_hearts=True)
        self.update()

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

    def set_mascot_width(self, width: int, *, emit_signal: bool = False) -> None:
        """Resize mascot cleanly to `width` (clamped 100..420), anchoring bottom-right."""
        clamped_w = _clamp_mascot_width(width)
        old_bottom_right = self.frameGeometry().bottomRight()
        self._config.mascot_width = clamped_w
        self._suppress_anchor_reposition = True
        try:
            self._rebuild_geometry_and_pixmap()
        finally:
            self._suppress_anchor_reposition = False
        new_pos = QPoint(
            old_bottom_right.x() - self.width() + 1,
            old_bottom_right.y() - self.height() + 1,
        )
        clamped_pos = self._clamp_pos_to_screen(new_pos)
        self.move(clamped_pos)
        self._toast.reposition_near(self.frameGeometry())
        if emit_signal:
            self._pos_save_timer.stop()
            self.position_changed.emit(clamped_pos.x(), clamped_pos.y())
            self.size_changed.emit(clamped_w)

    def set_scale(self, scale: str) -> None:
        if scale not in SCALE_WIDTHS:
            return
        self._config.mascot_scale = scale
        self.set_mascot_width(SCALE_WIDTHS[scale], emit_signal=False)
        self._pos_save_timer.stop()
        self.position_changed.emit(self.x(), self.y())

    def set_animation_mode(self, mode: str) -> None:
        """Switch animation fidelity between 'rich', 'subtle', and 'off'."""
        safe_mode = mode if mode in ANIMATION_MODES else "rich"
        self._animation_mode = safe_mode
        self._config.animation_mode = safe_mode
        self._last_tick_ts = time.monotonic()

        if safe_mode == "off":
            self._anim_timer.stop()
            self._quirk_timer.stop()
            self._snap_animations_neutral()
        elif safe_mode == "subtle":
            self._anim_timer.setInterval(55)
            self._anim_timer.start()
            self._quirk_timer.stop()
            self._quirk_tilt = 0.0
            self._quirk_tilt_target = 0.0
            self._quirk_tilt_remaining = 0.0
            self._quirk_hop_y = 0.0
            self._quirk_hop_vel = 0.0
            self._hover_lean_angle = 0.0
        else:
            self._anim_timer.setInterval(33)
            self._anim_timer.start()
            self._schedule_next_quirk()

        self._sync_movie_playback()
        self.update()

    def _snap_animations_neutral(self) -> None:
        self._bounce_y = 0.0
        self._bounce_vel = 0.0
        self._squash_x = 1.0
        self._squash_y = 1.0
        self._squash_vx = 0.0
        self._squash_vy = 0.0
        self._tilt_angle = 0.0
        self._tilt_vel = 0.0
        self._target_tilt = 0.0
        self._hover_lean_angle = 0.0
        self._hover_zoom = 1.0
        self._entrance_scale = 1.0
        self._entrance_vel = 0.0
        self._entrance_opacity = 1.0
        self._quirk_tilt = 0.0
        self._quirk_tilt_target = 0.0
        self._quirk_tilt_remaining = 0.0
        self._quirk_hop_y = 0.0
        self._quirk_hop_vel = 0.0
        self._particles.clear()
        self._ripples.clear()

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
        self._ripple_cooldown = 0.0
        if self._activity_kind == "idle":
            self._ripples.clear()
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
        self._temp_sprite_state = "happy"
        self._temp_state_timer.start(1600)
        self._rebuild_geometry_and_pixmap()

        if self._animation_mode == "off":
            return

        # Elastic jump with anticipation/landing squash-stretch + particle burst
        self._bounce_vel = -185.0
        self._squash_x = 1.09
        self._squash_y = 0.90
        self._squash_vx = -0.8
        self._squash_vy = 1.1
        count = random.randint(7, 10) if self._animation_mode == "rich" else 3
        self._spawn_burst_particles(count, include_hearts=True)

    def _clear_temp_state(self) -> None:
        if self._temp_sprite_state is not None:
            self._temp_sprite_state = None
            self._rebuild_geometry_and_pixmap()

    def _clear_size_pill(self) -> None:
        if self._size_pill_text:
            self._size_pill_text = ""
            self.update()

    def _emit_size_changed(self) -> None:
        self.size_changed.emit(self._config.mascot_width)

    def _pick_line(self, lines: list[str], last_line: str, fallback: str) -> str:
        pool = [ln.strip() for ln in (lines or []) if ln and ln.strip()]
        if not pool:
            return fallback
        if len(pool) > 1 and last_line in pool:
            pool = [ln for ln in pool if ln != last_line]
        return random.choice(pool)

    # Particle & Quirk Helpers
    def _schedule_next_quirk(self) -> None:
        if self._animation_mode == "rich":
            self._quirk_timer.start(random.randint(7000, 13000))

    def _trigger_idle_quirk(self) -> None:
        if self._animation_mode != "rich":
            return
        if self._activity_kind == "idle" and not self._dragging and not self._pressing:
            choice = random.choice(("hop", "tilt", "sparkle"))
            if choice == "hop":
                self._quirk_hop_vel = -105.0
                self._squash_x = 0.96
                self._squash_y = 1.06
            elif choice == "tilt":
                self._quirk_tilt_target = random.choice((-5.5, 5.5))
                self._quirk_tilt_remaining = 1.3
            else:
                self._spawn_burst_particles(5, include_hearts=False)
        self._schedule_next_quirk()

    def _spawn_burst_particles(self, count: int, *, include_hearts: bool = True) -> None:
        if self._animation_mode == "off" or count <= 0:
            return
        accent, secondary = self._sister_colors()
        cx = self.width() * 0.5
        cy = _PAD_TOP + self._base_h * 0.42
        spread_x = max(24.0, self._base_w * 0.34)
        spread_y = max(20.0, self._base_h * 0.25)

        for idx in range(count):
            angle = -math.pi * 0.5 + random.uniform(-1.15, 1.15)
            speed = random.uniform(36.0, 92.0)
            color = QColor(accent if idx % 2 == 0 else secondary)
            if idx % 3 == 2:
                color = color.lighter(130)
            kind = "heart" if (include_hearts and idx % 3 == 0) else "star"
            self._particles.append(
                _Particle(
                    x=cx + random.uniform(-spread_x, spread_x),
                    y=cy + random.uniform(-spread_y, spread_y * 0.6),
                    vx=math.cos(angle) * speed,
                    vy=math.sin(angle) * speed - random.uniform(10.0, 32.0),
                    life=0.0,
                    max_life=random.uniform(0.75, 1.25),
                    size=random.uniform(5.0, 8.8),
                    kind=kind,
                    color=color,
                    rot=random.uniform(-25.0, 25.0),
                    vrot=random.uniform(-90.0, 90.0),
                )
            )
        if len(self._particles) > 28:
            self._particles = self._particles[-28:]

    def _spawn_activity_ripple(self) -> None:
        accent, _ = self._sister_colors()
        if self._activity_kind == "speaking":
            color = QColor("#a78bfa")
            speed = 46.0
            max_r = max(self._base_w, self._base_h) * 0.62
            start_r = max(22.0, self._base_w * 0.24)
        elif self._activity_kind == "listening":
            color = QColor("#fb7185")
            speed = 54.0
            max_r = max(self._base_w, self._base_h) * 0.66
            start_r = max(20.0, self._base_w * 0.22)
        else:
            color = QColor(accent)
            speed = 42.0
            max_r = max(self._base_w, self._base_h) * 0.58
            start_r = max(20.0, self._base_w * 0.22)

        self._ripples.append(
            _Ripple(
                radius=start_r,
                max_radius=max_r,
                alpha=0.68,
                speed=speed,
                color=color,
                width=2.0,
            )
        )
        if len(self._ripples) > 6:
            self._ripples = self._ripples[-6:]

    # Animation & Paint
    def _on_anim_tick(self) -> None:
        if self._animation_mode == "off":
            return

        now = time.monotonic()
        nominal_dt = max(0.016, self._anim_timer.interval() / 1000.0)
        raw_dt = now - self._last_tick_ts
        self._last_tick_ts = now
        # Fallback to nominal_dt when called programmatically in tight loops
        dt = nominal_dt if raw_dt < 0.005 else min(0.08, raw_dt)

        self._anim_phase = (self._anim_phase + dt * 2.8) % (math.tau * 100.0)

        # Bounce spring physics
        if abs(self._bounce_y) > 0.05 or abs(self._bounce_vel) > 0.5:
            prev_y = self._bounce_y
            self._bounce_vel += (-self._bounce_y * 165.0 - self._bounce_vel * 11.5) * dt
            self._bounce_y += self._bounce_vel * dt
            # Trigger subtle landing squish when passing bottom
            if prev_y < -2.0 and self._bounce_y >= 0.0 and self._bounce_vel > 30.0:
                self._squash_x = max(self._squash_x, 1.055)
                self._squash_y = min(self._squash_y, 0.945)
        else:
            self._bounce_y = 0.0
            self._bounce_vel = 0.0

        # Quirk hop spring physics
        if abs(self._quirk_hop_y) > 0.05 or abs(self._quirk_hop_vel) > 0.5:
            self._quirk_hop_vel += (-self._quirk_hop_y * 190.0 - self._quirk_hop_vel * 13.0) * dt
            self._quirk_hop_y += self._quirk_hop_vel * dt
        else:
            self._quirk_hop_y = 0.0
            self._quirk_hop_vel = 0.0

        # Squash & stretch spring recovery toward (1.0, 1.0)
        if (
            abs(self._squash_x - 1.0) > 0.002
            or abs(self._squash_y - 1.0) > 0.002
            or abs(self._squash_vx) > 0.01
            or abs(self._squash_vy) > 0.01
        ):
            self._squash_vx += ((1.0 - self._squash_x) * 140.0 - self._squash_vx * 12.0) * dt
            self._squash_vy += ((1.0 - self._squash_y) * 140.0 - self._squash_vy * 12.0) * dt
            self._squash_x = max(0.80, min(1.22, self._squash_x + self._squash_vx * dt))
            self._squash_y = max(0.80, min(1.22, self._squash_y + self._squash_vy * dt))
        else:
            self._squash_x = 1.0
            self._squash_y = 1.0
            self._squash_vx = 0.0
            self._squash_vy = 0.0

        # Entrance pop spring (0.84 -> ~1.05 -> 1.00) + opacity fade-in
        if abs(self._entrance_scale - 1.0) > 0.002 or abs(self._entrance_vel) > 0.01:
            self._entrance_vel += ((1.0 - self._entrance_scale) * 170.0 - self._entrance_vel * 10.5) * dt
            self._entrance_scale = max(0.75, min(1.12, self._entrance_scale + self._entrance_vel * dt))
        else:
            self._entrance_scale = 1.0
            self._entrance_vel = 0.0
        if self._entrance_opacity < 1.0:
            self._entrance_opacity = min(1.0, self._entrance_opacity + dt * 4.4)

        # Pendulum drag swing spring
        if not self._dragging:
            self._target_tilt = 0.0
        self._tilt_vel += ((self._target_tilt - self._tilt_angle) * 130.0 - self._tilt_vel * 10.5) * dt
        self._tilt_angle += self._tilt_vel * dt
        if not self._dragging and abs(self._tilt_angle) < 0.04 and abs(self._tilt_vel) < 0.15:
            self._tilt_angle = 0.0
            self._tilt_vel = 0.0

        # Hover cursor lean & zoom
        target_lean = 0.0
        target_zoom = 1.0
        if self._file_drop_hover:
            target_zoom = 1.045
        elif self._hovered and not self._dragging:
            if self._animation_mode == "rich":
                local_x = self.mapFromGlobal(QCursor.pos()).x()
                half_w = max(1.0, self._base_w * 0.5)
                rel = max(-1.0, min(1.0, (local_x - self.width() * 0.5) / half_w))
                target_lean = rel * 3.5
                target_zoom = 1.03
            else:
                target_zoom = 1.015

        lerp = min(1.0, dt * 10.0)
        self._hover_lean_angle += (target_lean - self._hover_lean_angle) * lerp
        self._hover_zoom += (target_zoom - self._hover_zoom) * lerp

        # Autonomous quirk head tilt decay
        if self._quirk_tilt_remaining > 0.0:
            self._quirk_tilt_remaining = max(0.0, self._quirk_tilt_remaining - dt)
            if self._quirk_tilt_remaining <= 0.0:
                self._quirk_tilt_target = 0.0
        self._quirk_tilt += (self._quirk_tilt_target - self._quirk_tilt) * min(1.0, dt * 8.0)

        # Ambient starlight motes drift
        mote_count = 5 if self._animation_mode == "rich" else 2
        for mote in self._ambient_motes[:mote_count]:
            mote.y_norm -= mote.speed * dt
            if mote.y_norm < -0.08:
                mote.y_norm = 1.06

        # Update burst particles
        if self._particles:
            alive: list[_Particle] = []
            for p in self._particles:
                p.life += dt
                if p.life < p.max_life:
                    p.x += p.vx * dt
                    p.y += p.vy * dt
                    p.vx *= max(0.0, 1.0 - 1.6 * dt)
                    p.vy -= 18.0 * dt  # gentle buoyant drift upward
                    p.rot += p.vrot * dt
                    alive.append(p)
            self._particles = alive

        # Activity ripples (speaking / listening)
        if self._animation_mode == "rich" and self._activity_kind in ("speaking", "listening"):
            self._ripple_cooldown -= dt
            if self._ripple_cooldown <= 0.0:
                self._spawn_activity_ripple()
                self._ripple_cooldown = 0.58 if self._activity_kind == "speaking" else 0.72
        if self._ripples:
            alive_ripples: list[_Ripple] = []
            for r in self._ripples:
                r.radius += r.speed * dt
                progress = r.radius / max(1.0, r.max_radius)
                r.alpha = max(0.0, 0.70 * (1.0 - progress))
                if r.radius < r.max_radius:
                    alive_ripples.append(r)
            self._ripples = alive_ripples

        self.update()

    def paintEvent(self, event) -> None:  # noqa: ANN001
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)

        accent, secondary = self._sister_colors()
        is_off = self._animation_mode == "off"
        is_rich = self._animation_mode == "rich"

        # Breathing, float, and activity secondary motion
        if is_off:
            breath_sx = 1.0
            breath_sy = 1.0
            float_y = 0.0
            idle_sway = 0.0
            act_sway = 0.0
            act_bounce = 0.0
            act_scale = 1.0
        else:
            breath = math.sin(self._anim_phase)
            breath_sx = 1.0 - 0.007 * breath
            breath_sy = 1.0 + 0.015 * breath
            float_amp = 5.0 if (is_rich and self._activity_kind == "thinking") else 2.5
            float_y = math.sin(self._anim_phase * 0.9) * float_amp
            sway_factor = 1.0 if is_rich else 0.45
            idle_sway = sway_factor * (
                1.05 * math.sin(self._anim_phase * 0.65)
                + 0.45 * math.sin(self._anim_phase * 1.35 + 0.7)
            )
            act_sway = 0.0
            act_bounce = 0.0
            act_scale = 1.0
            if self._activity_kind == "speaking" and is_rich:
                act_sway = 2.2 * math.sin(self._anim_phase * 2.8)
                act_bounce = -abs(math.sin(self._anim_phase * 3.2)) * 3.5
            elif self._activity_kind == "listening":
                act_scale = 1.035

        drag_sx = 0.97 if (self._dragging and not is_off) else 1.0
        drag_sy = 1.04 if (self._dragging and not is_off) else 1.0

        total_sx = breath_sx * self._squash_x * self._hover_zoom * self._entrance_scale * drag_sx * act_scale
        total_sy = breath_sy * self._squash_y * self._hover_zoom * self._entrance_scale * drag_sy * act_scale
        total_tilt = (
            self._tilt_angle
            + self._hover_lean_angle
            + self._quirk_tilt
            + (idle_sway if self._activity_kind == "idle" else 0.0)
            + act_sway
        )

        pivot_x = self.width() * 0.5
        pivot_y = float(_PAD_TOP + self._base_h) + float_y + self._bounce_y + self._quirk_hop_y + act_bounce
        aura_cy = pivot_y - self._base_h * 0.5

        # Glowing aura behind sprite when active, hovered, or accepting file drop
        if self._activity_kind != "idle" or self._hovered or self._file_drop_hover:
            pulse = 1.0 if is_off else (0.75 + 0.25 * math.sin(self._anim_phase * 1.8))
            glow_color = QColor(accent)
            if self._file_drop_hover:
                glow_color = QColor("#34d399")
            elif self._activity_kind == "tool":
                glow_color = QColor("#38bdf8")
            elif self._activity_kind == "listening":
                glow_color = QColor("#fb7185")
            elif self._activity_kind == "speaking":
                glow_color = QColor("#a78bfa")

            radius = max(self._base_w, self._base_h) * 0.56
            aura = QRadialGradient(QPointF(pivot_x, aura_cy), radius)
            c0 = QColor(glow_color)
            c0.setAlpha(int((96 if (self._activity_kind != "idle" or self._file_drop_hover) else 54) * pulse))
            c1 = QColor(secondary)
            c1.setAlpha(int(36 * pulse))
            aura.setColorAt(0.0, c0)
            aura.setColorAt(0.56, c1)
            aura.setColorAt(1.0, QColor(0, 0, 0, 0))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(aura)
            painter.drawEllipse(QPointF(pivot_x, aura_cy), radius, radius)

        if self._file_drop_hover:
            ring_rect = QRectF(
                8.0,
                float(_PAD_TOP) - 8.0,
                max(40.0, float(self.width()) - 16.0),
                max(40.0, float(self._base_h) + 16.0),
            )
            painter.save()
            drop_pen = QPen(QColor(52, 211, 153, 210), 2.0, Qt.PenStyle.DashLine)
            drop_pen.setDashPattern([8.0, 5.0])
            painter.setPen(drop_pen)
            painter.setBrush(QColor(16, 185, 129, 20))
            painter.drawRoundedRect(ring_rect, 18.0, 18.0)
            painter.restore()

        # Activity rings behind sprite (soundwave / listening ripples & Playwright cyan halos)
        if is_rich:
            for r in self._ripples:
                rc = QColor(r.color)
                rc.setAlpha(int(255 * r.alpha))
                painter.setPen(QPen(rc, r.width))
                painter.setBrush(Qt.BrushStyle.NoBrush)
                painter.drawEllipse(QPointF(pivot_x, aura_cy), r.radius, r.radius)

            if self._activity_kind == "tool":
                self._paint_tool_halos(painter, pivot_x, aura_cy)

        # Draw sprite anchored at bottom-center pivot so her feet stay grounded
        painter.save()
        if self._entrance_opacity < 0.999:
            painter.setOpacity(self._entrance_opacity)
        painter.translate(pivot_x, pivot_y)
        if abs(total_tilt) > 0.02:
            painter.rotate(total_tilt)
        if abs(total_sx - 1.0) > 0.001 or abs(total_sy - 1.0) > 0.001:
            painter.scale(total_sx, total_sy)

        target_rect = QRectF(
            -self._sprite_w * 0.5 + self._sprite_offset_x,
            -float(self._sprite_h) + self._sprite_offset_y,
            float(self._sprite_w),
            float(self._sprite_h),
        )
        if self._movie is not None:
            frame_img = self._movie.currentImage()
            if not frame_img.isNull():
                painter.drawImage(target_rect, frame_img)
        elif not self._pixmap.isNull():
            painter.drawPixmap(target_rect, self._pixmap, QRectF(self._pixmap.rect()))
        painter.restore()

        # Ambient starlight motes & orbiting thinking motes
        if not is_off:
            self._paint_ambient_motes(painter, accent, secondary)
            if is_rich and self._activity_kind == "thinking":
                self._paint_thinking_orbit_motes(painter, pivot_x, aura_cy, accent, secondary)
            if is_rich and self._activity_kind == "tool":
                self._paint_tool_motes(painter, pivot_x, aura_cy)

        # Burst particles (stars & hearts)
        if self._particles:
            for p in self._particles:
                t = p.life / max(0.01, p.max_life)
                alpha = int(max(0.0, min(1.0, (1.0 - t) * 1.15)) * 235)
                col = QColor(p.color)
                col.setAlpha(alpha)
                sz = p.size * (0.75 + 0.35 * math.sin(t * math.pi))
                if p.kind == "heart":
                    _draw_heart(painter, QPointF(p.x, p.y), sz, col, p.rot)
                else:
                    _draw_four_point_star(painter, QPointF(p.x, p.y), sz, col, p.rot)

        # Top pill overlay (drop indicator > size badge > status pill)
        if self._file_drop_hover:
            top_pill_text = (
                f"📎 {self._file_drop_count}件をドロップして添付！"
                if self._file_drop_count > 1
                else "📎 ドロップして添付！"
            )
            pill_accent = QColor("#34d399")
        else:
            top_pill_text = self._size_pill_text or (
                self._status_text if self._activity_kind != "idle" else ""
            )
            pill_accent = QColor("#38bdf8") if self._size_pill_text else accent
        if top_pill_text:
            self._paint_top_pill(painter, top_pill_text, pill_accent)

    def _top_pill_rect(self, text: str) -> QRectF:
        font = QFont("Noto Sans JP", 9, QFont.Weight.Bold)
        text_w = QFontMetrics(font).horizontalAdvance(text) if text else 0
        pill_w = min(float(self.width() - 8), float(text_w + 26))
        return QRectF((self.width() - pill_w) * 0.5, 4.0, pill_w, 24.0)

    def _paint_top_pill(self, painter: QPainter, text: str, accent: QColor) -> None:
        font = QFont("Noto Sans JP", 9, QFont.Weight.Bold)
        painter.setFont(font)
        fm = painter.fontMetrics()
        pill_rect = self._top_pill_rect(text)
        pill_w = pill_rect.width()

        bg_grad = QLinearGradient(pill_rect.topLeft(), pill_rect.bottomRight())
        bg_grad.setColorAt(0.0, QColor(15, 20, 38, 234))
        bg_grad.setColorAt(1.0, QColor(28, 34, 62, 234))
        painter.setBrush(bg_grad)
        painter.setPen(QPen(accent.lighter(125), 1.2))
        painter.drawRoundedRect(pill_rect, 12.0, 12.0)

        elided = fm.elidedText(text, Qt.TextElideMode.ElideRight, int(pill_w - 14))
        painter.setPen(QColor(248, 250, 252))
        painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, elided)

    def _paint_ambient_motes(self, painter: QPainter, accent: QColor, secondary: QColor) -> None:
        count = 5 if self._animation_mode == "rich" else 2
        base_x = (self.width() - self._base_w) * 0.5
        base_y = float(_PAD_TOP)
        for idx, mote in enumerate(self._ambient_motes[:count]):
            twinkle = 0.25 + 0.75 * (0.5 + 0.5 * math.sin(self._anim_phase * mote.twinkle_speed + mote.phase))
            fade_edge = math.sin(max(0.0, min(1.0, mote.y_norm)) * math.pi)
            alpha = int(165 * twinkle * fade_edge)
            if alpha <= 4:
                continue
            col = QColor(secondary if idx % 2 == 0 else accent).lighter(135)
            col.setAlpha(alpha)
            mx = base_x + mote.x_norm * self._base_w + math.sin(self._anim_phase + mote.phase) * 5.0
            my = base_y + mote.y_norm * self._base_h
            _draw_four_point_star(painter, QPointF(mx, my), mote.size, col, self._anim_phase * 12.0)

    def _paint_thinking_orbit_motes(
        self,
        painter: QPainter,
        cx: float,
        cy: float,
        accent: QColor,
        secondary: QColor,
    ) -> None:
        rx = self._base_w * 0.48
        ry = self._base_h * 0.34
        for idx in range(4):
            theta = self._anim_phase * 1.1 + idx * (math.tau / 4.0)
            mx = cx + math.cos(theta) * rx
            my = cy + math.sin(theta) * ry - self._base_h * 0.08
            twinkle = 0.55 + 0.45 * math.sin(self._anim_phase * 2.4 + idx * 1.4)
            col = QColor(secondary if idx % 2 else accent).lighter(140)
            col.setAlpha(int(220 * twinkle))
            _draw_four_point_star(painter, QPointF(mx, my), 4.5 + 1.8 * twinkle, col, theta * 18.0)

    def _paint_tool_halos(self, painter: QPainter, cx: float, cy: float) -> None:
        r_outer = max(self._base_w, self._base_h) * 0.48
        r_inner = r_outer * 0.84
        deg1 = (self._anim_phase * 38.0) % 360.0
        deg2 = (-self._anim_phase * 52.0) % 360.0

        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)

        pen1 = QPen(QColor(56, 189, 248, 175), 2.0, Qt.PenStyle.DashLine)
        pen1.setDashPattern([10.0, 7.0])
        painter.setPen(pen1)
        painter.translate(cx, cy)
        painter.rotate(deg1)
        painter.drawEllipse(QRectF(-r_outer, -r_outer, r_outer * 2, r_outer * 2))
        painter.restore()

        painter.save()
        painter.setBrush(Qt.BrushStyle.NoBrush)
        pen2 = QPen(QColor(34, 211, 238, 150), 1.8, Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen2)
        painter.translate(cx, cy)
        painter.rotate(deg2)
        arc_rect = QRectF(-r_inner, -r_inner, r_inner * 2, r_inner * 2)
        painter.drawArc(arc_rect, 15 * 16, 110 * 16)
        painter.drawArc(arc_rect, 195 * 16, 110 * 16)
        painter.restore()

    def _paint_tool_motes(self, painter: QPainter, cx: float, cy: float) -> None:
        radius = max(self._base_w, self._base_h) * 0.48
        for idx in range(3):
            theta = self._anim_phase * 1.7 + idx * (math.tau / 3.0)
            mx = cx + math.cos(theta) * radius
            my = cy + math.sin(theta) * radius
            col = QColor(103, 232, 249, 220)
            _draw_four_point_star(painter, QPointF(mx, my), 5.0, col, theta * 25.0)

    def _sprite_draw_origin(self) -> tuple[float, float]:
        ox = self.width() * 0.5 - self._sprite_w * 0.5 + self._sprite_offset_x
        oy = float(_PAD_TOP + self._base_h) - float(self._sprite_h) + self._sprite_offset_y
        return ox, oy

    def _is_point_on_mascot(self, pos: QPointF) -> bool:
        """Hit-test against the visible mascot silhouette (plus generous margin) and controls."""
        if self._minibar.isVisible() and self._minibar.geometry().contains(pos.toPoint()):
            return True

        top_pill_text = self._size_pill_text or (
            self._status_text if self._activity_kind != "idle" else ""
        )
        if top_pill_text and self._top_pill_rect(top_pill_text).adjusted(-4, -4, 4, 4).contains(pos):
            return True

        ox, oy = self._sprite_draw_origin()
        sx = pos.x() - ox
        sy = pos.y() - oy
        margin = 14.0
        content = self._sprite_content_rect
        if content.isNull() or content.isEmpty():
            content = QRectF(0.0, 0.0, float(self._sprite_w), float(self._sprite_h))

        if not content.adjusted(-margin, -margin, margin, margin).contains(sx, sy):
            return False

        if self._movie is not None or self._hit_image is None or self._hit_image.isNull():
            return True

        # Treat narrow central torso/leg-gap core as solid so clicks between ankles/arms still register.
        core_half_w = min(content.width() * 0.24, self._base_w * 0.16)
        core_cx = content.center().x()
        if (
            abs(sx - core_cx) <= core_half_w
            and (content.top() + content.height() * 0.10) <= sy <= (content.bottom() - content.height() * 0.06)
        ):
            return True

        hw = self._hit_image.width()
        hh = self._hit_image.height()
        cx = max(0, min(hw - 1, int(round(sx))))
        cy = max(0, min(hh - 1, int(round(sy))))
        if self._hit_image.pixelColor(cx, cy).alpha() > 20:
            return True

        ix0 = max(0, int(math.floor(sx - margin)))
        ix1 = min(hw - 1, int(math.ceil(sx + margin)))
        iy0 = max(0, int(math.floor(sy - margin)))
        iy1 = min(hh - 1, int(math.ceil(sy + margin)))
        r2 = margin * margin
        for py in range(iy0, iy1 + 1, 3):
            dy = py - sy
            for px in range(ix0, ix1 + 1, 3):
                dx = px - sx
                if dx * dx + dy * dy <= r2 and self._hit_image.pixelColor(px, py).alpha() > 20:
                    return True
        return False

    # Drag-and-drop file/folder attachment handling
    def dragEnterEvent(self, event: QDragEnterEvent) -> None:
        paths = extract_local_paths_from_mime(event.mimeData())
        if paths:
            self._file_drop_hover = True
            self._file_drop_count = len(paths)
            event.acceptProposedAction()
            self.update()
            return
        super().dragEnterEvent(event)

    def dragMoveEvent(self, event: QDragMoveEvent) -> None:
        if self._file_drop_hover or extract_local_paths_from_mime(event.mimeData()):
            event.acceptProposedAction()
            return
        super().dragMoveEvent(event)

    def dragLeaveEvent(self, event: QDragLeaveEvent) -> None:
        if self._file_drop_hover:
            self._file_drop_hover = False
            self._file_drop_count = 0
            self.update()
        super().dragLeaveEvent(event)

    def dropEvent(self, event: QDropEvent) -> None:
        paths = extract_local_paths_from_mime(event.mimeData())
        self._file_drop_hover = False
        self._file_drop_count = 0
        self.update()
        if paths:
            event.acceptProposedAction()
            self.trigger_bounce()
            self.files_dropped.emit(paths)
            return
        super().dropEvent(event)

    # Hover & Wheel Resize handling
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
            self.setCursor(Qt.CursorShape.ArrowCursor)
            self.update()

    def wheelEvent(self, event) -> None:  # noqa: ANN001
        delta_y = event.angleDelta().y()
        if delta_y == 0:
            delta_y = event.pixelDelta().y()
        if delta_y == 0:
            super().wheelEvent(event)
            return

        step = 14 if delta_y > 0 else -14
        new_w = _clamp_mascot_width(self._config.mascot_width + step)
        self.set_mascot_width(new_w, emit_signal=False)
        self._size_pill_text = f"📏 {new_w}px"
        self._size_pill_timer.start(900)
        self._size_save_timer.start(300)
        self.update()
        event.accept()

    # Mouse & Drag (XCB + Wayland fallback)
    def mousePressEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton:
            if not self._is_point_on_mascot(event.position()):
                event.ignore()
                return
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
            if self._is_point_on_mascot(event.position()):
                self.setCursor(Qt.CursorShape.OpenHandCursor)
            else:
                self.setCursor(Qt.CursorShape.ArrowCursor)
            super().mouseMoveEvent(event)
            return

        global_pos = event.globalPosition().toPoint()
        delta = global_pos - self._press_global_pos
        if not self._dragging and delta.manhattanLength() > 6:
            self._dragging = True
            self.setCursor(Qt.CursorShape.ClosedHandCursor)
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
                    self.setCursor(Qt.CursorShape.ArrowCursor)
                    self._temp_state_timer.start(900)
                    handle.startSystemMove()
                    event.accept()
                    return

        if self._dragging:
            dx = global_pos.x() - self._last_drag_x
            self._last_drag_x = global_pos.x()
            self._target_tilt = max(-14.0, min(14.0, dx * 1.15))

            target_pos = self._clamp_pos_to_screen(self._win_start_pos + delta)
            self.move(target_pos)
            self._toast.reposition_near(self.frameGeometry())
            event.accept()
            return

        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: ANN001
        if event.button() == Qt.MouseButton.LeftButton and self._pressing:
            was_dragging = self._dragging
            self._pressing = False
            self._dragging = False
            self._target_tilt = 0.0
            self.setCursor(
                Qt.CursorShape.OpenHandCursor
                if self._is_point_on_mascot(event.position())
                else Qt.CursorShape.ArrowCursor
            )

            if was_dragging:
                self._pos_save_timer.stop()
                self._temp_state_timer.start(350)
                if self._animation_mode != "off":
                    self._squash_x = 1.07
                    self._squash_y = 0.93
                clamped = self._clamp_pos_to_screen(self.pos())
                if clamped != self.pos():
                    self.move(clamped)
                self._toast.reposition_near(self.frameGeometry())
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
        self._pos_save_timer.start()

    def _emit_position_changed(self) -> None:
        self.position_changed.emit(self.x(), self.y())

    # Context menu
    def contextMenuEvent(self, event) -> None:  # noqa: ANN001
        if not self._is_point_on_mascot(QPointF(event.pos())):
            event.ignore()
            return
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

        size_menu = menu.addMenu("📏 サイズ変更")
        size_menu.setStyleSheet(_MENU_QSS)
        current_w = _clamp_mascot_width(self._config.mascot_width)
        for label, preset_w in _SIZE_PRESETS:
            act = QAction(label, size_menu)
            act.setCheckable(True)
            act.setChecked(current_w == preset_w)
            act.triggered.connect(
                lambda _checked=False, w=preset_w: self.set_mascot_width(w, emit_signal=True)
            )
            size_menu.addAction(act)
        size_menu.addSeparator()
        grow_act = QAction("拡大 (+25px)", size_menu)
        grow_act.setEnabled(current_w < MASCOT_WIDTH_MAX)
        grow_act.triggered.connect(
            lambda _checked=False, w=_clamp_mascot_width(current_w + 25): self.set_mascot_width(
                w, emit_signal=True
            )
        )
        size_menu.addAction(grow_act)

        shrink_act = QAction("縮小 (-25px)", size_menu)
        shrink_act.setEnabled(current_w > MASCOT_WIDTH_MIN)
        shrink_act.triggered.connect(
            lambda _checked=False, w=_clamp_mascot_width(current_w - 25): self.set_mascot_width(
                w, emit_signal=True
            )
        )
        size_menu.addAction(shrink_act)

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
        self._quirk_timer.stop()
        self._temp_state_timer.stop()
        self._hover_leave_timer.stop()
        self._pos_save_timer.stop()
        self._size_pill_timer.stop()
        self._size_save_timer.stop()
        self._stop_movie()
        self._toast.close()
        super().closeEvent(event)
