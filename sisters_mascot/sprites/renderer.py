"""High-DPI sprite loader and fallback chibi medallion renderer."""

from __future__ import annotations

import math
from collections import OrderedDict
from pathlib import Path

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QFont,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
    QRadialGradient,
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


def _extract_monogram(sister: SisterProfile) -> str:
    """Pick a crisp 1-character monogram for the sister's fallback medallion."""
    short_name = _extract_short_jp_name(sister.display_name)
    if short_name and not short_name.isascii():
        return short_name[0]
    romaji = (sister.romaji or short_name or "S").strip()
    return romaji[0].upper() if romaji else "S"


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


def _draw_sparkle(painter: QPainter, center: QPointF, radius: float, color: QColor) -> None:
    """Draw a four-pointed anime star sparkle."""
    path = QPainterPath()
    inner = radius * 0.28
    pts = [
        QPointF(center.x(), center.y() - radius),
        QPointF(center.x() + inner, center.y() - inner),
        QPointF(center.x() + radius, center.y()),
        QPointF(center.x() + inner, center.y() + inner),
        QPointF(center.x(), center.y() + radius),
        QPointF(center.x() - inner, center.y() + inner),
        QPointF(center.x() - radius, center.y()),
        QPointF(center.x() - inner, center.y() - inner),
    ]
    path.moveTo(pts[0])
    for p in pts[1:]:
        path.lineTo(p)
    path.closeSubpath()
    painter.save()
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(color)
    painter.drawPath(path)
    painter.restore()


