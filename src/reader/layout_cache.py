"""Shared persistent layout-cache policy for the PiBook EPUB reader."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path


# Increment this whenever a renderer/layout change can alter pagination,
# coordinates, font metrics, image placement, or the persistent cache format.
LAYOUT_CACHE_VERSION = 9

# Current renderer layout profile. These values describe the visual/layout
# assumptions used by PillowTextRenderer. Changing any of them automatically
# changes the cache signature below.
LAYOUT_PROFILE = {
    "renderer": "pillow_rich_text",
    "margin_left": 30,
    "margin_right": 30,
    "margin_top": 30,
    "margin_bottom": 40,
    "line_spacing": 1.3,
    "paragraph_spacing": 5,
    "paragraph_indent": 40,
    "base_font_factor": 18,
    "header_font_factor": 24,
}


def layout_signature(width: int, height: int, zoom_factor: float) -> str:
    """Return a short deterministic signature for layout-compatible caches."""
    payload = {
        "version": LAYOUT_CACHE_VERSION,
        "width": int(width),
        "height": int(height),
        "zoom_factor": float(zoom_factor),
        "profile": LAYOUT_PROFILE,
    }
    raw = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()[:12]


def cache_path(
    epub_path: str,
    width: int,
    height: int,
    zoom_factor: float,
) -> Path:
    """Return the persistent cache path for this exact layout signature."""
    signature = layout_signature(width, height, zoom_factor)
    return Path(
        f"{epub_path}.{int(width)}x{int(height)}."
        f"{float(zoom_factor)}.{signature}.cache"
    )


def cache_is_fresh(
    epub_path: str,
    width: int,
    height: int,
    zoom_factor: float,
) -> bool:
    """True only when a compatible cache exists and is newer than the EPUB."""
    source = Path(epub_path)
    cache = cache_path(epub_path, width, height, zoom_factor)

    try:
        return (
            source.is_file()
            and cache.is_file()
            and cache.stat().st_mtime_ns >= source.stat().st_mtime_ns
        )
    except OSError:
        return False
