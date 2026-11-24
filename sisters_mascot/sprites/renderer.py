"""High-DPI transparent sprite loader with per-state alignment and hit-mask support."""

from __future__ import annotations

from collections import OrderedDict
from dataclasses import dataclass
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

_MAX_CACHE_ITEMS = 72
_MAX_METRICS_ITEMS = 160
_ALPHA_CONTENT_THRESHOLD = 20
_ALPHA_CORE_THRESHOLD = 200


@dataclass(frozen=True, slots=True)
class _SpriteMetrics:
    width: int
    height: int
    min_x: int
    min_y: int
    max_x: int
    max_y: int
    content_w: int
    content_h: int
    core_x_norm: float
    foot_y_norm: float


@dataclass(frozen=True, slots=True)
class RenderedSprite:
    pixmap: QPixmap
    logical_w: int
    logical_h: int
    base_w: int
    base_h: int
    offset_x: float
    offset_y: float
    content_rect: QRectF
    hit_image: QImage | None


_METRICS_CACHE: OrderedDict[tuple[str, int], _SpriteMetrics] = OrderedDict()
_SPRITE_CACHE: OrderedDict[
    tuple[str, str, int, int, str, int, str, int], RenderedSprite
] = OrderedDict()


def clear_sprite_cache() -> None:
    """Clear the rendered sprite and metrics LRU caches."""
    _SPRITE_CACHE.clear()
    _METRICS_CACHE.clear()


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


def _lru_get(cache: OrderedDict, key):  # noqa: ANN001, ANN202
    val = cache.get(key)
    if val is not None:
        cache.move_to_end(key)
    return val


def _lru_put(cache: OrderedDict, key, val, limit: int) -> None:  # noqa: ANN001
    cache[key] = val
    cache.move_to_end(key)
    while len(cache) > limit:
        cache.popitem(last=False)


def _logical_size_for_aspect(src_w: int, src_h: int, target_width: int) -> tuple[int, int]:
    """Match Qt KeepAspectRatio scaling capped at height <= target_width * 2.2."""
    safe_w = max(1, src_w)
    safe_h = max(1, src_h)
    aspect = safe_h / safe_w
    if aspect <= 2.2:
        return target_width, max(1, int(round(target_width * aspect)))
    max_h = max(target_width, int(round(target_width * 2.2)))
    scaled_w = max(1, int(round(max_h * (safe_w / safe_h))))
    return scaled_w, max_h


