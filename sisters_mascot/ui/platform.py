"""Platform capability helpers shared by the mascot and panel windows."""

from __future__ import annotations

from PyQt6.QtGui import QGuiApplication


def supports_window_positioning() -> bool:
    """Return True when the platform allows client-side window positioning.

    On native Wayland the compositor owns window placement: ``QWidget.move()``
    requests are ignored and ``pos()`` does not report real screen coordinates,
    so window positions must not be persisted or restored there. X11 (xcb),
    Windows, macOS and the offscreen test platform all support positioning.
    """
    return not QGuiApplication.platformName().startswith("wayland")
