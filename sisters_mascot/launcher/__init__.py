"""Launcher utilities for desktop applications, folders, and bookmarks."""

from sisters_mascot.launcher.actions import launch_app, open_bookmark, open_folder
from sisters_mascot.launcher.desktop_apps import DesktopAppEntry, discover_desktop_apps

__all__ = [
    "DesktopAppEntry",
    "discover_desktop_apps",
    "launch_app",
    "open_folder",
    "open_bookmark",
]
