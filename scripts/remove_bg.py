#!/usr/bin/env python3
"""Chroma-key green background removal and app icon generator using PyQt6 QImage/QPainter."""

from __future__ import annotations

import argparse
import math
import os
from collections import deque
from pathlib import Path

# Ensure headless execution works without a display server.
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPointF, QRectF, Qt
from PyQt6.QtGui import (
    QColor,
    QGuiApplication,
    QImage,
    QLinearGradient,
    QPainter,
    QPainterPath,
    QPen,
    QPolygonF,
    QRadialGradient,
)

PROJECT_ROOT = Path("/home/dma/プロジェクト/sisters-mascot")
RAW_DIR = PROJECT_ROOT / "assets" / "raw"
SPRITES_DIR = PROJECT_ROOT / "assets" / "sprites"
APP_ICON_PATH = PROJECT_ROOT / "assets" / "app-icon.png"

DEFAULT_SPRITE_IDS = (
    "aoi",
    "ruri",
    "kohaku",
    "sumire",
    "akari",
    "shion",
    "hiiro",
    "mashiro",
    "yui",
    "ritsu",
    "nagi",
    "akane",
)
MAX_TOTAL_HEIGHT = 540
PADDING_PX = 12
MAX_CONTENT_HEIGHT = MAX_TOTAL_HEIGHT - (PADDING_PX * 2)


def _sample_corner_green(buf: bytearray, width: int, height: int) -> tuple[int, int, int]:
    samples: list[tuple[int, int, int]] = []
    for y in (0, 1, height - 2, height - 1):
        if not (0 <= y < height):
            continue
        for x in (0, 1, width - 2, width - 1):
            if not (0 <= x < width):
                continue
            off = (y * width + x) * 4
            samples.append((buf[off], buf[off + 1], buf[off + 2]))
    if not samples:
        return (0, 255, 0)
    rs = sorted(c[0] for c in samples)
    gs = sorted(c[1] for c in samples)
    bs = sorted(c[2] for c in samples)
    mid = len(samples) // 2
    return (rs[mid], gs[mid], bs[mid])


def _is_border_green(r: int, g: int, b: int, bg_r: int, bg_g: int, bg_b: int) -> bool:
    """Detect chroma-key green connected to borders (tolerant of JPEG anti-aliasing)."""
    max_rb = r if r >= b else b
    green_excess = g - max_rb
    if g >= 115 and green_excess >= 38:
        return True
    dist = abs(r - bg_r) + abs(g - bg_g) + abs(b - bg_b)
    return dist <= 135 and green_excess >= 25


def _is_pure_interior_green(r: int, g: int, b: int) -> bool:
    """Detect enclosed chroma-green pockets (including darkened 2-4px hair/collar gaps)."""
    max_rb = r if r >= b else b
    excess = g - max_rb
    if g >= 165 and excess >= 95 and r <= 125 and b <= 125:
        return True
    # Small JPEG-compressed gaps between dark hair strands and collars
    return g >= 70 and excess >= 36 and r <= 95 and b <= 95 and g >= max_rb * 1.55


def _is_interior_flood_neighbor(r: int, g: int, b: int) -> bool:
    max_rb = r if r >= b else b
    return g >= 60 and (g - max_rb) >= 28 and r <= 110 and b <= 110


