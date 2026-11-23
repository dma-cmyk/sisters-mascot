"""System tray icon and menu for Sisters Mascot."""

from __future__ import annotations

from PyQt6.QtCore import QPointF, Qt, pyqtSignal
from PyQt6.QtGui import QAction, QColor, QIcon, QLinearGradient, QPainter, QPainterPath, QPixmap
from PyQt6.QtWidgets import QMenu, QSystemTrayIcon, QWidget

from sisters_mascot import APP_NAME, ASSETS_DIR


def _build_fallback_tray_icon() -> QIcon:
    """Render a crisp 64x64 indigo crystal emblem when app-icon.png is absent."""
    size = 64
    pm = QPixmap(size, size)
    pm.fill(Qt.GlobalColor.transparent)

    painter = QPainter(pm)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    grad = QLinearGradient(QPointF(8, 8), QPointF(56, 56))
    grad.setColorAt(0.0, QColor("#6366f1"))
    grad.setColorAt(1.0, QColor("#38bdf8"))

    diamond = QPainterPath()
    diamond.moveTo(32, 4)
    diamond.lineTo(58, 32)
    diamond.lineTo(32, 60)
    diamond.lineTo(6, 32)
    diamond.closeSubpath()

    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(grad)
    painter.drawPath(diamond)

    inner = QPainterPath()
    inner.moveTo(32, 14)
    inner.lineTo(48, 32)
    inner.lineTo(32, 50)
    inner.lineTo(16, 32)
    inner.closeSubpath()
    painter.setBrush(QColor(255, 255, 255, 90))
    painter.drawPath(inner)

    painter.end()
    return QIcon(pm)


class SistersTrayIcon(QSystemTrayIcon):
    """System tray icon providing quick access to panel, corner dock, audio, and quit."""

    toggle_panel_requested = pyqtSignal()
    reset_corner_requested = pyqtSignal()
    mute_toggle_requested = pyqtSignal()
    quit_requested = pyqtSignal()

    def __init__(self, muted: bool = False, parent: QWidget | None = None) -> None:
        icon_path = ASSETS_DIR / "app-icon.png"
        icon = QIcon(str(icon_path)) if icon_path.exists() else _build_fallback_tray_icon()
        if icon.isNull():
            icon = _build_fallback_tray_icon()

        super().__init__(icon, parent)
        self.setToolTip(APP_NAME)

        self._menu = QMenu()
        self._toggle_act = QAction("💬 チャット＆ランチャーを表示/隠す", self._menu)
        self._toggle_act.triggered.connect(self.toggle_panel_requested.emit)
        self._menu.addAction(self._toggle_act)

        self._reset_act = QAction("📍 画面右下に戻す", self._menu)
        self._reset_act.triggered.connect(self.reset_corner_requested.emit)
        self._menu.addAction(self._reset_act)

        self._mute_act = QAction("🔊 音声読み上げ (ON/OFF)", self._menu)
        self._mute_act.setCheckable(True)
        self._mute_act.setChecked(not muted)
        self._mute_act.triggered.connect(self.mute_toggle_requested.emit)
        self._menu.addAction(self._mute_act)

        self._menu.addSeparator()

        self._quit_act = QAction("✖ 終了", self._menu)
        self._quit_act.triggered.connect(self.quit_requested.emit)
        self._menu.addAction(self._quit_act)

        self.setContextMenu(self._menu)
        self.activated.connect(self._on_activated)

    def set_muted(self, muted: bool) -> None:
        self._mute_act.blockSignals(True)
        self._mute_act.setChecked(not muted)
        self._mute_act.blockSignals(False)

    def update_tooltip(self, sister_label: str, agent_id: str) -> None:
        self.setToolTip(f"{APP_NAME} — {sister_label} ({agent_id})")

    def _on_activated(self, reason: QSystemTrayIcon.ActivationReason) -> None:
        if reason in (
            QSystemTrayIcon.ActivationReason.Trigger,
            QSystemTrayIcon.ActivationReason.DoubleClick,
        ):
            self.toggle_panel_requested.emit()
