"""Discover installed XDG desktop applications."""

from __future__ import annotations

import re
import shlex
from dataclasses import dataclass
from pathlib import Path

_FIELD_CODE_RE = re.compile(r"(?<!%)%[fFuUdDnNickvm]")


@dataclass
class DesktopAppEntry:
    name: str
    command: str
    icon: str
    desktop_file: str


def _clean_exec_command(raw_exec: str) -> str:
    """Strip XDG desktop entry field codes (e.g. %u, %F) while keeping executable & fixed args."""
    cleaned = _FIELD_CODE_RE.sub("", raw_exec).replace("%%", "%").strip()
    try:
        tokens = shlex.split(cleaned)
    except ValueError:
        return cleaned
    filtered = [tok for tok in tokens if tok]
    return shlex.join(filtered) if filtered else ""


def _parse_desktop_file(path: Path) -> tuple[bool, DesktopAppEntry | None]:
    """Parse the [Desktop Entry] section of a .desktop file."""
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return False, None

    in_entry = False
    fields: dict[str, str] = {}
    for line in lines:
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        if stripped.startswith("["):
            if in_entry:
                break
            in_entry = stripped == "[Desktop Entry]"
            continue
        if not in_entry or "=" not in stripped:
            continue
        key, value = stripped.split("=", 1)
        fields.setdefault(key.strip(), value.strip())

    if not fields:
        return False, None

    if fields.get("Type", "Application") != "Application":
        return False, None
    if fields.get("NoDisplay", "").lower() == "true" or fields.get("Hidden", "").lower() == "true":
        return True, None

    name = fields.get("Name[ja]") or fields.get("Name") or path.stem
    raw_exec = fields.get("Exec", "")
    command = _clean_exec_command(raw_exec)
    if not command:
        return False, None

    return False, DesktopAppEntry(
        name=name,
        command=command,
        icon=fields.get("Icon", ""),
        desktop_file=str(path),
    )


def discover_desktop_apps(search_dirs: tuple[Path, ...] | None = None) -> list[DesktopAppEntry]:
    """Discover non-hidden XDG desktop applications, preferring user-local overrides."""
    dirs = search_dirs or (
        Path.home() / ".local" / "share" / "applications",
        Path("/usr/share/applications"),
    )

    by_filename: dict[str, DesktopAppEntry | None] = {}
    for directory in dirs:
        if not directory.is_dir():
            continue
        for desktop_path in sorted(directory.glob("*.desktop")):
            filename = desktop_path.name
            if filename in by_filename:
                continue
            hidden, entry = _parse_desktop_file(desktop_path)
            if hidden:
                by_filename[filename] = None
            elif entry is not None:
                by_filename[filename] = entry

    seen_keys: set[tuple[str, str]] = set()
    entries: list[DesktopAppEntry] = []
    for entry in by_filename.values():
        if entry is None:
            continue
        dedup_key = (entry.name.casefold(), entry.command)
        if dedup_key in seen_keys:
            continue
        seen_keys.add(dedup_key)
        entries.append(entry)

    entries.sort(key=lambda item: item.name.casefold())
    return entries
