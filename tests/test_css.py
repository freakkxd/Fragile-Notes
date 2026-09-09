"""Тесты CSS-темы: валидность парсинга и целостность дизайн-токенов.

Ловим ситуацию, когда селектор ссылается на var(--ao-*), которого нет
в токенах (например, --ao-surface-control-hover использовался, но не
определялся — hover виджетов молча ломался).
"""

from __future__ import annotations

import re

import gi

gi.require_version("Gtk", "4.0")

from gi.repository import Gtk  # noqa: E402

from fragilenotes.ui.style import CSS, build_css  # noqa: E402

TOKEN_RE = re.compile(r"(--[\w-]+)\s*:")
VAR_RE = re.compile(r"var\(\s*(--[\w-]+)")
VAR_FALLBACK_RE = re.compile(r"var\(\s*(--[\w-]+)\s*,")


def _parse_errors(css: str) -> list[str]:
    provider = Gtk.CssProvider()
    errors: list[str] = []
    provider.connect("parsing-error", lambda _p, _s, se: errors.append(se.to_string()))
    provider.load_from_string(css)
    return errors


def test_css_parses_at_scales() -> None:
    for ui, zoom in [(0.9, 1.0), (1.0, 1.0), (1.25, 1.2), (1.5, 0.9)]:
        assert _parse_errors(build_css(ui, zoom)) == []


def test_no_undefined_tokens() -> None:
    defined = set(TOKEN_RE.findall(CSS))
    used = set(VAR_RE.findall(CSS))
    with_fallback = set(VAR_FALLBACK_RE.findall(CSS))
    missing = used - defined - with_fallback
    assert not missing, f"неопределённые переменные: {missing}"


def test_hover_tokens_defined() -> None:
    """Раньше использовались, но не объявлялись — hover у топбара/дерева не работал."""
    defined = set(TOKEN_RE.findall(CSS))
    assert {"--ao-surface-control-hover", "--ao-surface-row-hover"} <= defined


def test_selected_accent_defined() -> None:
    defined = set(TOKEN_RE.findall(CSS))
    assert {"--ao-selected", "--ao-selected-border", "--ao-focus-ring"} <= defined


def test_markdown_reading_css() -> None:
    """Режим чтения: класс .markdown (17.5px = 0.92rem от базы 19) и обёртка .md-read."""
    assert ".markdown {" in CSS
    assert "font-size: 17.5px;" in CSS
    assert "textview.markdown text" in CSS
    assert ".md-read" in CSS
    assert "--ao-surface-glass-soft" in CSS
