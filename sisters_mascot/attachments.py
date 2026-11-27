"""Pure-Python attachment metadata, path helpers, and prompt formatting."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Sequence

IMAGE_EXTENSIONS: frozenset[str] = frozenset(
    {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}
)
MAX_ATTACHMENTS: int = 20
MAX_INLINE_IMAGE_BYTES: int = 20 * 1024 * 1024
MAX_OPENCODE_FILE_BYTES: int = 10 * 1024 * 1024
FOLDER_PREVIEW_LIMIT: int = 12
FOLDER_COUNT_SCAN_LIMIT: int = 200
DEFAULT_ATTACHMENT_ONLY_PROMPT: str = (
    "添付したファイル・フォルダの内容を確認して、概要や気づいた点をわかりやすく教えてください。"
)


def format_bytes(size: int) -> str:
    """Format a byte count as a compact human-readable string."""
    val = max(0, int(size))
    if val < 1024:
        return f"{val} B"
    if val < 1024 * 1024:
        return f"{val / 1024:.1f} KB"
    if val < 1024 * 1024 * 1024:
        return f"{val / (1024 * 1024):.1f} MB"
    return f"{val / (1024 * 1024 * 1024):.1f} GB"


@dataclass(frozen=True, slots=True)
class AttachmentItem:
    """Immutable descriptor for an attached local file, image, or folder."""

    path: str
    name: str
    kind: str  # "folder" | "image" | "file"
    size_bytes: int | None = None
    item_count: int | None = None
    item_count_capped: bool = False
    preview_entries: tuple[str, ...] = ()

    @property
    def is_folder(self) -> bool:
        return self.kind == "folder"

    @property
    def is_image(self) -> bool:
        return self.kind == "image"

    @property
    def icon(self) -> str:
        if self.kind == "folder":
            return "📁"
        if self.kind == "image":
            return "🖼️"
        return "📄"

    @property
    def meta_summary(self) -> str:
        if self.is_folder:
            if self.item_count_capped and self.item_count is not None:
                return f"{self.item_count}+件"
            if self.item_count is not None:
                return f"{self.item_count}件"
            return "フォルダ"
        if self.size_bytes is not None:
            return format_bytes(self.size_bytes)
        return ""

    def chip_label(self, max_chars: int = 22) -> str:
        """Build a concise chip label with icon and optional ellipsis."""
        name = self.name or self.path
        if max_chars > 1 and len(name) > max_chars:
            short = name[: max_chars - 1] + "…"
        elif max_chars <= 1 and len(name) > max_chars:
            short = "…"
        else:
            short = name
        return f"{self.icon} {short}"

    @property
    def tooltip_text(self) -> str:
        kind_label = {
            "folder": "フォルダ",
            "image": "画像",
            "file": "ファイル",
        }.get(self.kind, "ファイル")
        meta = self.meta_summary
        detail = f"{kind_label} ({meta})" if meta else kind_label
        return f"{self.path}\n{detail}"


def build_attachment_item(raw_path: str | Path) -> AttachmentItem | None:
    """Inspect an existing local path and return an AttachmentItem, or None if invalid."""
    raw_str = str(raw_path).strip()
    if not raw_str:
        return None

    norm_path = os.path.abspath(os.path.expanduser(raw_str))
    p = Path(norm_path)
    try:
        if not p.exists():
            return None
        is_dir = p.is_dir()
    except OSError:
        return None

    name = p.name or norm_path

    if is_dir:
        count = 0
        capped = False
        sampled: list[tuple[bool, bool, str, str]] = []
        try:
            with os.scandir(p) as it:
                for entry in it:
                    count += 1
                    if count > FOLDER_COUNT_SCAN_LIMIT:
                        capped = True
                        count = FOLDER_COUNT_SCAN_LIMIT
                        break
                    ename = entry.name
                    try:
                        entry_is_dir = entry.is_dir(follow_symlinks=False)
                    except OSError:
                        entry_is_dir = False
                    disp = f"{ename}/" if entry_is_dir else ename
                    sampled.append((ename.startswith("."), not entry_is_dir, disp.lower(), disp))
        except OSError:
            return AttachmentItem(path=norm_path, name=name, kind="folder")

        sampled.sort(key=lambda item: (item[0], item[1], item[2]))
        preview = tuple(item[3] for item in sampled[:FOLDER_PREVIEW_LIMIT])
        return AttachmentItem(
            path=norm_path,
            name=name,
            kind="folder",
            item_count=count,
            item_count_capped=capped,
            preview_entries=preview,
        )

    kind = "image" if p.suffix.lower() in IMAGE_EXTENSIONS else "file"
    size_bytes: int | None
    try:
        size_bytes = p.stat().st_size
    except OSError:
        size_bytes = None
    return AttachmentItem(
        path=norm_path,
        name=name,
        kind=kind,
        size_bytes=size_bytes,
    )


def collect_attachment_dirs(
    attachments: Sequence[AttachmentItem], cwd: str = ""
) -> list[str]:
    """Return deduplicated existing directories for attachments outside `cwd`."""
    cwd_resolved: Path | None = None
    if cwd and cwd.strip():
        cwd_p = Path(os.path.abspath(os.path.expanduser(cwd.strip())))
        try:
            if cwd_p.is_dir():
                cwd_resolved = cwd_p.resolve()
        except OSError:
            cwd_resolved = None

    dirs: list[str] = []
    seen: set[Path] = set()
    for att in attachments:
        cand = Path(att.path) if att.is_folder else Path(att.path).parent
        try:
            if not cand.is_dir():
                continue
            cand_resolved = cand.resolve()
        except OSError:
            continue

        if cwd_resolved is not None and cand_resolved.is_relative_to(cwd_resolved):
            continue
        if cand_resolved in seen:
            continue
        seen.add(cand_resolved)
        dirs.append(str(cand))
    return dirs


def collect_image_paths(
    attachments: Sequence[AttachmentItem],
    *,
    max_bytes: int = MAX_INLINE_IMAGE_BYTES,
) -> list[str]:
    """Return existing raster image attachment paths within `max_bytes`."""
    paths: list[str] = []
    seen: set[str] = set()
    for att in attachments:
        if not att.is_image or att.path in seen:
            continue
        p = Path(att.path)
        try:
            if not p.is_file():
                continue
            size = att.size_bytes if att.size_bytes is not None else p.stat().st_size
            if size > max_bytes:
                continue
        except OSError:
            continue
        seen.add(att.path)
        paths.append(att.path)
    return paths


def collect_file_paths(
    attachments: Sequence[AttachmentItem],
    *,
    max_bytes: int = MAX_OPENCODE_FILE_BYTES,
) -> list[str]:
    """Return existing non-folder attachment paths within `max_bytes`."""
    paths: list[str] = []
    seen: set[str] = set()
    for att in attachments:
        if att.is_folder or att.path in seen:
            continue
        p = Path(att.path)
        try:
            if not p.is_file():
                continue
            size = att.size_bytes if att.size_bytes is not None else p.stat().st_size
            if size > max_bytes:
                continue
        except OSError:
            continue
        seen.add(att.path)
        paths.append(att.path)
    return paths


def build_prompt_with_attachments(
    prompt: str, attachments: Sequence[AttachmentItem]
) -> str:
    """Format a structured Japanese attachment section ahead of the user prompt."""
    clean_prompt = (prompt or "").strip()
    if not attachments:
        return clean_prompt

    lines: list[str] = ["【添付されたファイル・フォルダ】"]
    for att in attachments:
        if att.is_folder:
            parts: list[str] = []
            if att.item_count is not None:
                parts.append(f"直下: {att.meta_summary}")
            if att.preview_entries:
                preview_str = ", ".join(att.preview_entries)
                if (
                    att.item_count is not None
                    and att.item_count > len(att.preview_entries)
                ) or att.item_count_capped:
                    preview_str += ", ..."
                parts.append(f"例: {preview_str}")
            detail = f" ({' / '.join(parts)})" if parts else ""
            lines.append(f"- [フォルダ] {att.path}{detail}")
        elif att.is_image:
            detail = f" ({att.meta_summary})" if att.meta_summary else ""
            lines.append(f"- [画像] {att.path}{detail}")
        else:
            detail = f" ({att.meta_summary})" if att.meta_summary else ""
            lines.append(f"- [ファイル] {att.path}{detail}")

    lines.append(
        "※ 上記のファイル・フォルダはユーザーから添付されました。必要に応じて直接読み込み・一覧確認した上で回答してください。"
    )
    body = clean_prompt or DEFAULT_ATTACHMENT_ONLY_PROMPT
    return "\n".join(lines) + "\n\n" + body


def format_attachments_for_chat_display(
    attachments: Sequence[AttachmentItem],
) -> list[dict[str, str]]:
    """Serialize AttachmentItems into lightweight dicts for chat log rendering."""
    return [
        {
            "icon": att.icon,
            "name": att.name,
            "path": att.path,
            "kind": att.kind,
            "meta": att.meta_summary,
        }
        for att in attachments
    ]
