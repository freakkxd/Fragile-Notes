"""Тесты рабочего пространства: модель ribbon (нормализация, порядок, состав)."""

from __future__ import annotations

from fragilenotes.config import DEFAULT_WORKSPACE
from fragilenotes.ui import workspace as ws


def test_default_ribbon_matches_default_config():
    assert ws.default_ribbon() == DEFAULT_WORKSPACE["ribbon"]


def test_normalize_empty_uses_default():
    out = ws.normalize({})
    assert out["ribbon"] == ws.default_ribbon()


def test_normalize_filters_bad_items():
    settings = {"workspace": {"ribbon": [
        {"t": "view", "id": "nope"},
        {"t": "cmd", "id": "nope"},
        {"t": "open", "path": ""},
        {"t": "view", "id": "home"},
        {"t": "sep"},
        {"t": "open", "path": "Заметки/важно.md"},
    ]}}
    out = ws.normalize(settings)["ribbon"]
    assert [it["t"] for it in out] == ["view", "sep", "open"]
    assert out[0]["id"] == "home"
    assert out[2]["path"] == "Заметки/важно.md"


def test_normalize_trims_leading_separator():
    out = ws.normalize({"workspace": {"ribbon": [
        {"t": "sep"}, {"t": "sep"}, {"t": "view", "id": "files"},
    ]}})["ribbon"]
    assert out[0]["t"] == "view"


def test_consecutive_separators_collapse():
    out = ws.normalize({"workspace": {"ribbon": [
        {"t": "view", "id": "home"}, {"t": "sep"}, {"t": "sep"}, {"t": "view", "id": "files"},
    ]}})["ribbon"]
    seps = [i for i, it in enumerate(out) if it["t"] == "sep"]
    assert len(seps) == 1


def test_move_respects_bounds():
    items = ws.default_ribbon()
    n = len(items)
    ws.move(items, 0, -10)      # вверх за границу — без изменений
    assert items[0]["t"] == "cmd" and items[0]["id"] == "sidebar"
    ws.move(items, n - 1, 10)   # вниз за границу — без изменений
    assert items[-1]["t"] == "view" and items[-1]["id"] == "settings"


def test_move_slides_around_separator():
    items = [{"t": "view", "id": "home"}, {"t": "sep"}, {"t": "view", "id": "files"}]
    ws.move(items, 0, 1)       # home вниз — разделитель сдвигается, никого не теряем
    assert items == [{"t": "sep"}, {"t": "view", "id": "home"}, {"t": "view", "id": "files"}]
    items2 = [{"t": "view", "id": "home"}, {"t": "sep"}, {"t": "view", "id": "files"}]
    ws.move(items2, 2, -1)     # files вверх — разделитель сдвигается вниз
    assert items2 == [{"t": "view", "id": "home"}, {"t": "view", "id": "files"}, {"t": "sep"}]


def test_ribbon_views_lists_view_ids():
    items = ws.default_ribbon()
    assert "files" in ws.ribbon_views(items)
    assert "media" not in ws.ribbon_views(items[:4])
