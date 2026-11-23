"""High-DPI transparent sprite loader for all 17 Sisters."""

from __future__ import annotations

from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QImage,
    QPainter,
    QPen,
    QPixmap,
)

from sisters_mascot import SPRITES_DIR, USER_SPRITES_DIR
from sisters_mascot.sisters_loader import SisterProfile, find_sprite_path

_MAX_CACHE_ITEMS = 64
_PIXMAP_CACHE: OrderedDict[tuple[str, str, int, int, str, int], QPixmap] = OrderedDict()


def clear_sprite_cache() -> None:
    """Clear the rendered pixmap LRU cache (call after importing/resetting sprites)."""
    _PIXMAP_CACHE.clear()


def find_animated_sprite_path(sister_id: str, state: str = "idle") -> Path | None:
    """Resolve animated .gif sprite path, preferring user sprites over bundled sprites."""
    safe_state = state or "idle"
    states_to_check = [safe_state] if safe_state == "idle" else [safe_state, "idle"]
    for st in states_to_check:
        for root in (USER_SPRITES_DIR, SPRITES_DIR):
            candidate = root / sister_id / f"{st}.gif"
            if candidate.is_file():
                return candidate
    return None


def _extract_short_jp_name(display_name: str) -> str:
    """Extract clean short name from display strings like '碧 (Aoi)' or 'kohaku'."""
    name = (display_name or "").strip()
    if "(" in name:
        name = name.split("(", 1)[0].strip()
    if "（" in name:
        name = name.split("（", 1)[0].strip()
    return name or display_name


def _cache_get(key: tuple[str, str, int, int, str, int]) -> QPixmap | None:
    pixmap = _PIXMAP_CACHE.get(key)
    if pixmap is not None:
        _PIXMAP_CACHE.move_to_end(key)
    return pixmap


def _cache_put(key: tuple[str, str, int, int, str, int], pixmap: QPixmap) -> None:
    _PIXMAP_CACHE[key] = pixmap
    _PIXMAP_CACHE.move_to_end(key)
    while len(_PIXMAP_CACHE) > _MAX_CACHE_ITEMS:
        _PIXMAP_CACHE.popitem(last=False)


def _render_missing_sprite_placeholder(
    sister: SisterProfile,
    target_width: int,
    dpr: float,
) -> QPixmap:
    """Render a minimal translucent pill placeholder only if a sprite PNG is missing."""
    w = float(max(96, target_width))
    h = max(48, round(w * 0.42))
    phys_w = max(1, int(round(w * dpr)))
    phys_h = max(1, int(round(h * dpr)))

    image = QImage(phys_w, phys_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.scale(dpr, dpr)

    accent = QColor(sister.accent_color or "#7c3aed")
    if not accent.isValid():
        accent = QColor("#7c3aed")

    pill_rect = QRectF(4.0, 4.0, w - 8.0, h - 8.0)
    painter.setBrush(QColor(18, 22, 38, 215))
    painter.setPen(QPen(accent.lighter(120), 1.5))
    painter.drawRoundedRect(pill_rect, pill_rect.height() * 0.45, pill_rect.height() * 0.45)

    short_name = _extract_short_jp_name(sister.display_name)
    label = f"{sister.rank_label} · {short_name}".strip(" ·")
    painter.setFont(QFont("Noto Sans JP", max(9, int(round(w * 0.068))), QFont.Weight.Bold))
    painter.setPen(QColor(248, 250, 252))
    painter.drawText(pill_rect, Qt.AlignmentFlag.AlignCenter, label)
    painter.end()

    pixmap = QPixmap.fromImage(image)
    pixmap.setDevicePixelRatio(dpr)
    return pixmap


def render_sister_pixmap(
    sister: SisterProfile,
    state: str,
    target_width: int,
    dpr: float = 1.0,
) -> QPixmap:
    """Render a sister's transparent PNG/WebP sprite (or minimal placeholder if absent)."""
    safe_dpr = max(1.0, float(dpr or 1.0))
    safe_width = max(64, int(target_width))
    safe_state = state or "idle"

    sprite_path: Path | None = find_sprite_path(sister.id, safe_state)
    path_str = str(sprite_path) if sprite_path is not None else ""
    mtime_ns = 0
    if sprite_path is not None:
        try:
            mtime_ns = sprite_path.stat().st_mtime_ns
        except OSError:
            sprite_path = None
            path_str = ""

    cache_key = (
        sister.id,
        safe_state,
        safe_width,
        int(round(safe_dpr * 100)),
        path_str,
        mtime_ns,
    )
    cached = _cache_get(cache_key)
    if cached is not None:
        return cached

    if sprite_path is not None:
        img = QImage(path_str)
        if not img.isNull():
            phys_w = max(1, int(round(safe_width * safe_dpr)))
            max_phys_h = max(phys_w, int(round(safe_width * 2.2 * safe_dpr)))
            if img.width() > 0 and (img.height() / img.width()) <= 2.2:
                scaled = img.scaledToWidth(phys_w, Qt.TransformationMode.SmoothTransformation)
            else:
                scaled = img.scaled(
                    phys_w,
                    max_phys_h,
                    Qt.AspectRatioMode.KeepAspectRatio,
                    Qt.TransformationMode.SmoothTransformation,
                )
            pixmap = QPixmap.fromImage(scaled)
            pixmap.setDevicePixelRatio(safe_dpr)
            _cache_put(cache_key, pixmap)
            return pixmap

    pixmap = _render_missing_sprite_placeholder(sister, safe_width, safe_dpr)
    _cache_put(cache_key, pixmap)
    return pixmap
