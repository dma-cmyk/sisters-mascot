"""PyQt6 desktop mascot UI package."""

from sisters_mascot.ui.bubble_panel import BubblePanel
from sisters_mascot.ui.mascot_window import MascotWindow
from sisters_mascot.ui.tray import SistersTrayIcon
from sisters_mascot.ui.widgets import LauncherItemDialog, ToastBubble, markdown_to_bubble_html

__all__ = [
    "BubblePanel",
    "LauncherItemDialog",
    "MascotWindow",
    "SistersTrayIcon",
    "ToastBubble",
    "markdown_to_bubble_html",
]