def _band_widest_run_median(
    buf: bytes,
    w: int,
    h: int,
    y_start: int,
    y_end: int,
    fallback_x: float,
) -> float:
    mids: list[float] = []
    y0 = max(0, y_start)
    y1 = min(h, y_end)
    for y in range(y0, y1):
        row = y * w * 4
        best_len = 0
        best_mid: float | None = None
        run_start: int | None = None
        for x in range(w):
            if buf[row + x * 4 + 3] >= _ALPHA_CORE_THRESHOLD:
                if run_start is None:
                    run_start = x
            elif run_start is not None:
                run_len = x - run_start
                if run_len > best_len:
                    best_len = run_len
                    best_mid = (run_start + x - 1) * 0.5
                run_start = None
        if run_start is not None:
            run_len = w - run_start
            if run_len > best_len:
                best_len = run_len
                best_mid = (run_start + w - 1) * 0.5
        if best_mid is not None:
            mids.append(best_mid)
    if not mids:
        return fallback_x
    mids.sort()
    return mids[len(mids) // 2]


def _compute_sprite_metrics(rgba: QImage) -> _SpriteMetrics:
    w, h = rgba.width(), rgba.height()
    n_bytes = rgba.sizeInBytes()
    ptr = rgba.bits()
    ptr.setsize(n_bytes)
    buf = bytes(ptr)

    min_x, min_y = w, h
    max_x, max_y = -1, -1
    for y in range(h):
        row = y * w * 4
        for x in range(w):
            if buf[row + x * 4 + 3] > _ALPHA_CONTENT_THRESHOLD:
                if x < min_x:
                    min_x = x
                if x > max_x:
                    max_x = x
                if y < min_y:
                    min_y = y
                if y > max_y:
                    max_y = y

    if max_x < min_x or max_y < min_y:
        return _SpriteMetrics(
            width=w,
            height=h,
            min_x=0,
            min_y=0,
            max_x=max(0, w - 1),
            max_y=max(0, h - 1),
            content_w=w,
            content_h=h,
            core_x_norm=0.5,
            foot_y_norm=1.0,
        )

    content_w = max_x - min_x + 1
    content_h = max_y - min_y + 1
    bbox_mid_x = (min_x + max_x) * 0.5

    head_x = _band_widest_run_median(
        buf,
        w,
        h,
        min_y + int(content_h * 0.06),
        min_y + int(content_h * 0.18),
        bbox_mid_x,
    )
    chest_x = _band_widest_run_median(
        buf,
        w,
        h,
        min_y + int(content_h * 0.22),
        min_y + int(content_h * 0.38),
        bbox_mid_x,
    )
    core_x = 0.65 * head_x + 0.35 * chest_x
    return _SpriteMetrics(
        width=w,
        height=h,
        min_x=min_x,
        min_y=min_y,
        max_x=max_x,
        max_y=max_y,
        content_w=content_w,
        content_h=content_h,
        core_x_norm=core_x / max(1, w),
        foot_y_norm=(max_y + 1) / max(1, h),
    )


def _load_rgba_and_metrics(
    sprite_path: Path, mtime_ns: int
) -> tuple[QImage | None, _SpriteMetrics | None]:
    raw = QImage(str(sprite_path))
    if raw.isNull():
        return None, None
    rgba = raw.convertToFormat(QImage.Format.Format_RGBA8888)
    m_key = (str(sprite_path), mtime_ns)
    metrics = _lru_get(_METRICS_CACHE, m_key)
    if metrics is None:
        metrics = _compute_sprite_metrics(rgba)
        _lru_put(_METRICS_CACHE, m_key, metrics, _MAX_METRICS_ITEMS)
    return rgba, metrics


def _get_cached_metrics(sprite_path: Path, mtime_ns: int) -> _SpriteMetrics | None:
    m_key = (str(sprite_path), mtime_ns)
    metrics = _lru_get(_METRICS_CACHE, m_key)
    if metrics is not None:
        return metrics
    _, metrics = _load_rgba_and_metrics(sprite_path, mtime_ns)
    return metrics


def _render_missing_sprite_placeholder(
    sister: SisterProfile,
    target_width: int,
    dpr: float,
) -> RenderedSprite:
    """Render a minimal translucent pill placeholder only if a sprite PNG is missing."""
    w = max(96, int(round(target_width)))
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
    return RenderedSprite(
        pixmap=pixmap,
        logical_w=w,
        logical_h=h,
        base_w=w,
        base_h=h,
        offset_x=0.0,
        offset_y=0.0,
        content_rect=QRectF(0.0, 0.0, float(w), float(h)),
        hit_image=None,
    )


def _stat_mtime(path: Path | None) -> tuple[Path | None, str, int]:
    if path is None:
        return None, "", 0
    try:
        return path, str(path), path.stat().st_mtime_ns
    except OSError:
        return None, "", 0


def render_sister_sprite(
    sister: SisterProfile,
    state: str,
    target_width: int,
    dpr: float = 1.0,
) -> RenderedSprite:
    """Render a sister's sprite with per-state scale/anchor normalization and hit mask."""
    safe_dpr = max(1.0, float(dpr or 1.0))
    safe_width = max(64, int(target_width))
    safe_state = state or "idle"

    sprite_path, path_str, mtime_ns = _stat_mtime(find_sprite_path(sister.id, safe_state))
    idle_path, idle_path_str, idle_mtime_ns = _stat_mtime(find_sprite_path(sister.id, "idle"))

    cache_key = (
        sister.id,
        safe_state,
        safe_width,
        int(round(safe_dpr * 100)),
        path_str,
        mtime_ns,
        idle_path_str,
        idle_mtime_ns,
    )
    cached = _lru_get(_SPRITE_CACHE, cache_key)
    if cached is not None:
        return cached

    if sprite_path is not None:
        rgba, cur_m = _load_rgba_and_metrics(sprite_path, mtime_ns)
        if rgba is not None and cur_m is not None:
            if idle_path is not None and idle_path != sprite_path:
                idle_m = _get_cached_metrics(idle_path, idle_mtime_ns) or cur_m
            else:
                idle_m = cur_m

            base_w, base_h = _logical_size_for_aspect(idle_m.width, idle_m.height, safe_width)

            # Compensate wide-pose shrink (e.g. ritsu/dragged) without stretching aspect ratio.
            state_scale = 1.0
            if idle_path is not None and idle_path != sprite_path:
                cur_aspect = cur_m.height / max(1, cur_m.width)
                idle_aspect = idle_m.height / max(1, idle_m.width)
                if abs(cur_aspect - idle_aspect) <= 0.35:
                    cur_h_fill = cur_m.content_h / max(1, cur_m.height)
                    idle_h_fill = idle_m.content_h / max(1, idle_m.height)
                    cur_w_fill = cur_m.content_w / max(1, cur_m.width)
                    height_ratio = cur_h_fill / max(0.01, idle_h_fill)
                    if height_ratio < 0.90 and cur_w_fill >= 0.90:
                        state_scale = min(1.25, 0.95 / max(0.25, height_ratio))

            scaled_target_w = max(64, int(round(base_w * state_scale)))
            logical_w, logical_h = _logical_size_for_aspect(
                cur_m.width, cur_m.height, scaled_target_w
            )

            phys_w = max(1, int(round(logical_w * safe_dpr)))
            phys_h = max(1, int(round(logical_h * safe_dpr)))
            # Single-pass smooth resize from original source to final device pixels.
            scaled_phys = rgba.scaled(
                phys_w,
                phys_h,
                Qt.AspectRatioMode.IgnoreAspectRatio,
                Qt.TransformationMode.SmoothTransformation,
            )
            pixmap = QPixmap.fromImage(scaled_phys)
            pixmap.setDevicePixelRatio(safe_dpr)

            # Lightweight logical-resolution alpha map for accurate hit testing.
            if phys_w == logical_w and phys_h == logical_h:
                hit_image = scaled_phys.convertToFormat(QImage.Format.Format_Alpha8)
            else:
                hit_image = rgba.scaled(
                    logical_w,
                    logical_h,
                    Qt.AspectRatioMode.IgnoreAspectRatio,
                    Qt.TransformationMode.FastTransformation,
                ).convertToFormat(QImage.Format.Format_Alpha8)

            # Align body core X and foot baseline to the sister's idle pose.
            raw_ox = (idle_m.core_x_norm - 0.5) * base_w - (cur_m.core_x_norm - 0.5) * logical_w
            max_ox = base_w * 0.28
            offset_x = max(-max_ox, min(max_ox, raw_ox))

            raw_oy = (1.0 - cur_m.foot_y_norm) * logical_h - (1.0 - idle_m.foot_y_norm) * base_h
            max_oy = base_h * 0.12
            offset_y = max(-max_oy, min(max_oy, raw_oy))

            sx = logical_w / max(1, cur_m.width)
            sy = logical_h / max(1, cur_m.height)
            content_rect = QRectF(
                cur_m.min_x * sx,
                cur_m.min_y * sy,
                cur_m.content_w * sx,
                cur_m.content_h * sy,
            )

            rendered = RenderedSprite(
                pixmap=pixmap,
                logical_w=logical_w,
                logical_h=logical_h,
                base_w=base_w,
                base_h=base_h,
                offset_x=offset_x,
                offset_y=offset_y,
                content_rect=content_rect,
                hit_image=hit_image,
            )
            _lru_put(_SPRITE_CACHE, cache_key, rendered, _MAX_CACHE_ITEMS)
            return rendered

    rendered = _render_missing_sprite_placeholder(sister, safe_width, safe_dpr)
    _lru_put(_SPRITE_CACHE, cache_key, rendered, _MAX_CACHE_ITEMS)
    return rendered


def render_sister_pixmap(
    sister: SisterProfile,
    state: str,
    target_width: int,
    dpr: float = 1.0,
) -> QPixmap:
    """Render a sister's transparent PNG/WebP sprite (or minimal placeholder if absent)."""
    return render_sister_sprite(sister, state, target_width, dpr).pixmap