def remove_green_background(
    input_path: Path,
    output_path: Path,
    *,
    max_height: int = MAX_TOTAL_HEIGHT,
    padding: int = PADDING_PX,
) -> tuple[int, int]:
    """Remove green background via flood-fill, feather edges, despill green, crop, and pad."""
    src = QImage(str(input_path))
    if src.isNull():
        raise ValueError(f"Failed to read input image: {input_path}")

    img = src.convertToFormat(QImage.Format.Format_RGBA8888)
    w, h = img.width(), img.height()
    n_bytes = img.sizeInBytes()
    ptr = img.bits()
    ptr.setsize(n_bytes)
    buf = bytearray(ptr)

    bg_r, bg_g, bg_b = _sample_corner_green(buf, w, h)
    total = w * h
    removed = bytearray(total)
    queue: deque[int] = deque()

    def try_seed_border(x: int, y: int) -> None:
        idx = y * w + x
        if removed[idx]:
            return
        off = idx * 4
        if _is_border_green(buf[off], buf[off + 1], buf[off + 2], bg_r, bg_g, bg_b):
            removed[idx] = 1
            queue.append(idx)

    for x in range(w):
        try_seed_border(x, 0)
        try_seed_border(x, h - 1)
    for y in range(1, h - 1):
        try_seed_border(0, y)
        try_seed_border(w - 1, y)

    def run_flood(strict_interior: bool = False) -> None:
        while queue:
            idx = queue.popleft()
            x = idx % w
            y = idx // w
            if x > 0:
                n_idx = idx - 1
                if not removed[n_idx]:
                    off = n_idx * 4
                    ok = (
                        _is_border_green(buf[off], buf[off + 1], buf[off + 2], bg_r, bg_g, bg_b)
                        if not strict_interior
                        else _is_interior_flood_neighbor(buf[off], buf[off + 1], buf[off + 2])
                    )
                    if ok:
                        removed[n_idx] = 1
                        queue.append(n_idx)
            if x + 1 < w:
                n_idx = idx + 1
                if not removed[n_idx]:
                    off = n_idx * 4
                    ok = (
                        _is_border_green(buf[off], buf[off + 1], buf[off + 2], bg_r, bg_g, bg_b)
                        if not strict_interior
                        else _is_interior_flood_neighbor(buf[off], buf[off + 1], buf[off + 2])
                    )
                    if ok:
                        removed[n_idx] = 1
                        queue.append(n_idx)
            if y > 0:
                n_idx = idx - w
                if not removed[n_idx]:
                    off = n_idx * 4
                    ok = (
                        _is_border_green(buf[off], buf[off + 1], buf[off + 2], bg_r, bg_g, bg_b)
                        if not strict_interior
                        else _is_interior_flood_neighbor(buf[off], buf[off + 1], buf[off + 2])
                    )
                    if ok:
                        removed[n_idx] = 1
                        queue.append(n_idx)
            if y + 1 < h:
                n_idx = idx + w
                if not removed[n_idx]:
                    off = n_idx * 4
                    ok = (
                        _is_border_green(buf[off], buf[off + 1], buf[off + 2], bg_r, bg_g, bg_b)
                        if not strict_interior
                        else _is_interior_flood_neighbor(buf[off], buf[off + 1], buf[off + 2])
                    )
                    if ok:
                        removed[n_idx] = 1
                        queue.append(n_idx)

    run_flood(strict_interior=False)

    # Also remove enclosed bright chroma-green pockets (arm/torso/hair gaps).
    for idx in range(total):
        if removed[idx]:
            continue
        off = idx * 4
        if _is_pure_interior_green(buf[off], buf[off + 1], buf[off + 2]):
            removed[idx] = 1
            queue.append(idx)
            run_flood(strict_interior=True)

    # Zero alpha on removed pixels.
    for idx in range(total):
        if removed[idx]:
            buf[idx * 4 + 3] = 0

    # Build 2-ring boundary mask for smooth edge feathering & green despill.
    ring1 = bytearray(total)
    ring2 = bytearray(total)
    for y in range(h):
        row = y * w
        for x in range(w):
            idx = row + x
            if removed[idx]:
                continue
            adj = (
                (x > 0 and removed[idx - 1])
                or (x + 1 < w and removed[idx + 1])
                or (y > 0 and removed[idx - w])
                or (y + 1 < h and removed[idx + w])
            )
            if adj:
                ring1[idx] = 1

    for y in range(h):
        row = y * w
        for x in range(w):
            idx = row + x
            if removed[idx] or ring1[idx]:
                continue
            adj = (
                (x > 0 and ring1[idx - 1])
                or (x + 1 < w and ring1[idx + 1])
                or (y > 0 and ring1[idx - w])
                or (y + 1 < h and ring1[idx + w])
            )
            if adj:
                ring2[idx] = 1

    for idx in range(total):
        if removed[idx]:
            continue
        off = idx * 4
        r, g, b = buf[off], buf[off + 1], buf[off + 2]
        max_rb = r if r >= b else b
        excess = g - max_rb
        if not (ring1[idx] or ring2[idx]):
            # Also despill isolated anti-aliased green pixels near fine hair/pen lines.
            if excess > 22 and g >= max_rb * 1.25:
                buf[off + 1] = max_rb
            continue
        if excess > 0:
            # Despill green fringe cleanly without darkening neutral outlines.
            avg_rb = (r + b) // 2
            cap = max(max_rb, avg_rb)
            if ring1[idx]:
                buf[off + 1] = min(g, cap)
                if excess > 14:
                    alpha = max(35, min(230, 255 - excess * 3))
                    buf[off + 3] = alpha
                else:
                    buf[off + 3] = 220
            else:
                buf[off + 1] = min(g, int(cap + excess * 0.25))

    # Find tight bounding box of visible foreground pixels.
    min_x, min_y = w, h
    max_x, max_y = -1, -1
    for y in range(h):
        row = y * w
        for x in range(w):
            if buf[(row + x) * 4 + 3] > 8:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y

    if max_x < min_x or max_y < min_y:
        raise ValueError(f"No foreground pixels left after keying: {input_path}")

    keyed = QImage(bytes(buf), w, h, w * 4, QImage.Format.Format_RGBA8888).copy(
        min_x, min_y, max_x - min_x + 1, max_y - min_y + 1
    )

    target_content_h = max(1, max_height - (padding * 2))
    if keyed.height() > target_content_h:
        keyed = keyed.scaledToHeight(target_content_h, Qt.TransformationMode.SmoothTransformation)

    out_w = keyed.width() + padding * 2
    out_h = keyed.height() + padding * 2
    canvas = QImage(out_w, out_h, QImage.Format.Format_RGBA8888)
    canvas.fill(Qt.GlobalColor.transparent)

    painter = QPainter(canvas)
    painter.setRenderHint(QPainter.RenderHint.SmoothPixmapTransform, True)
    painter.drawImage(padding, padding, keyed)
    painter.end()

    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not canvas.save(str(output_path), "PNG"):
        raise OSError(f"Failed to write output PNG: {output_path}")
    return out_w, out_h