def _draw_chibi_silhouette(
    painter: QPainter,
    medallion_rect: QRectF,
    accent: QColor,
    secondary: QColor,
    state: str,
) -> None:
    """Draw a soft stylized chibi bust silhouette + ribbon inside the medallion."""
    cx = medallion_rect.center().x()
    cy = medallion_rect.center().y()
    r = medallion_rect.width() * 0.5

    painter.save()
    clip_path = QPainterPath()
    clip_path.addEllipse(medallion_rect)
    painter.setClipPath(clip_path)

    # Subtle glowing halo behind head
    halo = QRadialGradient(QPointF(cx, cy - r * 0.10), r * 0.72)
    halo_c = QColor(accent)
    halo_c.setAlpha(95)
    halo.setColorAt(0.0, halo_c)
    halo_mid = QColor(secondary)
    halo_mid.setAlpha(35)
    halo.setColorAt(0.6, halo_mid)
    halo.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(halo)
    painter.drawEllipse(medallion_rect)

    # Flowing twin hair / silhouette shadow
    hair_grad = QLinearGradient(
        QPointF(cx, cy - r * 0.52),
        QPointF(cx, cy + r * 0.92),
    )
    h1 = QColor(secondary).lighter(115)
    h1.setAlpha(185)
    h2 = QColor(accent).darker(135)
    h2.setAlpha(210)
    hair_grad.setColorAt(0.0, h1)
    hair_grad.setColorAt(1.0, h2)

    hair_path = QPainterPath()
    hair_path.moveTo(cx - r * 0.42, cy - r * 0.08)
    hair_path.quadTo(cx - r * 0.56, cy + r * 0.36, cx - r * 0.44, cy + r * 0.76)
    hair_path.lineTo(cx + r * 0.44, cy + r * 0.76)
    hair_path.quadTo(cx + r * 0.56, cy + r * 0.36, cx + r * 0.42, cy - r * 0.08)
    hair_path.arcTo(
        QRectF(cx - r * 0.38, cy - r * 0.52, r * 0.76, r * 0.76),
        0,
        180,
    )
    hair_path.closeSubpath()
    painter.setBrush(hair_grad)
    painter.drawPath(hair_path)

    # Chibi head silhouette
    head_rect = QRectF(cx - r * 0.28, cy - r * 0.44, r * 0.56, r * 0.54)
    head_grad = QLinearGradient(head_rect.topLeft(), head_rect.bottomRight())
    head_top = QColor(248, 244, 255, 225)
    head_bot = QColor(secondary).lighter(145)
    head_bot.setAlpha(215)
    head_grad.setColorAt(0.0, head_top)
    head_grad.setColorAt(1.0, head_bot)
    painter.setBrush(head_grad)
    painter.drawEllipse(head_rect)

    # Shoulders / dress collar
    body_path = QPainterPath()
    body_path.moveTo(cx - r * 0.52, cy + r * 0.96)
    body_path.quadTo(cx - r * 0.40, cy + r * 0.24, cx, cy + r * 0.16)
    body_path.quadTo(cx + r * 0.40, cy + r * 0.24, cx + r * 0.52, cy + r * 0.96)
    body_path.closeSubpath()
    body_grad = QLinearGradient(QPointF(cx, cy + r * 0.16), QPointF(cx, cy + r * 0.96))
    b1 = QColor(accent).lighter(120)
    b1.setAlpha(230)
    b2 = QColor(secondary).darker(120)
    b2.setAlpha(240)
    body_grad.setColorAt(0.0, b1)
    body_grad.setColorAt(1.0, b2)
    painter.setBrush(body_grad)
    painter.drawPath(body_path)

    # Cute bow / crystal brooch at collar
    bow_y = cy + r * 0.28
    bow_w = r * 0.16
    bow_h = r * 0.09
    bow_color = QColor(255, 255, 255, 220)
    painter.setBrush(bow_color)
    left_wing = QPainterPath()
    left_wing.moveTo(cx, bow_y)
    left_wing.lineTo(cx - bow_w, bow_y - bow_h)
    left_wing.lineTo(cx - bow_w, bow_y + bow_h)
    left_wing.closeSubpath()
    right_wing = QPainterPath()
    right_wing.moveTo(cx, bow_y)
    right_wing.lineTo(cx + bow_w, bow_y - bow_h)
    right_wing.lineTo(cx + bow_w, bow_y + bow_h)
    right_wing.closeSubpath()
    painter.drawPath(left_wing)
    painter.drawPath(right_wing)
    painter.setBrush(accent)
    painter.drawEllipse(QPointF(cx, bow_y), r * 0.045, r * 0.045)

    # Subtle eyes/blush hint for extra cuteness
    eye_y = cy - r * 0.15
    eye_dx = r * 0.105
    if state in ("happy", "talking"):
        pen = QPen(QColor(accent).darker(160), max(1.5, r * 0.028), Qt.PenStyle.SolidLine, Qt.PenCapStyle.RoundCap)
        painter.setPen(pen)
        painter.setBrush(Qt.BrushStyle.NoBrush)
        for sign in (-1, 1):
            arc_r = QRectF(cx + sign * eye_dx - r * 0.045, eye_y - r * 0.025, r * 0.09, r * 0.06)
            painter.drawArc(arc_r, 15 * 16, 150 * 16)
    else:
        painter.setPen(Qt.PenStyle.NoPen)
        painter.setBrush(QColor(accent).darker(150))
        for sign in (-1, 1):
            painter.drawEllipse(QPointF(cx + sign * eye_dx, eye_y), r * 0.032, r * 0.042)
        blush = QColor(255, 145, 175, 110)
        painter.setBrush(blush)
        for sign in (-1, 1):
            painter.drawEllipse(QPointF(cx + sign * (eye_dx + r * 0.05), eye_y + r * 0.055), r * 0.045, r * 0.024)

    painter.restore()


