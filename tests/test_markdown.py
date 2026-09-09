"""Тесты markdown-парсера рендерера (логика без GTK)."""

from __future__ import annotations

from fragilenotes.ui.markdown import inline_segments, parse_blocks


def test_frontmatter_meta():
    blocks = parse_blocks("---\ntitle: Заметка\ntags: [a, b]\n---\n\n## Раздел\n")
    assert blocks[0] == ("meta", [("title", "Заметка"), ("tags", "[a, b]")])
    assert blocks[1] == ("h2", "Раздел")


def test_heading_levels():
    blocks = parse_blocks("# H1\n## H2\n### H3\n#### H4\n")
    assert blocks == [("h1", "H1"), ("h2", "H2"), ("h3", "H3"), ("h4", "H4")]


def test_heading_clamped():
    blocks = parse_blocks("###### deep\n")
    assert blocks == [("h4", "deep")]


def test_code_fence():
    blocks = parse_blocks("```python\na = 1\nb = 2\n```\n")
    assert blocks == [("code", ("python", "a = 1\nb = 2"))]

def test_code_fence_no_lang():
    blocks = parse_blocks("```\nplain\n```\n")
    assert blocks == [("code", ("", "plain"))]


def test_task_boxes():
    blocks = parse_blocks("- [x] done\n- [ ] todo\n")
    assert blocks == [("tasks", [(True, "done"), (False, "todo")])]


def test_unordered_list():
    blocks = parse_blocks("- один\n- два\n")
    assert blocks == [("list", [("", "-", "один"), ("", "-", "два")])]


def test_numbered_list():
    blocks = parse_blocks("1. первый\n2. второй\n")
    assert blocks == [("list", [("", "1.", "первый"), ("", "2.", "второй")])]


def test_blockquote():
    blocks = parse_blocks("> цитата одна\n> цитата два\n")
    assert blocks == [("quote", "цитата одна\nцитата два")]


def test_paragraph_merges():
    blocks = parse_blocks("строка один\nстрока два\n")
    assert blocks == [("para", "строка один\nстрока два")]


def test_table():
    blocks = parse_blocks("| a | b |\n|---|---|\n| 1 | 2 |\n")
    assert blocks == [("table", "| a | b |\n|---|---|\n| 1 | 2 |")]


def test_hr():
    blocks = parse_blocks("текст\n\n---\n")
    assert blocks == [("para", "текст"), ("hr", "")]


def test_inline_bold_italic_code_link():
    segs = inline_segments("**жирный** и *курсив* и `код` и [ссылка](https://x)")
    assert segs == [
        ("жирный", "bold", None),
        (" и ", None, None),
        ("курсив", "italic", None),
        (" и ", None, None),
        ("код", "code", None),
        (" и ", None, None),
        ("ссылка", "link", "https://x"),
    ]


def test_inline_wikilink():
    segs = inline_segments("смотри [[Проект]] и [[Проект|читать]] и [[Заметка#раздел]]")
    assert segs == [
        ("смотри ", None, None),
        ("Проект", "wikilink", "Проект"),
        (" и ", None, None),
        ("читать", "wikilink", "Проект"),
        (" и ", None, None),
        ("Заметка", "wikilink", "Заметка"),
    ]


def test_inline_wikilink_no_alias():
    segs = inline_segments("[[Проект]]")
    assert segs == [("Проект", "wikilink", "Проект")]


def test_inline_mark():
    segs = inline_segments("важно ==подсветить== здесь")
    assert segs == [
        ("важно ", None, None),
        ("подсветить", "mark", None),
        (" здесь", None, None),
    ]


def test_inline_strikethrough():
    segs = inline_segments("черновик ~~удалить~~ осталось")
    assert segs == [
        ("черновик ", None, None),
        ("удалить", "strike", None),
        (" осталось", None, None),
    ]


def test_inline_tag():
    segs = inline_segments("статья #проекты и ещё")
    assert segs == [
        ("статья ", None, None),
        ("проекты", "tag", None),
        (" и ещё", None, None),
    ]


def test_tag_not_in_word():
    segs = inline_segments("язык C# и хеш #C")
    assert segs == [
        ("язык C# и хеш ", None, None),
        ("C", "tag", None),
    ]


def test_callout_is_parsed_from_quote():
    blocks = parse_blocks("> [!tm-sleep|-] 😴 Сон\n> **Лёг:** час\n> - [ ] Кошмар\n")
    kind, payload = blocks[0]
    assert kind == "callout"
    ctype, title, content = payload
    assert ctype == "tm-sleep"
    assert title == "😴 Сон"
    assert "**Лёг:** час" in content
    assert "- [ ] Кошмар" in content


def test_callout_default_title_from_type():
    blocks = parse_blocks("> [!warning]\n> берегись\n")
    assert blocks[0][0] == "callout"
    assert blocks[0][1][1] == ""


def test_plain_quote_is_not_callout():
    blocks = parse_blocks("> обычная цитата\n> без мэтча\n")
    assert blocks[0][0] == "quote"


def test_callout_renders_with_accent_and_tasks():
    from fragilenotes.ui.markdown import MarkdownView  # noqa: E402

    view = MarkdownView()
    view.set_markdown("> [!tm-mood] 🧠 Состояние\n> **Тревога:** 4\n> - [x] Нормально\n> - [ ] Дерьмово\n")
    buf = view._buf
    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
    assert "🧠 Состояние" in text
    assert "☑ Нормально" in text
    assert "☐ Дерьмово" in text
    first = buf.get_iter_at_offset(text.find("🧠"))
    names = [t.props.name for t in (first.get_tags() or [])]
    assert "callout_tm-mood" in names
    assert "callout" in names


def test_callout_wikilink_registered():
    from fragilenotes.ui.markdown import MarkdownView  # noqa: E402

    view = MarkdownView()
    view.set_markdown("> [!info] Справка\n> см. [[Таргет]]\n")
    targets = [t for _, _, t in view._wikilinks]
    assert targets == ["Таргет"]


def test_quote_renders_with_inline_styles() -> None:
    from fragilenotes.ui.markdown import MarkdownView  # noqa: E402

    view = MarkdownView()
    view.set_markdown("> идём **туда** и ~~назад~~, смотри [[Таргет]]\n")
    buf = view._buf
    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
    assert "▍ идём туда и назад, смотри Таргет" in text
    targets = [t for _, _, t in view._wikilinks]
    assert targets == ["Таргет"]


def test_highlight_applies_search_tag() -> None:
    from fragilenotes.ui.markdown import MarkdownView  # noqa: E402

    view = MarkdownView()
    view.set_markdown("гамма альфа бета альфа\nальфа финал", "АЛЬФА")
    buf = view._buf
    text = buf.get_text(buf.get_start_iter(), buf.get_end_iter(), True)
    assert text.count("альфа") == 3
    first = buf.get_iter_at_offset(text.find("альфа"))
    tags = [t.props.name for t in (first.get_tags() or [])]
    assert "search" in tags
    assert view._highlight == "альфа"
