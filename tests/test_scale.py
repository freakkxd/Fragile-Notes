"""Тесты масштабирования CSS (чистая логика, без GTK-дисплея)."""

from __future__ import annotations

from fragilenotes.ui.style import build_css


def test_default_unchanged():
    css = build_css(1.0, 1.0)
    assert "font-size: 15px;" in css      # база интерфейса
    assert "font-size: 13px;" in css      # моно-редактор
    assert "font-size: 17.5px;" in css    # тело заметки = 0.92rem от базы 19px (ao-glass-note)


def test_ui_scale_scales_all_fonts():
    css = build_css(1.5, 1.0)
    assert "font-size: 22.5px;" in css    # 15 × 1.5
    assert "font-size: 19.5px;" in css    # 13 × 1.5
    assert "font-size: 26.2px;" in css    # 17.5 × 1.5


def test_editor_zoom_only_editor():
    css = build_css(1.0, 1.25)
    assert "font-size: 16.2px;" in css    # 13 × 1.25
    assert "font-size: 21.9px;" in css    # 17.5 × 1.25
    assert "font-size: 15px;" in css      # интерфейс не тронут


def test_combined_ui_and_zoom():
    css = build_css(1.5, 1.5)
    assert "font-size: 29.2px;" in css    # 13 × 1.5 × 1.5
    assert "font-size: 39.4px;" in css    # 17.5 × 1.5 × 1.5
    assert "font-size: 22.5px;" in css    # интерфейс: только ui


def test_no_sentinels_left():
    css = build_css(1.4, 1.3)
    assert "@@" not in css


def test_interior_fit_scales_lengths_only():
    css = build_css(1.0, 1.0, 2.0)
    assert "font-size: 15px;" in css          # шрифты не тронуты (это ui-масштаб)
    assert "min-width: 88px;" in css          # ribbon 44 → 88
    assert "padding: 12px 8px;" in css        # ribbon 6/4 → 12/8


def test_interior_fit_works_with_ui():
    css = build_css(1.5, 1.0, 2.0)
    assert "font-size: 22.5px;" in css        # 15 × 1.5 (только ui)
    assert "min-width: 88px;" in css          # 44 × 2 (только fit)


def test_interior_fit_default_neutral():
    css = build_css(1.0, 1.0)
    assert "padding: 6px 4px;" in css         # fit=1 — длины без изменений