def _render_medallion_card(
    sister: SisterProfile,
    state: str,
    target_width: int,
    dpr: float,
) -> QPixmap:
    """Draw a polished high-DPI chibi medallion card when no PNG/WebP sprite exists yet."""
    w = float(max(96, target_width))
    h = round(w * 1.24)
    phys_w = max(1, int(round(w * dpr)))
    phys_h = max(1, int(round(h * dpr)))

    image = QImage(phys_w, phys_h, QImage.Format.Format_ARGB32_Premultiplied)
    image.fill(Qt.GlobalColor.transparent)

    painter = QPainter(image)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.TextAntialiasing, True)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.scale(dpr, dpr)

    accent = QColor(sister.accent_color or "#7c3aed")
    secondary = QColor(sister.secondary_color or "#38bdf8")
    if not accent.isValid():
        accent = QColor("#7c3aed")
    if not secondary.isValid():
        secondary = QColor("#38bdf8")

    cx = w * 0.5
    med_radius = w * 0.37
    med_cy = h * 0.45

    # Outer soft radial aura
    aura = QRadialGradient(QPointF(cx, med_cy), med_radius * 1.38)
    c0 = QColor(accent)
    c0.setAlpha(105 if state != "idle" else 75)
    c1 = QColor(secondary)
    c1.setAlpha(42)
    aura.setColorAt(0.0, c0)
    aura.setColorAt(0.58, c1)
    aura.setColorAt(1.0, QColor(0, 0, 0, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(aura)
    painter.drawEllipse(QPointF(cx, med_cy), med_radius * 1.35, med_radius * 1.35)

    # Medallion dark glass disc
    med_rect = QRectF(cx - med_radius, med_cy - med_radius, med_radius * 2, med_radius * 2)
    disc_grad = QLinearGradient(med_rect.topLeft(), med_rect.bottomRight())
    disc_grad.setColorAt(0.0, QColor(24, 28, 48, 232))
    disc_grad.setColorAt(1.0, QColor(14, 17, 32, 240))
    painter.setBrush(disc_grad)
    painter.drawEllipse(med_rect)

    # Stylized chibi silhouette inside medallion
    _draw_chibi_silhouette(painter, med_rect, accent, secondary, state)

    # Soft vignette + translucent monogram watermark behind front ring
    monogram = _extract_monogram(sister)
    mono_font = QFont("Noto Sans JP", max(14, int(round(w * 0.16))), QFont.Weight.Bold)
    painter.setFont(mono_font)
    mono_rect = QRectF(cx - med_radius * 0.6, med_cy + med_radius * 0.14, med_radius * 1.2, med_radius * 0.65)
    painter.setPen(QColor(255, 255, 255, 55))
    painter.drawText(mono_rect, Qt.AlignmentFlag.AlignCenter, monogram)

    # Double luminous outer ring
    ring_grad = QLinearGradient(med_rect.topLeft(), med_rect.bottomRight())
    ring_grad.setColorAt(0.0, accent.lighter(125))
    ring_grad.setColorAt(0.5, QColor(255, 255, 255, 210))
    ring_grad.setColorAt(1.0, secondary.lighter(120))
    painter.setBrush(Qt.BrushStyle.NoBrush)
    painter.setPen(QPen(ring_grad, max(2.2, w * 0.022)))
    painter.drawEllipse(med_rect)

    inner_ring = med_rect.adjusted(4.5, 4.5, -4.5, -4.5)
    inner_pen_color = QColor(255, 255, 255, 68)
    painter.setPen(QPen(inner_pen_color, 1.0))
    painter.drawEllipse(inner_ring)

    # Decorative anime star sparkles around the medallion
    _draw_sparkle(painter, QPointF(cx - med_radius * 0.92, med_cy - med_radius * 0.62), w * 0.048, QColor(255, 255, 255, 225))
    _draw_sparkle(painter, QPointF(cx + med_radius * 0.95, med_cy - med_radius * 0.35), w * 0.036, accent.lighter(140))
    _draw_sparkle(painter, QPointF(cx + med_radius * 0.80, med_cy + med_radius * 0.62), w * 0.030, secondary.lighter(140))

    # Top rank pill badge (e.g., 三女)
    rank_text = (sister.rank_label or f"#{sister.order}").strip()
    badge_w = max(w * 0.34, 54.0)
    badge_h = max(20.0, w * 0.115)
    badge_rect = QRectF(cx - badge_w * 0.5, med_rect.top() - badge_h * 0.42, badge_w, badge_h)
    badge_grad = QLinearGradient(badge_rect.topLeft(), badge_rect.bottomRight())
    badge_grad.setColorAt(0.0, accent)
    badge_grad.setColorAt(1.0, secondary.darker(115))
    painter.setPen(QPen(QColor(255, 255, 255, 180), 1.2))
    painter.setBrush(badge_grad)
    painter.drawRoundedRect(badge_rect, badge_h * 0.5, badge_h * 0.5)

    rank_font = QFont("Noto Sans JP", max(8, int(round(w * 0.054))), QFont.Weight.Bold)
    painter.setFont(rank_font)
    painter.setPen(QColor(255, 255, 255))
    painter.drawText(badge_rect, Qt.AlignmentFlag.AlignCenter, rank_text)

    # Bottom nameplate banner overlapping lower edge
    short_name = _extract_short_jp_name(sister.display_name)
    banner_w = w * 0.84
    banner_h = max(32.0, w * 0.19)
    banner_y = med_rect.bottom() - banner_h * 0.30
    banner_rect = QRectF(cx - banner_w * 0.5, banner_y, banner_w, banner_h)

    banner_bg = QLinearGradient(banner_rect.topLeft(), banner_rect.bottomRight())
    banner_bg.setColorAt(0.0, QColor(18, 22, 38, 236))
    banner_bg.setColorAt(1.0, QColor(28, 32, 56, 236))
    painter.setBrush(banner_bg)
    painter.setPen(QPen(accent.lighter(120), 1.5))
    painter.drawRoundedRect(banner_rect, banner_h * 0.45, banner_h * 0.45)

    name_font = QFont("Noto Sans JP", max(10, int(round(w * 0.078))), QFont.Weight.Bold)
    painter.setFont(name_font)
    painter.setPen(QColor(248, 250, 252))
    sub_text = (sister.romaji or "").strip()
    if sub_text and sub_text.lower() != short_name.lower():
        top_text_rect = QRectF(banner_rect.left() + 8, banner_rect.top() + 2, banner_rect.width() - 16, banner_h * 0.62)
        bot_text_rect = QRectF(banner_rect.left() + 8, banner_rect.top() + banner_h * 0.55, banner_rect.width() - 16, banner_h * 0.38)
        painter.drawText(top_text_rect, Qt.AlignmentFlag.AlignCenter, short_name)
        sub_font = QFont("Noto Sans JP", max(7, int(round(w * 0.044))), QFont.Weight.Medium)
        painter.setFont(sub_font)
        painter.setPen(QColor(203, 213, 225, 220))
        painter.drawText(bot_text_rect, Qt.AlignmentFlag.AlignCenter, sub_text)
    else:
        painter.drawText(banner_rect, Qt.AlignmentFlag.AlignCenter, short_name)

    # State indicator pill when dragged / thinking / talking
    state_symbols = {
        "thinking": "✨",
        "talking": "💬",
        "happy": "💖",
        "dragged": "💫",
    }
    if state in state_symbols:
        dot_r = max(10.0, w * 0.065)
        dot_center = QPointF(med_rect.right() - dot_r * 0.45, med_rect.top() + dot_r * 0.55)
        painter.setBrush(QColor(20, 24, 42, 230))
        painter.setPen(QPen(accent.lighter(130), 1.2))
        painter.drawEllipse(dot_center, dot_r, dot_r)
        sym_font = QFont("Noto Color Emoji", max(8, int(round(dot_r * 0.9))))
        painter.setFont(sym_font)
        painter.setPen(QColor(255, 255, 255))
        painter.drawText(
            QRectF(dot_center.x() - dot_r, dot_center.y() - dot_r, dot_r * 2, dot_r * 2),
            Qt.AlignmentFlag.AlignCenter,
            state_symbols[state],
        )

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
    """Render a sister's transparent PNG/WebP sprite or fallback chibi medallion."""
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

    pixmap = _render_medallion_card(sister, safe_state, safe_width, safe_dpr)
    _cache_put(cache_key, pixmap)
    return pixmap
