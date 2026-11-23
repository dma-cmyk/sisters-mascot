"""Detached launcher actions for desktop apps, folders, and bookmarks."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import webbrowser
from pathlib import Path


def _spawn_detached(argv: list[str], cwd: str | None = None) -> tuple[bool, str]:
    popen_kwargs: dict = {
        "cwd": cwd,
        "stdin": subprocess.DEVNULL,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if os.name == "posix":
        popen_kwargs["start_new_session"] = True
        popen_kwargs["close_fds"] = True
    else:
        popen_kwargs["creationflags"] = (
            getattr(subprocess, "DETACHED_PROCESS", 0)
            | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
        )
    try:
        subprocess.Popen(argv, **popen_kwargs)
        return True, f"起動しました: {argv[0]}"
    except OSError as exc:
        return False, f"起動に失敗しました ({argv[0]}): {exc}"


def launch_app(command: str) -> tuple[bool, str]:
    """Launch an application command detached from the mascot process."""
    text = command.strip()
    if not text:
        return False, "コマンドが空です。"

    try:
        argv = shlex.split(text, posix=(os.name == "posix"))
    except ValueError as exc:
        return False, f"コマンドの解析に失敗しました: {exc}"

    if not argv:
        return False, "コマンドが空です。"

    exe = argv[0].strip('"\'')
    argv[0] = exe
    if os.path.sep in exe or "/" in exe:
        resolved = Path(exe).expanduser()
        if not resolved.exists():
            return False, f"実行ファイルが見つかりません: {exe}"
        argv[0] = str(resolved)
    else:
        which_path = shutil.which(exe)
        if not which_path:
            return False, f"コマンドが見つかりません: {exe}"
        argv[0] = which_path

    return _spawn_detached(argv)


def open_folder(path: str) -> tuple[bool, str]:
    """Open a local directory in the system file manager across Linux/macOS/Windows."""
    target = Path(path).expanduser()
    if not target.exists():
        return False, f"フォルダが見つかりません: {target}"
    if not target.is_dir():
        return False, f"ディレクトリではありません: {target}"

    if sys.platform.startswith("win") and hasattr(os, "startfile"):
        try:
            os.startfile(str(target))
            return True, f"開きました: {target}"
        except OSError as exc:
            return False, f"フォルダを開けませんでした: {exc}"

    if sys.platform == "darwin":
        opener = shutil.which("open") or "open"
        return _spawn_detached([opener, str(target)])

    opener = next(
        (shutil.which(cmd) for cmd in ("xdg-open", "dolphin", "nautilus", "thunar") if shutil.which(cmd)),
        None,
    )
    if not opener:
        return False, "ファイルマネージャー (xdg-open / dolphin) が見つかりません。"
    return _spawn_detached([opener, str(target)])


def open_bookmark(url: str) -> tuple[bool, str]:
    """Open a web bookmark URL in Google Chrome (preferred) or the system browser."""
    cleaned = url.strip()
    if not cleaned:
        return False, "URLが空です。"
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"

    if sys.platform.startswith("linux"):
        chrome_candidates = [
            Path("/usr/bin/google-chrome-stable"),
            *(
                Path(p)
                for p in (
                    shutil.which("google-chrome-stable"),
                    shutil.which("google-chrome"),
                    shutil.which("chromium"),
                )
                if p
            ),
        ]
        for candidate in chrome_candidates:
            if candidate.is_file():
                return _spawn_detached([str(candidate), cleaned])

        opener = shutil.which("xdg-open")
        if opener:
            return _spawn_detached([opener, cleaned])
    elif sys.platform == "darwin":
        opener = shutil.which("open") or "open"
        return _spawn_detached([opener, cleaned])
    else:
        chrome = shutil.which("chrome")
        if chrome:
            return _spawn_detached([chrome, cleaned])

    try:
        if webbrowser.open(cleaned):
            return True, f"ブラウザで開きました: {cleaned}"
    except Exception as exc:
        return False, f"ブラウザの起動に失敗しました: {exc}"
    return False, "ブラウザが見つかりません。"