def generate_app_icon(output_path: Path = APP_ICON_PATH, size: int = 256) -> Path:
    """Render a 256x256 indigo crystal emblem icon using QPainter."""
    img = QImage(size, size, QImage.Format.Format_RGBA8888)
    img.fill(Qt.GlobalColor.transparent)

    painter = QPainter(img)
    painter.setRenderHint(QPainter.RenderHint.Antialiasing, True)

    margin = size * 0.06
    card_rect = QRectF(margin, margin, size - margin * 2, size - margin * 2)
    radius = size * 0.22

    # Rounded dark-indigo badge backdrop
    bg_grad = QLinearGradient(card_rect.topLeft(), card_rect.bottomRight())
    bg_grad.setColorAt(0.0, QColor("#1E1B4B"))
    bg_grad.setColorAt(0.5, QColor("#312E81"))
    bg_grad.setColorAt(1.0, QColor("#0F172A"))

    badge_path = QPainterPath()
    badge_path.addRoundedRect(card_rect, radius, radius)
    painter.fillPath(badge_path, bg_grad)

    # Outer luminous border
    border_pen = QPen(QColor(147, 197, 253, 180), size * 0.022)
    painter.setPen(border_pen)
    painter.drawPath(badge_path)

    # Soft inner radial aura
    cx, cy = size * 0.5, size * 0.5
    aura = QRadialGradient(QPointF(cx, cy), size * 0.38)
    aura.setColorAt(0.0, QColor(96, 165, 250, 140))
    aura.setColorAt(0.6, QColor(79, 70, 229, 70))
    aura.setColorAt(1.0, QColor(15, 23, 42, 0))
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(aura)
    painter.drawEllipse(QPointF(cx, cy), size * 0.38, size * 0.38)

    # Faceted indigo crystal diamond emblem
    r_top = size * 0.30
    r_side = size * 0.22
    top = QPointF(cx, cy - r_top)
    right = QPointF(cx + r_side, cy)
    bottom = QPointF(cx, cy + r_top)
    left = QPointF(cx - r_side, cy)
    center = QPointF(cx, cy)

    facets = [
        (QPolygonF([top, right, center]), QColor("#93C5FD")),
        (QPolygonF([top, left, center]), QColor("#E0F2FE")),
        (QPolygonF([left, bottom, center]), QColor("#4F46E5")),
        (QPolygonF([right, bottom, center]), QColor("#3B82F6")),
    ]
    for poly, color in facets:
        painter.setBrush(color)
        painter.setPen(QPen(QColor(224, 242, 254, 150), 1.5))
        painter.drawPolygon(poly)

    # Inner diamond highlight
    inner = QPolygonF([
        QPointF(cx, cy - r_top * 0.48),
        QPointF(cx + r_side * 0.48, cy),
        QPointF(cx, cy + r_top * 0.48),
        QPointF(cx - r_side * 0.48, cy),
    ])
    inner_grad = QLinearGradient(QPointF(cx, cy - r_top * 0.48), QPointF(cx, cy + r_top * 0.48))
    inner_grad.setColorAt(0.0, QColor(255, 255, 255, 220))
    inner_grad.setColorAt(1.0, QColor(129, 140, 248, 180))
    painter.setBrush(inner_grad)
    painter.setPen(QPen(QColor(255, 255, 255, 210), 1.5))
    painter.drawPolygon(inner)

    # Four subtle sparkle stars around the crystal
    painter.setPen(Qt.PenStyle.NoPen)
    painter.setBrush(QColor(224, 242, 254, 230))
    for angle_deg, dist, star_r in ((35, 0.30, 0.036), (145, 0.31, 0.028), (220, 0.29, 0.030), (320, 0.31, 0.038)):
        rad = math.radians(angle_deg)
        sx = cx + math.cos(rad) * (size * dist)
        sy = cy + math.sin(rad) * (size * dist)
        sr = size * star_r
        star = QPolygonF([
            QPointF(sx, sy - sr),
            QPointF(sx + sr * 0.28, sy - sr * 0.28),
            QPointF(sx + sr, sy),
            QPointF(sx + sr * 0.28, sy + sr * 0.28),
            QPointF(sx, sy + sr),
            QPointF(sx - sr * 0.28, sy + sr * 0.28),
            QPointF(sx - sr, sy),
            QPointF(sx - sr * 0.28, sy - sr * 0.28),
        ])
        painter.drawPolygon(star)

    painter.end()
    output_path.parent.mkdir(parents=True, exist_ok=True)
    if not img.save(str(output_path), "PNG"):
        raise OSError(f"Failed to save app icon: {output_path}")
    return output_path


def main() -> int:
    app = QGuiApplication.instance() or QGuiApplication([])
    _ = app

    parser = argparse.ArgumentParser(description="Remove chroma-green background from Sisters sprites.")
    parser.add_argument("input", nargs="?", type=Path, help="Input image path")
    parser.add_argument("output", nargs="?", type=Path, help="Output RGBA PNG path")
    parser.add_argument("--icon-only", action="store_true", help="Only generate assets/app-icon.png")
    args = parser.parse_args()

    if args.icon_only:
        icon = generate_app_icon()
        print(f"Generated app icon: {icon}")
        return 0

    if args.input and args.output:
        w, h = remove_green_background(args.input, args.output)
        print(f"Processed {args.input} -> {args.output} ({w}x{h})")
        return 0

    for sister_id in DEFAULT_SPRITE_IDS:
        src = RAW_DIR / f"{sister_id}.jpg"
        dst = SPRITES_DIR / sister_id / "idle.png"
        w, h = remove_green_background(src, dst)
        print(f"Processed {sister_id}: {src.name} -> {dst} ({w}x{h})")

    icon = generate_app_icon()
    print(f"Generated app icon: {icon}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
