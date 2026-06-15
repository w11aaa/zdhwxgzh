from __future__ import annotations

import sys
from typing import Optional

from PyQt5.QtGui import QFont, QFontDatabase
from PyQt5.QtWidgets import QApplication

_cached_ui_font_family: Optional[str] = None
_cached_emoji_font_family: Optional[str] = None
_cached_mono_font_family: Optional[str] = None


def _candidates() -> list[str]:
    if sys.platform == "win32":
        return [
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "Segoe UI",
            "Arial",
        ]
    if sys.platform == "darwin":
        return [
            "PingFang SC",
            "Hiragino Sans GB",
            "Heiti SC",
            "Helvetica Neue",
            "Arial",
        ]
    return [
        "Noto Sans CJK SC",
        "WenQuanYi Micro Hei",
        "DejaVu Sans",
        "Arial",
    ]


def _emoji_candidates() -> list[str]:
    if sys.platform == "win32":
        return [
            "Segoe UI Emoji",
            "Microsoft YaHei UI",
            "Microsoft YaHei",
            "Arial",
        ]
    if sys.platform == "darwin":
        return [
            "Apple Color Emoji",
            "PingFang SC",
            "Hiragino Sans GB",
            "Helvetica Neue",
            "Arial",
        ]
    return [
        "Noto Color Emoji",
        "Noto Emoji",
        "Noto Sans CJK SC",
        "DejaVu Sans",
        "Arial",
    ]


def _mono_candidates() -> list[str]:
    if sys.platform == "win32":
        return [
            "Consolas",
            "Cascadia Mono",
            "Courier New",
        ]
    if sys.platform == "darwin":
        return [
            "Menlo",
            "Monaco",
            "Courier",
        ]
    return [
        "DejaVu Sans Mono",
        "Noto Sans Mono CJK SC",
        "Noto Sans Mono",
        "Liberation Mono",
        "Monospace",
    ]


def _first_available(candidates: list[str], fallback: str) -> str:
    if QApplication.instance() is None:
        return fallback
    try:
        available = set(QFontDatabase().families())
        for family in candidates:
            if family in available:
                return family
    except Exception:
        pass
    return fallback


def get_ui_font_family() -> str:
    """Return a best-effort font family that exists on current OS."""
    global _cached_ui_font_family
    if _cached_ui_font_family:
        return _cached_ui_font_family

    # Avoid caching before QApplication is created.
    if QApplication.instance() is None:
        return QFont().defaultFamily()

    try:
        available = set(QFontDatabase().families())
        for family in _candidates():
            if family in available:
                _cached_ui_font_family = family
                return family
    except Exception:
        pass

    _cached_ui_font_family = QFont().defaultFamily()
    return _cached_ui_font_family


def get_emoji_font_family() -> str:
    """Return an emoji-capable font family for sidebar icon text."""
    global _cached_emoji_font_family
    if _cached_emoji_font_family:
        return _cached_emoji_font_family
    fallback = get_ui_font_family()
    _cached_emoji_font_family = _first_available(_emoji_candidates(), fallback)
    return _cached_emoji_font_family


def get_mono_font_family() -> str:
    """Return a readable monospace family for code-like UI text."""
    global _cached_mono_font_family
    if _cached_mono_font_family:
        return _cached_mono_font_family
    fallback = QFont().defaultFamily()
    _cached_mono_font_family = _first_available(_mono_candidates(), fallback)
    return _cached_mono_font_family


def get_emoji_font_family_css() -> str:
    return f"'{get_emoji_font_family()}'"

def get_ui_font_family_css() -> str:
    return f"'{get_ui_font_family()}'"

def get_ui_text_font_family_css() -> str:
    """Best-effort font-family list for normal UI text (CJK + emoji)."""
    return f"{get_ui_font_family_css()}, {get_emoji_font_family_css()}"


def get_mono_font_family_css() -> str:
    return f"'{get_mono_font_family()}'"


def ui_font(point_size: int = 12, weight: int = -1, italic: bool = False) -> QFont:
    """Convenience helper for a consistent UI font."""
    return QFont(get_ui_font_family(), point_size, weight, italic)
