"""Тесты поиска по vault и резолва wikillink (чистая логика, без GTK/индекса диска)."""

from __future__ import annotations

import tempfile
from pathlib import Path

from fragilenotes.services import vault as svc

_KEYS = {
    "vault_root": "",
    "daily_folder": "02 Daily",
    "tm_tasks_folder": "_System/TaskManagerMain/_system/tasks",
    "tm_comments_folder": "_System/TaskManagerMain/_system/comments",
    "tm_templates_root": "_System/TaskManagerMain/Templates",
}


def _vault(tmp: Path) -> dict:
    (tmp / "01 Home").mkdir()
    (tmp / "03 Projects").mkdir()
    (tmp / "01 Home" / "Встреча.md").write_text(
        "---\ntitle: Встреча с командой\n---\nсмотри [[Рефакторинг]]", encoding="utf-8"
    )
    (tmp / "03 Projects" / "Рефакторинг.md").write_text(
        "---\ntitle: Рефакторинг движка\n---\nтело", encoding="utf-8"
    )
    return {**_KEYS, "vault_root": str(tmp)}


def test_search_by_content_only() -> None:
    """Слово, встречающееся только в теле заметки, находится полнотекстовым поиском."""
    tmp = Path(tempfile.mkdtemp(prefix="fn-search-"))
    settings = _vault(tmp)
    hits = svc.search_notes(settings, "тело", limit=10)
    assert [h.path.name for h in hits] == ["Рефакторинг.md"]
    assert "тело" in hits[0].text


def test_title_ranked_above_content_match() -> None:
    """Заметка с совпадением в имени/title выше той, где слово только в содержании."""
    tmp = Path(tempfile.mkdtemp(prefix="fn-search-"))
    settings = _vault(tmp)
    hits = svc.search_notes(settings, "рефакторинг", limit=10)
    names = [h.path.name for h in hits]
    assert names[0] == "Рефакторинг.md"  # имя + title
    assert "Встреча.md" in names  # только по телу (wikilink [[Рефакторинг]])


def test_content_search_case_insensitive() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fn-search-"))
    settings = _vault(tmp)
    hits = svc.search_notes(settings, "ТЕЛО", limit=10)
    assert hits and hits[0].path.name == "Рефакторинг.md"
    tmp = Path(tempfile.mkdtemp(prefix="fn-search-"))
    settings = _vault(tmp)
    hits = svc.search_notes(settings, "движ", limit=10)
    assert [h.path.name for h in hits] == ["Рефакторинг.md"]
    assert hits[0].title == "Рефакторинг движка"


def test_search_case_insensitive_cyrillic() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fn-search-"))
    settings = _vault(tmp)
    hits = svc.search_notes(settings, "встрЕча", limit=10)
    assert hits[0].title == "Встреча с командой"


def test_search_empty_query() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fn-search-"))
    settings = _vault(tmp)
    assert svc.search_notes(settings, "") == []


def test_resolve_wikilink_by_stem() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fn-search-"))
    settings = _vault(tmp)
    p = svc.resolve_wikilink(settings, "Рефакторинг")
    assert p is not None
    assert p.name == "Рефакторинг.md"


def test_resolve_wikilink_by_title() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fn-search-"))
    settings = _vault(tmp)
    p = svc.resolve_wikilink(settings, "Рефакторинг движка")
    assert p is not None
    assert p.name == "Рефакторинг.md"


def test_resolve_wikilink_missing() -> None:
    tmp = Path(tempfile.mkdtemp(prefix="fn-search-"))
    settings = _vault(tmp)
    assert svc.resolve_wikilink(settings, "Нет такой заметки") is None
