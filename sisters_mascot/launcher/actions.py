"""Detached launcher actions for desktop apps, folders, and bookmarks."""

from __future__ import annotations

import os
import shlex
import shutil
import subprocess
from pathlib import Path


def _spawn_detached(argv: list[str], cwd: str | None = None) -> tuple[bool, str]:
    try:
        subprocess.Popen(
            argv,
            cwd=cwd,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
        )
        return True, f"起動しました: {argv[0]}"
    except OSError as exc:
        return False, f"起動に失敗しました ({argv[0]}): {exc}"


def launch_app(command: str) -> tuple[bool, str]:
    """Launch an application command detached from the mascot process."""
    text = command.strip()
    if not text:
        return False, "コマンドが空です。"

    try:
        argv = shlex.split(text)
    except ValueError as exc:
        return False, f"コマンドの解析に失敗しました: {exc}"

    if not argv:
        return False, "コマンドが空です。"

    exe = argv[0]
    if os.path.sep in exe:
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
    """Open a local directory in the system file manager via xdg-open."""
    target = Path(path).expanduser()
    if not target.exists():
        return False, f"フォルダが見つかりません: {target}"
    if not target.is_dir():
        return False, f"ディレクトリではありません: {target}"

    opener = shutil.which("xdg-open") or shutil.which("dolphin")
    if not opener:
        return False, "ファイルマネージャー (xdg-open / dolphin) が見つかりません。"
    return _spawn_detached([opener, str(target)])


def open_bookmark(url: str) -> tuple[bool, str]:
    """Open a web bookmark URL in Google Chrome (preferred) or xdg-open."""
    cleaned = url.strip()
    if not cleaned:
        return False, "URLが空です。"
    if "://" not in cleaned:
        cleaned = f"https://{cleaned}"

    chrome_candidates = [
        Path("/usr/bin/google-chrome-stable"),
        *(Path(p) for p in (shutil.which("google-chrome-stable"), shutil.which("google-chrome")) if p),
    ]
    for candidate in chrome_candidates:
        if candidate.is_file():
            return _spawn_detached([str(candidate), cleaned])

    opener = shutil.which("xdg-open")
    if opener:
        return _spawn_detached([opener, cleaned])
    return False, "ブラウザ (google-chrome-stable / xdg-open) が見つかりません。"
